#!/data/data/com.termux/files/usr/bin/bash
# Pairs the native control app, then calls every endpoint it exposes. Runs on
# the phone, so the requests never leave loopback.
LOG=/sdcard/Download/device-app-test.log
exec > "$LOG" 2>&1
BASE="$HOME/galaxy-server"
PORT=8790
TOKEN_FILE="$BASE/run/device-token"
mkdir -p "$BASE/run"

fail() { echo "FAIL: $*"; echo FAILED > /sdcard/Download/device-app-test.done; exit 1; }

fresh() {
  date +%s > /dev/null
}

echo "=== generate pairing token ==="
python - "$TOKEN_FILE" <<'PY'
import os, secrets, sys
path = sys.argv[1]
token = secrets.token_hex(24)
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(descriptor, 'w') as handle:
    handle.write(token + '\n')
print('token length', len(token))
PY
TOKEN=$(cat "$TOKEN_FILE")

echo "=== wait for the control server ==="
for i in $(seq 1 20); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "=== unauthenticated call must be refused ==="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$PORT/capabilities")
echo "no token: $code"
[ "$code" = "401" ] || fail "unauthenticated request returned $code, expected 401"

echo "=== pair ==="
pair=$(curl -s --max-time 5 -X POST -H 'Content-Type: application/json' \
  -d "{\"token\":\"$TOKEN\"}" "http://127.0.0.1:$PORT/pair")
echo "pair: $pair"
echo "$pair" | grep -q '"paired":true' || fail "pairing rejected: $pair"

echo "=== second pair attempt with a different token is useless and ignored ==="
again=$(curl -s --max-time 5 -X POST -H 'Content-Type: application/json' \
  -d '{"token":"00000000000000000000000000000000"}' "http://127.0.0.1:$PORT/pair")
echo "pair again: $again"

echo "=== health ==="
curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health"; echo

echo "=== capabilities ==="
curl -fsS --max-time 8 -H "X-Galaxy-Token: $TOKEN" "http://127.0.0.1:$PORT/capabilities" | python -m json.tool | head -60

echo "=== battery ==="
curl -fsS --max-time 5 -H "X-Galaxy-Token: $TOKEN" "http://127.0.0.1:$PORT/battery"; echo

echo "=== volume before ==="
curl -fsS --max-time 5 -H "X-Galaxy-Token: $TOKEN" "http://127.0.0.1:$PORT/volume"; echo

echo "=== torch on, 1 s, off ==="
curl -fsS --max-time 5 -X POST -H "X-Galaxy-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"on":true}' "http://127.0.0.1:$PORT/torch"; echo
sleep 1
curl -fsS --max-time 5 -X POST -H "X-Galaxy-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"on":false}' "http://127.0.0.1:$PORT/torch"; echo

echo "=== vibrate 150 ms ==="
curl -fsS --max-time 5 -X POST -H "X-Galaxy-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ms":150}' "http://127.0.0.1:$PORT/vibrate"; echo

echo "=== wrong token refused ==="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  -H 'X-Galaxy-Token: not-the-token' "http://127.0.0.1:$PORT/battery")
echo "wrong token: $code"
[ "$code" = "401" ] || fail "wrong token returned $code, expected 401"

echo "=== app log tail ==="
logcat -d -s GalaxyDevice 2>/dev/null | tail -10
echo "PASS" > /sdcard/Download/device-app-test.done
