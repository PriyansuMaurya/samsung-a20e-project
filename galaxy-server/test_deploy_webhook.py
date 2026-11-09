import hashlib
import hmac
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('status', Path(__file__).with_name('status.py'))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class DeployWebhookTest(unittest.TestCase):
    def test_signed_production_push_only(self):
        with tempfile.TemporaryDirectory() as directory:
            status.RUN_DIR = status.LOG_DIR = directory
            Path(directory, 'deploy-secret').write_bytes(b'test-secret')
            Path(directory, 'autodeploy-enabled').touch()
            server = status.Server(('127.0.0.1', 0), status.Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()

            def post(branch='master', secret=b'test-secret', event='push'):
                body = json.dumps({'ref': 'refs/heads/' + branch,
                                   'repository': {'full_name': 'PriyansuMaurya/samsung-a20e-project'}}).encode()
                signature = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request('POST', '/api/deploy/github', body,
                                   {'X-Hub-Signature-256': signature, 'X-GitHub-Event': event})
                response = connection.getresponse()
                code = response.status
                response.read()
                connection.close()
                return code

            try:
                with patch.object(status.subprocess, 'Popen') as launch:
                    self.assertEqual(post(secret=b'wrong'), 403)
                    self.assertEqual(post(branch='feature'), 200)
                    self.assertEqual(post(event='ping'), 200)
                    launch.assert_not_called()
                    self.assertEqual(post(), 202)
                    launch.assert_called_once()
                    self.assertTrue(launch.call_args.kwargs['start_new_session'])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
