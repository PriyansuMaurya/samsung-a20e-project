#!/data/data/com.termux/files/usr/bin/bash
# End-to-end validation of every telemetry pipeline against the live dataset.
# Read-mostly: it appends one canary line to deploy.log, briefly points the
# exporter at a closed port, and restores run/axiom.json byte for byte.
LOG=/sdcard/Download/validate-phone.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1

BASE="$HOME/galaxy-server"
CONFIG="$BASE/run/axiom.json"
BACKUP="$BASE/run/axiom.json.validate-backup"
trap 'if [ -f "$BACKUP" ]; then cat "$BACKUP" > "$CONFIG"; chmod 600 "$CONFIG"; rm -f "$BACKUP"; fi' EXIT

snap() {
  echo "--- $1"
  python - "$BASE" <<'PY'
import json, os, sqlite3, sys, time
base = sys.argv[1]
run = os.path.join(base, 'run')

def live():
    try:
        with open(os.path.join(run, 'telemetry-live.json')) as handle:
            return json.load(handle)
    except OSError:
        return {}

state = {}
try:
    with open(os.path.join(run, 'telemetry-state.json')) as handle:
        state = json.load(handle)
except OSError:
    pass
info = live()
print('revision', open(os.path.join(run, 'deployed-revision')).read().strip()[:12])
print('counters', json.dumps({k: info.get(k) for k in (
    'exported', 'failed', 'rejected', 'dropped', 'deferred', 'bytes_sent',
    'batches', 'last_status', 'last_error')}, sort_keys=True))
print('by_event', json.dumps(info.get('by_event'), sort_keys=True))
print('age_s', round(time.time() - info['updated_at'], 1) if info.get('updated_at') else None)
spool = os.path.join(run, 'telemetry-spool.ndjson')
print('spool', os.path.getsize(spool) if os.path.exists(spool) else 'absent')
print('cursors', json.dumps({k: state.get(k) for k in (
    'event_cursor', 'audit_cursor', 'sample_cursor', 'check_cursor', 'history_floor')},
    sort_keys=True))
print('logs', json.dumps(state.get('logs'), sort_keys=True))
try:
    con = sqlite3.connect('file:%s?mode=ro' % os.path.join(run, 'galaxy.db'), uri=True)
    print('rows', {t: con.execute('select count(*) from "%s"' % t).fetchone()[0]
                   for t in ('samples', 'checks', 'events', 'audits')})
    newest = con.execute('select ts, tunnel_errors, batt, tunnel_req from samples '
                         'order by ts desc limit 1').fetchone()
    print('newest_sample', newest)
    con.close()
except sqlite3.Error as error:
    print('db', error)
PY
}

filesizes() {
  echo "--- log files"
  ls -l --time-style=+%H:%M "$BASE"/log/*.log 2>/dev/null | awk '{print $5, $6, $7}'
}

echo "===== 1. baseline ====="
snap baseline
filesizes
echo "--- health"
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
echo "--- pid"
cat run/status.pid

echo "===== 2. canary through the log pipeline ====="
MARK="validation-canary $(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "$MARK" >> log/deploy.log
echo "appended: $MARK"
sleep 50
snap after-canary
echo "--- canary line still local"
tail -n 2 log/deploy.log

echo "===== 3. outage: endpoint refuses connections ====="
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
print('endpoint now', config['endpoint'])
PY
sleep 50
snap during-outage
echo "--- console unaffected"
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
echo "--- spool contains no credential"
python - "$BASE" <<'PY'
import os, re, sys
path = os.path.join(sys.argv[1], 'run', 'telemetry-spool.ndjson')
if not os.path.exists(path):
    print('no spool')
else:
    text = open(path, encoding='utf-8', errors='replace').read()
    config = __import__('json').load(open(os.path.join(sys.argv[1], 'run', 'axiom.json')))
    print('spool_bytes', len(text))
    print('lines', len(text.splitlines()))
    print('has_token', bool(config['token']) and config['token'] in text)
    print('xaat', bool(re.search(r'xaa[tm]-', text)))
    print('bearer', bool(re.search(r'(?i)bearer\s', text)))
PY

echo "===== 4. recovery ====="
cat "$BACKUP" > "$CONFIG"
chmod 600 "$CONFIG"
rm -f "$BACKUP"
echo "endpoint restored"
sleep 50
snap after-recovery
filesizes

echo "===== 5. retention and integrity ====="
echo "--- dataset retention"
"$BASE/serverctl" telemetry-retention
echo "--- lowering without --force-lower"
"$BASE/serverctl" telemetry-retention --days 7 --apply; echo "exit=$?"
echo "--- sqlite integrity"
python - "$BASE" <<'PY'
import sqlite3, sys, os
con = sqlite3.connect('file:%s?mode=ro' % os.path.join(sys.argv[1], 'run', 'galaxy.db'), uri=True)
print(con.execute('pragma integrity_check').fetchone()[0])
print('tables', [row[0] for row in con.execute(
    "select name from sqlite_master where type='table' order by name")])
con.close()
PY

echo "===== 6. resource use over 60 s ====="
python - "$BASE" <<'PY'
import os, sys, time
base = sys.argv[1]
pid = int(open(os.path.join(base, 'run', 'status.pid')).read().strip())
ticks = os.sysconf('SC_CLK_TCK')

def read(pid):
    fields = open('/proc/%d/stat' % pid).read().rsplit(')', 1)[1].split()
    return int(fields[11]) + int(fields[12]), int(fields[21])

def threads(pid):
    out = {}
    for name in os.listdir('/proc/%d/task' % pid):
        try:
            fields = open('/proc/%d/task/%s/stat' % (pid, name)).read().rsplit(')', 1)[1].split()
            comm = open('/proc/%d/task/%s/comm' % (pid, name)).read().strip()
            out[comm] = int(fields[11]) + int(fields[12])
        except OSError:
            pass
    return out

def live():
    import json
    return json.load(open(os.path.join(base, 'run', 'telemetry-live.json')))

first, first_threads = read(pid), threads(pid)
first_live = live()
started = time.time()
time.sleep(60)
second, second_threads = read(pid), threads(pid)
second_live = live()
elapsed = time.time() - started
rss = [line for line in open('/proc/%d/status' % pid) if line.startswith('VmRSS')][0].split()[1]
print('interval_s', round(elapsed, 1))
print('process_cpu_pct', round((second - first) / ticks / elapsed * 100, 2))
telemetry_cpu = (second_threads.get('telemetry', 0) - first_threads.get('telemetry', 0))
print('telemetry_thread_cpu_pct', round(telemetry_cpu / ticks / elapsed * 100, 3))
print('rss_kb', rss)
delta_bytes = second_live['bytes_sent'] - first_live['bytes_sent']
delta_events = second_live['exported'] - first_live['exported']
print('bytes_delta', delta_bytes, 'events_delta', delta_events)
print('bytes_per_event', round(delta_bytes / delta_events, 1) if delta_events else None)
print('bytes_per_day_at_this_rate', round(delta_bytes / elapsed * 86400))
print('threads', sorted(second_threads))
PY

echo "===== 7. final ====="
snap final
echo "--- health"
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
echo "--- config mode"
ls -l run/axiom.json
echo DONE > /sdcard/Download/validate-phone.done
