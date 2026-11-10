#!/usr/bin/env bash
# Verify the phone is reachable and the tunnel is healthy before blaming deploy.
set -euo pipefail
PHONE="https://galaxy.maurya.ie"
echo "==> probing $PHONE ..."
for path in "/" "/console" "/api/status" "/api/health"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 12 "$PHONE$path" || true)
  echo "$code  $path"
done
echo "==> Cloudflare-edge health probe endpoints served by the tunnel:"
for host in 104.21.40.201 172.67.138.145; do
  code=$(curl -s -o /dev/null -w "%{http_code}" -m 12 --resolve galaxy.maurya.ie:443:$host "https://galaxy.maurya.ie" || true)
  echo "$code  $host"
done
echo "==> tunnel ingress check (direct to edge, Host header):"
for host in 104.21.40.201 172.67.138.145; do
  body=$(curl -s -m 12 -H "Host: galaxy.maurya.ie" --resolve galaxy.maurya.ie:443:$host "http://$host" || true)
  echo "$host => $(printf '%s' "$body" | head -c 300)"
done
