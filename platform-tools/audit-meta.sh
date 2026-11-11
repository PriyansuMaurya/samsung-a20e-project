#!/data/data/com.termux/files/usr/bin/bash
LOG=/sdcard/Download/audit-meta.log
exec > "$LOG" 2>&1
python - <<'PY'
import json, os, urllib.error, urllib.request
config = json.load(open(os.path.expanduser('~/galaxy-server/run/axiom.json')))
token = config.get('manage_token') or config['token']
for path in ('/v2/datasets/%s' % config['dataset'], '/v2/datasets'):
    url = 'https://api.axiom.co' + path
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(request, timeout=20) as reply:
            body = reply.read(4000).decode('utf-8', 'replace')
        print(path, reply.status)
        print(body[:1500])
    except urllib.error.HTTPError as error:
        print(path, 'HTTP', error.code, error.read(300).decode('utf-8', 'replace'))
    except Exception as error:
        print(path, type(error).__name__, error)
PY
echo DONE > /sdcard/Download/audit-meta.done
