# Telemetry validation: Galaxy A20e server

Independent check of every pipeline that leaves the phone, run against the live
device and the live `galaxy-server` dataset on 2026-11-11 between 18:17 and
18:36 UTC (releases `4813ae7`, `267307d`). Local counts, cursors and resource
numbers were read from the device; delivery numbers come from the exporter's
counters, which are incremented only from Axiom's own ingest response.

Axiom-side query verification is the one thing this report cannot do: the API
token has ingest and per-dataset read, but not `query: read`, so APL returns
`403 token does not have access to resource: query with action: read`. Section 5
lists exactly what that leaves unproven and the queries that close it.

## 1. Pipelines and where they are produced

| Pipeline | Source | Path | Cadence |
| --- | --- | --- | --- |
| `galaxy.sample` | `samples` table + live sampler | sampler emits, `samples` history caught up once | 30 s |
| `galaxy.check` | `checks` table + probe | `record_check` emits, `checks` history caught up once | 60 s (2 hosts, 120 s per host) |
| `galaxy.event` | `events` table | id cursor, no live emit | within 15 s of the row |
| `galaxy.audit` | `audits` table | id cursor, no live emit | within 15 s |
| `galaxy.log` | 8 log files | byte offset + inode per file | within 15 s |
| `galaxy.health` | sampler | in-memory health state | on change, 5 min heartbeat |

`galaxy.health` used to be produced only when a console request built the status
payload, so an unobserved phone exported samples but never reported trouble.
The rules moved into `health_state()` and the sampler now calls them; the first
event of every restart is a health event, visible in the baseline of the second
pass (`galaxy.health: 1` at 18:27 with no console request in between).

## 2. Delivery reconciles exactly with local records

Window 18:33:55–18:35:55 (120 s), counters fresh at both ends (the live snapshot
is written at most once a minute, which is why an earlier comparison was skewed
by a 56 s stale baseline):

| Pipeline | New local rows | Delivered to Axiom | Difference |
| --- | --- | --- | --- |
| `galaxy.sample` | 4 | 4 | 0 |
| `galaxy.check` | 2 | 2 | 0 |
| `galaxy.event` | 0 | 0 | 0 |
| `galaxy.audit` | 0 | 0 | 0 |
| `galaxy.log` | 0 new lines | 0 | 0 |

Byte and line coverage for the same window: 0 new log bytes, 0 new log lines,
2,269 bytes posted, 0 queued, 0 spooled, 0 rejected, 0 dropped.

Every local source is at its high-water mark, read from
`run/telemetry-state.json` and `run/galaxy.db` at 18:31:

```
events unforwarded                              0   (cursor 291 == max id 291)
audits unforwarded                              0   (cursor 228 == max id 228)
samples older than the floor not yet caught up  0
checks  older than the floor not yet caught up  0
samples covered live  164      checks covered live  82
access offset=208/208  auth 18098/18098  deploy 1815/1815  deploy-manual 70/70
nginx-error 17896/17896  status 28681/28681  supervisor 3993/3993  tunnel 172352/172352
```

Each pipeline was also exercised with a real new record rather than a counter
increase alone:

- **Logs.** A canary line appended to `deploy.log` moved the cursor from 1706 to
  1745 bytes (the 39-byte line) and produced exactly one `galaxy.log` event,
  while the line stayed in the local file.
- **Access log.** Three requests through nginx (`404 /api/health`, `404
  /definitely-missing`, `401 /api/status`) grew `access.log` from 112 to 208
  bytes and produced three `galaxy.log` events with `service=access`
  (`warn`, `warn`, `warn` by status code). A `200` would not be logged at all:
  nginx writes only 4xx/5xx.
- **Audits.** One self-test row (`user=validation action=telemetry-self-test`)
  was written through `status.add_audit`, which advanced `audits` 227 → 228, the
  audit cursor 227 → 228, produced one `galaxy.audit` event and one
  `galaxy.event` (`admin`), and brought `auth.log` to its exact size. That row is
  deliberately left in place as evidence.
- **Samples and checks** are produced by the running sampler; pass 1 and pass 3
  show them arriving with no history backlog.

## 3. Failure recovery

Two independent outages, each by pointing `run/axiom.json` at a closed port for
45–50 s and restoring the original file byte for byte:

| Observation | Outage | After recovery |
| --- | --- | --- |
| Export state | `deferred: 1`, `last_error: URLError ... Connection refused` | `last_status: 200`, error cleared on next success |
| Spool | `run/telemetry-spool.ndjson`, 468 bytes, 1 line | absent |
| Delivered from spool | — | `spool_drained: 1`; `exported` rose 16 → 22 over the next two minutes (the drained event plus new ones) |
| Local records | unaffected; samples kept being written | cursors unchanged, no re-read of logs |
| Console | `/api/health` still `200`, sampler still running | unchanged |
| Secrets | no token, no `xaat-`, no `bearer` in the spool | — |

The spool→drain transition is now counted. Before this validation it was not:
the second failure of an already-spooled batch returned `False` and recorded
nothing, so the running service could sit with a full spool while
`/api/telemetry` reported `state: ok`. That is fixed (`_record_outcome(...,
delivered=False)` on the spool path); status now reports `degraded` and the
scrubbed error.

Bounded-loss behaviour was verified earlier by unit test, not on the device:
queue cap 2,000 events drops the oldest and counts `dropped`; spool cap 512 kB
trims the oldest lines; at the measured 3 events/min and 378 bytes/event the
spool holds roughly 8 hours of a total outage.

## 4. Retention, integrity, resources

- **Retention.** `telemetry-retention` reads 30 days from Axiom and 30 from the
  config. `--days 7 --apply` refused with exit 2 (`refusing to lower retention
  from 30 to 7 days`) and did not modify the dataset.
- **Integrity.** `pragma integrity_check` → `ok`. Row counts only grow
  (`samples` 5,558 → 5,572, `checks` 2,836 → 2,842, `events` 290 → 291,
  `audits` 227 → 228). `sessions` stays at 1 and is never read by the exporter.
  The new `samples.tunnel_errors` column exists and is populated (`159` in the
  newest row), which is how the schema migration on an existing database was
  checked.
- **Resources**, 60 s window with the service steady:

| Measure | Value |
| --- | --- |
| Whole service CPU | 0.62 % of one core |
| `telemetry` thread CPU | 0.37 % of one core |
| Service RSS | 32.4 MB |
| Bytes per event (NDJSON body) | 378 |
| Steady-state egress | ~1.6 MB/day (2,269 B / 120 s), ~3.3 MB/day in a period with log lines |
| Queue / spool at rest | 0 / 0 |

The sampler thread and the HTTP server account for the rest of the CPU. The
exporter writes the cursor file only when a cursor moves, writes the counter
snapshot at most once a minute, and opens the database read-only per cycle.

## 5. Axiom-side confirmation: blocked, with the queries that close it

What is proven: Axiom answered every ingest request with `2xx` and
`{"ingested": N, "failed": 0}`; N is what the counters and the reconciliation
above use. What is not proven: that those rows are readable in the dataset, and
that no duplicate `_time`/field combination exists.

The token scopes read back from `api.axiom.co` on 2026-11-11:

```
GET /v2/datasets/galaxy-server          200  (dataset exists, 30 d retention)
POST /v1/datasets/_apl                  403  query with action: read
GET  /v2/datasets/galaxy-server/fields  403  datasets with action: read
```

Add **Query: read** to the token in Axiom (Settings → API tokens → edit
scopes), or issue a read-only token and run these five queries; the counts
should equal the last line of each pipeline's ledger above:

```
['galaxy-server'] | where _time > ago(1h) | summarize events = count() by event
['galaxy-server'] | where event == 'galaxy.log' | summarize lines = count() by service, severity
['galaxy-server'] | where event == 'galaxy.sample' | summarize samples = count(), newest = max(_time)
['galaxy-server'] | where event == 'galaxy.audit' | project _time, user, action, outcome
['galaxy-server'] | summarize copies = count() by _time, event, tunnel_requests
  | where copies > 1
```

The last one is the duplicate check. If it returns rows, they are the
at-least-once window described in the next section, not a double send from a
normal cycle: the 120 s reconciliation showed a delta of exactly zero on four
pipelines.

## 6. Discrepancies found and resolved in this pass

| # | Finding | Fix |
| --- | --- | --- |
| 1 | A spool that could not drain reported `state: ok` with `last_error: null` | spool failures now record the error, increment `deferred`, and make status `degraded` |
| 2 | `failed` counted retryable transport failures and then counted the same events again as `exported` when the spool drained | `failed` now means only per-event failures named in a `2xx`; retryable batches count as `deferred`, permanent ones as `rejected` |
| 3 | Every `access.log` line had `severity: info`, including 500s, because the path contains no word like "error" | severity derived from the status code: 5xx `error`, 4xx `warn` |
| 4 | Live samples carried `tunnel_errors`; caught-up samples from `samples` did not, so one dashboard query covered only half the rows | `samples.tunnel_errors` added with a migration, inserted by the sampler, forwarded by the catch-up |
| 5 | Caught-up samples carried `https_ok`/`https_ms` that were always null and absent from live events | removed; the public HTTPS result lives in `galaxy.check` |
| 6 | `galaxy.health` was only emitted while somebody had the console open, despite being documented as a heartbeat | alert rules extracted to `health_state()`, called by the sampler as well as the payload |
| 7 | README said nginx access logging was disabled; `serverctl` enables failure-only logging | README corrected |
| 8 | `bytes_sent`, `spool_drained` were not observable, so network cost and spool drains could not be measured | added to the live counters and `/api/telemetry` |

Tests: 55 run in the suite, 43 of them in the telemetry module, which gained
coverage for each finding above. Five `test_terminal` tests fail on Windows only
(`pty` is unavailable) and pass in CI; the CI run for `267307d` was green and
deployed.

## 7. What stays local, and why

| Local source | Forwarded | Reason |
| --- | --- | --- |
| `sessions` (plaintext bearer tokens) | never | credential; also a security problem in its own right |
| `run/axiom.json`, `run/deploy-key`, `run/deploy-secret`, `run/htpasswd`, `run/terminal.passwd`, `~/.cloudflared` token | never | credentials |
| Successful HTTP requests (2xx) | not logged | nginx logs failures only; total traffic is visible as `tunnel_requests` in samples, per-path rate and latency are not |
| `auth.log.1` (rotated) | no | the same events exist as `audits` rows and `auth.log` resumes at the new inode |
| Android `logcat`/`dmesg`, `/proc/net/dev`, `/proc/stat` | no | blocked without root on Android 11 |
| `hourly` rollup | no | empty and unused |
| `run/galaxy.db`, `run/settings.json`, `run/monitor.json` | no | application state, not telemetry |
| `site/`, `site.previous/`, `run/deploy-repo/` | no | deployable artifacts, rebuilt from git |

## 8. Remaining risks

1. **No backup of `galaxy.db` or the credentials.** Axiom is forward-only
   telemetry in its own schema; it cannot restore a database. A lost phone
   loses all local history and every secret in `run/`. This is the largest risk
   and no part of this work addresses it.
2. **Local retention is shorter than Axiom's.** Samples are pruned at 7 days and
   checks at 3, against 30 days in Axiom. If ingest were failing silently for
   more than a week, the detail would be gone locally before anyone noticed.
   The counters and `degraded` state make it visible on the console, but nothing
   alerts off the phone.
3. **Duplicates on cursor loss.** Deleting `run/telemetry-state.json` re-sends
   all logs and rows that are still present, and a crash between Axiom's `2xx`
   and the cursor write re-sends one batch. Loss never happens; duplicates are
   possible and bounded to that window.
4. **`access.log` and `tunnel.log` never rotate.** `tunnel.log` has been static
   since 10 Nov, but every failed public request appends about 35 bytes. At
   1,000 scans/day that is 35 kB/day, forwarded to Axiom as well.
5. **The access log has no request timestamp** (the nginx format does not
   include `$time_local`), so `_time` for those events is the forwarding time,
   accurate to the 15 s flush interval.
6. **Counter snapshot lag.** `/api/telemetry` can be up to 60 s behind the
   running service, because the snapshot is written at most once a minute to
   spare flash. Reconciliation windows must start and end on a fresh snapshot.

## 9. Reproducing this

```sh
~/galaxy-server/serverctl telemetry-status          # counters, spool, state
~/galaxy-server/serverctl telemetry-retention       # Axiom vs configured
~/galaxy-server/serverctl telemetry-doctor          # token, dataset, region
python ~/galaxy-server/telemetry.py status          # same, from the CLI
```

The scripts used on the device were `validate-phone.sh`,
`validate-phone2.sh` and `validate-reconcile.sh`; their output is in
`/sdcard/Download/validate-*.log`.
