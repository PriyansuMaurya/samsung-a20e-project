import base64
import hashlib
import http.client
import importlib.util
import json
import os
import signal
import threading
from pathlib import Path
import tempfile
import time
import unittest

spec = importlib.util.spec_from_file_location('status', Path(__file__).with_name('status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class TerminalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        status.RUN_DIR = os.path.join(self.tmp.name, 'run')
        status.LOG_DIR = os.path.join(self.tmp.name, 'log')
        status.DB_PATH = os.path.join(status.RUN_DIR, 'test.db')
        status.PASSFILE = os.path.join(status.RUN_DIR, 'htpasswd')
        status.TERMINAL_PASSFILE = os.path.join(status.RUN_DIR, 'terminal.passwd')
        status.init_db()
        Path(status.PASSFILE).write_text('tester:{SHA}' + base64.b64encode(hashlib.sha1(b'test').digest()).decode())
        status.TERMINALS.clear()
        status.TERMINAL_FAILS.clear()

    def tearDown(self):
        for token, terminal in list(status.TERMINALS.items()):
            status.close_terminal(token, terminal['owner'])
        self.tmp.cleanup()

    def test_password_locked_until_configured(self):
        self.assertFalse(status.verify_terminal_password('secret'))
        status.set_terminal_password('a-strong-test-password')
        self.assertFalse(status.verify_terminal_password('wrong'))
        self.assertTrue(status.verify_terminal_password('a-strong-test-password'))

    def test_http_terminal_authentication_and_shell(self):
        server = status.Server(('127.0.0.1', 0), status.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host = '127.0.0.1:%s' % server.server_port
        def post(path, body, cookie=None, origin=True):
            conn = http.client.HTTPConnection(host, timeout=5)
            headers = {'Content-Type': 'application/json', 'Host': host}
            if origin: headers['Origin'] = 'http://' + host
            if cookie: headers['Cookie'] = cookie
            conn.request('POST', path, json.dumps(body), headers)
            response = conn.getresponse()
            result = response.status, json.loads(response.read()), response.getheader('Set-Cookie')
            conn.close()
            return result
        try:
            self.assertEqual(post('/api/terminal/open', {'password': 'anything'})[0], 401)
            code, _, cookie = post('/api/login', {'user': 'tester', 'password': 'test'})
            self.assertEqual(code, 200)
            cookie = cookie.split(';')[0]
            self.assertEqual(post('/api/terminal/open', {'password': 'anything'}, cookie)[0], 503)
            status.set_terminal_password('a-strong-test-password')
            self.assertEqual(post('/api/terminal/open', {'password': 'wrong'}, cookie)[0], 403)
            self.assertEqual(post('/api/terminal/open', {'password': 'a-strong-test-password'}, cookie, False)[0], 403)
            code, data, _ = post('/api/terminal/open', {'password': 'a-strong-test-password'}, cookie)
            self.assertEqual(code, 200)
            session = data['id']
            self.assertEqual(post('/api/terminal/resize', {'id': session, 'cols': 101, 'rows': 37}, cookie)[0], 200)
            self.assertEqual(post('/api/terminal/resize', {'id': session, 'cols': 10000, 'rows': 37}, cookie)[0], 400)
            self.assertEqual(post('/api/terminal/input', {'id': session, 'input': 'stty size; tty\n'}, cookie)[0], 200)
            result = b''
            for _ in range(50):
                _, response, _ = post('/api/terminal/read', {'id': session}, cookie)
                result += base64.b64decode(response['output'])
                if b'37 101' in result and b'/dev/pts/' in result: break
                time.sleep(.05)
            self.assertIn(b'37 101', result)
            self.assertIn(b'/dev/pts/', result)
            self.assertEqual(post('/api/terminal/input', {'id': session, 'input': 'sleep 30\n'}, cookie)[0], 200)
            time.sleep(.3)
            self.assertEqual(post('/api/terminal/input', {'id': session, 'input': '\u0003'}, cookie)[0], 200)
            time.sleep(.2)
            self.assertEqual(post('/api/terminal/input', {'id': session, 'input': "printf '%s%s\\n' INTERRUPT_ OK\n"}, cookie)[0], 200)
            interrupted = b''
            for _ in range(40):
                _, response, _ = post('/api/terminal/read', {'id': session}, cookie)
                interrupted += base64.b64decode(response['output'])
                if b'INTERRUPT_OK' in interrupted: break
                time.sleep(.05)
            self.assertIn(b'INTERRUPT_OK', interrupted, 'Ctrl+C did not interrupt foreground command')
            self.assertEqual(post('/api/terminal/input', {'id': session, 'input': "printf '%s%s\\n' DONE_ 456\n"}, cookie)[0], 200)
            output = b''
            for _ in range(50):
                code, data, _ = post('/api/terminal/read', {'id': session}, cookie)
                self.assertEqual(code, 200)
                output += base64.b64decode(data['output'])
                if b'DONE_456' in output: break
                time.sleep(.05)
            self.assertIn(b'DONE_456', output)
            self.assertEqual(post('/api/logout', {}, cookie)[0], 200)
            self.assertFalse(status.TERMINALS)
            self.assertEqual(post('/api/terminal/read', {'id': session}, cookie)[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_background_service_does_not_disable_interrupts(self):
        original = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            session = status.start_terminal('owner')
        finally:
            signal.signal(signal.SIGINT, original)
        terminal = status.terminal_for(session, 'owner')
        os.write(terminal['fd'], b"printf '%s%s\\n' READY_ SIGNAL; sleep 30\n")
        output = b''
        deadline = time.monotonic() + 5
        while b'READY_SIGNAL' not in output and time.monotonic() < deadline:
            output += status.read_terminal(terminal)
            time.sleep(.05)
        self.assertIn(b'READY_SIGNAL', output)
        time.sleep(.2)
        os.write(terminal['fd'], b"\x03")
        time.sleep(.2)
        os.write(terminal['fd'], b"printf '%s%s\\n' SIGNAL_ OK\n")
        output = b''
        deadline = time.monotonic() + 5
        while b'SIGNAL_OK' not in output and time.monotonic() < deadline:
            output += status.read_terminal(terminal)
            time.sleep(.05)
        self.assertIn(b'SIGNAL_OK', output)

    def test_terminal_attempts_are_rate_limited(self):
        server = status.Server(('127.0.0.1', 0), status.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        host = '127.0.0.1:%s' % server.server_port
        cookie = 'galaxy_session=' + status.new_session('tester')
        status.set_terminal_password('a-strong-test-password')
        try:
            for attempt in range(6):
                conn = http.client.HTTPConnection(host, timeout=5)
                conn.request('POST', '/api/terminal/open', json.dumps({'password': 'wrong'}),
                             {'Content-Type': 'application/json', 'Origin': 'http://' + host,
                              'Host': host, 'Cookie': cookie})
                response = conn.getresponse()
                self.assertEqual(response.status, 403 if attempt < 5 else 429)
                response.read()
                conn.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_session_is_bound_and_can_execute(self):
        session = status.start_terminal('owner')
        self.assertIsNone(status.terminal_for(session, 'other'))
        terminal = status.terminal_for(session, 'owner')
        self.assertIsNotNone(terminal)
        os.write(terminal['fd'], b"printf '%s%s\\n' TERMINAL_ OK\n")
        deadline = time.monotonic() + 5
        output = b''
        while b'TERMINAL_OK' not in output and time.monotonic() < deadline:
            output += status.read_terminal(terminal)
            time.sleep(.05)
        self.assertIn(b'TERMINAL_OK', output)
        status.close_terminal(session, 'owner')
        self.assertIsNone(status.terminal_for(session, 'owner'))


if __name__ == '__main__':
    unittest.main()
