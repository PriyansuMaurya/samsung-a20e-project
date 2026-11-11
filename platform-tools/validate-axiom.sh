#!/data/data/com.termux/files/usr/bin/bash
LOG=/sdcard/Download/validate-axiom.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1
python - <<'PY'
import json, os, time, urllib.error, urllib.request

config = json.load(open(os.path.expanduser('~/galaxy-server/run/axiom.json')))
token = config.get('manage_token') or config['token']
dataset = config['dataset']
edge = config['endpoint'].rstrip('/')

def call(method, url, payload=None, timeout=25):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, reply.read(3000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read(600).decode('utf-8', 'replace')
    except Exception as error:
        return None, '%s: %s' % (type(error).__name__, error)

now = int(time.time())
for label, method, url, payload in [
    ('dataset', 'GET', 'https://api.axiom.co/v2/datasets/%s' % dataset, None),
    ('fields-v1', 'GET', 'https://api.axiom.co/v1/datasets/%s/fields' % dataset, None),
    ('fields-v2', 'GET', 'https://api.axiom.co/v2/datasets/%s/fields' % dataset, None),
    ('apl-api', 'POST', 'https://api.axiom.co/v1/datasets/_apl?format=legacy',
     {'apl': "['%s'] | summarize count() by event" % dataset,
      'startTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 86400)),
      'endTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 60))}),
    ('apl-edge', 'POST', edge + '/v1/datasets/_apl?format=legacy',
     {'apl': "['%s'] | count" % dataset,
      'startTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 86400)),
      'endTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 60))}),
]:
    status, body = call(method, url, payload)
    print('%s %s %s' % (label, status, ' '.join(body.split())[:400]))
PY
echo DONE > /sdcard/Download/validate-axiom.done
