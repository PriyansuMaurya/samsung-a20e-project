#!/data/data/com.termux/files/usr/bin/bash
# Read-only capability audit. No photo, no microphone capture: those need the
# owner's consent and are deferred until the scope is agreed.
LOG=/sdcard/Download/device-audit.log
exec > "$LOG" 2>&1
echo "=== android ==="
getprop ro.build.version.release
getprop ro.product.model
getprop ro.product.device
getprop ro.build.version.sdk
echo "=== cpu / memory / storage ==="
nproc
grep -E 'MemTotal|MemAvailable' /proc/meminfo
df -h /data /sdcard 2>/dev/null
echo "=== termux packages ==="
pkg list-installed 2>/dev/null | grep -Ei 'termux|ffmpeg|python|openssl|curl' | head -20
echo "=== termux api calls present ==="
for cmd in termux-camera-info termux-camera-photo termux-microphone-record termux-torch \
           termux-vibrate termux-brightness termux-audio-info termux-media-player \
           termux-sensor termux-battery-status termux-wifi-connectioninfo \
           termux-telephony-deviceinfo termux-notification termux-toast termux-volume; do
  if command -v "$cmd" >/dev/null 2>&1; then echo "have $cmd"; else echo "MISSING $cmd"; fi
done
echo "=== cameras ==="
timeout 15 termux-camera-info 2>&1 | head -40
echo "=== sensors ==="
timeout 15 termux-sensor -l 2>&1 | head -20
echo "=== battery ==="
timeout 10 termux-battery-status 2>&1 | head -20
echo "=== audio state ==="
timeout 10 termux-audio-info 2>&1 | head -10
echo "=== media player ==="
timeout 10 termux-media-player info 2>&1 | head -5
echo "=== brightness (read only) ==="
settings get system screen_brightness 2>&1
settings get system screen_brightness_mode 2>&1
echo "=== wifi ==="
timeout 10 termux-wifi-connectioninfo 2>&1 | head -20
echo "=== telephony ==="
timeout 10 termux-telephony-deviceinfo 2>&1 | head -20
echo "=== torch, 400 ms, then off ==="
timeout 5 termux-torch on 2>&1; sleep 0.4; timeout 5 termux-torch off 2>&1; echo "torch exit=$?"
echo "=== vibrate, 200 ms ==="
timeout 5 termux-vibrate -d 200 -f 2>&1; echo "vibrate exit=$?"
echo "=== termux api app and its granted permissions ==="
pm list packages 2>/dev/null | grep -i termux
dumpsys package com.termux.api 2>/dev/null | sed -n '/grantedPermissions/,/^  [a-zA-Z]/p' | head -30
dumpsys package com.termux.api 2>/dev/null | grep -E 'versionName|targetSdk|firstInstallTime' | head -5
echo "=== who owns the server ==="
id
pgrep -af 'status.py|nginx|cloudflared' | head -6
echo "=== existing app routing (for adding endpoints) ==="
grep -n "path == '/api/" "$HOME/galaxy-server/status.py" | head -40
echo "=== upload space for recordings ==="
mkdir -p "$HOME/galaxy-server/media" && df -h "$HOME/galaxy-server/media" | tail -1
echo "=== screencap without root ==="
timeout 5 screencap -p /sdcard/Download/audit-screen.png 2>&1; echo "screencap exit=$?"
ls -l /sdcard/Download/audit-screen.png 2>&1
rm -f /sdcard/Download/audit-screen.png
echo DONE > /sdcard/Download/device-audit.done
