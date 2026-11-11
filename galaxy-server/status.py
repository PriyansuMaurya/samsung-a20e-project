#!/usr/bin/env python3
"""Galaxy Console API: live status, history, logs and controls.

Binds 127.0.0.1:8765 only. nginx serves the public landing page and the static
assets, and proxies /login, /console and /api/ here.

Auth is a signed-in session cookie checked on every private endpoint; the
password itself lives in run/htpasswd (serverctl passwd). Service actions run
only whitelisted commands; the separately authenticated terminal runs a shell.

Data that a non-root Termux app can actually read:
  /proc/meminfo, /proc/<pid>/status (VmRSS), /proc/<pid>/stat (starttime,
  utime, stime), shutil.disk_usage, termux-battery-status, cloudflared metrics
  on 127.0.0.1:20241 and HTTPS probes of the public hostnames.
Android blocks /proc/stat and /proc/loadavg, so system-wide CPU is reported as
unavailable; the CPU series is the share of one core used by the server's own
processes, measured from their /proc/<pid>/stat ticks.
"""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import http.client
import http.server
import json
import os
import platform
import re
import secrets
import signal
import shutil
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlparse

import telemetry

HOME = os.path.expanduser('~')
BASE = os.path.join(HOME, 'galaxy-server')
LOG_DIR = os.path.join(BASE, 'log')
RUN_DIR = os.path.join(BASE, 'run')
SITE_DIR = os.path.join(BASE, 'site')
PRIV_DIR = os.path.join(BASE, 'priv')
DB_PATH = os.path.join(RUN_DIR, 'galaxy.db')
SETTINGS_PATH = os.path.join(RUN_DIR, 'settings.json')
DEVICE_PATH = os.path.join(RUN_DIR, 'device.json')
PASSFILE = os.path.join(RUN_DIR, 'htpasswd')
TERMINAL_PASSFILE = os.path.join(RUN_DIR, 'terminal.passwd')
SERVERCTL = os.path.join(BASE, 'serverctl')
METRICS_URL = 'http://127.0.0.1:20241'

# Remote export is optional: with no run/axiom.json every emit is a no-op and
# the local files and database stay exactly as they were.
TELEMETRY = telemetry.Telemetry(RUN_DIR)

SAMPLE_SECONDS = 30
PROBE_SECONDS = 60
HOST_PROBE_SECONDS = 120
SESSION_SECONDS = 12 * 3600
RAW_RETENTION_DAYS = 7
EVENT_RETENTION_DAYS = 30
HOSTNAMES = ('galaxy.maurya.ie', 'small.maurya.ie')
DETAILED_HOST = 'small.maurya.ie'

LOG_FILES = {
    'nginx-error': 'nginx-error.log',
    'tunnel': 'tunnel.log',
    'supervisor': 'supervisor.log',
    'status': 'status.log',
    'auth': 'auth.log',
    # Release history: which commit was installed, and what a failed install
    # said. Both are operationally useful and contain no credentials.
    'deploy': 'deploy.log',
    'deploy-manual': 'manual-deploy.log',
    # Nginx writes only failed requests here, so the file stays small.
    'access': 'access.log',
}

SERVICES = (
    ('nginx', 'nginx', 'nginx: master'),
    ('status', 'status API', 'status.py'),
    ('supervisor', 'supervisor', 'supervise.sh'),
    ('cloudflared', 'cloudflared', 'cloudflared tunnel run'),
)

ACTIONS = {
    'server-restart': ('restart', 'Restart the server', True),
    'server-stop': ('stop', 'Stop the server', True),
    'tunnel-start': ('tunnel-start', 'Start the tunnel', False),
    'tunnel-stop': ('tunnel-stop', 'Stop the tunnel', True),
    'tunnel-restart': ('tunnel-restart', 'Restart the tunnel', True),
}

RANGES = {'1h': 3600, '24h': 86400, '7d': 604800}
BUCKETS = {'1h': 60, '24h': 300, '7d': 1800}

try:
    CLOCK_TICKS = os.sysconf('SC_CLK_TCK')
except (ValueError, OSError, AttributeError):
    CLOCK_TICKS = 100

DB_LOCK = threading.Lock()
LOGIN_FAILS = {}
PROBE_CACHE = {'hosts': {}}
TERMINALS = {}
TERMINAL_FAILS = []
TERMINAL_LOCK = threading.RLock()
TERMINAL_IDLE_SECONDS = 600
# Shells are cheap to start and expensive to keep: cap them per signed-in
# browser, and give a reloaded page time to reattach before reaping.
TERMINAL_MAX = 4
# A shell whose page goes away - a reload, a phone screen off, a tunnel blip -
# is kept for as long as an idle one, so coming back finds it still there.
TERMINAL_DETACH_SECONDS = 600
# Recent output is kept so a reloaded or second tab can redraw the screen.
TERMINAL_BUFFER = 32768


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _run(*cmd, timeout=5):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return ''


def _boottime():
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (OSError, AttributeError):
        return 0.0


def _iso(ts):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts)) if ts else None


def _human(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return '%dd %dh %dm' % (days, hours, mins)
    if hours:
        return '%dh %dm' % (hours, mins)
    return '%dm %ds' % (mins, rem % 60)


@contextmanager
def _db():
    connection = sqlite3.connect(DB_PATH, timeout=8, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=NORMAL')
        with connection:
            yield connection
    finally:
        connection.close()


def init_db():
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    with DB_LOCK, _db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS samples(
            ts INTEGER PRIMARY KEY,
            uptime_s INTEGER,
            mem_used_mb INTEGER, mem_total_mb INTEGER, mem_pct INTEGER,
            swap_pct INTEGER, free_gb REAL, storage_pct INTEGER,
            batt INTEGER, plugged INTEGER, temp_c REAL,
            cpu_pct REAL,
            nginx INTEGER, api INTEGER, supervisor INTEGER, tunnel_proc INTEGER,
            tunnel_ready INTEGER, tunnel_conns INTEGER, tunnel_req INTEGER,
            tunnel_errors INTEGER, https_ok INTEGER, https_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT, service TEXT
        );
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS checks(
            ts INTEGER NOT NULL, host TEXT NOT NULL, ok INTEGER,
            status INTEGER, ms INTEGER,
            PRIMARY KEY (ts, host)
        );
        CREATE TABLE IF NOT EXISTS audits(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL, user TEXT, action TEXT, outcome TEXT, detail TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY, user TEXT NOT NULL, expires INTEGER NOT NULL
        );
        ''')
        # Databases created before the service column existed keep working.
        try:
            db.execute('ALTER TABLE events ADD COLUMN service TEXT')
        except sqlite3.OperationalError:
            pass
        # Same for the tunnel error counter, added after the first release.
        try:
            db.execute('ALTER TABLE samples ADD COLUMN tunnel_errors INTEGER')
        except sqlite3.OperationalError:
            pass


def add_event(kind, detail='', service=None):
    with DB_LOCK, _db() as db:
        db.execute('INSERT INTO events(ts, kind, detail, service) VALUES (?,?,?,?)',
                   (int(time.time()), kind, detail[:300], service))


def add_audit(user, action, outcome, detail=''):
    try:
        path = os.path.join(LOG_DIR, 'auth.log')
        if os.path.exists(path) and os.path.getsize(path) > 64 * 1024:
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write('%s user=%s action=%s outcome=%s %s\n'
                         % (time.strftime('%Y-%m-%d %H:%M:%S'), user, action, outcome,
                            redact(detail)[1][:160]))
    except OSError:
        pass
    with DB_LOCK, _db() as db:
        db.execute('INSERT INTO audits(ts, user, action, outcome, detail) VALUES (?,?,?,?,?)',
                   (int(time.time()), user, action, outcome, detail[:300]))
        db.execute('INSERT INTO events(ts, kind, detail) VALUES (?,?,?)',
                   (int(time.time()), 'admin', '%s %s: %s' % (user, action, outcome)))


def read_settings():
    defaults = {'host_probes': True}
    try:
        with open(SETTINGS_PATH) as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            defaults.update({k: v for k, v in stored.items() if k in defaults})
    except (OSError, ValueError):
        pass
    return defaults


def write_settings(values):
    os.makedirs(RUN_DIR, exist_ok=True)
    temp = SETTINGS_PATH + '.tmp'
    with open(temp, 'w') as handle:
        json.dump(values, handle)
    os.replace(temp, SETTINGS_PATH)


# --------------------------------------------------------------------------- #
# device control app (127.0.0.1:8790)
# --------------------------------------------------------------------------- #

DEVICE_DEFAULT_PORT = 8790

# Only these controls exist, so a compromised console session cannot ask the
# app for anything the UI never offers.
DEVICE_CONTROLS = {
    'torch': ('POST', '/torch'),
    'vibrate': ('POST', '/vibrate'),
    'brightness': ('POST', '/brightness'),
    'volume': ('POST', '/volume'),
    'camera-start': ('POST', '/camera/start'),
    'camera-stop': ('POST', '/camera/stop'),
    'snapshot': ('GET', '/camera/snapshot'),
    'record-start': ('POST', '/audio/record/start'),
    'record-stop': ('POST', '/audio/record/stop'),
    'video-start': ('POST', '/video/start'),
    'video-stop': ('POST', '/video/stop'),
    'video-delete': ('POST', '/video/delete'),
    'rotation': ('POST', '/rotation'),
    'media-play': ('POST', '/media/play'),
    'media-control': ('POST', '/media/control'),
    'media-delete': ('POST', '/media/delete'),
    'photo-delete': ('POST', '/photo/delete'),
    'recording-play': ('POST', '/recording/play'),
    'recording-delete': ('POST', '/recording/delete'),
}

DEVICE_READS = {
    '/api/device/capabilities': '/capabilities',
    '/api/device/battery': '/battery',
    '/api/device/storage': '/storage',
    '/api/device/sensors': '/sensors',
    '/api/device/connectivity': '/connectivity',
    '/api/device/volume': '/volume',
    '/api/device/photos': '/photos',
    '/api/device/recordings': '/recordings',
    '/api/device/videos': '/videos',
    '/api/device/video': '/video/status',
    '/api/device/media': '/media/list',
    '/api/device/audio': '/audio/status',
}

DEVICE_STREAMS = {
    '/api/device/camera/stream': '/camera/stream',
    '/api/device/audio/stream': '/audio/stream',
    '/api/device/photo': '/photo',
    '/api/device/recording': '/recording',
    '/api/device/media/get': '/media/get',
}


def read_device():
    try:
        with open(DEVICE_PATH, encoding='utf-8') as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return {'token': str(stored.get('token') or ''),
            'port': int(stored.get('port') or DEVICE_DEFAULT_PORT)}


def write_device(values):
    os.makedirs(RUN_DIR, exist_ok=True)
    temp = DEVICE_PATH + '.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(values, handle)
        os.replace(temp, DEVICE_PATH)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def device_call(path, payload=None, method=None, timeout=12):
    """One request to the control app. Returns (status, decoded json)."""
    config = read_device()
    body = None
    headers = {'X-Galaxy-Token': config['token']}
    if payload is not None:
        body = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
        method = method or 'POST'
    connection = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=timeout)
    try:
        connection.request(method or 'GET', path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        try:
            return response.status, json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return response.status, {'error': 'unreadable response from the device app'}
    finally:
        connection.close()


def device_path(base, query):
    pairs = []
    for key in ('camera', 'size', 'fps', 'file'):
        value = (query.get(key) or [''])[0]
        if value:
            pairs.append(quote(key, safe='') + '=' + quote(value, safe=''))
    return base + ('?' + '&'.join(pairs) if pairs else '')


# --------------------------------------------------------------------------- #
# host metrics
# --------------------------------------------------------------------------- #

def memory():
    values = {}
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                parts = line.replace(':', ' ').split()
                if len(parts) >= 2 and parts[1].isdigit():
                    values[parts[0]] = int(parts[1])
    except OSError:
        return None
    total = values.get('MemTotal')
    available = values.get('MemAvailable')
    if not total or available is None:
        return None
    used = total - available
    swap_total = values.get('SwapTotal', 0)
    swap_free = values.get('SwapFree', 0)
    return {
        'total_mb': round(total / 1024),
        'available_mb': round(available / 1024),
        'used_mb': round(used / 1024),
        'used_percent': round(100 * used / total),
        'cached_mb': round(values.get('Cached', 0) / 1024),
        'swap_total_mb': round(swap_total / 1024),
        'swap_used_mb': round((swap_total - swap_free) / 1024),
        'swap_used_percent': round(100 * (swap_total - swap_free) / swap_total) if swap_total else 0,
    }


def storage():
    try:
        usage = shutil.disk_usage(HOME)
    except OSError:
        return None
    return {
        'total_gb': round(usage.total / 1024 ** 3, 1),
        'used_gb': round(usage.used / 1024 ** 3, 1),
        'free_gb': round(usage.free / 1024 ** 3, 1),
        'used_percent': round(100 * usage.used / usage.total),
    }


BATTERY_CACHE = {'ts': 0, 'value': None}


def battery():
    """Needs the Termux:API app plus the termux-api package. Cached briefly:
    every page poll asking the Android binder for the battery is wasteful."""
    if time.time() - BATTERY_CACHE['ts'] < 15:
        return BATTERY_CACHE['value']
    value = _battery_read()
    BATTERY_CACHE.update({'ts': time.time(), 'value': value})
    return value


def _battery_read():
    try:
        done = subprocess.run(['termux-battery-status'], capture_output=True,
                              text=True, timeout=4)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    temperature = data.get('temperature')
    return {
        'percent': data.get('percentage'),
        'status': data.get('status'),
        'plugged': data.get('plugged'),
        'charging': str(data.get('status', '')).upper() in ('CHARGING', 'FULL')
        or str(data.get('plugged', '')).upper() not in ('UNPLUGGED', 'NONE', ''),
        'temperature_c': round(temperature, 1) if isinstance(temperature, (int, float)) else None,
        'health': data.get('health'),
    }


def _host():
    return {
        'device': _run('getprop', 'ro.product.model').strip() or 'Samsung Galaxy A20e',
        'android': _run('getprop', 'ro.build.version.release').strip() or None,
        'kernel': platform.release(),
        'arch': platform.machine(),
        'python': '%d.%d.%d' % tuple(__import__('sys').version_info[:3]),
        'cores': os.cpu_count(),
    }


HOST = _host()


def _versions():
    nginx = re.search(r'nginx/([\d.]+)', _run('nginx', '-v'))
    cloudflared = re.search(r'cloudflared version ([\d.]+)', _run('cloudflared', '--version'))
    return {'nginx': nginx.group(1) if nginx else None,
            'cloudflared': cloudflared.group(1) if cloudflared else None}


VERSIONS = _versions()


def scan_processes():
    found = {}
    try:
        entries = os.listdir('/proc')
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as handle:
                command = handle.read().replace(b'\0', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if not command:
            continue
        for key, _label, needle in SERVICES:
            if needle in command and key not in found:
                found[key] = int(entry)
    return found


def process_stat(pid):
    """RSS in MB, seconds since start and CPU ticks used, from /proc."""
    rss = None
    age = None
    ticks = None
    try:
        with open('/proc/%d/status' % pid) as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    rss = round(int(line.split()[1]) / 1024, 1)
                    break
    except OSError:
        pass
    try:
        with open('/proc/%d/stat' % pid) as handle:
            fields = handle.read().rsplit(')', 1)[-1].split()
        started = int(fields[19]) / CLOCK_TICKS
        age = max(0, int(_boottime() - started))
        ticks = int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        pass
    return rss, age, ticks


def _fetch(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode('utf-8', 'replace')


def tunnel():
    state = {
        'ready': False, 'connections': 0, 'connector_id': None,
        'disabled': os.path.exists(os.path.join(RUN_DIR, 'tunnel-disabled')),
        'requests': None, 'errors': None, 'concurrent': None,
        'response_codes': {}, 'goroutines': None, 'memory_mb': None,
        'reachable': False,
    }
    try:
        ready = json.loads(_fetch(METRICS_URL + '/ready'))
        state['reachable'] = True
        state['ready'] = ready.get('status') == 200
        state['connections'] = ready.get('readyConnections') or 0
        state['connector_id'] = ready.get('connectorId')
    except (OSError, ValueError, urllib.error.URLError):
        return state
    try:
        text = _fetch(METRICS_URL + '/metrics', timeout=4)
    except (OSError, urllib.error.URLError):
        return state
    simple = {'requests': 'cloudflared_tunnel_total_requests',
              'errors': 'cloudflared_tunnel_request_errors',
              'goroutines': 'go_goroutines',
              'memory_mb': 'go_memstats_alloc_bytes'}
    for key, name in simple.items():
        match = re.search(r'^%s ([0-9.eE+-]+)$' % re.escape(name), text, re.MULTILINE)
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        state[key] = round(value / 1024 / 1024, 1) if key == 'memory_mb' else int(value)
    concurrent = 0.0
    for value in re.findall(
            r'^cloudflared_tunnel_concurrent_requests_per_tunnel\{[^}]*\}\s+'
            r'(\d+(?:\.\d+)?)$', text, re.MULTILINE):
        concurrent += float(value)
    match = re.search(
        r'^cloudflared_tunnel_concurrent_requests_per_tunnel (\d+(?:\.\d+)?)$',
        text, re.MULTILINE)
    if match:
        concurrent += float(match.group(1))
    state['concurrent'] = int(concurrent)
    codes = {}
    for code, value in re.findall(
            r'^cloudflared_tunnel_response_by_code\{[^}]*?(\d{3})[^}]*\}\s+'
            r'(\d+(?:\.\d+)?)$', text, re.MULTILINE):
        codes[code] = codes.get(code, 0) + int(float(value))
    state['response_codes'] = dict(sorted(codes.items(), key=lambda item: -item[1])[:5])
    return state


# --------------------------------------------------------------------------- #
# HTTPS probes
# --------------------------------------------------------------------------- #

def probe_url(url, timeout=12):
    """Return {ok, status, ms, error} for one URL, reusing one TLS context."""
    started = time.time()
    request = urllib.request.Request(url, method='GET',
                                     headers={'User-Agent': 'galaxy-console-probe'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.getcode()
            response.read(64)
        return {'ok': 200 <= status < 400, 'status': status,
                'ms': int((time.time() - started) * 1000), 'error': None}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'status': error.code,
                'ms': int((time.time() - started) * 1000),
                'error': 'HTTP %d' % error.code}
    except (urllib.error.URLError, OSError) as error:
        return {'ok': False, 'status': None,
                'ms': int((time.time() - started) * 1000),
                'error': str(getattr(error, 'reason', error))[:120]}


def last_check(host):
    with DB_LOCK, _db() as db:
        row = db.execute('SELECT ts, ok, status, ms FROM checks WHERE host=? '
                         'ORDER BY ts DESC LIMIT 1', (host,)).fetchone()
    if not row:
        return None
    return {'ts': row['ts'], 'at': _iso(row['ts']), 'ok': bool(row['ok']),
            'status': row['status'], 'ms': row['ms']}


def record_check(host, result):
    with DB_LOCK, _db() as db:
        db.execute('INSERT OR REPLACE INTO checks(ts, host, ok, status, ms) VALUES (?,?,?,?,?)',
                   (int(time.time()), host, 1 if result.get('ok') else 0,
                    result.get('status'), result.get('ms')))
    TELEMETRY.emit_check(host, result)


# --------------------------------------------------------------------------- #
# sampling
# --------------------------------------------------------------------------- #

STATE = {'prev_ticks': None, 'prev_t': None, 'prev_pids': {}, 'prev_ready': None,
         'prev_https': None, 'last_tick': 0, 'tick': 0, 'started': time.time()}

MONITOR_PATH = os.path.join(RUN_DIR, 'monitor.json')


def load_monitor():
    """Restore the pids seen before this process started.

    Without this, restarting the status service also reset the restart
    counters, so the service table forgot the very restarts an operator had
    just performed.
    """
    try:
        with open(MONITOR_PATH) as handle:
            stored = json.load(handle)
        pids = stored.get('pids') if isinstance(stored, dict) else None
        if isinstance(pids, dict):
            STATE['prev_pids'] = {key: value for key, value in pids.items() if value}
        if isinstance(stored, dict) and 'ready' in stored:
            STATE['prev_ready'] = stored['ready']
    except (OSError, ValueError, AttributeError):
        pass


def save_monitor():
    temp = MONITOR_PATH + '.tmp'
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(temp, 'w') as handle:
            json.dump({'pids': STATE['prev_pids'], 'ready': STATE['prev_ready']}, handle)
        os.replace(temp, MONITOR_PATH)
    except OSError:
        pass


def sample_once():
    now = int(time.time())
    mem = memory()
    disk = storage()
    batt = battery()
    tun = tunnel()
    pids = scan_processes()
    processes = []
    cpu_pct = None
    if pids:
        ticks = 0
        usable = True
        for key, pids_value in pids.items():
            rss, age, used = process_stat(pids_value)
            processes.append((key, pids_value, rss, age))
            if used is None:
                usable = False
            else:
                ticks += used
        if usable and STATE['prev_ticks'] is not None:
            elapsed = max(1, now - (STATE['prev_t'] or now))
            delta = max(0, ticks - STATE['prev_ticks'])
            cpu_pct = round(delta / CLOCK_TICKS / elapsed * 100, 1)
        if usable:
            STATE['prev_ticks'] = ticks
            STATE['prev_t'] = now
    state = {key: 1 if pids.get(key) else 0 for key, _label, _needle in SERVICES}
    state['status'] = 1  # this process is the status API; it is running by definition

    since = now - 86400
    with DB_LOCK, _db() as db:
        previous = db.execute(
            'SELECT ts, uptime_s, batt, plugged, tunnel_ready, https_ok FROM samples '
            'ORDER BY ts DESC LIMIT 1').fetchone()
        db.execute(
            'INSERT OR REPLACE INTO samples(ts, uptime_s, mem_used_mb, mem_total_mb, '
            'mem_pct, swap_pct, free_gb, storage_pct, batt, plugged, temp_c, cpu_pct, '
            'nginx, api, supervisor, tunnel_proc, tunnel_ready, tunnel_conns, tunnel_req, '
            'tunnel_errors, https_ok, https_ms) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (now,
             int(int(_boottime())),
             mem['used_mb'] if mem else None, mem['total_mb'] if mem else None,
             mem['used_percent'] if mem else None,
             mem['swap_used_percent'] if mem else None,
             disk['free_gb'] if disk else None, disk['used_percent'] if disk else None,
             batt.get('percent') if batt else None,
             1 if batt and batt.get('charging') else 0 if batt else None,
             batt.get('temperature_c') if batt else None,
             cpu_pct,
             state['nginx'], state['status'], state['supervisor'], state['cloudflared'],
             1 if tun['ready'] else 0, tun['connections'], tun['requests'],
             tun.get('errors'), None, None))

    # events: reboots, service restarts, tunnel transitions
    if previous is not None and previous['uptime_s'] and int(_boottime()) < previous['uptime_s'] - 60:
        add_event('reboot', 'Device rebooted; uptime restarted at %s' % _human(int(_boottime())))
    for key, pid, _rss, _age in processes:
        old = STATE['prev_pids'].get(key)
        if old and old != pid:
            label = next((l for k, l, _n in SERVICES if k == key), key)
            add_event('restart', '%s restarted (pid %s -> %s)' % (label, old, pid), key)
        STATE['prev_pids'][key] = pid
    for key, _label, _needle in SERVICES:
        if key not in [p[0] for p in processes] and STATE['prev_pids'].get(key):
            label = next((l for k, l, _n in SERVICES if k == key), key)
            add_event('down', '%s stopped (was pid %s)' % (label, STATE['prev_pids'][key]), key)
            STATE['prev_pids'][key] = None
    ready = bool(tun['ready'])
    if STATE['prev_ready'] is not None and ready != STATE['prev_ready']:
        add_event('tunnel-up' if ready else 'tunnel-down',
                  'Cloudflare connector %s' % ('reconnected' if ready else 'disconnected'))
    STATE['prev_ready'] = ready
    save_monitor()
    return {'now': now, 'mem': mem, 'disk': disk, 'batt': batt, 'tunnel': tun,
            'processes': processes, 'cpu': cpu_pct, 'pids': pids}


def _emit_sample(sample):
    """One structured metric row per sampler tick, independent of the console."""
    pids = sample['pids']
    tun = sample['tunnel']
    batt = sample['batt'] or {}
    mem = sample['mem'] or {}
    disk = sample['disk'] or {}
    TELEMETRY.emit({
        'event': 'galaxy.sample',
        '_time': telemetry.utc_iso(sample['now']),
        'uptime_s': int(_boottime()),
        'mem_pct': mem.get('used_percent'),
        'mem_used_mb': mem.get('used_mb'),
        'mem_total_mb': mem.get('total_mb'),
        'swap_pct': mem.get('swap_used_percent'),
        'free_gb': disk.get('free_gb'),
        'storage_pct': disk.get('used_percent'),
        'batt_pct': batt.get('percent'),
        'charging': bool(batt.get('charging')) if sample['batt'] else None,
        'temp_c': batt.get('temperature_c'),
        'cpu_pct': sample['cpu'],
        'services': {key: bool(pids.get(key)) for key, _label, _needle in SERVICES},
        'tunnel_ready': bool(tun.get('ready')),
        'tunnel_connections': tun.get('connections'),
        'tunnel_requests': tun.get('requests'),
        'tunnel_errors': tun.get('errors'),
    }, entry_point='sampler')


def prune():
    cutoff = int(time.time()) - RAW_RETENTION_DAYS * 86400
    with DB_LOCK, _db() as db:
        db.execute('DELETE FROM samples WHERE ts < ?', (cutoff,))
        db.execute('DELETE FROM checks WHERE ts < ?', (int(time.time()) - 86400 * 3,))
        db.execute('DELETE FROM events WHERE ts < ?',
                   (int(time.time()) - EVENT_RETENTION_DAYS * 86400,))
        db.execute('DELETE FROM audits WHERE ts < ?',
                   (int(time.time()) - EVENT_RETENTION_DAYS * 86400,))


def sampler():
    while True:
        started = time.monotonic()
        try:
            sample = sample_once()
            _emit_sample(sample)
            running = {key: bool(sample['pids'].get(key)) for key, _label, _needle in SERVICES}
            running['status'] = True  # this process is the status API
            TELEMETRY.emit_health(*health_state(
                sample['now'], sample['mem'], sample['disk'], sample['batt'],
                sample['tunnel'], running, time.time() - sample['now']))
            STATE['tick'] += 1
            if STATE['tick'] % 4 == 3 and read_settings().get('host_probes'):
                for host in HOSTNAMES:
                    result = probe_url('https://%s/' % host)
                    record_check(host, result)
            if STATE['tick'] % 120 == 0:
                prune()
        except Exception:
            import traceback
            traceback.print_exc()
        time.sleep(max(0, SAMPLE_SECONDS - (time.monotonic() - started)))


# --------------------------------------------------------------------------- #
# payloads
# --------------------------------------------------------------------------- #

def _state_from(ok, detail, warn=False):
    return {'state': 'ok' if ok and not warn else ('warn' if ok else 'crit'),
            'detail': detail}


SERVICES_CACHE = {'ts': 0, 'value': None}


def services_payload():
    if SERVICES_CACHE['value'] is not None and time.time() - SERVICES_CACHE['ts'] < 20:
        return SERVICES_CACHE['value']
    value = _services_read()
    SERVICES_CACHE.update({'ts': time.time(), 'value': value})
    return value


def _services_read():
    boottime = _boottime()
    pids = scan_processes()
    pids.setdefault('status', os.getpid())
    since = int(time.time()) - 86400
    with DB_LOCK, _db() as db:
        restarts = {row['service']: row['n']
                    for row in db.execute(
                        "SELECT service, count(*) AS n FROM events WHERE kind IN "
                        "('restart','down') AND service IS NOT NULL AND ts>=? "
                        "GROUP BY service", (since,))}
    result = []
    for key, label, _needle in SERVICES:
        pid = pids.get(key)
        rss, age, _ticks = process_stat(pid) if pid else (None, None, None)
        last_error = None
        log_name = {'nginx': 'nginx-error', 'status': 'status',
                    'supervisor': 'supervisor', 'cloudflared': 'tunnel'}.get(key)
        if log_name:
            for line in reversed(tail(log_name, 120)):
                if re.search(r'error|fail|panic|crit|warn', line, re.I):
                    last_error = redact(line)[1][:220]
                    break
        result.append({
            'key': key, 'label': label, 'running': pid is not None, 'pid': pid,
            'rss_mb': rss, 'uptime_s': age, 'uptime': _human(age),
            'restarts_24h': restarts.get(key, 0), 'last_error': last_error,
        })
    return result


def health_state(now, mem, disk, batt, tun, running, sample_age, https_state='unknown'):
    """The alert rules, shared by the console payload and the exported health.

    A health event used to be produced only while somebody had the console
    open, which meant an unobserved phone exported samples but never said it
    was in trouble. The sampler calls this with the data it already collected.
    """
    alerts = []
    if not running.get('nginx'):
        alerts.append({'level': 'crit', 'text': 'nginx is not running, the site is unreachable'})
    if not running.get('status'):
        alerts.append({'level': 'crit', 'text': 'status API is not running, this console has no data'})
    if not running.get('supervisor'):
        alerts.append({'level': 'warn', 'text': 'supervisor is not running, nothing will restore the server'})
    if tun['disabled']:
        alerts.append({'level': 'warn', 'text': 'tunnel is stopped, the public URL is offline'})
    elif not tun['ready']:
        alerts.append({'level': 'crit', 'text': 'tunnel connector is not ready, the public URL is offline'})
    # cloudflared's error counter is cumulative, so only a recent increase is
    # worth a health warning; a stale count would keep the console degraded.
    errors = tun.get('errors')
    if errors is not None:
        previous_errors = STATE.get('last_errors')
        if previous_errors is None:
            STATE['last_errors'] = errors
            STATE['last_errors_at'] = 0
        elif previous_errors != errors:
            STATE['last_errors'] = errors
            STATE['last_errors_at'] = now
        if errors and now - (STATE.get('last_errors_at') or 0) < 300:
            alerts.append({'level': 'warn',
                           'text': 'tunnel reported %d request error%s in the last 5 minutes'
                                   % (errors, '' if errors == 1 else 's')})
    if mem and mem['used_percent'] >= 90:
        alerts.append({'level': 'crit', 'text': 'memory at %d%%, Android may kill Termux' % mem['used_percent']})
    elif mem and mem['used_percent'] >= 80:
        alerts.append({'level': 'warn', 'text': 'memory at %d%%' % mem['used_percent']})
    if disk and disk['used_percent'] >= 90:
        alerts.append({'level': 'crit', 'text': 'storage %d%% full' % disk['used_percent']})
    if batt and batt.get('percent') is not None and batt['percent'] <= 15 and not batt.get('charging'):
        alerts.append({'level': 'warn', 'text': 'battery at %d%% and discharging' % batt['percent']})
    if sample_age is not None and sample_age > SAMPLE_SECONDS * 4:
        alerts.append({'level': 'warn', 'text': 'metrics are %ds old, the sampler may be stalled' % sample_age})
    if https_state == 'crit':
        alerts.append({'level': 'crit', 'text': 'public HTTPS check is failing'})
    health = 'crit' if any(a['level'] == 'crit' for a in alerts) else (
        'warn' if alerts else 'ok')
    return health, alerts


def live_payload():
    now = int(time.time())
    boottime = _boottime()
    mem = memory()
    disk = storage()
    batt = battery()
    tun = tunnel()
    services = services_payload()
    running = {service['key']: service['running'] for service in services}
    settings = read_settings()
    with DB_LOCK, _db() as db:
        last = db.execute('SELECT ts, cpu_pct, tunnel_ready FROM samples '
                          'ORDER BY ts DESC LIMIT 1').fetchone()
        audits = [dict(row) for row in db.execute(
            'SELECT ts, user, action, outcome FROM audits ORDER BY ts DESC LIMIT 8')]
    sample_age = (now - last['ts']) if last else None
    stale = sample_age is not None and sample_age > SAMPLE_SECONDS * 4

    components = {}
    phone_problems = []
    if mem and mem['used_percent'] >= 90:
        phone_problems.append('memory %d%%' % mem['used_percent'])
    if disk and disk['used_percent'] >= 90:
        phone_problems.append('storage %d%%' % disk['used_percent'])
    if batt and batt.get('percent') is not None and batt['percent'] <= 10 \
            and not batt.get('charging'):
        phone_problems.append('battery %d%%' % batt['percent'])
    components['phone'] = {
        'state': 'crit' if phone_problems else 'ok',
        'detail': ', '.join(phone_problems) if phone_problems
        else 'Up %s, %d core%s' % (_human(int(boottime)), HOST['cores'],
                                   '' if HOST['cores'] == 1 else 's'),
        'at': _iso(now),
    }
    components['nginx'] = {
        'state': 'ok' if running.get('nginx') else 'crit',
        'detail': 'Serving :8080' if running.get('nginx') else 'Not running',
        'at': _iso(now),
    }
    components['api'] = {
        'state': 'warn' if stale else 'ok',
        'detail': ('Sample %ds old' % sample_age) if stale else 'Responding on :8765',
        'at': _iso(last['ts']) if last else None,
    }
    if tun['disabled']:
        components['tunnel'] = {'state': 'warn', 'detail': 'Stopped by an operator', 'at': _iso(now)}
    elif tun['ready']:
        components['tunnel'] = {'state': 'ok',
                                'detail': '%d connection%s' % (tun['connections'],
                                                               '' if tun['connections'] == 1 else 's'),
                                'at': _iso(now)}
    else:
        components['tunnel'] = {'state': 'crit',
                                'detail': 'Connector down, public URL offline',
                                'at': _iso(now)}
    checks = {host: last_check(host) for host in HOSTNAMES}
    components['https'] = {'state': 'unknown',
                           'detail': 'Not monitored: no external probe configured',
                           'at': None}

    health, alerts = health_state(now, mem, disk, batt, tun, running, sample_age,
                                  components['https']['state'])
    TELEMETRY.emit_health(health, alerts)

    cpu = None
    if last is not None:
        cpu = last['cpu_pct']
    return {
        'generated_at': _iso(now),
        'server_time': now,
        'health': health,
        'alerts': alerts,
        'host': HOST,
        'uptime_s': int(boottime),
        'uptime': _human(int(boottime)),
        'memory': mem,
        'storage': disk,
        'battery': batt,
        'cpu': {
            'pct': cpu,
            'cores': HOST['cores'],
            'available': cpu is not None,
            'source': 'server processes (/proc/<pid>/stat)',
            'system_available': False,
        },
        'services': services,
        'tunnel': tun,
        'components': components,
        'sample': {'ts': last['ts'] if last else None,
                   'at': _iso(last['ts']) if last else None,
                   'age_s': sample_age, 'interval_s': SAMPLE_SECONDS,
                   'stale': bool(stale)},
        'probe': {'enabled': False, 'url': None,
                  'targets': [{'host': host, 'last': checks[host]} for host in HOSTNAMES]},
        'networking': {'hostnames': [
            {'host': host, 'role': 'console and metrics' if host == DETAILED_HOST else 'landing page',
             'last': checks[host]} for host in HOSTNAMES]},
        'server_disabled': os.path.exists(os.path.join(RUN_DIR, 'server-disabled')),
        'versions': VERSIONS,
        'audits': [{'at': _iso(row['ts']), 'user': row['user'], 'action': row['action'],
                    'outcome': row['outcome']} for row in audits],
        'ranges': list(RANGES),
    }


def timeline(rng):
    bucket = BUCKETS.get(rng, 300)
    window = RANGES.get(rng, 86400)
    now = int(time.time())
    since = now - window
    fields = ('ts', 'mem_pct', 'cpu_pct', 'batt', 'plugged', 'free_gb',
              'nginx', 'api', 'tunnel_proc', 'tunnel_ready', 'https_ok', 'https_ms')
    with DB_LOCK, _db() as db:
        rows = db.execute(
            'SELECT (ts / ?) * ? AS bucket, avg(mem_pct) AS mem, avg(cpu_pct) AS cpu, '
            'avg(batt) AS batt, max(plugged) AS plugged, avg(free_gb) AS free_gb, '
            'min(nginx) AS nginx, min(api) AS api, min(tunnel_proc) AS tunnel_proc, '
            'min(tunnel_ready) AS tunnel_ready, min(https_ok) AS https_ok, '
            'avg(https_ms) AS https_ms, count(*) AS samples, min(ts) AS first_ts '
            'FROM samples WHERE ts >= ? GROUP BY bucket ORDER BY bucket',
            (bucket, bucket, since)).fetchall()
        events = db.execute(
            'SELECT ts, kind, detail FROM events WHERE ts >= ? '
            "AND kind NOT IN ('https-up','https-down') ORDER BY ts", (since,)).fetchall()
        total = db.execute('SELECT count(*) AS n, min(ts) AS first FROM samples').fetchone()
    points = []
    for row in rows:
        points.append({
            't': row['bucket'], 'samples': row['samples'],
            'mem': round(row['mem'], 1) if row['mem'] is not None else None,
            'cpu': round(row['cpu'], 1) if row['cpu'] is not None else None,
            'batt': round(row['batt']) if row['batt'] is not None else None,
            'plugged': row['plugged'],
            'free_gb': row['free_gb'],
            'nginx': row['nginx'], 'api': row['api'],
            'tunnel': (min(row['tunnel_ready'], row['tunnel_proc'])
                       if row['tunnel_ready'] is not None and row['tunnel_proc'] is not None
                       else None),
            # Older https_ok samples were checked by the phone, not externally.
            'https': None, 'https_ms': None,
        })
    return {
        'range': rng, 'bucket_s': bucket, 'since': since, 'until': now,
        'points': points,
        'events': [{'t': row['ts'], 'kind': row['kind'], 'detail': row['detail']}
                   for row in events],
        'coverage': {'first_sample': total['first'], 'samples': total['n'],
                     'raw_retention_days': RAW_RETENTION_DAYS},
    }


REDACTIONS = (
    (re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[token]'),
    (re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{6,}'), 'bearer [redacted]'),
    (re.compile(r'(?i)\b(token|password|passwd|secret|apikey|api_key)'
                r'(\s*[:=]\s*)([^\s,;"\']{4,})'), r'\1\2[redacted]'),
)

# Long mixed-case strings are usually keys or session ids, but log lines are
# full of file paths, so only redact blobs that look like credentials.
LONG_BLOB = re.compile(r'[A-Za-z0-9_+\-/=]{32,}')


def _redact_blob(match):
    value = match.group(0)
    if '/' in value or '.' in value:
        return value
    if not (any(char.isdigit() for char in value)
            and any(char.isupper() for char in value)
            and any(char.islower() for char in value)):
        return value
    return '[redacted]'


def redact(text):
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    text = LONG_BLOB.sub(_redact_blob, text)
    return text.count('[redacted]') + text.count('[token]'), text


def classify(line):
    if re.search(r'\b(crit|emerg|alert|panic|fatal)\b', line, re.I):
        return 'crit'
    if re.search(r'\b(error|err|failed|failure|denied|refused)\b', line, re.I):
        return 'error'
    if re.search(r'\b(warn|warning|deprecated)\b', line, re.I):
        return 'warn'
    return 'info'


TIMESTAMP = re.compile(r'^(\d{4}[/-]\d\d[/-]\d\d[ T]\d\d:\d\d:\d\d)')


def tail(name, lines):
    filename = LOG_FILES.get(name)
    if not filename:
        return None
    path = os.path.join(LOG_DIR, filename)
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 96 * 1024))
            text = handle.read().decode('utf-8', 'replace')
    except OSError:
        return []
    return text.splitlines()[-lines:]


def logs_payload(name, severity, query, limit):
    raw = tail(name, 600)
    if raw is None:
        return None
    out = []
    total_redactions = 0
    for line in raw:
        hits, clean = redact(line)
        total_redactions += hits
        level = classify(clean)
        if severity and severity != 'all' and level != severity:
            continue
        if query and query.lower() not in clean.lower():
            continue
        match = TIMESTAMP.match(clean)
        out.append({'ts': match.group(1).replace('T', ' ') if match else None,
                    'sev': level, 'text': clean[:400]})
    sizes = {}
    for key, filename in LOG_FILES.items():
        try:
            sizes[key] = round(os.path.getsize(os.path.join(LOG_DIR, filename)) / 1024, 1)
        except OSError:
            sizes[key] = None
    return {'service': name, 'severity': severity or 'all', 'query': query or '',
            'matched': len(out), 'total': len(raw), 'lines': out[-limit:],
            'redactions': total_redactions, 'sizes_kb': sizes}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #

def read_passfile():
    try:
        with open(PASSFILE) as handle:
            user, _, digest = handle.read().strip().partition(':')
        return user, digest
    except OSError:
        return None, None


def verify_password(user, secret):
    expected_user, digest = read_passfile()
    if not expected_user or not digest:
        return False
    if not hmac.compare_digest(user.encode(), expected_user.encode()):
        return False
    if digest.startswith('{SHA}'):
        candidate = '{SHA}' + base64.b64encode(hashlib.sha1(secret.encode()).digest()).decode()
        return hmac.compare_digest(candidate, digest)
    return False


def drop_session(token):
    if not token:
        return
    with TERMINAL_LOCK:
        for terminal_id, terminal in list(TERMINALS.items()):
            if terminal['owner'] == token:
                close_terminal(terminal_id, token)
    with DB_LOCK, _db() as db:
        db.execute('DELETE FROM sessions WHERE token = ?',
                   (hashlib.sha256(token.encode()).hexdigest(),))


def new_session(user):
    token = secrets.token_urlsafe(24)
    now = time.time()
    # Sessions live in SQLite, not memory: restarting the status service is a
    # normal console action, and it must not sign the operator out with it.
    with DB_LOCK, _db() as db:
        db.execute('DELETE FROM sessions WHERE expires < ?', (int(now),))
        db.execute('INSERT INTO sessions(token, user, expires) VALUES (?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(), user,
                    int(now + SESSION_SECONDS)))
    return token


def session_user(handler):
    cookie = handler.headers.get('Cookie') or ''
    for part in cookie.split(';'):
        key, _, value = part.strip().partition('=')
        if key != 'galaxy_session' or not value:
            continue
        digest = hashlib.sha256(value.encode()).hexdigest()
        with DB_LOCK, _db() as db:
            row = db.execute('SELECT user, expires FROM sessions WHERE token = ?',
                             (digest,)).fetchone()
            if row and row['expires'] > time.time():
                return row['user'], value
            if row:
                db.execute('DELETE FROM sessions WHERE token = ?', (digest,))
        return None, None
    return None, None


def set_terminal_password(password):
    if len(password) < 16:
        raise ValueError('Terminal password must contain at least 16 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000)
    os.makedirs(os.path.dirname(TERMINAL_PASSFILE), exist_ok=True)
    temp = TERMINAL_PASSFILE + '.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(temp, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'salt': salt.hex(), 'hash': digest.hex()}, handle)
        os.replace(temp, TERMINAL_PASSFILE)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def verify_terminal_password(password):
    try:
        with open(TERMINAL_PASSFILE, encoding='utf-8') as handle:
            record = json.load(handle)
        salt, expected = bytes.fromhex(record['salt']), bytes.fromhex(record['hash'])
        if len(salt) != 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000)
        return hmac.compare_digest(actual, expected)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def start_terminal(owner):
    import pty
    if os.geteuid() == 0:
        raise PermissionError('Terminal cannot run as root')
    master, slave = pty.openpty()
    try:
        shell = os.environ.get('SHELL') or '/bin/sh'
        # Establish a controlling terminal after setsid so job control and Ctrl+C work.
        launch = ('import fcntl,termios,os,sys,signal; '
                  'signal.signal(signal.SIGINT,signal.SIG_DFL); '
                  'signal.signal(signal.SIGQUIT,signal.SIG_DFL); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],[sys.argv[1],"-i"])')
        process = subprocess.Popen([sys.executable, '-c', launch, shell],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   start_new_session=True, close_fds=True,
                                   env={**os.environ, 'TERM': 'xterm-256color'})
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    os.set_blocking(master, False)
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with TERMINAL_LOCK:
        TERMINALS[token] = {'owner': owner, 'fd': master, 'process': process,
                            'last_activity': now, 'user': None,
                            'buffer': bytearray(), 'detached_at': now,
                            'created': time.time(), 'stream': None}
    return token


def terminal_for(token, owner):
    with TERMINAL_LOCK:
        terminal = TERMINALS.get(token)
        if terminal is None or not hmac.compare_digest(terminal['owner'], owner):
            return None
        return terminal


def read_terminal(terminal):
    try:
        chunk = os.read(terminal['fd'], 16384)
    except (BlockingIOError, OSError):
        return b''
    if chunk:
        buffer = terminal.setdefault('buffer', bytearray())
        buffer.extend(chunk)
        overflow = len(buffer) - TERMINAL_BUFFER
        if overflow > 0:
            del buffer[:overflow]
    return chunk


def close_terminal(token, owner):
    with TERMINAL_LOCK:
        terminal = terminal_for(token, owner)
        if not terminal:
            return False
        del TERMINALS[token]
        try:
            os.killpg(terminal['process'].pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.close(terminal['fd'])
        terminal['process'].wait(timeout=3)
        if terminal['user']:
            add_audit(terminal['user'], 'terminal', 'closed', 'session ended')
        return True


def reap_terminals():
    while True:
        time.sleep(15)
        with TERMINAL_LOCK:
            now = time.monotonic()
            for token, terminal in list(TERMINALS.items()):
                detached = terminal.get('detached_at')
                if (terminal['process'].poll() is not None
                        or now - terminal['last_activity'] > TERMINAL_IDLE_SECONDS
                        or (detached is not None and now - detached > TERMINAL_DETACH_SECONDS)):
                    close_terminal(token, terminal['owner'])


def secure_request(handler):
    proto = (handler.headers.get('X-Forwarded-Proto') or '').lower()
    return proto == 'https'


def same_origin(handler):
    origin = handler.headers.get('Origin')
    if not origin:
        return True
    host = (handler.headers.get('Host') or '').strip()
    return origin.split('://')[-1] == host


# --------------------------------------------------------------------------- #
# http
# --------------------------------------------------------------------------- #

LOGIN_HTML = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>Sign in · Galaxy Console</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%23C3F451'/%3E%3Crect x='10' y='10' width='12' height='12' rx='4' fill='%23070A0E'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/console.css?v=8"></head>
<body class="login-body">
<main class="login">
  <div class="login-mark" aria-hidden="true"></div>
  <h1>Galaxy Console</h1>
  <p class="login-sub">Sign in to reach metrics, logs and service controls for the phone in the drawer.</p>
  <form method="post" action="/login" id="login-form">
    <label for="user">User</label>
    <input id="user" name="user" autocomplete="username" required autofocus>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <p class="login-error" id="login-error" role="alert" hidden></p>
    <button type="submit" class="btn primary">Sign in</button>
  </form>
  <p class="login-foot"><a href="/">Back to the public page</a></p>
</main>
<script>
document.getElementById('login-form').addEventListener('submit', async function (event) {
  event.preventDefault();
  var error = document.getElementById('login-error');
  error.hidden = true;
  var response = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({user: document.getElementById('user').value,
                          password: document.getElementById('password').value})
  });
  if (response.ok) { window.location.href = '/console'; return; }
  var data = await response.json().catch(function () { return {}; });
  error.textContent = data.error || 'Sign in failed';
  error.hidden = false;
});
</script>
</body></html>'''


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser aborting a poll is normal; do not fill status.log with it.
        error = __import__('sys').exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'galaxy-console'
    protocol_version = 'HTTP/1.1'

    def _headers(self, code, kind='application/json', extra=None, length=None):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if length is not None:
            self.send_header('Content-Length', str(length))
        self.end_headers()

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self._headers(code, 'application/json', length=len(body))
        self.wfile.write(body)

    def _html(self, code, body, extra=None):
        raw = body.encode()
        self._headers(code, 'text/html; charset=utf-8', extra, len(raw))
        self.wfile.write(raw)

    def _redirect(self, target):
        self._headers(302, 'text/plain', {'Location': target}, 0)

    def _private(self):
        user, _token = session_user(self)
        if not user:
            self._json(401, {'error': 'sign in required'})
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/login':
            if session_user(self)[0]:
                self._redirect('/console')
                return
            self._html(200, LOGIN_HTML)
            return
        if path == '/logout':
            _user, token = session_user(self)
            drop_session(token)
            self._redirect('/')
            return
        if path == '/console':
            if not session_user(self)[0]:
                self._redirect('/login')
                return
            try:
                with open(os.path.join(PRIV_DIR, 'console.html'), encoding='utf-8') as handle:
                    body = handle.read()
            except OSError:
                self._html(500, '<h1>Console files missing</h1>')
                return
            user = session_user(self)[0]
            self._html(200, body.replace('{{USER}}', user),
                       {'Content-Security-Policy': "default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self' 'unsafe-inline'; "
                                                   "script-src 'self' 'unsafe-inline'; "
                                                   "connect-src 'self'; frame-ancestors 'none'"})
            return
        if path == '/api/health':
            # Loopback liveness for serverctl and the supervisor. nginx marks
            # every proxied request, so this is never exposed publicly.
            trusted = (self.client_address[0] in ('127.0.0.1', '::1')
                       and not self.headers.get('X-Galaxy-Proxy'))
            if trusted:
                self._json(200, {'ok': True, 'uptime_s': int(time.time() - STATE['started'])})
            else:
                self._json(404, {'error': 'not found'})
            return
        if path.startswith('/api/'):
            user = self._private()
            if not user:
                return
            self._api_get(path, parse_qs(parsed.query))
            return
        self._json(404, {'error': 'not found'})

    def _stream_terminal(self, query):
        # Server-sent events: push PTY output as it arrives instead of making
        # the browser poll. Each round trip through the public tunnel costs
        # roughly 150 ms, so a fixed poll interval added that to every echo and
        # capped output at one read per tick.
        _user, owner = session_user(self)
        ids = query.get('id') or []
        token = ids[0] if ids else ''
        # Only the newest stream may read the pty. A closed browser connection
        # is otherwise only noticed on the next write, and the stale reader
        # would keep consuming output meant for the reconnected page.
        marker = object()
        with TERMINAL_LOCK:
            terminal = TERMINALS.get(token)
            if terminal is None or not hmac.compare_digest(terminal['owner'], owner):
                self._json(404, {'error': 'Terminal session is closed'})
                return
            terminal['stream'] = marker
            terminal['detached_at'] = None
            # A fresh page has an empty xterm; replay what it missed. Each
            # attach gets the whole buffer, so tabs never steal each other's
            # scrollback.
            backlog = bytes(terminal.get('buffer') or b'')
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        last_write = time.monotonic()
        try:
            self.wfile.write(b': open\n\n')
            if backlog:
                self.wfile.write(b'data: ' + base64.b64encode(backlog) + b'\n\n')
            self.wfile.flush()
            while True:
                with TERMINAL_LOCK:
                    terminal = TERMINALS.get(token)
                    if (terminal is None or terminal.get('stream') is not marker
                            or not hmac.compare_digest(terminal['owner'], owner)):
                        break
                    chunk = read_terminal(terminal)
                    if chunk:
                        terminal['last_activity'] = time.monotonic()
                    ended = terminal['process'].poll() is not None
                now = time.monotonic()
                if chunk:
                    self.wfile.write(b'data: ' + base64.b64encode(chunk) + b'\n\n')
                elif ended or now - last_write > 10:
                    if ended:
                        break
                    # nginx closes an idle proxied response after 30 s; a
                    # comment line keeps the stream and the tunnel open.
                    self.wfile.write(b': keepalive\n\n')
                else:
                    time.sleep(0.01)
                    continue
                self.wfile.flush()
                last_write = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with TERMINAL_LOCK:
                terminal = TERMINALS.get(token)
                if terminal is not None and terminal.get('stream') is marker:
                    terminal['stream'] = None
                    terminal['detached_at'] = time.monotonic()

    def _device_unavailable(self, message):
        self._json(503, {'error': message, 'paired': bool(read_device()['token'])})

    def _device_get(self, path, query):
        config = read_device()
        if path == '/api/device/summary':
            if not config['token']:
                self._json(200, {'paired': False,
                                 'error': 'The phone app is not paired yet'})
                return
            try:
                _status, health = device_call('/health', timeout=4)
            except (OSError, http.client.HTTPException):
                self._device_unavailable('The control app is not running on the phone')
                return
            payload = {'paired': True, 'health': health}
            for key, route in (('capabilities', '/capabilities'), ('battery', '/battery'),
                               ('storage', '/storage'), ('volume', '/volume'),
                               ('connectivity', '/connectivity')):
                try:
                    _status, data = device_call(route, timeout=8)
                except (OSError, http.client.HTTPException):
                    data = {'error': 'unavailable'}
                payload[key] = data
            self._json(200, payload)
            return
        if not config['token']:
            self._device_unavailable('The phone app is not paired yet')
            return
        if path in DEVICE_STREAMS:
            self._device_stream(DEVICE_STREAMS[path], query)
            return
        route = DEVICE_READS.get(path)
        if not route:
            self._json(404, {'error': 'not found'})
            return
        try:
            status, data = device_call(route, timeout=15)
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        self._json(status, data)

    def _device_stream(self, route, query):
        # MJPEG and PCM are pushed straight through: buffering would add the
        # whole tunnel round trip to every frame.
        config = read_device()
        target = device_path(route, query)
        try:
            connection = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=None)
            connection.request('GET', target, headers={'X-Galaxy-Token': config['token']})
            response = connection.getresponse()
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        if response.status != 200:
            raw = response.read()
            connection.close()
            try:
                self._json(response.status, json.loads(raw.decode('utf-8')))
            except (ValueError, UnicodeDecodeError):
                self._json(502, {'error': 'The device app refused the stream'})
            return
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', response.getheader('Content-Type')
                         or 'application/octet-stream')
        self.send_header('Cache-Control', 'no-store, must-revalidate')
        self.send_header('X-Accel-Buffering', 'no')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            while True:
                chunk = response.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _api_get(self, path, query):
        if path.startswith('/api/device/'):
            self._device_get(path, query)
            return
        if path == '/api/terminal/stream':
            self._stream_terminal(query)
            return
        if path == '/api/terminal/list':
            _user, owner = session_user(self)
            if not owner:
                self._json(401, {'error': 'sign in required'})
                return
            with TERMINAL_LOCK:
                terminals = sorted(
                    ({'id': token, 'created': terminal['created']}
                     for token, terminal in TERMINALS.items()
                     if hmac.compare_digest(terminal['owner'], owner)),
                    key=lambda item: item['created'])
            self._json(200, {'terminals': terminals})
            return
        if path == '/api/status':
            try:
                self._json(200, live_payload())
            except (OSError, KeyError, ValueError, TypeError):
                self._json(503, {'error': 'status unavailable'})
            return
        if path == '/api/timeline':
            rng = (query.get('range') or ['24h'])[0]
            if rng not in RANGES:
                self._json(400, {'error': 'range must be one of 1h, 24h, 7d'})
                return
            self._json(200, timeline(rng))
            return
        if path == '/api/logs':
            service = (query.get('service') or ['nginx-error'])[0]
            severity = (query.get('severity') or ['all'])[0]
            term = (query.get('q') or [''])[0][:120]
            try:
                limit = max(10, min(int((query.get('limit') or ['200'])[0]), 400))
            except ValueError:
                limit = 200
            payload = logs_payload(service, severity, term, limit)
            if payload is None:
                self._json(404, {'error': 'unknown log'})
                return
            self._json(200, payload)
            return
        if path == '/api/telemetry':
            self._json(200, TELEMETRY.status())
            return
        if path == '/api/settings':
            db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
            with DB_LOCK, _db() as db:
                counts = {row['name']: db.execute('SELECT count(*) FROM ' + row['name']).fetchone()[0]
                          for row in db.execute(
                              "SELECT name FROM sqlite_master WHERE type='table' AND "
                              "name IN ('samples','events','checks','audits')")}
            self._json(200, {'settings': read_settings(), 'hostnames': list(HOSTNAMES),
                             'db': {'path': DB_PATH, 'size_kb': round(db_size / 1024, 1),
                                    'rows': counts, 'raw_retention_days': RAW_RETENTION_DAYS},
                             'sample_seconds': SAMPLE_SECONDS,
                             'session_hours': SESSION_SECONDS // 3600,
                             'actions': [{'key': k, 'label': v[1], 'dangerous': v[2]}
                                         for k, v in ACTIONS.items()]})
            return
        if path == '/api/events':
            try:
                limit = max(1, min(int((query.get('limit') or ['60'])[0]), 200))
            except ValueError:
                limit = 60
            with DB_LOCK, _db() as db:
                rows = db.execute('SELECT ts, kind, detail FROM events '
                                  'ORDER BY ts DESC LIMIT ?', (limit,)).fetchall()
                audits = db.execute('SELECT ts, user, action, outcome, detail FROM audits '
                                    'ORDER BY ts DESC LIMIT ?', (limit,)).fetchall()
            self._json(200, {
                'events': [{'at': _iso(r['ts']), 'kind': r['kind'], 'detail': r['detail']}
                           for r in rows],
                'audits': [{'at': _iso(r['ts']), 'user': r['user'], 'action': r['action'],
                            'outcome': r['outcome'], 'detail': r['detail']} for r in audits]})
            return
        self._json(404, {'error': 'not found'})

    def _device_pair_local(self):
        token = ''
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if 0 < length <= 2048:
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                if isinstance(data, dict):
                    token = str(data.get('token') or '').strip()
        except (ValueError, TypeError, UnicodeDecodeError):
            pass
        token = token or secrets.token_hex(24)
        if not 16 <= len(token) <= 128:
            self._json(400, {'error': 'Pairing token must be 16 to 128 characters'})
            return
        config = read_device()
        try:
            _status, result = device_call('/pair', {'token': token}, timeout=8)
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        if result.get('paired') is not True:
            add_audit('local', 'device-pair', 'failed', str(result.get('error') or 'refused'))
            self._json(400, {'error': result.get('error')
                             or 'Open the pairing window in the Galaxy Device app first'})
            return
        write_device({'token': token, 'port': config['port']})
        add_audit('local', 'device-pair', 'ok', 'paired from the phone itself')
        self._json(200, {'ok': True, 'paired': True})

    def _device_pair(self, user):
        data = self._body(2048)
        if data is None:
            return
        token = str(data.get('token') or '').strip() or secrets.token_hex(24)
        if not 16 <= len(token) <= 128:
            self._json(400, {'error': 'Pairing token must be 16 to 128 characters'})
            return
        config = read_device()
        try:
            _status, result = device_call('/pair', {'token': token}, timeout=8)
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        if result.get('paired') is not True:
            add_audit(user, 'device-pair', 'failed', str(result.get('error') or 'refused'))
            self._json(400, {'error': result.get('error')
                             or 'Open the pairing window in the Galaxy Device app first'})
            return
        write_device({'token': token, 'port': config['port']})
        add_audit(user, 'device-pair', 'ok', 'control app paired')
        self._json(200, {'ok': True, 'paired': True})

    def _device_control(self, user):
        data = self._body(8192)
        if data is None:
            return
        action = data.get('action')
        spec = DEVICE_CONTROLS.get(action) if isinstance(action, str) else None
        if not spec:
            self._json(400, {'error': 'unknown device action'})
            return
        payload = data.get('payload')
        if not isinstance(payload, dict):
            payload = {}
        method, route = spec
        try:
            if method == 'GET':
                query = {key: [str(value)] for key, value in payload.items()}
                status, result = device_call(device_path(route, query), timeout=30)
            else:
                status, result = device_call(route, payload, timeout=30)
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        detail = str(result.get('error') or result.get('file') or result.get('size') or '')
        add_audit(user, 'device-' + action, 'ok' if status == 200 else 'failed', detail[:160])
        self._json(status, result)

    def _device_upload(self, user):
        if not same_origin(self):
            self._json(403, {'error': 'cross-origin request refused'})
            return
        name = (self.headers.get('X-Filename') or '').strip()
        if not name or len(name) > 120 or '/' in name or '\\' in name or name.startswith('.'):
            self._json(400, {'error': 'X-Filename header is required'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 8 * 1024 * 1024:
            self._json(413, {'error': 'File must be smaller than 8 MB'})
            return
        body = self.rfile.read(length)
        config = read_device()
        if not config['token']:
            self._device_unavailable('The phone app is not paired yet')
            return
        connection = http.client.HTTPConnection('127.0.0.1', config['port'], timeout=30)
        try:
            connection.request('POST', '/media/upload', body=body,
                               headers={'X-Galaxy-Token': config['token'],
                                        'X-Filename': name,
                                        'Content-Type': 'application/octet-stream'})
            response = connection.getresponse()
            raw = response.read()
            status = response.status
        except (OSError, http.client.HTTPException):
            self._device_unavailable('The control app is not running on the phone')
            return
        finally:
            connection.close()
        try:
            result = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            result = {'error': 'unreadable response from the device app'}
        add_audit(user, 'device-upload', 'ok' if status == 200 else 'failed', name)
        self._json(status, result)

    def _body(self, limit=8192):
        if not same_origin(self):
            self._json(403, {'error': 'cross-origin request refused'})
            return None
        if not (self.headers.get('Content-Type') or '').startswith('application/json'):
            self._json(415, {'error': 'expected application/json'})
            return None
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > limit:
                raise ValueError
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, TypeError):
            self._json(400, {'error': 'bad request'})
            return None

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/deploy/github':
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= 2 * 1024 * 1024:
                self._json(413, {'error': 'Invalid webhook size'})
                return
            body = self.rfile.read(length)
            try:
                with open(os.path.join(RUN_DIR, 'deploy-secret'), 'rb') as stream:
                    secret = stream.read().strip()
            except OSError:
                secret = b''
            expected = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
            if not secret or not hmac.compare_digest(expected, self.headers.get('X-Hub-Signature-256', '')):
                self._json(403, {'error': 'Invalid webhook signature'})
                return
            try:
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
            except (ValueError, UnicodeDecodeError):
                self._json(400, {'error': 'Invalid webhook payload'})
                return
            if (self.headers.get('X-GitHub-Event') in ('actions-deploy', 'actions-deploy-status')
                    and data.get('ref') == 'refs/heads/master'
                    and re.fullmatch(r'[0-9a-f]{40}', str(data.get('sha', '')))
                    and isinstance(data.get('repository'), dict)
                    and data['repository'].get('full_name') == 'PriyansuMaurya/samsung-a20e-project'
                    and not data.get('deleted')
                    and os.path.exists(os.path.join(RUN_DIR, 'autodeploy-enabled'))):
                if self.headers.get('X-GitHub-Event') == 'actions-deploy-status':
                    try:
                        with open(os.path.join(RUN_DIR, 'deployed-revision')) as marker:
                            revision = marker.read().strip()
                    except OSError:
                        revision = None
                    self._json(200, {'revision': revision})
                    return
                with open(os.path.join(LOG_DIR, 'deploy.log'), 'ab') as log:
                    subprocess.Popen([sys.executable, os.path.join(BASE, 'autodeploy.py'), data['sha']],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True)
                self._json(202, {'status': 'deployment queued'})
            else:
                self._json(200, {'status': 'ignored'})
            return
        if path == '/api/device/pair-local':
            # Loopback only and never through nginx: this is the bridge the
            # phone itself uses, so pairing is possible before any session
            # exists and survives a browser that has never signed in.
            if (self.client_address[0] not in ('127.0.0.1', '::1')
                    or self.headers.get('X-Galaxy-Proxy')):
                self._json(404, {'error': 'not found'})
                return
            self._device_pair_local()
            return
        if path.startswith('/api/device/'):
            user = self._private()
            if not user:
                return
            if path == '/api/device/pair':
                self._device_pair(user)
                return
            if path == '/api/device/unpair':
                write_device({'token': '', 'port': read_device()['port']})
                add_audit(user, 'device-pair', 'forgotten', 'dashboard unpaired')
                self._json(200, {'ok': True, 'paired': False})
                return
            if path == '/api/device/control':
                self._device_control(user)
                return
            if path == '/api/device/media/upload':
                self._device_upload(user)
                return
            self._json(404, {'error': 'not found'})
            return
        client = (self.headers.get('CF-Connecting-IP') or self.headers.get('X-Real-IP')
                  or self.client_address[0])
        if path == '/api/login':
            data = self._body(2048)
            if data is None:
                return
            window = time.time() - 300
            attempts = [value for value in LOGIN_FAILS.get(client, []) if value > window]
            if len(attempts) >= 6:
                add_audit(data.get('user') or '?', 'login', 'blocked',
                          'too many attempts from %s' % client)
                self._json(429, {'error': 'Too many attempts. Wait a few minutes.'})
                return
            user = str(data.get('user') or '')[:64]
            secret = str(data.get('password') or '')
            if verify_password(user, secret):
                LOGIN_FAILS.pop(client, None)
                token = new_session(user)
                add_audit(user, 'login', 'ok', 'session opened')
                cookie = 'galaxy_session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d' % (
                    token, SESSION_SECONDS)
                if secure_request(self):
                    cookie += '; Secure'
                self._headers(200, 'application/json', {'Set-Cookie': cookie}, 2)
                self.wfile.write(b'{}')
                return
            attempts.append(time.time())
            LOGIN_FAILS[client] = attempts
            add_audit(user or '?', 'login', 'failed', 'wrong credentials')
            self._json(401, {'error': 'Wrong user or password'})
            return
        if path == '/api/logout':
            user, token = session_user(self)
            drop_session(token)
            if user:
                add_audit(user, 'logout', 'ok')
            self._headers(200, 'application/json',
                          {'Set-Cookie': 'galaxy_session=deleted; Path=/; Max-Age=0; HttpOnly'}, 2)
            self.wfile.write(b'{}')
            return
        if path.startswith('/api/terminal/'):
            user, owner = session_user(self)
            if not user:
                self._json(401, {'error': 'sign in required'})
                return
            origin = self.headers.get('Origin') or ''
            expected = ('https' if secure_request(self) else 'http') + '://' + (self.headers.get('Host') or '')
            if origin != expected:
                self._json(403, {'error': 'Invalid request origin'})
                return
            data = self._body(32768)
            if not isinstance(data, dict):
                if data is not None:
                    self._json(400, {'error': 'Expected an object'})
                return
            if path == '/api/terminal/open':
                with TERMINAL_LOCK:
                    now = time.monotonic()
                    TERMINAL_FAILS[:] = [t for t in TERMINAL_FAILS if now - t < 300]
                    if len(TERMINAL_FAILS) >= 5:
                        add_audit(user, 'terminal', 'blocked', 'too many attempts')
                        self._json(429, {'error': 'Too many attempts. Wait five minutes.'})
                        return
                    if not os.path.isfile(TERMINAL_PASSFILE):
                        self._json(503, {'error': 'Set the terminal password on the phone first'})
                        return
                    password = data.get('password')
                    if not isinstance(password, str) or not verify_terminal_password(password):
                        TERMINAL_FAILS.append(now)
                        add_audit(user, 'terminal', 'failed', 'incorrect terminal password')
                        self._json(403, {'error': 'Incorrect terminal password'})
                        return
                    mine = [t for t in TERMINALS.values()
                            if hmac.compare_digest(t['owner'], owner)]
                    if len(mine) >= TERMINAL_MAX:
                        self._json(409, {'error': 'Too many terminal sessions open (%d max)' % TERMINAL_MAX})
                        return
                    try:
                        terminal_id = start_terminal(owner)
                    except (OSError, PermissionError) as error:
                        add_audit(user, 'terminal', 'failed', 'could not start shell')
                        self._json(503, {'error': 'Could not start a non-root terminal'})
                        return
                    TERMINALS[terminal_id]['user'] = user
                    TERMINAL_FAILS.clear()
                    add_audit(user, 'terminal', 'opened', 'session started')
                self._json(200, {'id': terminal_id})
                return
            terminal_id = data.get('id')
            if not isinstance(terminal_id, str):
                self._json(400, {'error': 'Missing terminal session'})
                return
            with TERMINAL_LOCK:
                terminal = terminal_for(terminal_id, owner)
                if not terminal:
                    self._json(404, {'error': 'Terminal session is closed'})
                    return
                if path == '/api/terminal/close':
                    close_terminal(terminal_id, owner)
                    self._json(200, {'ok': True})
                    return
                if path == '/api/terminal/resize':
                    cols, rows = data.get('cols'), data.get('rows')
                    if type(cols) is not int or type(rows) is not int or not (2 <= cols <= 500 and 2 <= rows <= 300):
                        self._json(400, {'error': 'Invalid terminal size'})
                        return
                    import fcntl, struct, termios
                    try:
                        fcntl.ioctl(terminal['fd'], termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                    except OSError:
                        self._json(503, {'error': 'Terminal could not be resized'})
                        return
                    self._json(200, {'ok': True})
                    return
                if path == '/api/terminal/input':
                    text = data.get('input')
                    if not isinstance(text, str) or len(text.encode('utf-8')) > 8192:
                        self._json(400, {'error': 'Invalid terminal input'})
                        return
                    try:
                        os.write(terminal['fd'], text.encode('utf-8'))
                    except (OSError, BlockingIOError):
                        self._json(503, {'error': 'Terminal input could not be sent'})
                        return
                    terminal['last_activity'] = time.monotonic()
                    self._json(200, {'ok': True})
                    return
                if path == '/api/terminal/read':
                    output = base64.b64encode(read_terminal(terminal)).decode('ascii')
                    ended = terminal['process'].poll() is not None
                    if ended and not output:
                        close_terminal(terminal_id, owner)
                    self._json(200, {'output': output, 'ended': ended and not output})
                    return
            self._json(404, {'error': 'not found'})
            return
        if path == '/api/action':
            user = self._private()
            if not user:
                return
            data = self._body(2048)
            if data is None:
                return
            action = data.get('action')
            if not isinstance(action, str) or action not in ACTIONS:
                add_audit(user, str(action)[:40], 'refused', 'unknown action')
                self._json(400, {'error': 'unknown action'})
                return
            subcommand, label, _dangerous = ACTIONS[action]
            if action in ('server-stop', 'server-restart', 'tunnel-stop', 'tunnel-restart'):
                # These actions cut the connection carrying this reply, so the
                # answer is sent first and the work happens a second later.
                subprocess.Popen(
                    ['sh', '-c', 'sleep 1; exec "$0" "$1" >/dev/null 2>&1', SERVERCTL, subcommand],
                    start_new_session=True)
                add_audit(user, action, 'scheduled', 'runs in 1s')
                self._json(200, {'ok': True, 'action': action, 'label': label,
                                 'output': 'Scheduled. The console reconnects on its own.'})
                return
            output = _run(SERVERCTL, subcommand, timeout=30).strip()
            SERVICES_CACHE['value'] = None
            add_audit(user, action, 'ok' if 'error' not in output.lower() else 'failed',
                      output[-240:] or 'no output')
            self._json(200, {'ok': True, 'action': action, 'label': label,
                             'output': output[-1500:]})
            return
        if path == '/api/settings':
            user = self._private()
            if not user:
                return
            data = self._body(2048)
            if data is None:
                return
            settings = read_settings()
            if data.get('probe_enabled'):
                self._json(400, {'error': 'External monitoring is not configured'})
                return
            settings['host_probes'] = bool(data.get('host_probes', True))
            write_settings(settings)
            add_audit(user, 'settings', 'ok',
                      'phone hostname checks %s' % ('on' if settings['host_probes'] else 'off'))
            self._json(200, {'settings': settings})
            return
        self._json(404, {'error': 'not found'})

    def log_message(self, format, *args):
        pass


def main():
    import sys
    if len(sys.argv) == 2 and sys.argv[1] == '--set-terminal-password':
        import getpass
        password = getpass.getpass('New terminal password (at least 16 characters): ')
        if password != getpass.getpass('Confirm terminal password: '):
            raise SystemExit('Passwords do not match')
        try:
            set_terminal_password(password)
        except ValueError as error:
            raise SystemExit(str(error))
        print('Terminal password set')
        return
    if len(sys.argv) >= 2 and sys.argv[1] == '--telemetry-backfill':
        try:
            limit = max(1, int(sys.argv[2])) if len(sys.argv) > 2 else 5000
        except ValueError:
            limit = 5000
        print(json.dumps(TELEMETRY.backfill(LOG_FILES, LOG_DIR, DB_PATH, limit)))
        return
    init_db()
    load_monitor()
    TELEMETRY.start(LOG_FILES, LOG_DIR, DB_PATH)
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=reap_terminals, daemon=True).start()
    Server(('127.0.0.1', 8765), Handler).serve_forever()


if __name__ == '__main__':
    main()
