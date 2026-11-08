#!/data/data/com.termux/files/usr/bin/bash
set -eu
cp /sdcard/Download/galaxy-server/status.py "$HOME/galaxy-server/status.py"
"$HOME/galaxy-server/serverctl" stop
"$HOME/galaxy-server/serverctl" start
