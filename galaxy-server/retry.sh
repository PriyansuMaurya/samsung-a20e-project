#!/data/data/com.termux/files/usr/bin/bash
printf '%s\n' 'deb https://mirrors.nevacloud.com/applications/termux/termux-main stable main' > "$PREFIX/etc/apt/sources.list"
bash /sdcard/Download/galaxy-server/setup.sh > /sdcard/Download/galaxy-install.log 2>&1
