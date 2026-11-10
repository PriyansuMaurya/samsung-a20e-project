import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location(
    'request_deploy', Path(__file__).with_name('request_deploy.py'))
request_deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(request_deploy)

SHA = 'a' * 40
ENVIRONMENT = {'DEPLOY_SECRET': 'test-secret', 'GITHUB_SHA': SHA,
               'GITHUB_REF': 'refs/heads/master',
               'GITHUB_REPOSITORY': 'PriyansuMaurya/samsung-a20e-project'}


def http_error(code, body, url=request_deploy.ENDPOINT):
    return urllib.error.HTTPError(url, code, 'error', {}, io.BytesIO(body))


class RequestDeployTest(unittest.TestCase):
    def run_main(self, urlopen, sleeps=None):
        with patch.dict(os.environ, ENVIRONMENT), \
                patch.object(request_deploy.urllib.request, 'urlopen', urlopen), \
                patch.object(request_deploy.time, 'sleep') as sleep:
            try:
                request_deploy.main()
            finally:
                if sleeps is not None:
                    sleeps.append(sleep.call_count)

    def test_origin_down_502_fails_fast_with_phone_instruction(self):
        sleeps = []
        calls = []

        def urlopen(*args, **kwargs):
            calls.append(args)
            raise http_error(502, b'error code: 502')

        with self.assertRaises(RuntimeError) as raised:
            self.run_main(urlopen, sleeps)
        message = str(raised.exception)
        self.assertIn('502', message)
        self.assertIn('serverctl start', message)
        self.assertEqual(len(calls), request_deploy.ORIGIN_DOWN_LIMIT)
        self.assertEqual(sleeps, [request_deploy.ORIGIN_DOWN_LIMIT - 1])

    def test_rejected_signature_is_not_retried(self):
        calls = []

        def urlopen(*args, **kwargs):
            calls.append(args)
            raise http_error(403, b'{"error": "Invalid webhook signature"}')

        with self.assertRaises(RuntimeError) as raised:
            self.run_main(urlopen)
        self.assertIn('PHONE_DEPLOY_SECRET', str(raised.exception))
        self.assertEqual(len(calls), 1)

    def test_cloudflare_block_is_reported_as_such(self):
        def urlopen(*args, **kwargs):
            raise http_error(403, b'error code: 1010')

        with self.assertRaises(RuntimeError) as raised:
            self.run_main(urlopen)
        self.assertIn('1010', str(raised.exception))

    def test_live_revision_ends_the_run_without_queueing(self):
        events = []

        def urlopen(request, **kwargs):
            events.append(request.get_header('X-github-event'))
            return io.BytesIO(json.dumps({'revision': SHA}).encode())

        with patch('builtins.print') as output:
            self.run_main(urlopen)
        self.assertEqual(events, ['actions-deploy-status'])
        self.assertIn('Verified live commit', str(output.call_args.args[0]))


if __name__ == '__main__':
    unittest.main()
