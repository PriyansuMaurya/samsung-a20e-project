# Public access for galaxy.maurya.ie and small.maurya.ie

Status on 9 November 2026, 18:35 UTC: **working**. Both public names resolve to Cloudflare's edge and serve the site over HTTPS. The records are now proxied, and the connector installed on the phone was already healthy, so no further change was needed on the device.

## Verified

- Tunnel token installed at `~/.cloudflared/galaxy-token`, mode `600`, owner `u0_a282`, 248 bytes, CR characters stripped. It never appeared in chat, a shell prompt, or shell history: it was pushed over USB to shared storage, installed by a script, and the shared-storage copy was deleted.
- `cloudflared version 2026.10.7` runs as `cloudflared tunnel run --token-file ~/.cloudflared/galaxy-token`, pid 8960.
- Log shows all connectivity pre-checks passing and **four registered connections**: `ams08`, `ams07`, `dub01`, `dub02`, QUIC.
- The dashboard ingress config Cloudflare pushed to the connector is:

  ```json
  {"ingress":[{"hostname":"galaxy.maurya.ie","service":"http://127.0.0.1:8080"},
              {"hostname":"small.maurya.ie","service":"http://127.0.0.1:8080"},
              {"service":"http_status:404"}],"warp-routing":{"enabled":false}}
  ```

- On the phone, nginx listens on `0.0.0.0:8080` and `status.py` on `127.0.0.1:8765`, so the tunnel's target is valid.

## Route chosen on 9 November 2026: public IP, not a hostname

The owner asked for IP-only access, so the hostname route below is on hold. A tunnel cannot serve a bare IP; Cloudflare matches by hostname. The intended address is `http://37.228.243.220:8080`, which needs a TCP port-forward on the router (`192.168.0.1`): external 8080 to `192.168.0.74:8080`, plus a DHCP reservation so the phone keeps that address. The public IPv4 is not CGNAT, so the forward can work, and it is dynamic, so the address will change eventually.

The nginx source-address allow rule (`127.0.0.1` and `192.168.0.0/24`) was removed from `serverctl` so a forwarded connection, which arrives with the visitor's public source address, would not get a 403. That route is now moot: the tunnel works, and **every path on port 8080 requires HTTP basic auth** (user `galaxy`, `{SHA}` entry in `run/htpasswd`), so the absence of the address filter no longer exposes anything. Change the password on the phone with `~/galaxy-server/serverctl passwd`.

State of that route on 9 November 2026, 18:20 UTC: the DHCP reservation for `192.168.0.74` is in place and confirmed in the router's reserved list. **No port-forward rule exists**, because the Virgin Media Connect Box UI exposes only IPv6 port filtering under Security and no IPv4 forwarding control was found. External probes of `37.228.243.220:8080` from three continents time out, which is consistent with a missing rule and does not by itself prove ISP filtering. The egress address is a genuine public IPv4 in AS6830 (Liberty Global / Virgin Media, Clonee), the first hop beyond the hub is Virgin's own `ie-dub01a-rt3.aorta.net`, and no carrier-NAT hop (100.64.0.0/10 or RFC1918) appears, so CGNAT is unlikely and the hub UI is the likelier blocker. Next options: locate the forwarding page or the DMZ host setting on the hub, put the hub in modem mode behind an owned router, or fall back to the hostname tunnel / a mesh VPN.

## Resolved: the DNS records are now proxied

Both names were grey-clouded CNAMEs to the tunnel. A tunnel only serves traffic through the Cloudflare edge, so each record had to be **Proxied** (orange cloud). With the toggle on, the authoritative answer becomes Cloudflare anycast addresses instead of the unresolvable CNAME target, and the edge forwards to the connector. That was the only remaining blocker.

Current state, verified 9 November 2026 18:35 UTC:

```
small.maurya.ie   A  104.21.40.201, 172.67.138.145   (Cloudflare edge, proxied)
galaxy.maurya.ie  A  104.21.40.201, 172.67.138.145   (Cloudflare edge, proxied)
```

- `curl https://small.maurya.ie/` returns HTTP 200 and the real page (`GALAXY`, `Small device`, `Big possibilities`).
- `curl https://small.maurya.ie/api/status` returns live data: `{"uptime": "0d 2h", "memory_percent": 60, "storage_gb": 16.2}`.
- Independent HTTP checks from **three external nodes** (Austria, Canada, Russia) all returned `200 OK` against the Cloudflare addresses, so this is confirmed from outside the home network, not just from the LAN.

## Rollback

- Disable or delete the two public hostnames on the tunnel, or delete the two CNAME records.
- The pre-existing records replaced by the CNAMEs were `small.maurya.ie` → `216.198.79.65`, `64.29.17.1` (Vercel anycast; those addresses also serve `maurya.ie`). Restore them if `small` must go back to the earlier host.
- On the phone: `rm ~/.cloudflared/galaxy-token`, then `pkill -f 'cloudflared tunnel run'`. The supervisor stops restarting the connector once the file is gone.

## Operational notes

- The supervisor re-checks every 60 s and restarts `cloudflared` if it dies, so the connector survives as long as Termux does. It is still subject to Android killing the app; the battery whitelist and wake lock are what prevent that.
- The dashboard and `/api/*` are reachable from the internet through the tunnel hostnames only with the password; anonymous requests get 401. `/api/status` exposes uptime, memory with swap, storage, battery, temperatures, service PIDs and tunnel state. `/api/action` runs whitelisted `serverctl` subcommands and needs the same password plus a JSON POST.
- Stopping the server from the dashboard also stops the dashboard, so recovery needs the phone: `~/galaxy-server/serverctl start`. The supervisor honours `run/server-disabled` and `run/tunnel-disabled` and will not undo a deliberate stop.
- Rotation is a dashboard action: rotate the token, replace the file the same way (USB push → script install), and the supervisor picks up the new token when it restarts the connector.

References: [Cloudflare Tunnel setup](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/), [tunnel tokens](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/tunnel-run-parameters/), [published applications and DNS](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/routing-to-tunnel/dns/).
