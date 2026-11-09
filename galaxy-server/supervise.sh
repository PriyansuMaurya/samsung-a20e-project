#!/data/data/com.termux/files/usr/bin/bash
# Keeps the local server alive for as long as this process runs.
# Android reclaims cached app processes; a wake lock plus the battery
# whitelist in the README are what stop Termux and its children being killed.
# The health check goes straight to the status service on a loopback-only
# endpoint: the public paths need a session cookie and a 401 would look like an
# outage.
set -u
base="$HOME/galaxy-server"
mkdir -p "$base/run" "$base/log"

if [ -s "$base/run/supervisor.pid" ] && kill -0 "$(cat "$base/run/supervisor.pid")" 2>/dev/null; then
  echo "supervisor already running" >&2
  exit 0
fi
echo $$ > "$base/run/supervisor.pid"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  echo "$(date -Is) wake lock acquired"
else
  echo "$(date -Is) termux-wake-lock unavailable; expect Android to suspend the server"
fi

echo "$(date -Is) supervisor started (pid $$)"
while :; do
  # server-disabled is set by "serverctl stop" and by the dashboard's Stop button.
  if [ ! -e "$base/run/server-disabled" ] \
     && ! curl -fsS --max-time 5 http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
    echo "$(date -Is) server unhealthy; starting"
    "$base/serverctl" start
  fi
  # tunnel-disabled is set by "serverctl tunnel-stop" and by the dashboard toggle.
  if [ ! -e "$base/run/tunnel-disabled" ] \
     && [ -s "$HOME/.cloudflared/galaxy-token" ] \
     && ! pgrep -f 'cloudflared tunnel run' >/dev/null 2>&1; then
    echo "$(date -Is) tunnel down; starting"
    nohup cloudflared tunnel run --token-file "$HOME/.cloudflared/galaxy-token" \
      >> "$base/log/tunnel.log" 2>&1 < /dev/null &
  fi
  if [ -e "$base/run/autodeploy-enabled" ]; then
    python "$base/autodeploy.py" >> "$base/log/deploy.log" 2>&1
  fi
  sleep 15
done
