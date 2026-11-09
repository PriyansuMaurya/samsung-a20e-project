# Galaxy A20e local server

Termux, Termux:Boot, Python, Git, Nginx and cloudflared were installed on the connected phone. The homepage, assets and corrected status endpoint returned HTTP 200 from the Windows laptop. Reboot startup is verified; `https://small.maurya.ie` and `https://galaxy.maurya.ie` serve the site over HTTPS through the Cloudflare tunnel, confirmed by checks from outside the network on 9 November 2026 (see `PUBLIC-ACCESS.md`). The address at the time of inspection was `192.168.0.74`; addresses can change. Nginx listens on port 8080 and requires HTTP basic auth on every path, including the API, because the site is published through the tunnel. Credentials live in `run/htpasswd` (`serverctl passwd` rewrites them). Without the password the site answers 401 to LAN and internet clients alike. The user has requested public access; see `PUBLIC-ACCESS.md` for its pending setup.

## Verified state, 9 November 2026

Both services had stopped. The phone had been up for about 17 hours while the Termux process was only about 3.5 hours old and nothing was listening on 8080 or 8765, so Termux had been killed and restarted without the server. Android reclaims cached app processes; Termux was not exempt from battery optimisation, held no wake lock, and nothing restarted the services when it died.

Fixed and verified from the laptop:

- `com.termux` and `com.termux.boot` are in the battery-optimisation whitelist (`cmd deviceidle whitelist +com.termux`).
- `supervise.sh` holds `termux-wake-lock` and re-runs `serverctl start` within about a minute when `/api/status` stops answering. The boot hook now starts the supervisor instead of a one-shot `serverctl start`.
- Recovery was exercised: `serverctl stop` took the site down, and the supervisor restored it with new PIDs in under 60 seconds. Homepage, CSS, JavaScript and `/api/status` then returned HTTP 200.
- A real reboot was tested. Termux:Boot only fires after the first unlock of the secure lockscreen; about 84 seconds after unlock the site came back on its own with new PIDs and the API reporting `0d 0h` uptime.

Termux had also lost shared-storage access (`/sdcard/...` returned permission denied), so the `adb push` then `cp` workflow below did not work. It was restored with `pm grant com.termux android.permission.READ_EXTERNAL_STORAGE` and `appops set com.termux MANAGE_EXTERNAL_STORAGE allow`; neither survived on its own and both need re-checking if the documented workflow fails again. As a fallback, files can be served from the laptop over the LAN and fetched with `curl` inside Termux.

Startup after a real reboot is verified, but it depends on one unlock: the phone has a secure lockscreen, and Termux:Boot only receives the boot event afterwards.

## Phone prerequisites

Install Termux and Termux:Boot from the same source: [F-Droid Termux](https://f-droid.org/en/packages/com.termux/) and [Termux:Boot](https://f-droid.org/en/packages/com.termux.boot/). Open each app once. Allow Termux to run in the background in Samsung battery settings. Termux:Boot needs to be opened once before it can receive boot events. After reboot, Android may require the phone to be unlocked before startup; the current phone has a secure lockscreen, so nothing starts until it is unlocked. `supervise.sh` holds a partial wake lock, which keeps the server answering at some battery cost. If battery life matters more than uptime, delete the `termux-wake-lock` call and accept that Android may suspend the server.

From this directory on Windows, copy the project to shared storage:

```powershell
..\platform-tools\adb.exe push . /sdcard/Download/galaxy-server
```

In Termux, grant shared-storage permission when prompted, copy the files into Termux private storage and install packages:

```sh
termux-setup-storage
cp -R ~/storage/downloads/galaxy-server ~/galaxy-install
bash ~/galaxy-install/setup.sh
```

The install script runs `pkg update -y`, `pkg upgrade -y`, and `pkg install -y python git nginx curl`; it installs site and service files under `~/galaxy-server`, writes `~/.termux/boot/galaxy-server` to launch `supervise.sh`, starts the server, and starts the supervisor. Review the scripts before running. Termux and Termux:Boot are unmodified apps; no root is needed. Grant the battery-optimisation exemption once per install: `adb shell cmd deviceidle whitelist +com.termux` and `adb shell cmd deviceidle whitelist +com.termux.boot`. Without it and the wake lock, Android kills Termux and the server goes down.

## Operations and updates

```sh
~/galaxy-server/serverctl status
~/galaxy-server/serverctl restart
~/galaxy-server/serverctl stop
~/galaxy-server/serverctl start
tail -n 50 ~/galaxy-server/log/nginx-error.log
tail -n 50 ~/galaxy-server/log/status.log
tail -n 50 ~/galaxy-server/log/supervisor.log
~/galaxy-server/supervise.sh
```

`supervise.sh` is a long-running loop, not a one-shot: it takes the wake lock, writes `run/supervisor.pid`, rechecks the status service on `127.0.0.1:8765` every 60 seconds, and exits immediately if another copy is already running. It skips both the server and the tunnel while their disable flags exist, so a deliberate stop from the dashboard stays stopped.

Restart restarts the status process and gracefully reloads Nginx, so code changes in `status.py` take effect. Stop writes `run/server-disabled`, quits Nginx and kills the status process, and the supervisor then leaves the server alone; `serverctl start` clears that flag. Tunnel control is separate: `serverctl tunnel-stop` writes `run/tunnel-disabled` and kills the connector, `tunnel-start` clears the flag and starts it, `tunnel-restart` does both. Nginx access logging is disabled to limit writes and avoid collecting client addresses; error and backend logs are stored in `~/galaxy-server/log`. They are not automatically rotated; inspect and truncate them periodically if they grow.

To update website files from Windows, edit `site`, then from this directory run:

```powershell
..\platform-tools\adb.exe push site/. /sdcard/Download/galaxy-site/
```

In Termux, after storage permission has been granted:

```sh
cp -R ~/storage/downloads/galaxy-site/. ~/galaxy-server/site/
```

Static changes appear on the next page load. This copy overwrites existing files but does not delete files removed on the laptop. Only transfer trusted files; shared storage is not private. Disconnect USB when done.

## Dashboard and control API

The site at `/` is a control dashboard: uptime, memory (with swap), storage, battery and temperature, a per-service process table, live tunnel state from the connector's own `/ready` endpoint, log viewing and control buttons.

| Piece | Where |
|---|---|
| Dashboard markup, styles, logic | `site/index.html`, `site/style.css`, `site/app.js` |
| Status, logs and actions | `status.py` on `127.0.0.1:8765`, proxied by Nginx under `/api/` |
| Password | `run/htpasswd`, `{SHA}` format, set with `~/galaxy-server/serverctl passwd` |

Actions are whitelisted serverctl subcommands; the request body is never passed to a shell, and `/api/action` accepts POST with `application/json` only. Valid actions: `server-restart`, `server-stop`, `tunnel-start`, `tunnel-stop`, `tunnel-restart`. Log names are whitelisted to `tunnel`, `supervisor`, `nginx-error` and `status`, capped at 200 lines from the last 64 KB of the file.

Stopping the server takes the dashboard down with it, because Nginx serves the dashboard. The way back is the phone: `~/galaxy-server/serverctl start`.

Battery and temperature come from `termux-battery-status`, which needs both the Termux:API app and the `termux-api` package. Without them the tile reports `n/a` and everything else still works. Android blocks `/proc/uptime`, `/proc/loadavg`, `/sys/class/power_supply` and `/sys/class/net` from a non-root app, so uptime comes from `CLOCK_BOOTTIME` and load average is not shown at all.

## Checks still required on the phone

1. Open `http://127.0.0.1:8080/` in the phone browser and `http://192.168.0.74:8080/` on a separate Wi-Fi device. The dashboard should show Online and numeric values. Run `curl -i http://127.0.0.1:8080/api/status` in Termux.
2. Run `ps -A -o PID,RSS,%CPU,ARGS | grep -E 'nginx|status.py'` if supported; Android may restrict process visibility. Record per-process RSS and CPU after several idle minutes. Use Android battery settings or a controlled multi-hour charge/discharge comparison for battery impact; a momentary CPU reading cannot establish battery consumption.
3. Re-test from a second Wi-Fi device, and from the phone browser, after a restart. Check battery restrictions and the error logs if startup fails. Reserve the phone's DHCP address on the router if desired, and adjust the URL if its IP changes.

Sources: [Termux installation](https://github.com/termux/termux-app#installation), [Termux:Boot instructions](https://github.com/termux/termux-boot).
