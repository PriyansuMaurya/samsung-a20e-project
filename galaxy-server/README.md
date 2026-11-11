# Galaxy A20e local server

Termux, Termux:Boot, Python, Git, Nginx and cloudflared were installed on the connected phone. The homepage, assets and corrected status endpoint returned HTTP 200 from the Windows laptop. Reboot startup is verified; `https://small.maurya.ie` and `https://galaxy.maurya.ie` serve the site over HTTPS through the Cloudflare tunnel, confirmed by checks from outside the network on 9 November 2026 (see `PUBLIC-ACCESS.md`). The address at the time of inspection was `192.168.0.74`; addresses can change. Nginx listens on port 8080. The landing page and its assets are public; the console, its metrics, logs and controls sit behind a session sign-in, and the API answers 401 to anyone without a valid cookie. The console password lives in `run/htpasswd` (`serverctl passwd` rewrites it). See `PUBLIC-ACCESS.md` for how the public hostnames are published.

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

Pushes to GitHub's `master` branch run `.github/workflows/deploy.yml`. GitHub Actions validates syntax and runs the terminal and deployment tests on its own runner. Only after those checks pass does it send an HMAC-SHA256 signed deployment request to `/api/deploy/github`. The phone fetches the exact tested commit using a repository-specific read-only SSH deploy key. Direct push webhooks are ignored. The phone never polls GitHub, which avoids idle network and CPU work.

Actions retries notifications and checks the live revision for up to four minutes during a deployment. If the phone is offline longer, the workflow fails; rerun the failed workflow from GitHub Actions when the phone is available. There is no background deployment polling or self-hosted Actions runner on the phone.

`autodeploy.py` validates Python and shell syntax, installs the site and private console shell, restarts the service, and checks local health. It restores the previous files if installation or startup fails. Runtime credentials, the database, logs and tunnel configuration are preserved. Deployment pauses while the server is deliberately stopped.

Configuration lives in `~/galaxy-server/run`: `deploy-key`, `deploy-secret`, `autodeploy-enabled`, and `deployed-revision`. Keep the key and signing secret private. GitHub Actions uses the matching repository secret `PHONE_DEPLOY_SECRET`; the old direct push webhook is disabled. Remove `autodeploy-enabled` to pause deployment; recreate it with `touch` to resume. Inspect `log/deploy.log` for failures and `run/deployed-revision` for the live commit. GitHub repository settings contain the `galaxy-phone-autodeploy` read-only deploy key. A fresh phone installation requires provisioning these credentials before enabling deployment.

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

`supervise.sh` is a long-running loop, not a one-shot: it takes the wake lock, writes `run/supervisor.pid`, rechecks the status service on `127.0.0.1:8765` every 60 seconds, and exits immediately if another copy is already running. It skips both the server and the tunnel while their disable flags exist, so a deliberate stop from the dashboard stays stopped. It performs no GitHub polling.

Restart restarts the status process and gracefully reloads Nginx, so code changes in `status.py` take effect. Stop writes `run/server-disabled`, quits Nginx and kills the status process, and the supervisor then leaves the server alone; `serverctl start` clears that flag. Tunnel control is separate: `serverctl tunnel-stop` writes `run/tunnel-disabled` and kills the connector, `tunnel-start` clears the flag and starts it, `tunnel-restart` does both. Nginx access logging keeps failures only (`access_log ... if=` on 4xx/5xx): the method, path and status, without query strings or client addresses, so a console poll that succeeds writes nothing to flash. Error and backend logs are stored in `~/galaxy-server/log`. They are not automatically rotated; inspect and truncate them periodically if they grow.

To update the site from Windows, push `site/` and the Python files, then restart:

```powershell
..\platform-tools\adb.exe push site/. /sdcard/Download/galaxy-site/
..\platform-tools\adb.exe push status.py /sdcard/Download/galaxy-status.py
..\platform-tools\adb.exe push telemetry.py /sdcard/Download/galaxy-telemetry.py
```

In Termux, after storage permission has been granted:

```sh
cp -R ~/storage/downloads/galaxy-site/. ~/galaxy-server/site/
cp ~/storage/downloads/galaxy-status.py ~/galaxy-server/status.py
cp ~/storage/downloads/galaxy-telemetry.py ~/galaxy-server/telemetry.py
cp ~/galaxy-server/site/console.html ~/galaxy-server/priv/console.html
~/galaxy-server/serverctl restart
```

The console shell lives in `priv/` so it is not served as a static file; keep the copy there in step with `site/console.html`. Static changes appear on the next page load, and Python changes need the restart. Only transfer trusted files; shared storage is not private. Disconnect USB when done.

## Browser terminal

The console's Terminal view opens an interactive shell as the Termux user, not root. It supports terminal control keys, resize and full-screen programs, subject to the packages and permissions available on the phone. Set a separate terminal password on the phone before using it:

```sh
~/galaxy-server/serverctl terminal-passwd
```

Use at least 16 characters. Dashboard sign-in and the terminal password are both required, including on the home network. The terminal is available through the public console once configured; anyone with both credentials can run commands as the Termux user. Close the session when finished. Sessions also close on sign-out or after ten minutes without input. Commands and terminal output are not recorded in the audit log.

Screen reader mode is off by default to keep large terminal output responsive. Enable **Screen reader mode** in the terminal toolbar for accessible terminal output; it takes effect immediately and the browser remembers the choice when local storage is available.

Switching between dashboard views keeps every terminal shell and its output stream alive, including commands running while another view is open. Returning to Terminal restores the output and fits the active shell to the visible view. **+ New shell** opens up to four shells, each with its own tab, stream and scrollback; **Close shell** ends the tab you are on.

A browser reload reattaches to the shells the page had open, without asking for the terminal password again: the page remembers the shell ids for that browser tab and the server replays recent output for the redraw. A shell that is opened after a reload asks for the password once, and it is kept in memory only. A dropped output stream reattaches on its own rather than closing the tab, so a tunnel blip does not cost the session. Shells close after ten minutes without input, whether or not the page is still attached. Dashboard navigation also preserves log filters, the auto-refresh choice, chart range, unsaved probe settings and each view's scroll position.

## Console and control API

`/` is the public landing page. `/console` is the admin console: a signed-in single-page app with nine views — Overview, Metrics, Services, Networking, Device, Music, Logs, Terminal and Settings — reached through `/login` and left with `/logout`. Nginx serves the static landing page from `site/` and proxies `/console`, `/login`, `/logout` and `/api/*` to `status.py` on `127.0.0.1:8765`; nothing else is routed. The session cookie is `HttpOnly`, `SameSite=Lax`, `Secure` behind the tunnel and expires after 12 hours, and every private endpoint re-checks it server-side, so a missing or expired cookie gets 401 instead of the page.

| Piece | Where |
|---|---|
| Landing page | `site/index.html`, `site/style.css` |
| Console shell, styles, logic | `priv/console.html`, `site/console.css`, `site/console.js` |
| Status, charts, logs, actions | `status.py` on `127.0.0.1:8765` |
| Password | `run/htpasswd`, `{SHA}` format, set with `~/galaxy-server/serverctl passwd` |

Endpoints, all session-checked unless noted: `GET /api/status` (health, the five component indicators, KPIs, services, tunnel and probe state), `GET /api/timeline?range=1h|24h|7d`, `GET /api/logs?service=&severity=&q=&limit=`, `GET /api/events`, `GET /api/music/search?q=&category=`, `GET /api/telemetry`, `GET`/`POST /api/settings`, `POST /api/action`, `POST /api/login`, `POST /api/logout`, and the device routes described below (`/api/device/*`). `GET /api/health` is loopback-only and refuses any request carrying nginx's proxy header, so it answers `serverctl status` and the supervisor and returns 404 to everyone else.

`status.py` samples every 30 seconds into `run/galaxy.db`: raw samples are kept for 7 days and pruned, and the 24-hour and 7-day views aggregate those rows into 5- and 30-minute buckets in SQL. A bucket with no sample is returned as a gap, never as a zero, so the charts show missing data as missing. Health is computed server-side from memory, storage, battery, service states, tunnel readiness and the HTTPS probe, and returned as `health` plus `alerts`; values older than four sample intervals are marked stale and a component with no reading is reported as not monitored.

The 24-hour health timeline is the main chart: nginx, the tunnel and public HTTPS each get a row, with markers for reboots, service restarts and disconnects taken from the event log. CPU, memory, battery (with charging bands) and free storage are separate line charts. No external probe integration is configured, so public HTTPS is **Not monitored**. The separate hostname checks run from the phone and do not establish external availability.

Actions are whitelisted serverctl subcommands; nothing from a request reaches a shell, `/api/action` accepts `application/json` only, and cross-origin POSTs are refused. Valid actions: `server-restart`, `server-stop`, `tunnel-start`, `tunnel-stop`, `tunnel-restart`. The server and tunnel actions drop the connection that carried the request, so they are scheduled a second later and the reply is sent first. Actions are recorded in `log/auth.log` and the audit table; actions that disconnect the console are recorded as scheduled, not as verified outcomes.

Logs are read from whitelisted names — `nginx-error`, `tunnel`, `supervisor`, `status` and `auth` — searched by term and severity, capped per request, and passed through `redact()` so tunnel tokens, passwords and cookies never reach the browser. `auth.log` rotates itself at 64 KB.

Per-service numbers come from `/proc/<pid>/status` (`VmRSS`) and `/proc/<pid>/stat` field 22 (start time in clock ticks, subtracted from `CLOCK_BOOTTIME`), with restarts counted from the event log over 24 hours and the newest matching error line from that service's log. Tunnel numbers come from the connector's own Prometheus endpoint on `127.0.0.1:20241`: `cloudflared_tunnel_total_requests`, `cloudflared_tunnel_request_errors`, `cloudflared_tunnel_response_by_code`, `go_goroutines` and `go_memstats_alloc_bytes`. Android blocks `/proc/stat`, `/proc/loadavg` and `/proc/net/dev` from a non-root app, so CPU is reported for the server processes only and there is no network throughput — the console labels both rather than inventing numbers.

Stopping the server takes the console down with it, because Nginx serves the console. The way back is the phone: `~/galaxy-server/serverctl start`.

Battery and temperature come from `termux-battery-status`, which needs both the Termux:API app and the `termux-api` package. Without them the cards report `n/a` and everything else still works. Android blocks `/proc/uptime`, `/proc/loadavg`, `/sys/class/power_supply` and `/sys/class/net` from a non-root app, so uptime comes from `CLOCK_BOOTTIME` and load average is not shown at all.

## Device control

The **Device** view drives a small companion app (`android/device-control`) that owns the camera, microphone, flashlight, vibration, brightness and speaker. Termux:API cannot do this: it exposes stills, file recording and vibration, but no camera preview, no live microphone and no brightness control.

- The app listens on `127.0.0.1:8790` only and requires a pairing token. `status.py` stores that token in `run/device.json` (mode 0600) and proxies `/api/device/*` to it with the console session as the only gate, so the browser never holds the token and nothing on WiFi can reach the app directly.
- Included: live JPEG preview with camera/size/rate selection, snapshots and a gallery, 720p30 MP4 video recorded with sound *while* the preview keeps streaming (Camera2 shares one session between the JPEG stream and the recorder), screen rotation, flashlight, vibration, brightness, per-stream volume, live microphone listening in the tab (Web Audio over a WAV/PCM stream), 16 kHz WAV recordings you can download, play on the phone or delete, media upload that plays on the phone speaker, plus battery, storage, sensor and read-only connectivity readouts (transport, addresses, link speed, RSSI, Bluetooth and airplane state; the app cannot switch radios).
- Still absent: a live browser-to-phone audio channel, because the console does not capture the browser microphone. Phone-to-browser audio is live, and uploaded audio plays on the phone while the microphone streams.
- Actions are allowlisted (`torch`, `vibrate`, `brightness`, `volume`, camera start/stop/snapshot, record start/stop, media play/control/delete, photo/recording delete) and each one is written to the audit log as `device-<action>`.

`DEVICE-CONTROL.md` has the hardware audit, every endpoint, and the Android restrictions behind each omission.

## Music player

The **Music** view searches the free [Jamendo](https://devportal.jamendo.com/) catalogue and plays Creative Commons tracks **on the phone speaker**, not in the browser. A search box covers track and artist names, and the chips filter by genre (`rock`, `pop`, `electronic`, `hiphop`, `jazz`, `classical`, `lounge`, `world`); with no search the results are ordered by popularity.

Create a free application at [devportal.jamendo.com](https://devportal.jamendo.com/) and store its client id on the phone. The key is asked for without echo and written to `run/jamendo.json` (mode 0600):

```sh
~/galaxy-server/serverctl jamendo-key
```

Only the search request passes through `status.py` (`/api/music/search`), which calls `https://api.jamendo.com/v3.0/tracks/` with `namesearch`, `fuzzytags`, `imagesize=200` and `limit=10`, and returns just the fields the player draws — name, artist, album, duration, cover `image` and stream `audio` URL. The client id and Jamendo's large `waveform` payload never reach the browser.

Pressing play sends the track's `audio` URL to the phone app (`POST /api/device/control` with `music-play`, which becomes `/media/play-url` on the app). The app streams it with `MediaPlayer`, so the sound comes out of the phone and the browser never decodes audio. The now-playing panel is a remote: it polls `/api/device/audio` for state and position, sends `media-control` for pause, resume and stop, and sets the phone's media volume. Both `status.py` and the app accept only `https://*.jamendo.com` stream URLs, so a compromised session cannot point the player at an address on the home network. Cover art still loads in the browser from Jamendo's host, the only third party the console's `Content-Security-Policy` allows, and `connect-src` remains `'self'`. Playback continues while other views are open and after the browser tab closes; a stream the phone cannot open is reported on its card and in the panel instead of leaving a dead control.

## Remote telemetry (Axiom)

The server had no remote telemetry sink before this change: samples, events and logs live on the phone. `telemetry.py` adds an optional Axiom export alongside them. Local files and SQLite stay authoritative, so an Axiom outage does not change the console or lose operational data.

- `run/axiom.json` (mode 0600) holds the edge endpoint, dataset, API token and retention. Deployment preserves `run/`.
- Batches of up to 100 structured events are posted as NDJSON, with retries, a bounded disk spool and redaction before anything leaves the phone. Retention is a dataset setting and lowering it deletes older Axiom data, so the command refuses without `--force-lower`.
- Forwarded sources: the five operational logs plus `deploy.log` and `manual-deploy.log`, `events`, `audits` (structured), and the `samples` and `checks` tables. Samples and checks are emitted live, so history only fills in rows written before forwarding started; `history_batch` bounds a catch-up to 200 rows per cycle. The `sessions` table, `run/htpasswd`, `run/terminal.passwd`, `run/deploy-key` and `run/deploy-secret` are never read.
- The first start with no cursor forwards existing `events` rows and the current log files from the beginning; offsets only advance for lines that were queued.
- `serverctl telemetry-setup`, `telemetry-test`, `telemetry-status`, `telemetry-doctor`, `telemetry-retention`, `telemetry-backfill`. `telemetry-doctor` reports the dataset lookup, posts a probe event to every Axiom edge region and names the one that accepts it; `--apply` writes that endpoint into the config, and `--create` creates the dataset when the token is allowed to.
- The Settings view shows export state from `/api/telemetry`; the token is never returned.

See `AXIOM.md` for the audit, event schema, dashboards and rollback.

## Checks still required on the phone

1. Open `http://127.0.0.1:8080/` in the phone browser and `http://192.168.0.74:8080/` on a separate Wi-Fi device. The landing page should load without a password; `/console` should ask for one and then show live values, the health timeline and the service table. In Termux, `curl -i http://127.0.0.1:8080/api/status` should return 401 and `~/galaxy-server/serverctl status` should return the health JSON.
2. Run `ps -A -o PID,RSS,%CPU,ARGS | grep -E 'nginx|status.py'` if supported; Android may restrict process visibility. Record per-process RSS and CPU after several idle minutes. Use Android battery settings or a controlled multi-hour charge/discharge comparison for battery impact; a momentary CPU reading cannot establish battery consumption.
3. Re-test from a second Wi-Fi device, and from the phone browser, after a restart. Check battery restrictions and the error logs if startup fails. Reserve the phone's DHCP address on the router if desired, and adjust the URL if its IP changes.

Sources: [Termux installation](https://github.com/termux/termux-app#installation), [Termux:Boot instructions](https://github.com/termux/termux-boot).
