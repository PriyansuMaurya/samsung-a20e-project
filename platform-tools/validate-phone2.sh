#!/data/data/com.termux/files/usr/bin/bash
# Second validation pass. Adds the pipelines the first pass could not exercise
# (access log failures, one audit row, health heartbeat) and fixes the resource
# measurement. Restores run/axiom.json byte for byte on exit.
LOG=/sdcard/Download/validate-phone2.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1

BASE="$HOME/galaxy-server"
CONFIG="$BASE/run/axiom.json"
BACKUP="$BASE/run/axiom.json.validate-backup"
trap 'if [ -f "$BACKUP" ]; then cat "$BACKUP" > "$CONFIG"; chmod 600 "$CONFIG"; rm -f "$BACKUP"; fi' EXIT

counters() {
  python - "$BASE" "$1" <<'PY'
import json, os, sqlite3, sys, time
base, label = sys.argv[1], sys.argv[2]
run = os.path.join(base, 'run')
info = json.load(open(os.path.join(run, 'telemetry-live.json')))
state = json.load(open(os.path.join(run, 'telemetry-state.json')))
spool = os.path.join(run, 'telemetry-spool.ndjson')
print('[%s] age_s=%s counters=%s' % (
    label, round(time.time() - info['updated_at'], 1),
    json.dumps({k: info.get(k) for k in ('exported', 'failed', 'rejected', 'dropped',
                                        'deferred', 'bytes_sent', 'spool_drained',
                                        'batches', 'last_status', 'last_error')},
               sort_keys=True)))
print('[%s] by_event=%s spool=%s' % (
    label, json.dumps(info.get('by_event'), sort_keys=True),
    os.path.getsize(spool) if os.path.exists(spool) else 'absent'))
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(run, 'galaxy.db'), uri=True)
print('[%s] rows=%s maxids=%s' % (
    label,
    {t: con.execute('select count(*) from "%s"' % t).fetchone()[0]
     for t in ('samples', 'checks', 'events', 'audits', 'sessions')},
    {t: con.execute('select coalesce(max(id), 0) from "%s"' % t).fetchone()[0]
     for t in ('events', 'audits')}))
print('[%s] cursors=%s' % (label, json.dumps({k: state.get(k) for k in (
    'event_cursor', 'audit_cursor', 'sample_cursor', 'check_cursor',
    'history_floor')}, sort_keys=True)))
con.close()
PY
}

echo "===== 1. baseline ====="
python - "$BASE" <<'PY'
import json, os, sys
run = os.path.join(sys.argv[1], 'run')
print('revision', open(os.path.join(run, 'deployed-revision')).read().strip()[:12])
PY
counters baseline

echo "===== 2. access log: one 404 and one 401 are logged, a 200 is not ====="
before=$(stat -c %s log/access.log)
curl -s -o /dev/null -w 'health=%{http_code}\n' --max-time 5 http://127.0.0.1:8080/api/health
curl -s -o /dev/null -w 'missing=%{http_code}\n' --max-time 5 http://127.0.0.1:8080/definitely-missing
curl -s -o /dev/null -w 'private=%{http_code}\n' --max-time 5 http://127.0.0.1:8080/api/status
sleep 25
after=$(stat -c %s log/access.log)
echo "access.log bytes: before=$before after=$after"
echo "--- access.log tail"
tail -n 4 log/access.log
counters after-access

echo "===== 3. audit pipeline: one self-test audit row ====="
python - "$BASE" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1]))
import status
status.add_audit('validation', 'telemetry-self-test', 'ok',
                 'audit pipeline check, no state changed')
print('audit row written')
PY
sleep 25
echo "--- auth.log tail"
tail -n 2 log/auth.log
counters after-audit

echo "===== 4. health event without a console visitor ====="
sleep 20
counters after-health

echo "===== 5. outage and drain, counted ====="
cp "$CONFIG" "$BACKUP"
python - "$CONFIG" <<'PY'
import json, os, sys
path = sys.argv[1]
config = json.load(open(path))
config['endpoint'] = 'https://127.0.0.1:9'
temp = path + '.tmp'
with open(temp, 'w') as handle:
    json.dump(config, handle, indent=2, sort_keys=True)
    handle.write('\n')
os.chmod(temp, 0o600)
os.replace(temp, path)
PY
sleep 45
counters during-outage
python - "$BASE" <<'PY'
import json, os, re, sys
base = sys.argv[1]
path = os.path.join(base, 'run', 'telemetry-spool.ndjson')
config = json.load(open(os.path.join(base, 'run', 'axiom.json')))
if not os.path.exists(path):
    print('no spool written')
else:
    text = open(path, encoding='utf-8', errors='replace').read()
    print('spool_lines', len(text.splitlines()),
          'has_token', bool(config['token']) and config['token'] in text,
          'xaat', bool(re.search(r'xaa[tm]-', text)),
          'bearer', bool(re.search(r'(?i)bearer\s', text)))
PY
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
cat "$BACKUP" > "$CONFIG"
chmod 600 "$CONFIG"
rm -f "$BACKUP"
sleep 45
counters after-recovery

echo "===== 6. coverage ledger ====="
python - "$BASE" <<'PY'
import json, os, sqlite3, sys, time
base = sys.argv[1]
run = os.path.join(base, 'run')
state = json.load(open(os.path.join(run, 'telemetry-state.json')))
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(run, 'galaxy.db'), uri=True)
print('events unforwarded', con.execute(
    'select count(*) from events where id > ?', (state['event_cursor'],)).fetchone()[0])
print('audits unforwarded', con.execute(
    'select count(*) from audits where id > ?', (state['audit_cursor'],)).fetchone()[0])
floor = state.get('history_floor') or 0
print('samples older than floor not yet caught up', con.execute(
    'select count(*) from samples where ts < ? and ts > ?',
    (floor, state.get('sample_cursor') or 0)).fetchone()[0])
print('checks older than floor not yet caught up', con.execute(
    'select count(*) from checks where ts < ? and ts > ?',
    (floor, state.get('check_cursor') or 0)).fetchone()[0])
print('samples covered live (ts >= floor)', con.execute(
    'select count(*) from samples where ts >= ?', (floor,)).fetchone()[0])
print('checks covered live (ts >= floor)', con.execute(
    'select count(*) from checks where ts >= ?', (floor,)).fetchone()[0])
print('sessions', con.execute('select count(*) from sessions').fetchone()[0],
      '(never exported)')
print('hourly rows', con.execute('select count(*) from hourly').fetchone()[0])
for name in sorted(state.get('logs') or {}):
    path = os.path.join(base, 'log', name + '.log')
    if name == 'deploy-manual':
        path = os.path.join(base, 'log', 'manual-deploy.log')
    size = os.path.getsize(path) if os.path.exists(path) else None
    print('log %-14s offset=%s size=%s behind=%s' % (
        name, state['logs'][name]['offset'], size,
        (size - state['logs'][name]['offset']) if size is not None else 'n/a'))
con.close()
PY

echo "===== 7. resources over 60 s ====="
python - "$BASE" <<'PY'
import json, os, sys, time
base = sys.argv[1]
run = os.path.join(base, 'run')
pid = int(open(os.path.join(run, 'status.pid')).read().strip())
ticks = os.sysconf('SC_CLK_TCK')

def cpu(pid):
    fields = open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()
    return int(fields[11]) + int(fields[12])

def thread_cpu(pid):
    out = {}
    for task in os.listdir('/proc/%d/task' % pid):
        try:
            fields = open('/proc/%d/task/%s/stat' % (pid, task)).read().rsplit(')', 1)[1].split()
            comm = open('/proc/%d/task/%s/comm' % (pid, task)).read().strip()
        except OSError:
            continue
        out[comm] = int(fields[11]) + int(fields[12])
    return out

def live():
    return json.load(open(os.path.join(run, 'telemetry-live.json')))

first, first_threads, first_live = cpu(pid), thread_cpu(pid), live()
started = time.time()
time.sleep(60)
second, second_threads, second_live = cpu(pid), thread_cpu(pid), live()
elapsed = time.time() - started
rss = [line for line in open('/proc/%d/status' % pid) if line.startswith('VmRSS')][0].split()[1]
print('interval_s', round(elapsed, 1))
print('process_cpu_pct', round((second - first) / ticks / elapsed * 100, 2))
print('telemetry_thread_cpu_pct', round(
    (second_threads.get('telemetry', 0) - first_threads.get('telemetry', 0))
    / ticks / elapsed * 100, 3))
print('rss_kb', rss)
print('threads', sorted(second_threads))
delta_bytes = second_live['bytes_sent'] - first_live['bytes_sent']
delta_events = second_live['exported'] - first_live['exported']
print('bytes_delta', delta_bytes, 'events_delta', delta_events)
print('bytes_per_event', round(delta_bytes / delta_events, 1) if delta_events else None)
print('bytes_per_day', round(delta_bytes / elapsed * 86400) if elapsed else None)
spool = os.path.join(run, 'telemetry-spool.ndjson')
print('spool_bytes', os.path.getsize(spool) if os.path.exists(spool) else 0)
print('queue', second_live.get('queued'))
PY

echo "===== 8. final ====="
counters final
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
ls -l run/axiom.json run/telemetry-state.json
echo DONE > /sdcard/Download/validate-phone2.done
