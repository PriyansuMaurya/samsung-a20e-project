import base64
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))

import status
import telemetry

TOKEN = 'xaat-test-token-1234'


class FakeAxiom:
    """A local stand-in for the ingest and management endpoints."""

    def __init__(self):
        self.requests = []
        self.script = []
        self.default = (200, {'ingested': None, 'failed': 0}, {})
        self.lock = threading.Lock()
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return 'http://127.0.0.1:%d' % self.server.server_port

    def queue(self, status, payload=None, headers=None):
        self.script.append((status, payload or {}, headers or {}))

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, code, payload, headers):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                with outer.lock:
                    outer.requests.append({
                        'method': method, 'path': self.path, 'body': body,
                        'auth': self.headers.get('Authorization'),
                        'content_type': self.headers.get('Content-Type'),
                    })
                    if outer.script:
                        code, payload, headers = outer.script.pop(0)
                    else:
                        code, payload, headers = outer.default
                    if payload.get('ingested') is None:
                        payload = dict(payload)
                        payload['ingested'] = len([line for line in body.splitlines() if line])
                self._reply(code, payload, headers)

            def do_POST(self):
                self._handle('POST')

            def do_GET(self):
                self._handle('GET')

            def do_PUT(self):
                self._handle('PUT')

            def log_message(self, *args):
                pass

        return Handler

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_client(directory, endpoint, **overrides):
    config = dict(telemetry.DEFAULTS)
    config.update({
        'enabled': True,
        'endpoint': endpoint,
        'dataset': 'galaxy-server',
        'token': TOKEN,
        'batch_size': 100,
        'flush_seconds': 2,
        'timeout_seconds': 2,
        'max_retries': 0,
        'retry_backoff_seconds': 0,
        'max_batch_bytes': 4096,
        'spool_max_kb': 64,
        'max_queue': 50,
    })
    config.update(overrides)
    run_dir = os.path.join(directory, 'run')
    path = os.path.join(run_dir, telemetry.CONFIG_NAME)
    telemetry.save_config(path, config)
    return telemetry.Telemetry(run_dir)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.axiom = FakeAxiom()
        self.addCleanup(self.axiom.close)
        self.client = make_client(self.temp.name, self.axiom.endpoint)

    def bodies(self):
        return [json.loads(line) for request in self.axiom.requests
                for line in request['body'].splitlines() if line]

    def test_batches_events_as_ndjson_with_timestamp_and_identity(self):
        self.client = make_client(self.temp.name, self.axiom.endpoint, batch_size=2)
        for index in range(3):
            self.client.emit({'event': 'galaxy.test', 'index': index,
                              '_time': telemetry.utc_iso()})
        self.client.flush()
        self.client.flush()

        self.assertEqual(len(self.axiom.requests), 2)
        first, second = self.axiom.requests
        self.assertEqual(first['path'],
                         '/v1/ingest/galaxy-server?timestamp-field=_time')
        self.assertEqual(first['auth'], 'Bearer ' + TOKEN)
        self.assertEqual(first['content_type'], 'application/x-ndjson')
        self.assertEqual(len(first['body'].splitlines()), 2)
        self.assertEqual(len(second['body'].splitlines()), 1)
        events = self.bodies()
        self.assertEqual([event['index'] for event in events], [0, 1, 2])
        for event in events:
            self.assertTrue(event['_time'].endswith('Z'))
            self.assertEqual(event['source'], 'galaxy-server')
            self.assertEqual(event['device'], 'galaxy-a20e')
            self.assertEqual(event['entry_point'], 'sampler')

    def test_retries_transient_failure_then_succeeds(self):
        self.client = make_client(self.temp.name, self.axiom.endpoint, max_retries=2)
        self.axiom.queue(503, {'error': 'unavailable'})
        self.axiom.queue(503, {'error': 'unavailable'})
        self.client.emit({'event': 'galaxy.test'})
        result = self.client.flush()
        self.assertTrue(result['ok'])
        self.assertEqual(len(self.axiom.requests), 3)
        self.assertEqual(self.client.status()['exported'], 1)

    def test_spools_transient_batch_and_drains_it_next_time(self):
        self.axiom.default = (500, {'error': 'down'}, {})
        self.client = make_client(self.temp.name, self.axiom.endpoint, max_retries=1)
        self.client.emit({'event': 'galaxy.test', 'message': 'keep me'})
        self.assertFalse(self.client.flush()['ok'])
        first_body = self.axiom.requests[-1]['body']
        self.assertTrue(os.path.exists(self.client.spool_path))
        self.assertEqual(self.client.status()['queued'], 0)

        self.axiom.default = (200, {'ingested': None, 'failed': 0}, {})
        result = self.client.flush()
        self.assertTrue(result['ok'])
        self.assertIn(b'keep me', self.axiom.requests[-1]['body'])
        self.assertEqual(self.axiom.requests[-1]['body'], first_body)
        self.assertFalse(os.path.exists(self.client.spool_path))

    def test_permanent_failure_is_not_retried_or_spooled(self):
        self.axiom.queue(401, {'error': 'bad token'})
        self.client.emit({'event': 'galaxy.test'})
        result = self.client.flush()
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.axiom.requests), 1)
        self.assertFalse(os.path.exists(self.client.spool_path))
        state = self.client.status()
        self.assertEqual(state['rejected'], 1)
        self.assertEqual(state['queued'], 0)
        self.assertIn('401', state['last_error'])

    def test_credentials_are_redacted_before_export(self):
        self.client.emit({'event': 'galaxy.test', 'message':
                          'token=%s password=hunter2 Authorization: Bearer abcdef123456 '
                          'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMzI'
                          % TOKEN,
                          'nested': {'api_key': 'secret-value-here'}})
        self.client.flush()
        text = self.axiom.requests[0]['body'].decode()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn('hunter2', text)
        self.assertNotIn('secret-value-here', text)
        self.assertNotIn('abcdef123456', text)
        self.assertIn('[redacted]', text)

    def test_status_never_exposes_the_token(self):
        state = self.client.status()
        self.assertNotIn(TOKEN, json.dumps(state))
        self.assertEqual(state['dataset'], 'galaxy-server')
        self.assertEqual(state['endpoint'], '127.0.0.1')
        self.assertIn(state['state'], ('idle', 'ok'))

    def test_queue_is_bounded_and_drops_oldest(self):
        for index in range(60):
            self.client.emit({'event': 'galaxy.test', 'index': index})
        state = self.client.status()
        self.assertEqual(state['queued'], 50)
        self.assertEqual(state['dropped'], 10)

    def test_one_batch_stays_under_the_byte_limit(self):
        for index in range(10):
            self.client.emit({'event': 'galaxy.test', 'index': index,
                              'message': 'x' * 1000})
        while self.client.status()['queued']:
            self.client.flush()
        self.assertGreaterEqual(len(self.axiom.requests), 2)
        for request in self.axiom.requests:
            self.assertLess(len(request['body']), 5000)
        sent = sum(len(request['body'].splitlines()) for request in self.axiom.requests)
        self.assertEqual(sent, 10)

    def test_spool_is_bounded(self):
        self.axiom.default = (500, {'error': 'down'}, {})
        self.client = make_client(self.temp.name, self.axiom.endpoint,
                                  spool_max_kb=16, batch_size=5, max_batch_bytes=5 * 1024)
        for _ in range(12):
            for _ in range(5):
                self.client.emit({'event': 'galaxy.test', 'message': 'x' * 1000})
            self.client.flush()
        size = os.path.getsize(self.client.spool_path)
        self.assertLessEqual(size, 16 * 1024 + 1200)
        self.assertGreater(self.client.status()['dropped'], 0)

    def test_log_forwarding_is_incremental_and_survives_truncation(self):
        log_dir = os.path.join(self.temp.name, 'log')
        os.makedirs(log_dir)
        path = os.path.join(log_dir, 'status.log')
        Path(path).write_text('2026-11-09 10:00:00 first\n2026-11-09 10:00:01 second\n')
        files = {'status': 'status.log'}
        db_path = os.path.join(self.temp.name, 'run', 'galaxy.db')

        self.assertEqual(self.client.drain(files, log_dir, db_path)['logs'], 2)
        self.assertEqual(self.client.drain(files, log_dir, db_path)['logs'], 0)
        with open(path, 'a') as handle:
            handle.write('2026-11-09 10:00:02 third\n')
        self.assertEqual(self.client.drain(files, log_dir, db_path)['logs'], 1)
        self.client.flush()
        messages = [event['message'] for event in self.bodies() if event['event'] == 'galaxy.log']
        self.assertEqual(messages, ['2026-11-09 10:00:00 first',
                                    '2026-11-09 10:00:01 second',
                                    '2026-11-09 10:00:02 third'])
        self.assertTrue(self.bodies()[0]['_time'].endswith('Z'))

        # Rewriting the file shorter means the stored offset is meaningless.
        Path(path).write_text('2026-11-09 10:05:00 after truncate\n')
        self.assertEqual(self.client.drain(files, log_dir, db_path)['logs'], 1)

    def test_events_are_forwarded_once_by_row_id(self):
        db_path = os.path.join(self.temp.name, 'run', 'events.db')
        connection = sqlite3.connect(db_path)
        connection.execute('CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                           'ts INTEGER, kind TEXT, detail TEXT, service TEXT)')
        connection.execute("INSERT INTO events(ts, kind, detail, service) VALUES (100, 'restart', 'nginx restarted', 'nginx')")
        connection.execute("INSERT INTO events(ts, kind, detail) VALUES (101, 'reboot', 'device rebooted')")
        connection.commit()
        connection.close()

        self.assertEqual(self.client.drain({}, self.temp.name, db_path)['events'], 2)
        self.assertEqual(self.client.drain({}, self.temp.name, db_path)['events'], 0)
        connection = sqlite3.connect(db_path)
        connection.execute("INSERT INTO events(ts, kind, detail) VALUES (102, 'down', 'tunnel down')")
        connection.commit()
        connection.close()
        self.assertEqual(self.client.drain({}, self.temp.name, db_path)['events'], 1)
        self.client.flush()
        kinds = [event['kind'] for event in self.bodies() if event['event'] == 'galaxy.event']
        self.assertEqual(kinds, ['restart', 'reboot', 'down'])

    def test_backfill_resumes_without_duplicates(self):
        log_dir = os.path.join(self.temp.name, 'log')
        os.makedirs(log_dir)
        Path(log_dir, 'status.log').write_text(
            '2026-11-09 10:00:00 one\n2026-11-09 10:00:01 two\n2026-11-09 10:00:02 three\n')
        files = {'status': 'status.log'}
        db_path = os.path.join(self.temp.name, 'run', 'galaxy.db')

        first = self.client.backfill(files, log_dir, db_path, limit=2)
        self.assertEqual(first['forwarded'], 2)
        second = self.client.backfill(files, log_dir, db_path, limit=10)
        self.assertEqual(second['forwarded'], 1)
        third = self.client.backfill(files, log_dir, db_path, limit=10)
        self.assertEqual(third['forwarded'], 0)
        messages = [event['message'] for event in self.bodies() if event['event'] == 'galaxy.log']
        self.assertEqual([message.split()[-1] for message in messages], ['one', 'two', 'three'])

    def test_retention_refuses_to_lower_without_force(self):
        original = telemetry.MANAGE_ENDPOINT
        telemetry.MANAGE_ENDPOINT = self.axiom.endpoint
        self.addCleanup(setattr, telemetry, 'MANAGE_ENDPOINT', original)
        self.axiom.queue(200, {'retentionDays': 30})
        with self.assertRaises(telemetry.ConfigError):
            self.client.apply_retention(7)
        self.axiom.queue(200, {'retentionDays': 30})
        self.axiom.queue(200, {'retentionDays': 7})
        result = self.client.apply_retention(7, force_lower=True)
        self.assertEqual(result['retention_days'], 7)
        put = [request for request in self.axiom.requests if request['method'] == 'PUT'][-1]
        self.assertEqual(json.loads(put['body']), {'description': telemetry.DESCRIPTION,
                                                   'retentionDays': 7})

    def test_management_requests_use_the_management_token(self):
        original = telemetry.MANAGE_ENDPOINT
        telemetry.MANAGE_ENDPOINT = self.axiom.endpoint
        self.addCleanup(setattr, telemetry, 'MANAGE_ENDPOINT', original)
        self.client = make_client(self.temp.name, self.axiom.endpoint,
                                  manage_token='xaat-management-token')
        self.axiom.queue(200, {'retentionDays': 30})
        self.assertEqual(self.client.dataset_retention(), 30)
        self.assertEqual(self.axiom.requests[-1]['auth'],
                         'Bearer xaat-management-token')

    def _history_db(self, rows, sessions=None):
        path = os.path.join(self.temp.name, 'run', 'history.db')
        connection = sqlite3.connect(path)
        connection.execute(
            'CREATE TABLE samples (ts INTEGER PRIMARY KEY, uptime_s INTEGER, '
            'mem_used_mb INTEGER, mem_total_mb INTEGER, mem_pct INTEGER, swap_pct INTEGER, '
            'free_gb REAL, storage_pct INTEGER, batt INTEGER, plugged INTEGER, temp_c REAL, '
            'cpu_pct REAL, nginx INTEGER, api INTEGER, supervisor INTEGER, tunnel_proc INTEGER, '
            'tunnel_ready INTEGER, tunnel_conns INTEGER, tunnel_req INTEGER, https_ok INTEGER, '
            'https_ms INTEGER)')
        connection.execute(
            'CREATE TABLE checks (ts INTEGER NOT NULL, host TEXT NOT NULL, ok INTEGER, '
            'status INTEGER, ms INTEGER, PRIMARY KEY (ts, host))')
        connection.execute(
            'CREATE TABLE audits (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, '
            'user TEXT, action TEXT, outcome TEXT, detail TEXT)')
        connection.execute('CREATE TABLE sessions (token TEXT PRIMARY KEY, user TEXT NOT NULL, '
                           'expires INTEGER NOT NULL)')
        for row in rows:
            connection.execute(row[0], row[1])
        if sessions:
            connection.execute('INSERT INTO sessions VALUES (?,?,?)', sessions)
        connection.commit()
        connection.close()
        return path

    def test_history_forwards_samples_checks_and_audits_once(self):
        now = int(time.time())
        db_path = self._history_db([
            ('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (now - 100, 1000, 300, 2800, 60, 0, 20.5, 80, 55, 1, 28.0, 3.5,
              1, 1, 1, 1, 1, 4, 900, 1, 120)),
            ('INSERT INTO checks VALUES (?,?,?,?,?)',
             (now - 90, 'galaxy.maurya.ie', 1, 200, 140)),
            ('INSERT INTO audits(ts, user, action, outcome, detail) VALUES (?,?,?,?,?)',
             (now - 80, 'console', 'server-restart', 'scheduled', 'runs in 1s')),
        ])
        counts = self.client.drain({}, self.temp.name, db_path)
        self.assertEqual(counts['history'], 3)
        self.client.flush()
        events = self.bodies()
        self.assertEqual([event['event'] for event in events],
                         ['galaxy.audit', 'galaxy.sample', 'galaxy.check'])
        self.assertEqual(events[0]['action'], 'server-restart')
        self.assertEqual(events[1]['services'], {'nginx': True, 'status': True,
                                                 'supervisor': True, 'cloudflared': True})
        self.assertEqual(events[1]['batt_pct'], 55)
        self.assertEqual(events[2]['host'], 'galaxy.maurya.ie')
        self.assertEqual(self.client.drain({}, self.temp.name, db_path)['history'], 0)

    def test_history_skips_rows_newer_than_the_forwarding_floor(self):
        now = int(time.time())
        db_path = self._history_db([
            ('INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (now + 600, 1, 1, 1, 1, 0, 1.0, 1, 1, 0, 20.0, 0.0, 1, 1, 1, 1, 1, 1, 1, 1, 1)),
        ])
        self.assertEqual(self.client.drain({}, self.temp.name, db_path)['history'], 0)

    def test_history_never_reads_session_tokens(self):
        db_path = self._history_db([], sessions=('session-token-abc123', 'console', 9999999999))
        self.client.drain({}, self.temp.name, db_path)
        self.client.flush()
        self.assertNotIn('session-token-abc123',
                         json.dumps(self.axiom.requests, default=str))

    def test_release_logs_are_forwarded(self):
        self.assertIn('deploy', status.LOG_FILES)
        self.assertIn('deploy-manual', status.LOG_FILES)
        log_dir = os.path.join(self.temp.name, 'log')
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, 'deploy.log'), 'a', encoding='utf-8') as handle:
            handle.write('2026-11-11T16:54:17 deployed fbffad6\n')
        counts = self.client.drain({'deploy': 'deploy.log'}, log_dir,
                                   os.path.join(self.temp.name, 'run', 'none.db'))
        self.assertEqual(counts['logs'], 1)
        self.client.flush()
        messages = [event['message'] for event in self.bodies() if event['event'] == 'galaxy.log']
        self.assertTrue(any('deployed fbffad6' in message for message in messages))

    def _managed(self):
        original = telemetry.MANAGE_ENDPOINT
        telemetry.MANAGE_ENDPOINT = self.axiom.endpoint
        self.addCleanup(setattr, telemetry, 'MANAGE_ENDPOINT', original)

    def test_doctor_finds_the_region_that_accepts_ingest(self):
        self._managed()
        self.client = make_client(self.temp.name, 'https://us-east-1.aws.edge.axiom.co')
        self.axiom.queue(200, {'retentionDays': 30})
        report = self.client.diagnose(hosts={'local': self.axiom.endpoint})
        self.assertEqual(report['verdict'], 'ready')
        self.assertEqual(report['recommended_endpoint'], self.axiom.endpoint)
        self.assertFalse(report['endpoint_updated'])
        self.assertEqual(report['endpoint'], 'https://us-east-1.aws.edge.axiom.co')
        self.assertEqual(report['next'], 'serverctl telemetry-doctor --apply')

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes only')
    def test_doctor_applies_the_working_region_without_loosening_the_config(self):
        self._managed()
        self.client = make_client(self.temp.name, 'https://eu-central-1.aws.edge.axiom.co')
        self.axiom.queue(404, {'code': 404, 'message': 'dataset not found'})
        report = self.client.diagnose(apply=True, hosts={'local': self.axiom.endpoint})
        self.assertTrue(report['endpoint_updated'])
        self.assertEqual(report['endpoint'], self.axiom.endpoint)
        stored = json.loads(Path(self.client.config_path).read_text())
        self.assertEqual(stored['endpoint'], self.axiom.endpoint)
        self.assertEqual(stat.S_IMODE(os.stat(self.client.config_path).st_mode), 0o600)

    def test_doctor_explains_a_missing_dataset(self):
        self._managed()
        for _ in range(3):
            self.axiom.queue(404, {'code': 404, 'message': 'dataset not found'})
        report = self.client.diagnose(hosts={'us-east-1': self.axiom.endpoint,
                                             'eu-central-1': self.axiom.endpoint})
        self.assertEqual(report['verdict'], 'no edge region found the dataset')
        self.assertIn('create dataset', report['next'])
        self.assertEqual(report['ingest']['us-east-1']['status'], 404)

    def test_doctor_surfaces_a_token_that_may_not_create_the_dataset(self):
        self._managed()
        self.axiom.queue(404, {'code': 404, 'message': 'dataset not found'})
        self.axiom.queue(403, {'code': 403, 'message': 'forbidden'})
        for _ in range(2):
            self.axiom.queue(404, {'code': 404, 'message': 'dataset not found'})
        report = self.client.diagnose(create=True, hosts={'us-east-1': self.axiom.endpoint,
                                                          'eu-central-1': self.axiom.endpoint})
        self.assertEqual(report['dataset_create']['status'], 403)
        self.assertFalse(report['dataset_create']['ok'])
        create = [request for request in self.axiom.requests if request['method'] == 'POST'
                  and request['path'] == '/v2/datasets']
        self.assertEqual(len(create), 1)
        self.assertEqual(json.loads(create[0]['body'])['name'], 'galaxy-server')

    def test_doctor_never_prints_the_token(self):
        self._managed()
        self.axiom.queue(401, {'code': 401, 'message': 'token not supported'})  # lookup
        self.axiom.queue(403, {'code': 403, 'message': 'forbidden'})            # create
        self.axiom.queue(401, {'code': 401, 'message': 'token not supported'})  # ingest
        self.axiom.queue(401, {'code': 401, 'message': 'token not supported'})  # ingest
        report = self.client.diagnose(create=True, hosts={'us-east-1': self.axiom.endpoint,
                                                          'eu-central-1': self.axiom.endpoint})
        self.assertEqual(report['verdict'], 'token rejected by every edge region')
        self.assertNotIn(TOKEN, json.dumps(report))


class ConfigTest(unittest.TestCase):
    def test_rejects_insecure_endpoint_and_dataset(self):
        for change in ({'endpoint': 'http://example.com'},
                       {'endpoint': 'https://example.com/v1'},
                       {'dataset': 'bad dataset'},
                       {'max_retries': 99},
                       {'enabled': 'yes'}):
            config = dict(telemetry.DEFAULTS)
            config.update(change)
            with self.assertRaises(telemetry.ConfigError):
                telemetry.validate_config(config)

    @unittest.skipUnless(os.name == 'posix', 'POSIX file modes only')
    def test_rejects_group_readable_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'axiom.json')
            telemetry.save_config(path, dict(telemetry.DEFAULTS, token=TOKEN))
            self.assertIsNotNone(telemetry.load_config(path))
            os.chmod(path, 0o644)
            with self.assertRaises(telemetry.ConfigError):
                telemetry.load_config(path)

    def test_disabled_without_a_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            client = telemetry.Telemetry(directory)
            self.assertFalse(client.emit({'event': 'galaxy.test'}))
            self.assertEqual(client.status()['state'], 'off')
            self.assertFalse(os.path.exists(client.spool_path))


class ConsoleApiTest(unittest.TestCase):
    """Exercise the real status server over HTTP, without the phone."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.saved = {name: getattr(status, name) for name in (
            'RUN_DIR', 'LOG_DIR', 'PRIV_DIR', 'DB_PATH', 'SETTINGS_PATH',
            'PASSFILE', 'TERMINAL_PASSFILE', 'MONITOR_PATH', 'TELEMETRY')}
        status.RUN_DIR = str(root / 'run')
        status.LOG_DIR = str(root / 'log')
        status.PRIV_DIR = str(root / 'priv')
        status.DB_PATH = str(root / 'run' / 'galaxy.db')
        status.SETTINGS_PATH = str(root / 'run' / 'settings.json')
        status.PASSFILE = str(root / 'run' / 'htpasswd')
        status.TERMINAL_PASSFILE = str(root / 'run' / 'terminal.passwd')
        status.MONITOR_PATH = str(root / 'run' / 'monitor.json')
        for directory in (status.RUN_DIR, status.LOG_DIR, status.PRIV_DIR):
            os.makedirs(directory, exist_ok=True)
        digest = '{SHA}' + base64.b64encode(
            hashlib.sha1(b'secret').digest()).decode()
        Path(status.PASSFILE).write_text('admin:%s\n' % digest)
        status.init_db()
        status.TELEMETRY = telemetry.Telemetry(status.RUN_DIR)
        self.server = status.Server(('127.0.0.1', 0), status.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = self.login()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for name, value in self.saved.items():
            setattr(status, name, value)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port,
                                                timeout=15)
        payload = json.dumps(body).encode() if body is not None else None
        merged = {'Content-Type': 'application/json'} if body is not None else {}
        merged.update(headers or {})
        connection.request(method, path, payload, merged)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, response, data)
        connection.close()
        return result

    def login(self):
        code, response, _data = self.request('POST', '/api/login',
                                             {'user': 'admin', 'password': 'secret'})
        self.assertEqual(code, 200)
        cookie = response.getheader('Set-Cookie') or ''
        return cookie.split(';')[0]

    def test_private_endpoints_need_a_session(self):
        for path in ('/api/telemetry', '/api/status', '/api/settings',
                     '/api/timeline?range=24h', '/api/events'):
            code, _response, _data = self.request('GET', path)
            self.assertEqual(code, 401, path)

    def test_dashboard_endpoints_still_answer(self):
        headers = {'Cookie': self.cookie}
        for path in ('/api/status', '/api/settings', '/api/timeline?range=24h',
                     '/api/events', '/api/logs?service=status', '/api/telemetry'):
            code, _response, data = self.request('GET', path, headers=headers)
            self.assertEqual(code, 200, path)
            json.loads(data)
        code, _response, data = self.request('GET', '/api/health')
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(data)['ok'])

    def test_telemetry_endpoint_reports_state_without_the_token(self):
        run_dir = status.RUN_DIR
        config_path = os.path.join(run_dir, telemetry.CONFIG_NAME)
        config = dict(telemetry.DEFAULTS, enabled=True, dataset='galaxy-server',
                      token=TOKEN, endpoint='https://us-east-1.aws.edge.axiom.co')
        telemetry.save_config(config_path, config)
        status.TELEMETRY = telemetry.Telemetry(run_dir)
        code, _response, data = self.request('GET', '/api/telemetry',
                                             headers={'Cookie': self.cookie})
        self.assertEqual(code, 200)
        text = data.decode()
        self.assertNotIn(TOKEN, text)
        payload = json.loads(text)
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['dataset'], 'galaxy-server')


class LiveServerTest(unittest.TestCase):
    """Start the real status service on a temporary HOME and export to Axiom."""

    def setUp(self):
        probe = socket.socket()
        try:
            probe.bind(('127.0.0.1', 8765))
        except OSError:
            self.skipTest('port 8765 is already in use')
        finally:
            probe.close()

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.axiom = FakeAxiom()
        self.addCleanup(self.axiom.close)
        home = Path(self.temp.name)
        base = home / 'galaxy-server'
        for name in ('run', 'log', 'priv'):
            os.makedirs(base / name)
        (base / 'log' / 'status.log').write_text('2026-11-11 09:00:00 local line kept\n')
        digest = '{SHA}' + base64.b64encode(hashlib.sha1(b'secret').digest()).decode()
        (base / 'run' / 'htpasswd').write_text('admin:%s\n' % digest)
        config = dict(telemetry.DEFAULTS, enabled=True, endpoint=self.axiom.endpoint,
                      dataset='galaxy-server', token=TOKEN, batch_size=10,
                      flush_seconds=2, timeout_seconds=2, max_retries=1,
                      retry_backoff_seconds=0)
        telemetry.save_config(str(base / 'run' / telemetry.CONFIG_NAME), config)

        env = dict(os.environ)
        env.update({'HOME': str(home), 'USERPROFILE': str(home),
                    'PYTHONPATH': str(Path(__file__).resolve().parent)})
        self.process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent / 'status.py')],
            cwd=str(Path(__file__).resolve().parent), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._stop)
        self.assertTrue(self._wait(lambda: self._health()), 'server did not start')

    def _stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _health(self):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2) as reply:
                return reply.status == 200
        except OSError:
            return False

    def _wait(self, predicate, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.25)
        return False

    def _ingests(self):
        with self.axiom.lock:
            return [request for request in self.axiom.requests
                    if request['method'] == 'POST'
                    and request['path'].startswith('/v1/ingest/')]

    def _events(self):
        return [json.loads(line) for request in self._ingests()
                for line in request['body'].splitlines() if line]

    def _cookie(self):
        connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=10)
        connection.request('POST', '/api/login', json.dumps(
            {'user': 'admin', 'password': 'secret'}), {'Content-Type': 'application/json'})
        login = connection.getresponse()
        login.read()
        cookie = (login.getheader('Set-Cookie') or '').split(';')[0]
        connection.close()
        self.assertEqual(login.status, 200)
        return cookie

    def _telemetry(self, cookie):
        connection = http.client.HTTPConnection('127.0.0.1', 8765, timeout=10)
        connection.request('GET', '/api/telemetry', headers={'Cookie': cookie})
        reply = connection.getresponse()
        payload = json.loads(reply.read())
        connection.close()
        self.assertEqual(reply.status, 200)
        return payload

    def test_real_sampler_and_log_forwarding_reach_axiom(self):
        self.assertTrue(self._wait(lambda: bool(self._ingests())),
                        'no ingest request reached Axiom')
        self.assertTrue(self._wait(lambda: any(
            event['event'] == 'galaxy.sample' for event in self._events())),
                        'the sampler did not export a sample')

        with open(Path(self.temp.name) / 'galaxy-server' / 'log' / 'status.log', 'a') as handle:
            handle.write('2026-11-11 09:00:01 new line exported\n')
        self.assertTrue(self._wait(lambda: any(
            event['event'] == 'galaxy.log' and 'new line exported' in event.get('message', '')
            for event in self._events())), 'log line was not forwarded')

        # The local file is still the operational record after export.
        local = (Path(self.temp.name) / 'galaxy-server' / 'log' / 'status.log').read_text()
        self.assertIn('local line kept', local)
        self.assertIn('new line exported', local)

        cookie = self._cookie()
        self.assertTrue(self._wait(lambda: self._telemetry(cookie)['exported'] > 0),
                        'console did not report exported events')
        payload = self._telemetry(cookie)
        self.assertEqual(payload['state'], 'ok')
        self.assertEqual(payload['queued'], 0)
        self.assertEqual(payload['spool_events'], 0)
        self.assertTrue(self._health())

    def test_axiom_outage_is_buffered_without_breaking_the_console(self):
        self.axiom.default = (500, {'error': 'down'}, {})
        with open(Path(self.temp.name) / 'galaxy-server' / 'log' / 'status.log', 'a') as handle:
            handle.write('2026-11-11 09:10:00 while axiom is down\n')
        spool = Path(self.temp.name) / 'galaxy-server' / 'run' / telemetry.SPOOL_NAME
        self.assertTrue(self._wait(lambda: spool.exists() and spool.stat().st_size > 0),
                        'failed events were not spooled')
        payload = self._telemetry(self._cookie())
        self.assertEqual(payload['state'], 'degraded')
        self.assertGreater(payload['spool_events'], 0)
        self.assertIn('500', payload['last_error'])
        self.assertTrue(self._health())
        local = (Path(self.temp.name) / 'galaxy-server' / 'log' / 'status.log').read_text()
        self.assertIn('while axiom is down', local)


if __name__ == '__main__':
    unittest.main()
