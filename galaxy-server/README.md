# Galaxy A20e local server

Termux, Termux:Boot, Python, Git, Nginx and cloudflared were installed on the connected phone. The homepage, assets and corrected status endpoint returned HTTP 200 from the Windows laptop. Reboot startup and public access have not been verified. The address at the time of inspection was `192.168.0.74`; addresses can change. Nginx listens on port 8080 and permits loopback and `192.168.0.0/24`. This is an address filter, not authentication: anyone on that subnet can view the site and status. Use only on a trusted network. The user has requested public access; see `PUBLIC-ACCESS.md` for its pending setup. If the network changes, update the allow rule in `serverctl` and restart. Binding on all interfaces means other interfaces can receive connections from matching source addresses.

## Phone prerequisites

Install Termux and Termux:Boot from the same source: [F-Droid Termux](https://f-droid.org/en/packages/com.termux/) and [Termux:Boot](https://f-droid.org/en/packages/com.termux.boot/). Open each app once. Allow Termux to run in the background in Samsung battery settings. Termux:Boot needs to be opened once before it can receive boot events. After reboot, Android may require the phone to be unlocked before startup. Test this on the device. The setup intentionally does not acquire a wake lock; if Android suspends the server, reliability and battery use need to be balanced.

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

The install script runs `pkg update -y`, `pkg upgrade -y`, and `pkg install -y python git nginx curl`; it installs site and service files under `~/galaxy-server`, writes `~/.termux/boot/galaxy-server`, and starts the services. Review the scripts before running. Termux and Termux:Boot are unmodified apps; no root is needed.

## Operations and updates

```sh
~/galaxy-server/serverctl status
~/galaxy-server/serverctl restart
~/galaxy-server/serverctl stop
~/galaxy-server/serverctl start
tail -n 50 ~/galaxy-server/log/nginx-error.log
tail -n 50 ~/galaxy-server/log/status.log
```

Restart validates the Nginx configuration and gracefully reloads it; it does not restart an already running status process. Stop uses Nginx's graceful quit. Nginx access logging is disabled to limit writes and avoid collecting client addresses; error and backend logs are stored in `~/galaxy-server/log`. They are not automatically rotated; inspect and truncate them periodically if they grow. No wake lock is used.

To update website files from Windows, edit `site`, then from this directory run:

```powershell
..\platform-tools\adb.exe push site/. /sdcard/Download/galaxy-site/
```

In Termux, after storage permission has been granted:

```sh
cp -R ~/storage/downloads/galaxy-site/. ~/galaxy-server/site/
```

Static changes appear on the next page load. This copy overwrites existing files but does not delete files removed on the laptop. Only transfer trusted files; shared storage is not private. Disconnect USB when done.

## Checks still required on the phone

1. Open `http://127.0.0.1:8080/` in the phone browser and `http://192.168.0.74:8080/` on a separate Wi-Fi device. The dashboard should show Online and numeric values. Run `curl -i http://127.0.0.1:8080/api/status` in Termux.
2. Run `ps -A -o PID,RSS,%CPU,ARGS | grep -E 'nginx|status.py'` if supported; Android may restrict process visibility. Record per-process RSS and CPU after several idle minutes. Use Android battery settings or a controlled multi-hour charge/discharge comparison for battery impact; a momentary CPU reading cannot establish battery consumption.
3. Test a restart and reboot, unlock the phone, wait at least a minute, and repeat the phone and second-device tests. Check battery restrictions and the error logs if startup fails. Reserve the phone's DHCP address on the router if desired, and adjust the URL if its IP changes.

Sources: [Termux installation](https://github.com/termux/termux-app#installation), [Termux:Boot instructions](https://github.com/termux/termux-boot).
