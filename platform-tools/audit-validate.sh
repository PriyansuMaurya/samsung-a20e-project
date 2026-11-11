#!/data/data/com.termux/files/usr/bin/bash
LOG=/sdcard/Download/audit-validate.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1

echo "=== revision ==="
cat run/deployed-revision
echo "=== access log ==="
ls -l log/access.log 2>&1
tail -n 8 log/access.log 2>&1
echo "=== live counters (serverctl reads the running service) ==="
"$HOME/galaxy-server/serverctl" telemetry-status
echo "=== live snapshot file ==="
python - <<'PY'
import json, os
path = os.path.expanduser('~/galaxy-server/run/telemetry-live.json')
try:
    live = json.load(open(path))
except OSError as error:
    print('missing:', error)
else:
    print({key: live.get(key) for key in ('exported', 'failed', 'rejected', 'dropped',
                                          'deferred', 'batches', 'last_status')})
    print('by_event', json.dumps(live.get('by_event'), sort_keys=True))
    print('age_s', round(__import__('time').time() - live['updated_at'], 1))
PY
echo "=== cursors ==="
python - <<'PY'
import json, os
state = json.load(open(os.path.expanduser('~/galaxy-server/run/telemetry-state.json')))
print({key: state.get(key) for key in ('sample_cursor', 'check_cursor', 'audit_cursor',
                                       'event_cursor', 'history_floor')})
print('logs', sorted(state.get('logs', {})))
PY
echo "=== local row counts ==="
python - <<'PY'
import os, sqlite3
con = sqlite3.connect('file:%s?mode=ro' % os.path.expanduser('~/galaxy-server/run/galaxy.db'), uri=True)
for table in ('samples', 'checks', 'events', 'audits'):
    print(' ', table, con.execute('select count(*) from "%s"' % table).fetchone()[0])
PY
echo "=== service errors since restart ==="
ls -l --time-style=full-iso log/status.log
ps -o pid,etime -p "$(cat run/status.pid)" 2>&1
echo "=== axiom config ==="
python - <<'PY'
import json, os
config = json.load(open(os.path.expanduser('~/galaxy-server/run/axiom.json')))
print({k: v for k, v in config.items() if 'token' not in k})
PY
echo DONE > /sdcard/Download/audit-validate.done
