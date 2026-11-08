#!/data/data/com.termux/files/usr/bin/bash
set -eu
umask 077
mkdir -p "$HOME/.cloudflared" "$HOME/.termux/boot" "$HOME/galaxy-server/log"
printf 'Paste the rotated Cloudflare tunnel token (input is hidden): '
IFS= read -r -s token
printf '\n'
if [ -z "$token" ]; then echo 'Token cannot be empty.' >&2; exit 1; fi
printf '%s' "$token" > "$HOME/.cloudflared/galaxy-token"
unset token
cat > "$HOME/.termux/boot/start-tunnel" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
exec cloudflared tunnel run --token-file "$HOME/.cloudflared/galaxy-token"
EOF
chmod 700 "$HOME/.termux/boot/start-tunnel"
nohup "$HOME/.termux/boot/start-tunnel" >> "$HOME/galaxy-server/log/tunnel.log" 2>&1 < /dev/null &
echo 'Tunnel started; check ~/galaxy-server/log/tunnel.log and the Cloudflare dashboard.'
