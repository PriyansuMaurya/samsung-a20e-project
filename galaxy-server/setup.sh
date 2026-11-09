#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$(dirname "$0")"
pkg update -y
pkg upgrade -y
pkg install -y python git nginx curl termux-api
mkdir -p "$HOME/.termux/boot" "$HOME/galaxy-server/run" "$HOME/galaxy-server/log" "$HOME/galaxy-server/priv"
cp -R site status.py serverctl supervise.sh autodeploy.py "$HOME/galaxy-server/"
cp site/console.html "$HOME/galaxy-server/priv/console.html"
chmod 600 "$HOME/galaxy-server/priv/console.html"
chmod 700 "$HOME/galaxy-server/serverctl" "$HOME/galaxy-server/supervise.sh"
cat > "$HOME/.termux/boot/galaxy-server" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
sleep 30
nohup "$HOME/galaxy-server/supervise.sh" >> "$HOME/galaxy-server/log/supervisor.log" 2>&1 < /dev/null &
EOF
chmod 700 "$HOME/.termux/boot/galaxy-server"
"$HOME/galaxy-server/serverctl" start
nohup "$HOME/galaxy-server/supervise.sh" >> "$HOME/galaxy-server/log/supervisor.log" 2>&1 < /dev/null &
