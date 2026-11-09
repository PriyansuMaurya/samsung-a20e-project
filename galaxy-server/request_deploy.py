"""Run on GitHub's runner: notify the phone and verify the tested SHA is live."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request


def main():
    secret = os.environ['DEPLOY_SECRET'].strip().encode()
    if not secret:
        raise RuntimeError('PHONE_DEPLOY_SECRET is not configured')
    sha = os.environ['GITHUB_SHA']
    body = json.dumps({'ref': os.environ['GITHUB_REF'], 'sha': sha,
                       'repository': {'full_name': os.environ['GITHUB_REPOSITORY']}}).encode()
    signature = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()

    def request(event):
        req = urllib.request.Request('https://galaxy.maurya.ie/api/deploy/github', body,
                                     {'Content-Type': 'application/json',
                                      'User-Agent': 'Galaxy-Deploy/1.0 (GitHub Actions)',
                                      'X-Hub-Signature-256': signature,
                                      'X-GitHub-Event': event})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)

    for attempt in range(24):
        try:
            if request('actions-deploy-status').get('revision') == sha:
                print('Verified live commit:', sha)
                return
            if attempt % 3 == 0:
                result = request('actions-deploy')
                if result.get('status') != 'deployment queued':
                    raise RuntimeError('Phone did not accept deployment')
        except (OSError, urllib.error.URLError) as error:
            print('Waiting for phone:', type(error).__name__, getattr(error, 'code', ''), flush=True)
        time.sleep(10)
    raise RuntimeError('Phone did not confirm deployment within four minutes; inspect log/deploy.log')


if __name__ == '__main__':
    main()
