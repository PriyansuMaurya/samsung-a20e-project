#!/data/data/com.termux/files/usr/bin/bash
LOG=/sdcard/Download/final-check.log
exec > "$LOG" 2>&1
cd "$HOME/galaxy-server" || exit 1
echo "revision $(cat run/deployed-revision)"
"$HOME/galaxy-server/serverctl" telemetry-status
echo "--- health"
curl -fsS --max-time 5 http://127.0.0.1:8765/api/health; echo
echo "--- state file"
python - <<'PY'
import json, os
state = json.load(open(os.path.expanduser('~/galaxy-server/run/telemetry-state.json')))
print(json.dumps({k: state.get(k) for k in ('event_cursor', 'audit_cursor',
                                            'sample_cursor', 'check_cursor',
                                            'history_floor')}, sort_keys=True))
print('logs', sorted(state.get('logs') or {}))
print('spool', os.path.exists(os.path.expanduser('~/galaxy-server/run/telemetry-spool.ndjson')))
PY
echo DONE > /sdcard/Download/final-check.done
