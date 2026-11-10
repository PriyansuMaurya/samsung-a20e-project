"""Run on GitHub's runner: notify the phone and verify the tested SHA is live."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = 'https://galaxy.maurya.ie/api/deploy/github'
# Cloudflare answers on the tunnel's behalf when the phone cannot: a 5xx means
# the connector never reached nginx on the phone, so more retries only postpone
# the failure. A short run of them rides out the restart a deployment performs.
ORIGIN_DOWN = frozenset({502, 520, 521, 522, 523, 524})
ORIGIN_DOWN_LIMIT = 4


def _detail(error, limit=120):
    """Return the Cloudflare or phone message that explains an HTTP error."""
    try:
        body = error.read(limit).decode('utf-8', 'replace')
    except (OSError, ValueError):
        return ''
    finally:
        error.close()
    return ' '.join(body.split())[:limit]


def _origin_down(error, detail):
    return RuntimeError(
        'Cloudflare reached the tunnel but the phone web server did not answer '
        '(HTTP %d %s). Start it on the phone with `~/galaxy-server/serverctl start`, '
        'then rerun this workflow; this run deployed nothing.'
        % (error.code, detail or 'no body'))


def main():
    secret = os.environ['DEPLOY_SECRET'].strip().encode()
    if not secret:
        raise RuntimeError('PHONE_DEPLOY_SECRET is not configured')
    sha = os.environ['GITHUB_SHA']
    body = json.dumps({'ref': os.environ['GITHUB_REF'], 'sha': sha,
                       'repository': {'full_name': os.environ['GITHUB_REPOSITORY']}}).encode()
    signature = 'sha256=' + hmac.new(secret, body, hashlib.sha256).hexdigest()

    def request(event):
        req = urllib.request.Request(ENDPOINT, body,
                                     {'Content-Type': 'application/json',
                                      'User-Agent': 'Galaxy-Deploy/1.0 (GitHub Actions)',
                                      'X-Hub-Signature-256': signature,
                                      'X-GitHub-Event': event})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)

    outages = 0
    for attempt in range(24):
        try:
            if request('actions-deploy-status').get('revision') == sha:
                print('Verified live commit:', sha)
                return
            if attempt % 3 == 0:
                result = request('actions-deploy')
                if result.get('status') != 'deployment queued':
                    raise RuntimeError('Phone did not accept deployment')
        except urllib.error.HTTPError as error:
            detail = _detail(error)
            if error.code in ORIGIN_DOWN:
                outages += 1
                print('Waiting for phone: HTTP %d %s' % (error.code, detail), flush=True)
                if outages >= ORIGIN_DOWN_LIMIT:
                    raise _origin_down(error, detail) from None
            elif detail.startswith('error code: 10'):
                # Cloudflare WAF and browser-integrity rules answer 403 with
                # their own code, which no amount of retrying will clear.
                raise RuntimeError(
                    'Cloudflare refused the request before it reached the phone '
                    '(%s): a firewall, browser-integrity or rate-limit rule on '
                    'galaxy.maurya.ie is blocking the GitHub Actions runner.' % detail)
            elif error.code == 429 or 500 <= error.code < 600:
                outages = 0
                print('Waiting for phone: HTTP %d %s' % (error.code, detail), flush=True)
            else:
                raise RuntimeError(
                    'The phone rejected the deployment request (HTTP %d %s). '
                    'Check that the repository secret PHONE_DEPLOY_SECRET matches '
                    'run/deploy-secret on the phone.' % (error.code, detail or 'no body'))
        except (OSError, urllib.error.URLError) as error:
            print('Waiting for phone:', type(error).__name__, getattr(error, 'reason', ''),
                  flush=True)
        time.sleep(10)
    raise RuntimeError('Phone did not confirm deployment within four minutes; inspect log/deploy.log')


if __name__ == '__main__':
    main()
