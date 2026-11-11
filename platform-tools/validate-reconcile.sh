#!/data/data/com.termux/files/usr/bin/bash
# Reconciles per-pipeline delivery against local records over a window whose
# counters are fresh at both ends (the live snapshot is written at most once a
# minute, so a stale snapshot would misalign any comparison).
LOG=/sdcard/Download/validate-reconcile.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1
BASE="$HOME/galaxy-server"

python - "$BASE" <<'PY'
import json, os, sqlite3, sys, time

base = sys.argv[1]
run = os.path.join(base, 'run')

def fresh(timeout=70):
    path = os.path.join(run, 'telemetry-live.json')
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = json.load(open(path))
        if time.time() - info['updated_at'] < 8:
            return info
        time.sleep(2)
    raise SystemExit('live counters never refreshed')

def snapshot():
    info = fresh()
    con = sqlite3.connect('file:%s?mode=ro' % os.path.join(run, 'galaxy.db'), uri=True)
    rows = {t: con.execute('select count(*) from "%s"' % t).fetchone()[0]
            for t in ('samples', 'checks', 'events', 'audits')}
    con.close()
    sizes = {}
    for name in ('access', 'auth', 'deploy', 'deploy-manual', 'nginx-error',
                 'status', 'supervisor', 'tunnel'):
        filename = 'manual-deploy.log' if name == 'deploy-manual' else name + '.log'
        path = os.path.join(base, 'log', filename)
        sizes[name] = os.path.getsize(path) if os.path.exists(path) else 0
    return {'at': time.time(), 'by_event': dict(info.get('by_event') or {}),
            'exported': info.get('exported'), 'bytes': info.get('bytes_sent'),
            'rows': rows, 'sizes': sizes}

first = snapshot()
print('window A', time.strftime('%H:%M:%S', time.localtime(first['at'])))
print('  exported', first['exported'], 'by_event', json.dumps(first['by_event'], sort_keys=True))
print('  rows', first['rows'])
print('  bytes', first['bytes'])
time.sleep(120)
second = snapshot()
print('window B', time.strftime('%H:%M:%S', time.localtime(second['at'])))
print('  exported', second['exported'], 'by_event', json.dumps(second['by_event'], sort_keys=True))
print('  rows', second['rows'])
print('  bytes', second['bytes'])
print('window_seconds', round(second['at'] - first['at'], 1))

local = {
    'galaxy.sample': second['rows']['samples'] - first['rows']['samples'],
    'galaxy.check': second['rows']['checks'] - first['rows']['checks'],
    'galaxy.event': second['rows']['events'] - first['rows']['events'],
    'galaxy.audit': second['rows']['audits'] - first['rows']['audits'],
}
delivered = {kind: second['by_event'].get(kind, 0) - first['by_event'].get(kind, 0)
             for kind in ('galaxy.sample', 'galaxy.check', 'galaxy.event',
                          'galaxy.audit', 'galaxy.log', 'galaxy.health')}
print('local_rows', json.dumps(local, sort_keys=True))
print('delivered', json.dumps(delivered, sort_keys=True))
for kind in ('galaxy.sample', 'galaxy.check', 'galaxy.event', 'galaxy.audit'):
    print('%-14s local=%-3d delivered=%-3d delta=%d' % (
        kind, local[kind], delivered[kind], delivered[kind] - local[kind]))
new_lines = 0
new_bytes = 0
for name, size in first['sizes'].items():
    if second['sizes'][name] <= size:
        continue
    filename = 'manual-deploy.log' if name == 'deploy-manual' else name + '.log'
    with open(os.path.join(base, 'log', filename), 'rb') as handle:
        handle.seek(size)
        added = handle.read(second['sizes'][name] - size)
    new_bytes += len(added)
    new_lines += added.count(b'\n')
print('new_log_lines', new_lines, 'new_log_bytes', new_bytes,
      'galaxy.log_delivered', delivered['galaxy.log'])
print('bytes_in_window', second['bytes'] - first['bytes'])
print('sessions_exported', 'session' in json.dumps(second['by_event']).lower())
PY
echo DONE > /sdcard/Download/validate-reconcile.done
