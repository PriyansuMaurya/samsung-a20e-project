#!/data/data/com.termux/files/usr/bin/bash
set -eu
cd "$(dirname "$0")"
pkg update -y
pkg upgrade -y
pkg install -y python git nginx curl
mkdir -p "$HOME/.termux/boot" "$HOME/galaxy-server/run" "$HOME/galaxy-server/log"
cp -R site status.py serverctl "$HOME/galaxy-server/"
chmod 700 "$HOME/galaxy-server/serverctl"
cat > "$HOME/.termux/boot/galaxy-server" <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
sleep 30
"$HOME/galaxy-server/serverctl" start
EOF
chmod 700 "$HOME/.termux/boot/galaxy-server"
"$HOME/galaxy-server/serverctl" start
