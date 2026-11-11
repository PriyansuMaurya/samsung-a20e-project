import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('status', Path(__file__).with_name('status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class FakeDevice(http.server.BaseHTTPRequestHandler):
    """Stands in for the Galaxy Device app on 127.0.0.1:8790."""

    replies = {}
    calls = []

    def log_message(self, *args):
        pass

    def _reply(self, code, payload, kind='application/json'):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        FakeDevice.calls.append(('GET', self.path, dict(self.headers)))
        if self.path.startswith('/camera/stream'):
            self._reply(200, b'--galaxyframe\r\n\r\n', 'multipart/x-mixed-replace')
            return
        self._reply(200, FakeDevice.replies.get(self.path, {'ok': True}))

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length)
        FakeDevice.calls.append(('POST', self.path,
                                 {'headers': dict(self.headers), 'body': body}))
        if self.path == '/pair':
            payload = json.loads(body or b'{}')
            if len(str(payload.get('token', ''))) < 16:
                self._reply(400, {'error': 'token too short'})
                return
            self._reply(200, {'ok': True, 'paired': True})
            return
        if self.path == '/media/upload':
            self._reply(200, {'ok': True, 'file': self.headers.get('X-Filename')})
            return
        self._reply(200, {'ok': True})


class DeviceProxyTest(unittest.TestCase):
    def setUp(self):
        self.device = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeDevice)
        threading.Thread(target=self.device.serve_forever, daemon=True).start()
        FakeDevice.calls = []
        FakeDevice.replies = {'/health': {'ok': True, 'paired': True},
                              '/capabilities': {'model': 'SM-A202F'},
                              '/battery': {'percentage': 80},
                              '/storage': {'free_bytes': 1},
                              '/volume': {'streams': {}}}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.device.server_close)
        self.addCleanup(self.device.shutdown)
        self.patches = [
            patch.object(status, 'RUN_DIR', self.directory.name),
            patch.object(status, 'LOG_DIR', self.directory.name),
            patch.object(status, 'DB_PATH', os.path.join(self.directory.name, 'galaxy.db')),
            patch.object(status, 'DEVICE_PATH', os.path.join(self.directory.name, 'device.json')),
            patch.object(status, 'session_user', return_value=('tester', 'token')),
            patch.object(status, 'add_audit'),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        status.init_db()
        status.write_device({'token': 'x' * 32, 'port': self.device.server_port})
        self.server = status.Server(('127.0.0.1', 0), status.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        extra = {'Origin': 'http://127.0.0.1:%d' % self.server.server_port}
        if body is not None and method == 'POST':
            extra['Content-Type'] = 'application/json'
        extra.update(headers or {})
        connection.request(method, path, body, extra)
        response = connection.getresponse()
        raw = response.read()
        code = response.status
        connection.close()
        try:
            return code, json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return code, raw

    def test_summary_requires_pairing_then_merges_reads(self):
        status.write_device({'token': '', 'port': self.device.server_port})
        code, payload = self.call('GET', '/api/device/summary')
        self.assertEqual(code, 200)
        self.assertFalse(payload['paired'])

        code, payload = self.call('POST', '/api/device/pair',
                                  json.dumps({'token': 'y' * 32}))
        self.assertEqual(code, 200)
        self.assertEqual(status.read_device()['token'], 'y' * 32)
        if os.name == 'posix':
            # The token is a bearer credential; Windows has no mode bits to check.
            mode = oct(os.stat(os.path.join(self.directory.name,
                                            'device.json')).st_mode & 0o777)
            self.assertEqual(mode, '0o600')

        code, payload = self.call('GET', '/api/device/summary')
        self.assertEqual(code, 200)
        self.assertTrue(payload['paired'])
        self.assertEqual(payload['capabilities']['model'], 'SM-A202F')
        self.assertEqual(payload['battery']['percentage'], 80)

    def test_pairing_refused_when_window_closed(self):
        FakeDevice.replies['/pair'] = None  # unused: the handler answers directly
        original = FakeDevice.do_POST

        def refuse(handler):
            length = int(handler.headers.get('Content-Length') or 0)
            handler.rfile.read(length)
            handler.send_response(403)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', '2')
            handler.end_headers()
            handler.wfile.write(b'{}')

        FakeDevice.do_POST = refuse
        self.addCleanup(setattr, FakeDevice, 'do_POST', original)
        code, payload = self.call('POST', '/api/device/pair', json.dumps({}))
        self.assertEqual(code, 400)
        self.assertIn('pairing window', payload['error'])
        self.assertEqual(status.read_device()['token'], 'x' * 32)

    def test_control_allowlist_and_passthrough(self):
        code, payload = self.call('POST', '/api/device/control',
                                  json.dumps({'action': 'rm-rf'}))
        self.assertEqual(code, 400)

        code, payload = self.call('POST', '/api/device/control',
                                  json.dumps({'action': 'torch', 'payload': {'on': True}}))
        self.assertEqual(code, 200)
        self.assertTrue(payload['ok'])
        method, path, _ = FakeDevice.calls[-1]
        self.assertEqual((method, path), ('POST', '/torch'))

        code, payload = self.call('POST', '/api/device/control',
                                  json.dumps({'action': 'snapshot', 'payload': {'camera': '1'}}))
        self.assertEqual(code, 200)
        self.assertEqual(FakeDevice.calls[-1][1], '/camera/snapshot?camera=1')

    def test_streaming_and_filename_rules(self):
        code, raw = self.call('GET', '/api/device/camera/stream?camera=0&fps=2')
        self.assertEqual(code, 200)
        self.assertIn(b'galaxyframe', raw)
        self.assertTrue(FakeDevice.calls[-1][1].startswith('/camera/stream'))

        code, payload = self.call('POST', '/api/device/media/upload', b'RIFF',
                                  {'X-Filename': 'tone.wav',
                                   'Content-Type': 'application/octet-stream'})
        self.assertEqual(code, 200)
        self.assertEqual(payload['file'], 'tone.wav')
        forwarded = FakeDevice.calls[-1][2]
        self.assertEqual(forwarded['body'], b'RIFF')
        self.assertEqual(forwarded['headers']['X-Galaxy-Token'], 'x' * 32)

        code, payload = self.call('POST', '/api/device/media/upload', b'x',
                                  {'X-Filename': '../escape.wav',
                                   'Content-Type': 'application/octet-stream'})
        self.assertEqual(code, 400)

    def test_unpaired_and_device_down_answers(self):
        status.write_device({'token': '', 'port': self.device.server_port})
        code, payload = self.call('GET', '/api/device/battery')
        self.assertEqual(code, 503)
        self.assertFalse(payload['paired'])

        status.write_device({'token': 'z' * 32, 'port': 9})
        code, payload = self.call('GET', '/api/device/battery')
        self.assertEqual(code, 503)
        self.assertIn('not running', payload['error'])
        status.write_device({'token': 'x' * 32, 'port': self.device.server_port})


if __name__ == '__main__':
    unittest.main()
