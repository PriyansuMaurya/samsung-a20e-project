# Axiom telemetry integration

This is the audit of the Galaxy A20e server before the change, the design of the
Axiom export, the migration risks and the operational checks. The local log
files and SQLite database remain the operational source of truth; Axiom is an
additional, remote sink. The integration is disabled until a token is
configured.

## 1. Audit of the application before the change

### Architecture

| Piece | Where | Notes |
| --- | --- | --- |
| Public site | `site/` served by nginx on `:8080` | Static landing page and assets |
| Console | `priv/console.html`, `site/console.js`, `site/console.css` | Session-checked SPA, seven views |
| API | `status.py` on `127.0.0.1:8765` | `http.server.ThreadingHTTPServer`, stdlib only |
| Edge | `cloudflared tunnel run` | Prometheus metrics on `127.0.0.1:20241` |
| Supervision | `supervise.sh` | Wake lock, 60 s health loop, no GitHub polling |
| Deployment | `.github/workflows/deploy.yml` + `autodeploy.py` | Signed, tested commits only, rollback on failure |

There is no application framework, database driver or third-party Python
package. Python 3 standard library, nginx, cloudflared and `termux-api` are the
runtime. Playwright is a development-only dependency for the browser test.

### Data flows

1. `sampler()` runs every 30 s: memory and storage, battery (cached 15 s),
   cloudflared metrics, process table scan, then one row in `samples`.
2. Every fourth tick (2 min) the two hostnames are probed from the phone and
   written to `checks`; results are not an external availability signal.
3. `add_event()` records restarts, reboots and tunnel transitions in `events`.
4. `add_audit()` writes `log/auth.log` and the `audits` table.
5. The console polls `/api/status` every 10 s while visible, `/api/timeline`
   on demand, `/api/logs` on demand. All private endpoints re-check the session
   cookie server side.
6. `live_payload()` recomputes health from the latest sample, service state,
   tunnel readiness and probe results; stale samples are labelled, not zeroed.

### Logging, metrics, events, storage, dashboards

| Signal | Current store | Retention / limits |
| --- | --- | --- |
| Samples | `run/galaxy.db` `samples` | 7 days raw, 30 s interval |
| Events | `run/galaxy.db` `events` | 30 days |
| Audits | `run/galaxy.db` `audits` + `log/auth.log` | 30 days, auth log rotates at 64 kB |
| Host checks | `run/galaxy.db` `checks` | 3 days |
| Sessions | `run/galaxy.db` `sessions` | 12 hours |
| Service logs | `log/{nginx-error,tunnel,supervisor,status,auth}.log` | No rotation except `auth.log` |
| Tunnel metrics | scraped from cloudflared, not persisted | live only |
| Process metrics | `/proc/<pid>/status`, `/proc/<pid>/stat` | live only |

The console is the only dashboard: Overview, Metrics, Services, Networking,
Logs, Terminal, Settings. It shows gaps for missing samples and labels CPU as
server-process CPU because Android blocks `/proc/stat`, `/proc/loadavg` and
`/proc/net/dev` for a non-root app. Public HTTPS is reported as
**Not monitored**: there is no external probe.

### Existing remote telemetry storage

**None.** The application had no remote telemetry sink. The Cloudflare tunnel
carries console and API traffic, not telemetry, and the Samsung/Google
telemetry packages in `installed-packages.txt` are operating-system components
outside this application. This integration therefore adds the first remote
sink; it does not replace a working remote store. "Replace the existing remote
storage" is implemented as a cut-over of the *export path* only: Axiom becomes
the remote store while local files and SQLite continue to serve the console and
survive an Axiom outage.

### Performance bottlenecks and risks found

1. `/api/status` calls `tunnel()` on every poll (two HTTP requests to
   cloudflared, 3-4 s timeouts) with no cache. Multiple open consoles multiply
   it. Left unchanged to keep this change scoped; the telemetry thread never
   calls it.
2. `/api/logs` reads the last 96 kB of a file and runs three regexes plus blob
   detection per line on every request. Bounded, but CPU grows with log size.
3. SQLite writes are serialised by `DB_LOCK`; the sampler, auth and console
   writes share it. Adding a second writer in the API would be a regression, so
   the exporter only reads the database.
4. Log files do not rotate; an always-on exporter increases the importance of
   external retention setting.
5. `checks` has no index beyond its `(ts, host)` primary key, and timeline
   aggregation scans the range; it is bounded by 7 days of 30 s samples.
6. `scan_processes()` reads every `/proc/*/cmdline` once per sample and once per
   20 s services cache window. Acceptable at this scale.

### Security review notes relevant to this change

- The console password is `{SHA}` (unsalted SHA-1) in `run/htpasswd`. This is an
  existing weakness, unchanged here; the terminal password already uses
  PBKDF2. Rotating the console password remains a follow-up.
- Redaction exists for console log reads and is reused in spirit here. The
  exporter applies its own scrubbing before anything leaves the phone,
  including nested `token`, `password`, `secret`, `api_key` and `authorization`
  fields, bearer tokens, JWTs and Axiom token prefixes.
- Actions are whitelisted; no request value reaches a shell. The exporter adds
  no inbound endpoint that accepts a URL, dataset or token from a request.

## 2. Design

`telemetry.py` is a deep module with a small interface:

```python
client = Telemetry(run_dir)                 # reads run/axiom.json, mode 0600
client.emit(event, entry_point='sampler')   # non-blocking, bounded queue
client.start(LOG_FILES, LOG_DIR, DB_PATH)   # forward new log bytes and events
client.flush()                              # one batch, with retries
client.status()                             # JSON-safe, never the token
client.backfill(LOG_FILES, LOG_DIR, DB_PATH)  # logs, events, samples, checks, audits
client.apply_retention(days)
```

### Events

All events are NDJSON objects with `_time` (UTC, ISO 8601), `event`, `source`
(`galaxy-server`), `device` (`galaxy-a20e`) and `entry_point`
(`sampler`, `api`, `forwarder`, `cli`).

| `event` | Fields |
| --- | --- |
| `galaxy.sample` | memory, swap, storage, battery, temperature, CPU, per-service booleans, tunnel counters (`tunnel_connections`, `tunnel_requests`, `tunnel_errors`) |
| `galaxy.log` | `service`, `severity`, `message`, timestamp parsed from the line; for `service=access`, severity follows the HTTP status (5xx `error`, 4xx `warn`) |
| `galaxy.event` | `kind` (`restart`, `down`, `reboot`, `tunnel-up`, `tunnel-down`, `admin`), `detail`, `service` |
| `galaxy.check` | `host`, `ok`, `status`, `ms`, `error` |
| `galaxy.audit` | `user`, `action`, `outcome`, `detail`; one per console action, from the `audits` table |
| `galaxy.health` | `health` (`ok`/`warn`/`crit`), `alerts`; emitted by the sampler on change plus a 5 min heartbeat, so a phone nobody is watching still reports trouble |

### History catch-up and release logs

All eight operational logs, including `deploy.log`, `manual-deploy.log` and the
failed-request `access.log`, are forwarded as `galaxy.log`. Samples and checks are emitted live as they happen,
so their history is only the rows written **before** forwarding began: the
exporter records a `history_floor` once and only sends rows older than it, which
is what keeps a row from arriving twice. `history_since` in the config can set
that floor explicitly when enabling history on an installation that has already
been exporting live. `history_batch` (default 200 rows per cycle) bounds the CPU
and network a catch-up may use; a 46-hour backlog drains in about ten minutes
and then costs nothing. Audit rows have no live event, so their id cursor simply
advances each cycle. The `sessions` table is never read: it holds sign-in tokens.

A live `galaxy.sample` and a caught-up one from the `samples` table carry the
same field names, so one saved query covers both. There is no HTTPS probe in
the sample path; that result is `galaxy.check` per host.

### Batching, retries and spool

- Defaults: 100 events per request, 15 s flush, 256 kB request cap, 2,000 event
  memory queue, 512 kB disk spool.
- On 429/5xx/timeout the batch is retried with exponential backoff and jitter,
  honouring `Retry-After`, up to 3 retries. After that it is appended to
  `run/telemetry-spool.ndjson`; the next flush sends the spool first, preserving
  order.
- 4xx responses are permanent: the batch is dropped, counted as `rejected`, and
  the status shows the scrubbed Axiom error. Invalid events cannot loop forever.
- Counters mean exactly one thing each: `exported` events Axiom acknowledged as
  ingested, `failed` events named in a 2xx response's `failures` list,
  `rejected` events dropped by a permanent 4xx, `deferred` batches kept for a
  retryable failure, `dropped` oldest-queue evictions. A spool that cannot
  drain, like a queue that cannot send, sets `last_error` and makes `state`
  `degraded`; it is never reported as `ok`. `bytes_sent` totals the NDJSON
  bodies posted, including refused ones, and `spool_drained` counts events
  delivered from the disk spool after an outage.
- The queue drops the oldest event when full and counts the loss. The spool
  trims the oldest lines over its cap. Both are bounded by design on a 3 GB
  phone.
- Events are redacted and size-capped before queueing, so neither memory, the
  spool nor the wire ever contain a credential.

### Cursors and historical logs

- `run/telemetry-state.json` stores a byte offset per log file and the last
  forwarded `events.id`.
- The first start with no state begins at offset 0, so the current log files and
  the full `events` table are forwarded automatically. Offsets advance only for
  lines that were actually queued, so a bounded drain never skips or repeats a
  line.
- A file that shrinks or is replaced restarts at offset 0. Rotated files such as
  `auth.log.1` are not forwarded; the live file is. The bytes between the last
  read and a rotation are therefore not forwarded from that file, but every
  audit row and restart is also in SQLite, which is what carries them.
- Delivery is at-least-once, not exactly-once: a crash after Axiom accepted a
  batch but before the cursor file was written resends that batch on restart.
  Nothing in the design avoids this, and nothing loses data because of it.
- `serverctl telemetry-backfill [limit]` is for maintenance after a long
  outage. The live service owns the cursors, so the command refuses to run
  while it is up.

### Retention

- Local retention is unchanged: 7 days raw samples, 30 days events and audits.
- Axiom retention is per dataset. `serverctl telemetry-retention --days 30
  --apply` reads the current value and refuses to lower it unless
  `--force-lower` is passed, because lowering deletes older data immediately.
- The config stores `retention_days` and `manage_token` (optional; falls back to
  the ingest token). Dataset management uses `https://api.axiom.co`, ingest uses
  the edge domain.

## 3. Setup on the phone

1. In Axiom create a dataset named `galaxy-server` and an **API token** with
   ingest permission for it. Add `datasets:write` too if this token will manage
   the dataset (create it, or set retention).
2. On the phone:

```sh
~/galaxy-server/serverctl telemetry-setup
# hidden prompt for the token; dataset, endpoint and retention can be passed
# ~/galaxy-server/serverctl telemetry-setup --dataset galaxy-server --retention-days 30

~/galaxy-server/serverctl telemetry-doctor --apply
~/galaxy-server/serverctl telemetry-test
~/galaxy-server/serverctl telemetry-status
```

The doctor is the first thing to run when ingest fails. Axiom's edge answers
`404 dataset not found` both for a dataset that does not exist and for a request
sent to the wrong region, so the command posts a probe event to every edge
region and reports each answer (`204` accepted, `404 dataset not found`, or
`401`/`403` for a token that cannot use that dataset). `--apply` writes the
accepting endpoint into `run/axiom.json`; without it the endpoint is only
reported. `--create` also attempts `POST https://api.axiom.co/v2/datasets`,
which answers `403` for an ingest-only token: such a token can never create a
dataset, so the dataset must be made in the Axiom UI instead. The command never
prints the token.

3. For automation, use `telemetry-setup --token-file /path/to/token` where the
   file is mode 0600. `--token` also works but warns because it lands in shell
   history.
4. `run/axiom.json` is written with mode 0600 and is never copied by deployment.
   Deployment preserves `run/`, so credentials and cursors survive a release.

To disable export without deleting the dataset or cursors, set `"enabled":
false` in `run/axiom.json` or re-run setup with `--disable`; restart the service
to take effect immediately (the config is re-read within seconds anyway).

## 4. Dashboards and queries

Import or paste these into an Axiom dashboard on the `galaxy-server` dataset.

**Health and resources (5 min buckets)**

```
['galaxy-server']
| where event == 'galaxy.sample'
| summarize mem = avg(mem_pct), cpu = avg(cpu_pct), free_gb = avg(free_gb),
            batt = avg(batt_pct) by bin(_time, 5m)
```

**Service availability**

```
['galaxy-server']
| where event == 'galaxy.sample'
| extend nginx = toint(services['nginx']), api = toint(services['status']),
         supervisor = toint(services['supervisor']), tunnel = toint(services['tunnel_ready'])
| summarize min(nginx), min(api), min(supervisor), min(tunnel) by bin(_time, 5m)
```

**Errors and critical log lines**

```
['galaxy-server']
| where event == 'galaxy.log' and severity in ('error', 'crit')
| summarize count() by bin(_time, 5m), service
```

**Recent operational events**

```
['galaxy-server']
| where event in ('galaxy.event', 'galaxy.health')
| project _time, event, kind, service, detail, health, alerts
| order by _time desc
| limit 100
```

**Top error messages (24 h)**

```
['galaxy-server']
| where event == 'galaxy.log' and severity == 'error'
| summarize count() by service, message
| order by count_ desc
| limit 20
```

**Battery and charging**

```
['galaxy-server']
| where event == 'galaxy.sample'
| summarize batt = avg(batt_pct), temp = avg(temp_c), charging = max(charging)
  by bin(_time, 30m)
```

Suggested alert: `galaxy.health` with `health == 'crit'` for 10 minutes; and no
`galaxy.sample` for 5 minutes (the phone or the tunnel is down).

The existing phone console is unchanged: Overview, Metrics, Services,
Networking, Logs, Terminal and Settings keep working, and Settings gains a
read-only "Remote telemetry" card fed by `/api/telemetry`.

## 5. Migration risks and controls

| Risk | Control |
| --- | --- |
| Token leak | `run/axiom.json` 0600 and rejected if group/other readable on POSIX; token never returned by status, logged or put in an error; dedicated ingest token |
| Data loss during cut-over | Nothing is deleted locally; offsets start at 0 and forward existing logs/events; dual-write |
| Duplicate or skipped lines | One writer for cursors; byte offsets advance only for queued lines; event cursor by row id |
| Axiom outage | Bounded spool, retry with backoff, local console unaffected |
| Axiom rejects timestamps | `_time` is UTC from the source line or now; Axiom rejects only pre-1970 or >24 h future |
| Lowering retention deletes data | Refused unless `--force-lower` |
| Phone resource pressure | 2,000 event queue, 512 kB spool, request cap, 10 batches per tick, no database writes from the exporter |
| Deployment overwrite | `autodeploy.py` ships `telemetry.py` and preserves `run/`; rollback restores the previous file set |
| Clock/timezone confusion | Log-line timestamps parsed as phone local time and converted to UTC once |
| Log growth without rotation | Documented; Axiom retention and local prune still apply |

## 6. Rollback

- Disable: `serverctl telemetry-setup --disable` (keeps the token) or edit
  `enabled` in `run/axiom.json`.
- Remove: delete `run/axiom.json`; optional spool/state files are
  `run/telemetry-spool.ndjson` and `run/telemetry-state.json`.
- No local operational data is touched by any of these steps.

## 7. Verification performed

- `python -m py_compile status.py telemetry.py autodeploy.py`, `node --check
  site/console.js`, `bash -n serverctl setup.sh supervise.sh`.
- `python -m unittest discover -s galaxy-server -p 'test_*.py'`: telemetry,
  deployment and console API tests pass (22 telemetry tests including a real
  status process exporting to a fake Axiom endpoint, and an outage that spools
  events without breaking the console). The terminal tests require a POSIX PTY
  and cannot run on Windows; they are unaffected by this change and run in CI
  on Ubuntu.
- `node --test test_console.cjs`: both browser tests pass, including Settings
  navigation with the new card.
- The live-server test verified a `galaxy.sample` and a forwarded `galaxy.log`
  arriving at the ingest endpoint, the local log still intact, `/api/telemetry`
  reporting `ok`, zero queued, zero spooled, and `/api/health` still 200.
