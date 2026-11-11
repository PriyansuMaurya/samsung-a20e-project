#!/usr/bin/env python3
"""Axiom telemetry export for the Galaxy console.

The phone keeps its log files and SQLite database as the operational source of
truth. This module adds a second, remote sink: structured NDJSON events are
queued, batched and POSTed to an Axiom dataset, with bounded memory, a disk
spool for transient failures and exponential backoff on 429/5xx responses.

Interface (all a caller needs to know):

    client = Telemetry(run_dir)
    client.emit({'event': 'galaxy.sample', ...}, entry_point='sampler')
    client.start(LOG_FILES, LOG_DIR, DB_PATH)   # log/event forwarding loop
    client.flush()                              # send what is queued
    client.status()                             # JSON-safe, never the token
    client.backfill(LOG_FILES, LOG_DIR, DB_PATH)
    client.apply_retention(days)

Everything else - batching, retry timing, spool rotation, redaction, cursors -
is implementation. Credentials live in run/axiom.json, mode 0600, and are
never returned by status(), printed or logged.

Axiom ingest: POST {endpoint}/v1/ingest/{dataset}?timestamp-field=_time
with Authorization: Bearer <api token> and application/x-ndjson.
See https://axiom.co/docs/restapi/ingest.
"""
import argparse
import datetime
import getpass
import json
import os
import random
import re
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

CONFIG_NAME = 'axiom.json'
SPOOL_NAME = 'telemetry-spool.ndjson'
STATE_NAME = 'telemetry-state.json'
DEVICE = 'galaxy-a20e'
SOURCE = 'galaxy-server'
DEFAULT_ENDPOINT = 'https://us-east-1.aws.edge.axiom.co'
MANAGE_ENDPOINT = 'https://api.axiom.co'
# Axiom serves ingest from one edge endpoint per region; a request sent to the
# wrong region is answered with 404 "dataset not found" even though the dataset
# exists, so the region has to be discovered rather than assumed.
EDGE_HOST = 'https://%s.aws.edge.axiom.co'
EDGE_REGIONS = ('us-east-1', 'eu-central-1')
DESCRIPTION = 'Galaxy A20e server console telemetry'

# Axiom rejects timestamps more than 24 hours in the future or before 1970.
MAX_EVENT_BYTES = 4000
READ_CHUNK = 64 * 1024
LOG_READ_LIMIT = 256 * 1024

DEFAULTS = {
    'enabled': False,
    'endpoint': DEFAULT_ENDPOINT,
    'dataset': 'galaxy-server',
    'token': '',
    'manage_token': '',
    'retention_days': 30,
    'batch_size': 100,
    'flush_seconds': 15,
    'timeout_seconds': 10,
    'max_retries': 3,
    'retry_backoff_seconds': 2,
    'max_batch_bytes': 256 * 1024,
    'spool_max_kb': 512,
    'max_queue': 2000,
    'log_forward': True,
    'history_batch': 200,
    # Epoch seconds for the one-time catch-up of samples/checks written before
    # forwarding began. 0 means "from the moment forwarding first ran".
    'history_since': 0,
}

LIMITS = {
    'retention_days': (0, 3650),
    'batch_size': (1, 1000),
    'flush_seconds': (2, 3600),
    'timeout_seconds': (1, 60),
    'max_retries': (0, 10),
    'retry_backoff_seconds': (0, 60),
    'max_batch_bytes': (4096, 5 * 1024 * 1024),
    'spool_max_kb': (16, 64 * 1024),
    'max_queue': (50, 100000),
    'history_batch': (0, 5000),
    'history_since': (0, 4102444800),
}

# Columns copied from the samples table for history forwarding. The live
# sampler emits the same names, so one dashboard query covers both.
SAMPLE_COLUMNS = ('ts', 'uptime_s', 'mem_used_mb', 'mem_total_mb', 'mem_pct',
                  'swap_pct', 'free_gb', 'storage_pct', 'batt', 'plugged', 'temp_c',
                  'cpu_pct', 'nginx', 'api', 'supervisor', 'tunnel_proc',
                  'tunnel_ready', 'tunnel_conns', 'tunnel_req', 'https_ok', 'https_ms')

DATASET = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
TIMESTAMP = re.compile(r'^(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')

REDACTIONS = (
    (re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[token]'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{6,}'), 'bearer [redacted]'),
    (re.compile(r'(?i)\bxaa[tm]-[A-Za-z0-9._-]{8,}'), '[token]'),
    (re.compile(r'(?i)\b(token|password|passwd|secret|apikey|api_key)'
                r'(\s*[:=]\s*)([^\s,;"\']{4,})'), r'\1\2[redacted]'),
)
LONG_BLOB = re.compile(r'[A-Za-z0-9_+\-/=]{32,}')
SENSITIVE_KEY = re.compile(
    r'(?i)^(token|manage_token|password|passwd|secret|api_?key|authorization|cookie|session)$')


class ConfigError(Exception):
    """Raised when run/axiom.json is unsafe or malformed."""


def utc_iso(ts=None):
    return datetime.datetime.fromtimestamp(
        time.time() if ts is None else ts, datetime.timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%SZ')


def _redact_blob(match):
    value = match.group(0)
    if '/' in value or '.' in value:
        return value
    if not (any(char.isdigit() for char in value)
            and any(char.isupper() for char in value)
            and any(char.islower() for char in value)):
        return value
    return '[redacted]'


def scrub(text, limit=MAX_EVENT_BYTES):
    """Remove credentials and bound the size of one string value."""
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    text = LONG_BLOB.sub(_redact_blob, text)
    if len(text) > limit:
        text = text[:limit]
    return text


def _clean(value, depth=0):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        if depth >= 3:
            return scrub(json.dumps(value, default=str))
        cleaned = {}
        for key, item in list(value.items())[:40]:
            name = str(key)[:64]
            cleaned[name] = '[redacted]' if SENSITIVE_KEY.match(name) else _clean(item, depth + 1)
        return cleaned
    if isinstance(value, (list, tuple)):
        if depth >= 3:
            return scrub(json.dumps(value, default=str))
        return [_clean(item, depth + 1) for item in list(value)[:40]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub(str(value))


def validate_config(config):
    endpoint = config.get('endpoint')
    if not isinstance(endpoint, str) or not endpoint:
        raise ConfigError('endpoint must be a URL')
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.username or parsed.password:
        raise ConfigError('endpoint must not contain credentials')
    if parsed.path not in ('', '/'):
        raise ConfigError('endpoint must be a base URL, not a path')
    if parsed.query or parsed.fragment:
        raise ConfigError('endpoint must not contain a query or fragment')
    if parsed.scheme == 'https':
        pass
    elif parsed.scheme == 'http' and parsed.hostname in LOOPBACK:
        pass
    else:
        raise ConfigError('endpoint must use https')
    if not parsed.hostname:
        raise ConfigError('endpoint is missing a host')

    dataset = config.get('dataset')
    if not isinstance(dataset, str) or not DATASET.match(dataset):
        raise ConfigError('dataset must be 1-128 characters of A-Z a-z 0-9 . _ -')

    for key in ('token', 'manage_token'):
        value = config.get(key) or ''
        if not isinstance(value, str) or not re.fullmatch(r'[!-~]{0,512}', value):
            raise ConfigError('%s must be a single printable token' % key)

    for key, (low, high) in LIMITS.items():
        try:
            number = int(config.get(key))
        except (TypeError, ValueError):
            raise ConfigError('%s must be a whole number' % key)
        if not low <= number <= high:
            raise ConfigError('%s must be between %d and %d' % (key, low, high))
        config[key] = number

    for key in ('enabled', 'log_forward'):
        value = config.get(key)
        if not isinstance(value, bool):
            if value in (0, 1):
                config[key] = bool(value)
            else:
                raise ConfigError('%s must be true or false' % key)
    return config


def load_config(path):
    if not os.path.exists(path):
        return dict(DEFAULTS)
    if os.name == 'posix':
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise ConfigError('axiom.json must be chmod 600; it contains an API token')
    try:
        with open(path, encoding='utf-8') as handle:
            stored = json.load(handle)
    except (OSError, ValueError) as error:
        raise ConfigError('cannot read %s: %s' % (os.path.basename(path), type(error).__name__))
    if not isinstance(stored, dict):
        raise ConfigError('axiom.json must contain an object')
    config = dict(DEFAULTS)
    config.update({key: value for key, value in stored.items() if key in DEFAULTS})
    return validate_config(config)


def save_config(path, config):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temp = path + '.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(config, handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    os.chmod(path, 0o600)


def classify(line):
    if re.search(r'\b(crit|emerg|alert|panic|fatal)\b', line, re.I):
        return 'crit'
    if re.search(r'\b(error|err|failed|failure|denied|refused)\b', line, re.I):
        return 'error'
    if re.search(r'\b(warn|warning|deprecated)\b', line, re.I):
        return 'warn'
    return 'info'


def line_time(line):
    match = TIMESTAMP.match(line)
    if not match:
        return time.time()
    try:
        return time.mktime(tuple(int(part) for part in match.groups()) + (0, 0, -1))
    except (OverflowError, ValueError):
        return time.time()


class Telemetry:
    """A bounded, non-blocking exporter. Callers never wait on the network."""

    CONFIG_REFRESH = 2.0
    SPOOL_REFRESH = 5.0

    def __init__(self, run_dir, config_path=None):
        self.run_dir = run_dir
        self.config_path = config_path or os.path.join(run_dir, CONFIG_NAME)
        self.spool_path = os.path.join(run_dir, SPOOL_NAME)
        self.state_path = os.path.join(run_dir, STATE_NAME)
        self._lock = threading.RLock()
        self._queue = deque()
        self._config_cache = None
        self._config_checked = 0.0
        self._config_error = None
        self._spool_cache = (0.0, 0, 0)
        self._stop = threading.Event()
        self._thread = None
        self._health = None
        self._stats = {
            'exported': 0, 'failed': 0, 'rejected': 0, 'dropped': 0,
            'batches': 0, 'deferred': 0,
            'last_success_at': None, 'last_error': None,
            'last_error_at': None, 'last_status': None,
        }

    # ---------------------------------------------------------------- config

    def config(self):
        now = time.monotonic()
        if self._config_cache is not None and now - self._config_checked < self.CONFIG_REFRESH:
            return self._config_cache
        self._config_checked = now
        try:
            config = load_config(self.config_path)
            self._config_error = None
        except (ConfigError, OSError) as error:
            config = dict(DEFAULTS)
            self._config_error = scrub(str(error), 200)
        self._config_cache = config
        return config

    def _ready(self, config):
        return bool(config['enabled'] and config['token'] and config['dataset'])

    # ----------------------------------------------------------------- events

    def emit(self, event, entry_point='sampler'):
        """Queue one structured event. Returns False when export is disabled."""
        config = self.config()
        if not self._ready(config):
            return False
        if not isinstance(event, dict):
            return False
        record = _clean(event)
        record.setdefault('event', 'galaxy.event')
        record.setdefault('source', SOURCE)
        record.setdefault('device', DEVICE)
        record.setdefault('entry_point', entry_point)
        record.setdefault('_time', utc_iso())
        with self._lock:
            if len(self._queue) >= config['max_queue']:
                self._queue.popleft()
                self._stats['dropped'] += 1
            self._queue.append(record)
        return True

    def emit_health(self, health, alerts):
        """Emit a health event on change, plus a five-minute heartbeat."""
        now = time.time()
        if self._health and self._health[0] == health and now - self._health[1] < 300:
            return False
        self._health = (health, now)
        return self.emit({'event': 'galaxy.health', 'health': health,
                          'alerts': alerts, '_time': utc_iso(now)},
                         entry_point='api')

    def emit_check(self, host, result):
        return self.emit({'event': 'galaxy.check', 'host': host,
                          'ok': bool(result.get('ok')), 'status': result.get('status'),
                          'ms': result.get('ms'), 'error': result.get('error'),
                          '_time': utc_iso()}, entry_point='sampler')

    # ------------------------------------------------------------------ flush

    def flush(self):
        """Send at most one batch. Safe to call from any thread."""
        config = self.config()
        if not self._ready(config):
            return {'ok': False, 'reason': 'disabled'}
        with self._lock:
            spooled, spool_lines = self._spool_take(
                config['batch_size'], config['max_batch_bytes'])
        if spool_lines and not spooled:
            # A malformed or partial last line must not block the spool.
            with self._lock:
                self._spool_drop(spool_lines)
            spool_lines = 0
        if spooled:
            outcome = self._send_with_retry(spooled, config)
            if not outcome['ok']:
                return {'ok': False, 'reason': outcome['error']}
            with self._lock:
                self._spool_drop(spool_lines)
            self._record_outcome(outcome)
            return {'ok': True, 'spool': True, 'events': len(spooled),
                    'ingested': outcome['ingested'], 'failed': outcome['failed']}

        with self._lock:
            batch = [self._queue.popleft()
                     for _ in range(min(config['batch_size'], len(self._queue)))]
        if not batch:
            return {'ok': True, 'events': 0}
        batch, rest = self._fit(batch, config)
        if rest:
            with self._lock:
                self._queue.extendleft(reversed(rest))
        outcome = self._send_with_retry(batch, config)
        if not outcome['ok']:
            if outcome['retryable']:
                with self._lock:
                    self._spool_append(batch)
                    self._stats['deferred'] += 1
            else:
                with self._lock:
                    self._stats['rejected'] += len(batch)
            self._record_outcome(outcome)
            return {'ok': False, 'reason': outcome['error'], 'events': len(batch)}
        self._record_outcome(outcome)
        return {'ok': True, 'events': len(batch), 'ingested': outcome['ingested'],
                'failed': outcome['failed']}

    def _fit(self, batch, config):
        """Split a batch so one request stays under the byte limit."""
        limit = config['max_batch_bytes']
        fitted, size = [], 0
        for index, event in enumerate(batch):
            length = len(json.dumps(event, separators=(',', ':')).encode()) + 1
            if fitted and size + length > limit:
                return fitted, batch[index:]
            fitted.append(event)
            size += length
        return fitted, []

    def _send_with_retry(self, batch, config):
        attempt = 0
        while True:
            outcome = self._post(batch, config)
            if outcome['ok'] or not outcome['retryable']:
                return outcome
            if attempt >= config['max_retries']:
                return outcome
            delay = config['retry_backoff_seconds'] * (2 ** attempt)
            delay *= 0.5 + random.random() / 2
            if outcome.get('retry_after'):
                delay = max(delay, min(outcome['retry_after'], 30))
            attempt += 1
            if self._stop.is_set() and self._thread is not None:
                return outcome
            time.sleep(min(30.0, delay))

    def _post(self, batch, config):
        lines = [json.dumps(event, separators=(',', ':'), ensure_ascii=False)
                 for event in batch]
        body = ('\n'.join(lines) + '\n').encode('utf-8')
        url = '%s/v1/ingest/%s?timestamp-field=_time' % (
            config['endpoint'].rstrip('/'), urllib.parse.quote(config['dataset'], safe=''))
        request = urllib.request.Request(url, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + config['token'],
            'Content-Type': 'application/x-ndjson',
            'User-Agent': 'galaxy-console-telemetry/1',
        })
        try:
            with urllib.request.urlopen(request, timeout=config['timeout_seconds']) as response:
                status = response.getcode()
                text = response.read(65536).decode('utf-8', 'replace')
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                text = error.read(2048).decode('utf-8', 'replace')
            except OSError:
                text = ''
            retry_after = error.headers.get('Retry-After') if error.headers else None
            try:
                retry_after = float(retry_after) if retry_after else None
            except ValueError:
                retry_after = None
            return {'ok': False, 'status': status,
                    'retryable': status == 429 or status >= 500 or status in (408, 409),
                    'error': scrub('HTTP %d %s' % (status, text), 240),
                    'retry_after': retry_after, 'ingested': 0, 'failed': len(batch)}
        except (urllib.error.URLError, OSError, ValueError) as error:
            return {'ok': False, 'status': None, 'retryable': True,
                    'error': scrub('%s: %s' % (type(error).__name__, error), 240),
                    'ingested': 0, 'failed': len(batch)}

        ingested, failed, error_text = len(batch), 0, None
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            if isinstance(payload.get('ingested'), int):
                ingested = payload['ingested']
            if isinstance(payload.get('failed'), int):
                failed = payload['failed']
            failures = payload.get('failures')
            if isinstance(failures, list) and failures:
                first = failures[0]
                error_text = first.get('error') if isinstance(first, dict) else None
        ok = 200 <= status < 300
        return {'ok': ok, 'status': status, 'retryable': False,
                'error': scrub(error_text or ('HTTP %d' % status), 240) if not ok else None,
                'ingested': ingested, 'failed': failed}

    def _record_outcome(self, outcome):
        with self._lock:
            self._stats['batches'] += 1
            self._stats['exported'] += outcome.get('ingested') or 0
            self._stats['failed'] += outcome.get('failed') or 0
            self._stats['last_status'] = outcome.get('status')
            if outcome['ok']:
                self._stats['last_success_at'] = time.time()
            else:
                self._stats['last_error'] = outcome['error']
                self._stats['last_error_at'] = time.time()

    # ------------------------------------------------------------------ spool

    def _spool_take(self, limit, max_bytes=None):
        lines, total = [], 0
        try:
            with open(self.spool_path, encoding='utf-8') as handle:
                for line in handle:
                    if len(lines) >= limit:
                        break
                    size = len(line.encode('utf-8'))
                    if lines and max_bytes and total + size > max_bytes:
                        break
                    lines.append(line)
                    total += size
        except OSError:
            return [], 0
        events = []
        for line in lines:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events, len(lines)

    def _spool_drop(self, count):
        try:
            with open(self.spool_path, encoding='utf-8') as handle:
                lines = handle.readlines()
        except OSError:
            return
        keep = lines[count:]
        if keep:
            temp = self.spool_path + '.tmp'
            with open(temp, 'w', encoding='utf-8') as handle:
                handle.writelines(keep)
            os.replace(temp, self.spool_path)
        else:
            try:
                os.unlink(self.spool_path)
            except OSError:
                pass
        self._spool_cache = (0.0, 0, 0)

    def _spool_append(self, batch):
        limit = self.config()['spool_max_kb'] * 1024
        try:
            with open(self.spool_path, 'a', encoding='utf-8') as handle:
                for event in batch:
                    handle.write(json.dumps(event, separators=(',', ':'),
                                            ensure_ascii=False) + '\n')
            os.chmod(self.spool_path, 0o600)
        except OSError:
            self._stats['dropped'] += len(batch)
            return
        self._trim_spool(limit)
        self._spool_cache = (0.0, 0, 0)

    def _trim_spool(self, limit):
        try:
            size = os.path.getsize(self.spool_path)
        except OSError:
            return
        if size <= limit:
            return
        try:
            with open(self.spool_path, encoding='utf-8') as handle:
                lines = handle.readlines()
        except OSError:
            return
        keep, kept = [], 0
        for line in reversed(lines):
            length = len(line.encode())
            if keep and kept + length > limit:
                break
            keep.append(line)
            kept += length
        keep.reverse()
        temp = self.spool_path + '.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            handle.writelines(keep)
        os.replace(temp, self.spool_path)
        with self._lock:
            self._stats['dropped'] += len(lines) - len(keep)

    def _spool_info(self):
        now = time.monotonic()
        if now - self._spool_cache[0] < self.SPOOL_REFRESH:
            return self._spool_cache[1], self._spool_cache[2]
        size, lines = 0, 0
        try:
            size = os.path.getsize(self.spool_path)
            with open(self.spool_path, 'rb') as handle:
                lines = handle.read().count(b'\n')
        except OSError:
            pass
        self._spool_cache = (now, round(size / 1024, 1), lines)
        return self._spool_cache[1], self._spool_cache[2]

    # ------------------------------------------------------------------ state

    def _load_state(self):
        try:
            with open(self.state_path, encoding='utf-8') as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return {'logs': {}, 'event_cursor': 0}
        if not isinstance(state, dict):
            return {'logs': {}, 'event_cursor': 0}
        state.setdefault('logs', {})
        state.setdefault('event_cursor', 0)
        state.setdefault('audit_cursor', 0)
        state.setdefault('sample_cursor', 0)
        state.setdefault('check_cursor', 0)
        if not isinstance(state['logs'], dict):
            state['logs'] = {}
        return state

    def _save_state(self, state):
        temp = self.state_path + '.tmp'
        try:
            with open(temp, 'w', encoding='utf-8') as handle:
                json.dump(state, handle)
            os.replace(temp, self.state_path)
        except OSError:
            pass

    # --------------------------------------------------------------- draining

    def drain(self, log_files, log_dir, db_path, limit=None):
        """Forward new bytes from the log files and new rows from events.

        Offsets only advance for events that were actually queued, so a
        bounded drain never loses a line and never sends one twice.
        """
        config = self.config()
        if not self._ready(config) or not config['log_forward']:
            return {'logs': 0, 'events': 0}
        budget = int(limit or 5000)
        state = self._load_state()
        before = json.dumps(state, sort_keys=True)
        forwarded = self._forward_logs(log_files, log_dir, state, budget)
        forwarded_events = self._forward_events(db_path, state, budget - forwarded)
        forwarded_history = self._forward_history(
            db_path, state, budget - forwarded - forwarded_events)
        # An idle cycle should not rewrite the cursor file to flash.
        if json.dumps(state, sort_keys=True) != before:
            self._save_state(state)
        return {'logs': forwarded, 'events': forwarded_events, 'history': forwarded_history}

    def _forward_logs(self, log_files, log_dir, state, budget):
        count = 0
        for name, filename in log_files.items():
            if count >= budget:
                break
            path = os.path.join(log_dir, filename)
            try:
                info = os.stat(path)
            except OSError:
                continue
            token = '%s:%s' % (info.st_dev, info.st_ino)
            entry = state['logs'].get(name)
            if not isinstance(entry, dict):
                entry = {'offset': 0, 'file': token}
            offset = int(entry.get('offset') or 0)
            if entry.get('file') != token or info.st_size < offset:
                offset = 0
            if info.st_size <= offset:
                state['logs'][name] = {'offset': offset, 'file': token}
                continue
            consumed = 0
            try:
                with open(path, 'rb') as handle:
                    handle.seek(offset)
                    while consumed < LOG_READ_LIMIT and count < budget:
                        chunk = handle.read(READ_CHUNK)
                        if not chunk:
                            break
                        end = chunk.rfind(b'\n')
                        if end < 0:
                            if len(chunk) < READ_CHUNK:
                                break
                            # A line longer than the read window: forward it
                            # truncated rather than stalling the file forever.
                            complete = chunk
                        else:
                            complete = chunk[:end + 1]
                        line_bytes = 0
                        reached_budget = False
                        stopped = False
                        for raw in complete.splitlines(keepends=True):
                            if count >= budget:
                                reached_budget = True
                                break
                            text = raw.decode('utf-8', 'replace').rstrip('\r\n')
                            if not text:
                                line_bytes += len(raw)
                                continue
                            event = {'event': 'galaxy.log', 'service': name,
                                     'severity': classify(text), 'message': text,
                                     '_time': utc_iso(line_time(text))}
                            if not self.emit(event, entry_point='forwarder'):
                                stopped = True
                                break
                            count += 1
                            line_bytes += len(raw)
                        offset += line_bytes
                        consumed += line_bytes
                        if reached_budget or stopped or line_bytes < len(complete):
                            break
                        if len(chunk) < READ_CHUNK:
                            break
            except OSError:
                pass
            state['logs'][name] = {'offset': offset, 'file': token}
        return count

    def _forward_events(self, db_path, state, budget):
        if budget <= 0 or not os.path.exists(db_path):
            return 0
        cursor = int(state.get('event_cursor') or 0)
        uri = 'file:%s?mode=ro' % urllib.parse.quote(db_path.replace('\\', '/'))
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=3)
            rows = connection.execute(
                'SELECT id, ts, kind, detail, service FROM events '
                'WHERE id > ? ORDER BY id LIMIT ?', (cursor, budget)).fetchall()
        except sqlite3.Error:
            return 0
        finally:
            if connection is not None:
                connection.close()
        count = 0
        for row in rows:
            event = {'event': 'galaxy.event', 'kind': row[2], 'detail': row[3],
                     'service': row[4], '_time': utc_iso(row[1])}
            if self.emit(event, entry_point='forwarder'):
                cursor = row[0]
                count += 1
        state['event_cursor'] = cursor
        return count

    def _forward_history(self, db_path, state, budget):
        """Forward table rows that are not covered by a live event.

        Samples and checks are emitted live as they happen, so history is
        limited to rows older than the moment forwarding began: a row reaches
        Axiom once, from either path, never both. Audits have no live event at
        all, so their id cursor advances on every cycle. Nothing here reads the
        sessions table, which holds sign-in tokens.
        """
        config = self.config()
        budget = min(int(budget), int(config['history_batch'] or 0))
        if budget <= 0 or not os.path.exists(db_path):
            return 0
        floor = state.get('history_floor')
        if not isinstance(floor, int):
            floor = int(config['history_since'] or 0) or int(time.time())
            state['history_floor'] = floor
        uri = 'file:%s?mode=ro' % urllib.parse.quote(db_path.replace('\\', '/'))
        connection = None
        sent = 0
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=3)
            sent += self._forward_audits(connection, state, budget)
            sent += self._forward_sample_history(connection, state, floor, budget - sent)
            sent += self._forward_check_history(connection, state, floor, budget - sent)
        except sqlite3.Error:
            return sent
        finally:
            if connection is not None:
                connection.close()
        return sent

    def _forward_audits(self, connection, state, budget):
        cursor = int(state.get('audit_cursor') or 0)
        rows = connection.execute(
            'SELECT id, ts, user, action, outcome, detail FROM audits '
            'WHERE id > ? ORDER BY id LIMIT ?', (cursor, budget)).fetchall()
        count = 0
        for row in rows:
            event = {'event': 'galaxy.audit', 'user': row[2], 'action': row[3],
                     'outcome': row[4], 'detail': row[5], '_time': utc_iso(row[1])}
            if not self.emit(event, entry_point='forwarder'):
                break
            cursor = row[0]
            count += 1
        state['audit_cursor'] = cursor
        return count

    def _forward_sample_history(self, connection, state, floor, budget):
        if budget <= 0:
            return 0
        cursor = int(state.get('sample_cursor') or 0)
        rows = connection.execute(
            'SELECT %s FROM samples WHERE ts < ? AND ts > ? ORDER BY ts LIMIT ?'
            % ', '.join(SAMPLE_COLUMNS), (floor, cursor, budget)).fetchall()
        count = 0
        for row in rows:
            value = dict(zip(SAMPLE_COLUMNS, row))
            event = {'event': 'galaxy.sample', '_time': utc_iso(value['ts']),
                     'uptime_s': value['uptime_s'], 'mem_pct': value['mem_pct'],
                     'mem_used_mb': value['mem_used_mb'],
                     'mem_total_mb': value['mem_total_mb'],
                     'swap_pct': value['swap_pct'], 'free_gb': value['free_gb'],
                     'storage_pct': value['storage_pct'], 'batt_pct': value['batt'],
                     'charging': (bool(value['plugged'])
                                  if value['plugged'] is not None else None),
                     'temp_c': value['temp_c'], 'cpu_pct': value['cpu_pct'],
                     'services': {'nginx': bool(value['nginx']),
                                  'status': bool(value['api']),
                                  'supervisor': bool(value['supervisor']),
                                  'cloudflared': bool(value['tunnel_proc'])},
                     'tunnel_ready': bool(value['tunnel_ready']),
                     'tunnel_connections': value['tunnel_conns'],
                     'tunnel_requests': value['tunnel_req'],
                     'https_ok': (bool(value['https_ok'])
                                  if value['https_ok'] is not None else None),
                     'https_ms': value['https_ms']}
            if not self.emit(event, entry_point='backfill'):
                break
            cursor = value['ts']
            count += 1
        state['sample_cursor'] = cursor
        return count

    def _forward_check_history(self, connection, state, floor, budget):
        if budget <= 0:
            return 0
        cursor = int(state.get('check_cursor') or 0)
        host_cursor = state.get('check_host') or ''
        rows = connection.execute(
            'SELECT ts, host, ok, status, ms FROM checks '
            'WHERE ts < ? AND (ts > ? OR (ts = ? AND host > ?)) '
            'ORDER BY ts, host LIMIT ?',
            (floor, cursor, cursor, host_cursor, budget)).fetchall()
        count = 0
        for ts, host, ok, status, ms in rows:
            event = {'event': 'galaxy.check', 'host': host, 'ok': bool(ok),
                     'status': status, 'ms': ms, '_time': utc_iso(ts)}
            if not self.emit(event, entry_point='backfill'):
                break
            cursor, host_cursor = ts, host
            count += 1
        state['check_cursor'], state['check_host'] = cursor, host_cursor
        return count

    # ------------------------------------------------------------------- loop

    def start(self, log_files, log_dir, db_path):
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        args=(log_files, log_dir, db_path),
                                        name='telemetry', daemon=True)
        self._thread.start()
        return True

    def _loop(self, log_files, log_dir, db_path):
        while not self._stop.is_set():
            try:
                self.drain(log_files, log_dir, db_path)
                # A busy log can produce more than one batch per tick. Send a
                # bounded number now instead of letting the queue grow.
                for _ in range(10):
                    result = self.flush()
                    if not result.get('ok') or not result.get('events'):
                        break
            except Exception as error:  # never let the export thread die
                with self._lock:
                    self._stats['last_error'] = scrub('%s: %s' % (type(error).__name__, error), 200)
                    self._stats['last_error_at'] = time.time()
            interval = max(2, min(int(self.config()['flush_seconds']), 3600))
            self._stop.wait(interval)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def backfill(self, log_files, log_dir, db_path, limit=5000):
        """Forward what the cursors have not reached, then flush."""
        config = self.config()
        if not self._ready(config):
            return {'ok': False, 'reason': 'telemetry is disabled or unconfigured'}
        total = 0
        while total < limit:
            if self._stop.is_set() and self._thread is not None:
                break
            counts = self.drain(log_files, log_dir, db_path,
                                limit=min(500, limit - total))
            moved = counts['logs'] + counts['events'] + counts['history']
            if not moved:
                break
            total += moved
            self.flush()
        self.flush()
        return {'ok': True, 'forwarded': total, 'queued': len(self._queue),
                'spooled': self._spool_info()[1]}

    # --------------------------------------------------------------- retention

    def _manifest(self, config):
        token = config.get('manage_token') or config['token']
        url = '%s/v2/datasets/%s' % (MANAGE_ENDPOINT,
                                     urllib.parse.quote(config['dataset'], safe=''))
        return token, url

    def _manage(self, method, url, token, payload=None, timeout=10):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json',
            'User-Agent': 'galaxy-console-telemetry/1',
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read(65536).decode('utf-8', 'replace')
                return response.getcode(), text
        except urllib.error.HTTPError as error:
            return error.code, error.read(4096).decode('utf-8', 'replace')
        except (urllib.error.URLError, OSError) as error:
            raise ConfigError('Axiom management request failed: %s' % type(error).__name__)

    def dataset_retention(self):
        config = self.config()
        token, url = self._manifest(config)
        if not token:
            raise ConfigError('no token configured')
        status, text = self._manage('GET', url, token)
        if status == 404:
            return None
        if status != 200:
            raise ConfigError('could not read dataset (HTTP %d)' % status)
        try:
            return json.loads(text).get('retentionDays')
        except (ValueError, AttributeError):
            return None

    def apply_retention(self, days=None, force_lower=False):
        """Set the Axiom dataset retention. Lowering deletes older data."""
        config = self.config()
        days = config['retention_days'] if days is None else int(days)
        token, url = self._manifest(config)
        if not token:
            raise ConfigError('no token configured')
        current = self.dataset_retention()
        if current and days and days < current and not force_lower:
            raise ConfigError(
                'refusing to lower retention from %d to %d days; '
                'data older than the new window is deleted. Pass --force-lower.'
                % (current, days))
        status, text = self._manage('PUT', url, token,
                                    {'description': DESCRIPTION, 'retentionDays': days})
        if status not in (200, 201):
            raise ConfigError('dataset update failed (HTTP %d): %s'
                              % (status, scrub(text, 200)))
        return {'ok': True, 'previous': current, 'retention_days': days}

    # -------------------------------------------------------------- diagnosis

    def _ingest_probe(self, base, config):
        """Post one probe event to an ingest base URL and report the answer."""
        url = '%s/v1/ingest/%s?timestamp-field=_time' % (
            base.rstrip('/'), urllib.parse.quote(config['dataset'], safe=''))
        body = (json.dumps({'event': 'galaxy.probe', 'device': DEVICE, '_time': utc_iso()},
                           separators=(',', ':')) + '\n').encode()
        request = urllib.request.Request(url, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + config['token'],
            'Content-Type': 'application/x-ndjson',
            'User-Agent': 'galaxy-console-telemetry/1',
        })
        try:
            with urllib.request.urlopen(request, timeout=config['timeout_seconds']) as response:
                return response.getcode(), scrub(
                    response.read(512).decode('utf-8', 'replace'), 120)
        except urllib.error.HTTPError as error:
            try:
                detail = error.read(512).decode('utf-8', 'replace')
            except OSError:
                detail = ''
            return error.code, scrub(detail, 120)
        except (urllib.error.URLError, OSError, ValueError) as error:
            return None, scrub('%s: %s' % (type(error).__name__, error), 120)

    def create_dataset(self):
        """Create the dataset. Axiom answers 409 when it already exists."""
        config = self.config()
        token, _url = self._manifest(config)
        if not token:
            raise ConfigError('no token configured')
        status, text = self._manage('POST', MANAGE_ENDPOINT + '/v2/datasets', token,
                                    {'name': config['dataset'],
                                     'description': DESCRIPTION,
                                     'retentionDays': config['retention_days']})
        return {'status': status, 'ok': status in (200, 201, 409),
                'detail': scrub(text, 160)}

    def diagnose(self, create=False, apply=False, hosts=None):
        """Find out why ingest fails: token, dataset, or edge region.

        Axiom's edge answers 404 "dataset not found" both when the dataset is
        missing and when the request went to a region the organisation does not
        live in, so every region is probed with a real event instead of trusting
        the configured endpoint.
        """
        config = self.config()
        report = {'dataset': config['dataset'], 'endpoint': config['endpoint'],
                  'retention_days': config['retention_days'],
                  'token': bool(config['token']),
                  'management_token': bool(config.get('manage_token')),
                  'dataset_lookup': None, 'dataset_create': None, 'ingest': {},
                  'recommended_endpoint': None, 'endpoint_updated': False,
                  'verdict': None, 'next': None}
        if not config['token']:
            report['verdict'] = 'no token configured'
            report['next'] = 'serverctl telemetry-setup'
            return report
        token, url = self._manifest(config)
        status, text = self._manage('GET', url, token)
        report['dataset_lookup'] = {'status': status, 'detail': scrub(text, 160)}
        if create and status != 200:
            report['dataset_create'] = self.create_dataset()

        hosts = hosts or {region: EDGE_HOST % region for region in EDGE_REGIONS}
        for region, base in hosts.items():
            status, detail = self._ingest_probe(base, config)
            report['ingest'][region] = {'status': status, 'detail': detail}
        working = [region for region, result in report['ingest'].items()
                   if result['status'] and 200 <= result['status'] < 300]
        rejected = [region for region, result in report['ingest'].items()
                    if result['status'] in (401, 403)]

        if working:
            best = working[0]
            report['recommended_endpoint'] = hosts[best]
            if apply and config['endpoint'].rstrip('/') != hosts[best].rstrip('/'):
                config['endpoint'] = hosts[best].rstrip('/')
                save_config(self.config_path, config)
                self._config_cache = None
                self._config_checked = 0
                report['endpoint'] = config['endpoint']
                report['endpoint_updated'] = True
            report['verdict'] = 'ready'
            report['next'] = ('serverctl telemetry-doctor --apply' if not report['endpoint_updated']
                              and report['recommended_endpoint'].rstrip('/')
                              != config['endpoint'].rstrip('/') else 'serverctl telemetry-test')
        elif len(rejected) == len(report['ingest']):
            report['verdict'] = 'token rejected by every edge region'
            report['next'] = ('create an Axiom API token with ingest permission for '
                              'dataset %r, then serverctl telemetry-setup' % config['dataset'])
        else:
            report['verdict'] = 'no edge region found the dataset'
            if report['dataset_lookup']['status'] == 200:
                report['verdict'] = 'dataset exists but ingest is refused'
            report['next'] = ('create dataset %r in Axiom (Settings > Datasets > '
                              'New dataset) and allow it on the API token, or run '
                              'serverctl telemetry-doctor --create with a token that '
                              'may manage datasets' % config['dataset'])
        return report

    # ----------------------------------------------------------------- status

    def status(self):
        config = self.config()
        spool_kb, spool_events = self._spool_info()
        with self._lock:
            stats = dict(self._stats)
            queued = len(self._queue)
        now = time.time()
        state = 'off'
        if self._config_error:
            state = 'error'
        elif self._ready(config):
            recent = stats['last_error_at'] and now - stats['last_error_at'] < max(
                60, config['flush_seconds'] * 4)
            state = 'degraded' if recent else ('ok' if stats['last_success_at'] else 'idle')
        return {
            'state': state,
            'enabled': bool(config['enabled']),
            'configured': bool(config['token'] and config['dataset']),
            'config_error': self._config_error,
            'endpoint': urllib.parse.urlparse(config['endpoint']).hostname,
            'dataset': config['dataset'],
            'retention_days': config['retention_days'],
            'device': DEVICE,
            'queued': queued,
            'spool_events': spool_events,
            'spool_kb': spool_kb,
            'exported': stats['exported'],
            'failed': stats['failed'],
            'rejected': stats['rejected'],
            'dropped': stats['dropped'],
            'deferred': stats['deferred'],
            'batches': stats['batches'],
            'last_success_at': utc_iso(stats['last_success_at']) if stats['last_success_at'] else None,
            'last_error': stats['last_error'],
            'last_error_at': utc_iso(stats['last_error_at']) if stats['last_error_at'] else None,
            'last_status': stats['last_status'],
        }

    def send_test(self):
        if not self.emit({'event': 'galaxy.test', 'message': 'telemetry self-test'},
                         entry_point='cli'):
            return {'ok': False, 'reason': 'telemetry is disabled or unconfigured'}
        return self.flush()


# ---------------------------------------------------------------------- setup

def setup_config(path, args):
    try:
        config = load_config(path)
    except ConfigError:
        if not args.force:
            raise
        config = dict(DEFAULTS)
    if args.endpoint:
        config['endpoint'] = args.endpoint.rstrip('/')
    if args.dataset:
        config['dataset'] = args.dataset
    if args.retention_days is not None:
        config['retention_days'] = args.retention_days
    if args.batch_size is not None:
        config['batch_size'] = args.batch_size
    if args.flush_seconds is not None:
        config['flush_seconds'] = args.flush_seconds
    if args.spool_max_kb is not None:
        config['spool_max_kb'] = args.spool_max_kb
    if args.token_file:
        if os.name == 'posix' and stat.S_IMODE(os.stat(args.token_file).st_mode) & 0o077:
            raise ConfigError('token file must be chmod 600')
        with open(args.token_file, encoding='utf-8') as handle:
            config['token'] = handle.read().strip()
    elif args.token:
        print('warning: passing --token leaves the value in shell history', file=sys.stderr)
        config['token'] = args.token
    elif not config['token']:
        config['token'] = getpass.getpass('Axiom API token (input hidden): ').strip()
    if args.disable:
        config['enabled'] = False
    elif config['token']:
        config['enabled'] = True
    validate_config(config)
    save_config(path, config)
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description='Galaxy console Axiom telemetry')
    default_config = os.environ.get(
        'GALAXY_AXIOM_CONFIG',
        os.path.expanduser('~/galaxy-server/run/axiom.json'))
    parser.add_argument('--config', default=default_config)
    sub = parser.add_subparsers(dest='command', required=True)

    setup = sub.add_parser('setup', help='write run/axiom.json with mode 0600')
    setup.add_argument('--endpoint', default=None)
    setup.add_argument('--dataset', default=None)
    setup.add_argument('--retention-days', type=int, default=None)
    setup.add_argument('--batch-size', type=int, default=None)
    setup.add_argument('--flush-seconds', type=int, default=None)
    setup.add_argument('--spool-max-kb', type=int, default=None)
    setup.add_argument('--token', default=None)
    setup.add_argument('--token-file', default=None)
    setup.add_argument('--disable', action='store_true')
    setup.add_argument('--force', action='store_true')

    sub.add_parser('status', help='show export health without secrets')
    sub.add_parser('test', help='send one event and flush')

    doctor = sub.add_parser('doctor', help='find why ingest fails: token, dataset or region')
    doctor.add_argument('--create', action='store_true', help='create the dataset if the token may')
    doctor.add_argument('--apply', action='store_true', help='write the working edge endpoint')

    retention = sub.add_parser('retention', help='read or set dataset retention')
    retention.add_argument('--days', type=int, default=None)
    retention.add_argument('--apply', action='store_true')
    retention.add_argument('--force-lower', action='store_true')

    args = parser.parse_args(argv)

    if args.command == 'setup':
        try:
            config = setup_config(args.config, args)
        except (ConfigError, OSError, ValueError) as error:
            print('error: %s' % error, file=sys.stderr)
            return 2
        print('wrote %s%s' % (args.config, ' (mode 0600)' if os.name == 'posix' else ''))
        print('dataset %s at %s, retention %d days, %s'
              % (config['dataset'], urllib.parse.urlparse(config['endpoint']).hostname,
                 config['retention_days'], 'enabled' if config['enabled'] else 'disabled'))
        print('next: python %s --config %s test' % (os.path.basename(__file__), args.config))
        return 0

    client = Telemetry(os.path.dirname(args.config) or '.', args.config)
    if args.command == 'status':
        print(json.dumps(client.status(), indent=2))
        return 0
    if args.command == 'test':
        result = client.send_test()
        print(json.dumps(result, indent=2))
        return 0 if result.get('ok') else 1
    if args.command == 'doctor':
        try:
            result = client.diagnose(create=args.create, apply=args.apply)
        except (ConfigError, OSError, ValueError) as error:
            print('error: %s' % error, file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0 if result.get('verdict') == 'ready' else 1
    if args.command == 'retention':
        try:
            if not args.apply:
                print(json.dumps({'dataset_retention_days': client.dataset_retention(),
                                  'configured_retention_days':
                                      client.config()['retention_days']}, indent=2))
                return 0
            result = client.apply_retention(args.days, args.force_lower)
        except (ConfigError, ValueError) as error:
            print('error: %s' % error, file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
