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
from urllib.parse import parse_qs, urlparse

spec = importlib.util.spec_from_file_location('status', Path(__file__).with_name('status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)

TRACK = {
    'id': '1848357',
    'name': 'mañana será tarde',
    'duration': 272,
    'artist_name': 'fankel',
    'album_name': 'mañana será tarde',
    'image': 'https://usercontent.jamendo.com?type=album&id=368084&width=200',
    'audio': 'https://prod-1.storage.jamendo.com/?trackid=1848357&format=mp31&from=app-devsite',
    'waveform': '{"peaks":[' + ','.join(['0'] * 500) + ']}',
    'shareurl': 'https://www.jamendo.com/track/1848357',
}


class FakeJamendo(http.server.BaseHTTPRequestHandler):
    """Stands in for api.jamendo.com."""

    calls = []
    results = [TRACK]
    fail = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeJamendo.calls.append(self.path)
        if FakeJamendo.fail:
            raw = json.dumps(FakeJamendo.fail).encode()
            self.send_response(500)
        else:
            raw = json.dumps({'headers': {'status': 'success', 'code': 0,
                                          'results_count': len(FakeJamendo.results)},
                              'results': FakeJamendo.results}).encode()
            self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class MusicTest(unittest.TestCase):
    def setUp(self):
        self.jamendo = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeJamendo)
        threading.Thread(target=self.jamendo.serve_forever, daemon=True).start()
        FakeJamendo.calls = []
        FakeJamendo.results = [TRACK]
        FakeJamendo.fail = None
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.jamendo.server_close)
        self.addCleanup(self.jamendo.shutdown)
        self.patches = [
            patch.object(status, 'RUN_DIR', self.directory.name),
            patch.object(status, 'LOG_DIR', self.directory.name),
            patch.object(status, 'PRIV_DIR', self.directory.name),
            patch.object(status, 'DB_PATH', os.path.join(self.directory.name, 'galaxy.db')),
            patch.object(status, 'MUSIC_PATH', os.path.join(self.directory.name, 'jamendo.json')),
            patch.object(status, 'JAMENDO_API',
                         'http://127.0.0.1:%d/v3.0/tracks/' % self.jamendo.server_port),
            patch.object(status, 'session_user', return_value=('tester', 'token')),
            patch.object(status, 'add_audit'),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        status.init_db()
        self.server = status.Server(('127.0.0.1', 0), status.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read()
        code = response.status
        headers = dict(response.getheaders())
        connection.close()
        try:
            return code, json.loads(raw.decode()), headers
        except (ValueError, UnicodeDecodeError):
            return code, raw, headers

    def test_search_needs_a_client_id(self):
        code, payload, _headers = self.call('GET', '/api/music/search?q=rain')
        self.assertEqual(code, 200)
        self.assertFalse(payload['configured'])
        self.assertIn('serverctl jamendo-key', payload['error'])
        self.assertEqual(FakeJamendo.calls, [])

    def test_search_forwards_the_query_and_trims_the_payload(self):
        status.set_music_client('a' * 32)
        code, payload, _headers = self.call('GET', '/api/music/search?q=manana')
        self.assertEqual(code, 200)
        self.assertTrue(payload['configured'])
        self.assertEqual(payload['count'], 1)
        track = payload['tracks'][0]
        self.assertEqual(track['name'], 'mañana será tarde')
        self.assertEqual(track['artist'], 'fankel')
        self.assertEqual(track['image'], TRACK['image'])
        self.assertEqual(track['audio'], TRACK['audio'])
        # The waveform is hundreds of KB and the player never uses it.
        self.assertNotIn('waveform', track)

        params = parse_qs(urlparse(FakeJamendo.calls[-1]).query)
        self.assertEqual(params['namesearch'], ['manana'])
        self.assertEqual(params['client_id'], ['a' * 32])
        self.assertEqual(params['limit'], ['10'])
        self.assertEqual(params['format'], ['json'])
        self.assertNotIn('order', params)

    def test_category_only_browses_by_popularity(self):
        status.set_music_client('b' * 32)
        code, payload, _headers = self.call('GET', '/api/music/search?category=Electronic!')
        self.assertEqual(code, 200)
        self.assertEqual(payload['count'], 1)
        params = parse_qs(urlparse(FakeJamendo.calls[-1]).query)
        self.assertEqual(params['fuzzytags'], ['electronic'])
        self.assertEqual(params['order'], ['popularity_total'])
        self.assertNotIn('namesearch', params)

    def test_jamendo_refusal_and_unplayable_tracks_are_handled(self):
        status.set_music_client('c' * 32)
        FakeJamendo.fail = {'headers': {'status': 'failed', 'code': 5,
                                        'error_message': 'Invalid client_id'}}
        code, payload, _headers = self.call('GET', '/api/music/search?q=rain')
        self.assertEqual(code, 502)
        self.assertIn('Invalid client_id', payload['error'])
        self.assertEqual(payload['tracks'], [])

        FakeJamendo.fail = None
        FakeJamendo.results = [dict(TRACK, audio=''), dict(TRACK, id='2', audio='http://x/y.mp3')]
        code, payload, _headers = self.call('GET', '/api/music/search?q=rain')
        self.assertEqual(code, 200)
        self.assertEqual(payload['tracks'], [])

    def test_unreachable_jamendo_is_reported(self):
        status.set_music_client('d' * 32)
        with patch.object(status, 'JAMENDO_API', 'http://127.0.0.1:9/v3.0/tracks/'):
            code, payload, _headers = self.call('GET', '/api/music/search?q=rain')
        self.assertEqual(code, 502)
        self.assertIn('unreachable', payload['error'])

    def test_client_id_validation_and_file_mode(self):
        with self.assertRaises(ValueError):
            status.set_music_client('short')
        with self.assertRaises(ValueError):
            status.set_music_client('bad id with spaces')
        status.set_music_client('  ' + 'e' * 32 + '  ')
        self.assertEqual(status.read_music()['client_id'], 'e' * 32)
        if os.name == 'posix':
            mode = oct(os.stat(os.path.join(self.directory.name, 'jamendo.json')).st_mode & 0o777)
            self.assertEqual(mode, '0o600')

    def test_console_csp_allows_jamendo_media_only(self):
        Path(self.directory.name, 'console.html').write_text('<p>{{USER}}</p>', encoding='utf-8')
        code, _body, headers = self.call('GET', '/console')
        self.assertEqual(code, 200)
        csp = headers['Content-Security-Policy']
        self.assertIn('media-src \'self\' https://*.jamendo.com', csp)
        self.assertIn('img-src \'self\' data: https://*.jamendo.com', csp)
        self.assertIn('connect-src \'self\'', csp)


if __name__ == '__main__':
    unittest.main()
