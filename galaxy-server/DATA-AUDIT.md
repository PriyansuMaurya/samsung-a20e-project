# Data audit: Galaxy A20e server

Measured on the phone on 2026-11-11 (paths relative to `~/galaxy-server`, sizes
in bytes). Everything below was read from the device over adb, not inferred.

## 1. What is stored locally

| Source | Purpose | Size | Growth | Sensitivity | Local retention | Forwarded |
| --- | --- | --- | --- | --- | --- | --- |
| `run/galaxy.db` `samples` | 30 s metrics history, 5,465 rows | ~490 kB of the DB | 2,880 rows/day, ~2 MB per 7 days | Low | 7 days (`prune`) | live + history |
| `run/galaxy.db` `checks` | Probe results, 2,788 rows | ~65 kB | 960 rows/day | Low | 3 days | live + history |
| `run/galaxy.db` `events` | Restart/down/reboot/tunnel state, 286 rows | ~30 kB | ~150/day | Medium | 30 days | incremental |
| `run/galaxy.db` `audits` | Console actions with outcome, 227 rows | ~25 kB | per action | Medium (usernames) | 30 days | incremental (`galaxy.audit`) |
| `run/galaxy.db` `sessions` | Live sign-in tokens, 1 row | bytes | per login | **High** (bearer tokens, plaintext) | expiry, 12 h | **never** |
| `run/galaxy.db` `hourly` | Pre-aggregated rollup | 0 rows, unused | none | Low | none | no (derived) |
| `log/tunnel.log` | cloudflared output | 172,352 | static since 10 Nov | Low | none | yes |
| `log/status.log` | API tracebacks | 28,681 | only on error | Low | none | yes |
| `log/nginx-error.log` | nginx errors | 17,896 | slow | Low | none | yes |
| `log/auth.log` | Sign-ins, actions | 17,985 | rotates at 64 kB | Medium | 1 rotation | yes |
| `log/supervisor.log` | Supervisor restarts | 3,993 | slow | Low | none | yes |
| `log/deploy.log` | Deploy/rollback output | 1,496 | per deploy | Low | none | **added this round** |
| `log/manual-deploy.log` | Hand-run deploys | 70 | per deploy | Low | none | **added this round** |
| `run/axiom.json` | Axiom endpoint, dataset, token | 422 | none | **High** (API token) | n/a | no |
| `run/deploy-key`, `run/deploy-secret` | Git deploy key, webhook HMAC | 411 + 44 | none | **High** | n/a | no |
| `run/htpasswd`, `run/terminal.passwd` | Console and terminal credentials | 43 + 120 | none | **High** | n/a | no |
| `run/settings.json`, `run/monitor.json` | UI settings, previous pids | 21 + 101 | tiny | Low | n/a | no |
| `run/telemetry-state.json` | Forwarding cursors | 293 | tiny | Low | n/a | no |
| `priv/console.html` | Shell copy served at `/console` | 23,597 | per deploy | Low | n/a | no |
| `site/`, `site.previous/` | Static console assets | ~0.7 MB each | per deploy | Low | n/a | no |
| `run/deploy-repo/` | Autodeploy checkout | ~25 MB | per deploy | Low | n/a | no |
| `run/pull/` | Manual checkout used for this audit | ~25 MB | none | Low | n/a | removed after the audit |
| `~/ .tunnel-token`, `.dashboard-password`, `.dashboard-htpasswd` | Cloudflare token, old basic-auth | 248 + 24 + 41 | none | **High** | n/a | no |
| `~/.cache` (pip) | Package download cache | 39.5 MB | static | Low | n/a | no |

Storage total for `~/galaxy-server`: 64.4 MB during the audit, 54.2 MB after
removing the manual `run/pull` checkout. About 25 MB of what remains is the
autodeploy checkout (Windows platform-tools included) and 1.4 MB is duplicated
site assets.

## 2. What reaches Axiom

Dataset `galaxy-server`, endpoint `us-east-1.aws.edge.axiom.co`, retention 30
days, batches of 100 events / 15 s / 256 kB.

- `galaxy.log` — the seven log files above, line by line, with `service`,
  `severity` and the timestamp parsed from the line.
- `galaxy.sample` — live every 30 s, plus the history before forwarding began.
- `galaxy.check` — live per probe, plus history.
- `galaxy.event` — `events` rows, incremental by id.
- `galaxy.audit` — `audits` rows, incremental by id (added this round).
- `galaxy.health` — health state on change and a 5-minute heartbeat.

Redaction runs before queueing: bearer tokens, JWTs, `xaat-`/`xaatm-` tokens and
`token|password|secret|api_key|authorization` keys are replaced. The `sessions`
table is never selected by any forwarding query.

## 3. Gaps found and closed

- Deploy history (`deploy.log`, `manual-deploy.log`) was local only: a rollback
  left no remote trace. Now forwarded as `galaxy.log`.
- `samples` and `checks` were exported only from the moment live export began;
  the 46-hour backlog existed only on the phone. Now caught up once, bounded to
  200 rows per cycle and split so checks cannot starve behind samples.
- `audits` reached Axiom only as freeform `auth.log` lines; the structured
  `user`/`action`/`outcome` fields are now sent as `galaxy.audit`.
- `autodeploy.py` copied `status.py` before `telemetry.py`, so a supervisor
  restart in that window crash-looped with `ModuleNotFoundError`. `status.py` is
  now installed last; no traceback has appeared since.

## 4. Telemetry vs data that needs a recoverable backup

Telemetry (Axiom is a copy, local is authoritative, losing Axiom costs nothing
operationally): logs, `samples`, `checks`, `events`, `audits`.

Requires a separate, restorable backup — nothing in Axiom can restore these:

- `run/axiom.json`, `run/deploy-key`, `run/deploy-secret`, `run/htpasswd`,
  `run/terminal.passwd`, `~/.tunnel-token` — credentials, all mode 0600.
- `run/settings.json`, `run/monitor.json` — behaviour and restart counters.
- `run/galaxy.db` — the audit trail and event history for the last 30 days.
- `priv/console.html` and `site/` are regenerated by a deploy from git and need
  no backup; `run/nginx.conf` is regenerated by `serverctl`.

## 5. Data-loss risks

1. No backup exists for the credentials, settings or `galaxy.db`. Losing the
   phone loses the deploy key, the webhook secret, the Axiom token and 30 days of
   audit history. Axiom holds exported events but cannot restore a database.
2. Local retention is shorter than Axiom's: samples 7 days, checks 3 days versus
   30 days in Axiom. Data older than those windows is gone locally by design.
3. Bounded buffering: 2,000 queued events and a 512 kB spool. An outage longer
   than that drops the oldest events and counts them in `dropped`. Verified: a
   60-second black-hole produced a 468-byte spool, no secrets, and it drained
   when the endpoint returned.
4. Deleting `run/telemetry-state.json` re-sends every log from offset 0 and every
   row from cursor 0 — duplicates in Axiom, no data loss.
5. Only `auth.log` rotates (at 64 kB). `tunnel.log`, `status.log`,
   `nginx-error.log` and `supervisor.log` grow without a cap.
6. `sessions` holds live bearer tokens in plaintext in SQLite; anyone with
   `run/galaxy.db` can impersonate a console session until it expires.

## 6. Verification performed

- Unit and integration: 43 tests, 5 failures are the pre-existing Windows-only
  `pty` tests. New tests cover history forward-once, floor exclusion, audit
  forwarding, release-log forwarding and that session tokens never leave.
- Live catch-up: `sample_cursor` reached 1794417042 and `check_cursor`
  1794416952, both below the `history_floor` 1794417060; `audit_cursor` 227 of
  227; `deploy` and `deploy-manual` appear in the log cursor map.
- Outage: spool written, contained only `galaxy.sample`, no token, no `xaat-`,
  no bearer; spool removed after the endpoint was restored.
- Retention: `telemetry-retention --days 7 --apply` refused with exit 2 and left
  retention at 30 days.
- Integrity: `pragma integrity_check` = ok; row counts continued to grow; no
  authoritative file was modified or deleted.
