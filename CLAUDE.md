# GrainTime: notes for Claude

Truck time on site (inbound weigh → outbound weigh) for Mercer Landmark grain
elevators. See README.md for install, services and deployment notes.

## Architecture (Docker Compose)

- `init` (one-shot) generates `db_password` and `encryption_key` into the
  `secrets` volume. `migrate` (one-shot) runs Alembic. Then `api`, `collector`,
  `web`, plus `db` (PostgreSQL).
- **Zero manual configuration.** Nothing is entered by CLI or config file.
  Sites, users, settings and the first admin come from the browser (setup
  wizard, then the admin panel). Env vars are optional overrides only
  (`GRAINTIME_*`, used by tests). Don't add required env vars or config files.
- **The built web app (`web/dist`) is committed.** The web image only copies
  it into nginx, so deploying never downloads npm packages: the deployment
  network inspects HTTPS to npm, and nobody should have to install a
  certificate. After any change under `web/`, run `npm run build` and commit
  `web/dist` (the `web-dist up to date` GitHub check fails otherwise). Never
  "fix" certificate errors by disabling TLS verification.
- **Only the collector talks to site databases.** The api queues work in
  `collector_jobs` (`test_connection`, `discovery`; later preview and
  backfill). The collector claims jobs with `FOR UPDATE SKIP LOCKED`, runs at
  most one per site, and wipes `params` when a job finishes.
- Sites store host + port. An optional instance name lets the collector look
  the port up via SQL Server Browser (UDP 1434, `sitedb.browse_instances`) on
  Test connection, or when a saved port stops answering
  (`sitedb.open_connection`). Failure diagnosis lists the server's instances
  and detects addresses inside Docker's own network.
- Two drivers, chosen per site by `auth_method`: SQL Server logins use
  Microsoft ODBC Driver 18 (`sitedb._connect_odbc`); Windows (domain) accounts
  use python-tds with NTLM via pyspnego (`sitedb._connect_windows`), because
  ODBC on Linux only does Kerberos. Both go through `sitedb.connect`, which
  applies the same session settings and timeouts.
- Ticket collection: mapping profiles (`common/profiles.py`, structured, no SQL)
  -> `collector/mapping.py` builds bounded SELECTs -> `common/normalize.py`
  (pure; UTC, DST, statuses, single-weigh, split tickets) ->
  `common/tickets_store.py` (idempotent upsert on site + source ticket).
  `collector/poller.py` schedules polls (high-water mark, look-back, nightly
  re-check, back-off, stale); `collector/collect.py` also runs Preview and
  backfill jobs. The poller and jobs share the one-connection-per-site lock.
- `common/login_script.py` generates the site login with column-level SELECT
  from a profile. `devtools/mock_gms.py` is a dev-only CompuWeigh GMS mock
  (compose profile `mock`) that uses that script.
- Statistics: `common/metrics.py` is the one place the metric definitions are
  implemented (SQL over `tickets`); `api/routes_stats.py` serves them as JSON
  or CSV to any signed-in user; `api/routes_dashboards.py` stores per-user
  dashboards (filters + widget list, JSONB). The web dashboard
  (`web/src/pages/Dashboard.tsx`) draws charts with the hand-written SVG kit
  in `web/src/charts/charts.tsx` (no chart library; one y-axis per chart).
- Code: `backend/graintime/{common,api,collector}`, `backend/migrations`,
  `web/src`. The setup wizard and the admin panel share `SiteForm`,
  `DiscoveryPanel` and `DefaultsForm`, so the first site is registered through
  the same code path as every other.

## Site-database safety rules (production scale systems)

1. SELECT only. Never write, create or alter anything.
2. A dedicated read-only SQL login.
3. Poll incrementally from a high-water mark plus a short look-back. Never
   rescan a table on a timer.
4. `READ UNCOMMITTED`, `LOCK_TIMEOUT 5000`, `DEADLOCK_PRIORITY LOW` (set in
   `collector/sitedb.connect`).
5. One connection per site at a time; 5 s connect timeout, 15 s command
   timeout; poll every 60 s by default.
6. On an unreachable site: exponential backoff, mark it stale, catch up later.
   One site must never affect the others.
7. Backfill is admin-triggered only, in small date-bounded batches with pauses.
8. Everything above applies equally to Test connection, Preview data and
   backfill. Every discovery query is bounded with `TOP`; row counts come from
   `sys.partitions`.

## Site passwords

- Entered in the browser, Fernet-encrypted (`common/crypto.py`) with the key
  from the secrets volume, which is never stored in the database.
- Write-only: no API response includes them (`SiteOut` has `has_password`
  only). Never log them or a connection string. `ConnectionSpec.__repr__`
  hides the password, and `common/logging.redact` is a safety net, not a
  licence to log.
- Audit entries record password changes only as "(changed)"
  (`common/audit.scrub`).

## Metric definitions (identical everywhere)

- Time on site = outbound weigh − inbound weigh, for completed, non-voided
  tickets with both weighs. Store UTC, display America/New_York, and handle the
  fall-back DST hour.
- Excluded: voided tickets, single-weigh tickets (stored tares), and durations
  above the ceiling (default 4 h). Count the exclusions and show the count.
- Current time on site = median of trucks that weighed out in the last 60 min.
  With fewer than 3, use the last 5 completed trucks within 2 h. With none,
  say "no recent trucks".
- Trucks on site now = inbound weigh without an outbound weigh, opened within
  the last 6 h.
- Report median and p90. Keep received and shipped separate (received is the
  default).
- Known limitation: time spent queueing before the inbound scale is not
  captured.

## Running and testing

```bash
docker compose up -d --build                 # full stack, then open :8080
cd backend && TEST_DATABASE_URL=postgresql+psycopg://u:p@host:5432/graintime_test pytest
# optional SQL Server integration test: MSSQL_TEST_HOST/PORT/DATABASE/USER/PASSWORD
# optional mock GMS end-to-end test: GMS_TEST_HOST, GMS_TEST_SA_PASSWORD
docker compose --profile mock up -d --build  # dev stack with a mock CompuWeigh GMS site
cd web && npm run build                      # typecheck + build; commit web/dist
```

Tests cover the setup flow and first-run window, the admin role on every admin
route (walked automatically), password encryption, write-only passwords and
absence from logs, audit entries, collector job handling, and each Test
connection failure cause.
