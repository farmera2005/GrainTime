# GrainTime: Grain Elevator Truck Times

Truck time on site (inbound weigh to outbound weigh) for Mercer Landmark grain
elevators: scale house wall displays, a management dashboard, and a public
farmer page.

**Status:** first-run setup, site management, Test connection and Discovery
are in place. Ticket polling starts once the discovery report has been
reviewed and the mapping profile confirmed.

## Install and first run

On the Linux VM, with Docker and Docker Compose installed:

```bash
docker compose up -d --build
```

Then open **http://&lt;vm-address&gt;:8080** in a browser and follow the setup
wizard. That is the only command. Nothing needs editing beforehand: there is no
config file to fill in and no settings in environment variables.

The wizard walks through:

1. **Administrator account.** A local account that can always sign in (the
   break-glass login). It must be created within 60 minutes of the stack
   starting, so nobody else on the network can claim a fresh install. If that
   window passes, restart the `api` container from your Docker management tool.
2. **Defaults.** Green/yellow/red thresholds, poll interval, metric settings,
   default backfill depth, and public page limits. Suggested values are filled
   in; every site can override them later.
3. **First site.** Name, short code, and the connection to the site's
   CompuWeigh SQL Server, with **Test connection** before saving. The site is
   saved with polling off. This step can be skipped: **Skip for now** goes
   straight to the end of setup, and sites are added later in the admin panel.
4. **Discovery.** Reads the scale database's structure and a few masked sample
   rows, and shows the report in the browser with download buttons. Send the
   two files to the project team so the mapping profile can be designed.

### If the build fails

The build downloads Python packages from PyPI (api, collector) and the
Microsoft ODBC driver from the Debian and Microsoft package servers
(collector). The web app is **not** built during deployment: its compiled
files are committed in `web/dist`, so no npm packages are downloaded and no
certificates need installing. Failures are almost always network-related:

- **Just run `docker compose up -d --build` again.** A second run reuses
  everything that already downloaded.
- **Behind a proxy?** Docker needs the proxy configured for builds (Linux:
  the `proxies` section of `~/.docker/config.json` and a systemd drop-in for
  the daemon; Docker Desktop: Settings → Resources → Proxies).
- If only one service failed, the others may show as `CANCELED`. That just
  means the build stopped; it isn't a separate error.

### Managing sites after setup

In the admin panel, **Sites** lists every site, with an **Edit** button on each.
A site's page lets you:

- change its name, code, address, map link and connection details. Leave the
  password blank to keep the saved one; Test connection works with the saved
  password.
- choose where it appears: management dashboard, public page. Polling can be
  switched on once the site has a confirmed mapping profile.
- run discovery.
- **archive** it (hidden everywhere, polling off, history kept, can be
  restored), or **delete** it permanently, which requires typing the site's
  name.

Every change is recorded in the audit log.

### What happens automatically on first start

The one-shot `init` service generates, into the `secrets` Docker volume:

| Secret | Purpose |
|---|---|
| `db_password` | Password of the central PostgreSQL database |
| `encryption_key` | Key that encrypts site database passwords at rest. Kept only in this volume, never in the database |

`migrate` then creates the central database schema, and `api`, `collector` and
`web` start. Restarts reuse the existing secrets.

**Back up both volumes** (`graintime_pgdata` and `graintime_secrets`). If the
encryption key is lost, no data is lost, but every site password has to be
re-entered in the admin panel.

`.env.example` lists the single optional override (`WEB_PORT`, the host port
for the web app). You don't need to create a `.env` file.

## Services

| Service | Role |
|---|---|
| `init` | One-shot: generates missing secrets |
| `db` | PostgreSQL central store (persistent volume) |
| `migrate` | One-shot: applies database migrations |
| `api` | Internal FastAPI backend for the admin panel and setup wizard (and later the dashboards) |
| `collector` | **The only component that connects to site databases.** Runs Test connection and Discovery jobs queued by the api; later polls tickets |
| `web` | React frontend served by nginx; proxies `/api` to `api`. The only published port (8080), on the internal network |

The api never connects to a site. Test connection and Discovery are written
to the `collector_jobs` table, picked up by the collector within about a
second, and the browser polls for the result. At most one job runs per site at
a time.

### Libraries chosen

| Library | Why |
|---|---|
| FastAPI + Uvicorn | Required stack; typed request validation via Pydantic |
| SQLAlchemy 2 + psycopg 3 | Mature, well-supported PostgreSQL access |
| Alembic | Standard migrations for SQLAlchemy |
| pyodbc + Microsoft ODBC Driver 18 | Required driver; generic SQL Server access with no edition-specific features |
| cryptography (Fernet) | Authenticated encryption for site passwords at rest |
| argon2-cffi | Current recommended password hashing for local accounts |
| React + TypeScript + Vite, React Router | Required stack; Vite for fast builds, no other UI dependencies |
| nginx (unprivileged image) | Serves the built frontend and proxies the api |

## Security notes

- Site passwords are write-only: the api never returns them, the UI never shows
  them, and logs pass through a redaction filter as a second line of defence.
  A password typed for a one-off Test connection is stored only as ciphertext
  and is wiped from the job when it finishes.
- Sessions are server-side, in HttpOnly SameSite=Strict cookies. Every
  state-changing request also needs an `X-GrainTime` header (CSRF guard).
  Failed sign-ins are rate-limited.
- Every change made in the admin panel or the wizard goes to `audit_log` (who,
  when, old and new values). Passwords are recorded only as "(changed)".
- Nothing is exposed to the internet. How the public page is published will be
  decided separately.

<a id="deployment-notes-connecting-to-site-sql-servers"></a>
## Deployment notes: connecting to site SQL Servers

When Test connection or Discovery fails, the collector names the cause and
links to the matching section below. If you can, set each site up as follows
before adding it. `docs/sql/discovery_login.sql` creates the temporary
read-only discovery login.

<a id="deployment-notes-connecting-to-site-sql-servers-port-closed"></a>
### Port closed: TCP/IP disabled or dynamic port

SQL Server Express ships with TCP/IP **disabled**, and named instances use
**dynamic ports**. In SQL Server Configuration Manager → SQL Server Network
Configuration → Protocols for *INSTANCE*: enable **TCP/IP**. Under
IP Addresses → **IPAll**, clear *TCP Dynamic Ports* and set *TCP Port*
(e.g. 1433). Then restart the SQL Server service. In GrainTime, enter the
host and this port, never `HOST\INSTANCE`.

<a id="deployment-notes-connecting-to-site-sql-servers-host-unreachable"></a>
### Host unreachable or port filtered

Usually Windows Firewall at the site. Add an inbound rule allowing TCP on the
SQL port from the Docker host's address. Also check the WAN/VPN route from
the Docker host to the site.

<a id="deployment-notes-connecting-to-site-sql-servers-dns-failed"></a>
### Host name does not resolve

The Docker host can't resolve the name. Use the server's IP address, or fix
DNS.

<a id="deployment-notes-connecting-to-site-sql-servers-mixed-mode-disabled"></a>
### SQL authentication (mixed mode) disabled

Windows authentication from a Linux container isn't practical, so GrainTime
uses a SQL login. In SSMS: Server Properties → Security → *SQL Server and
Windows Authentication mode*, then restart the service.

<a id="deployment-notes-connecting-to-site-sql-servers-login-failed"></a>
### Login failed

Check the login name, the password, that the login is enabled, and that mixed
mode is on. The SQL Server error log at the site records the exact reason
(error 18456 with a state number).

<a id="deployment-notes-connecting-to-site-sql-servers-database-not-found"></a>
### Database not found

Check the database name, and that the login is mapped to a user in that
database.

<a id="deployment-notes-connecting-to-site-sql-servers-cert-untrusted"></a>
### Certificate not trusted

ODBC Driver 18 encrypts by default and checks the server certificate. Most
SQL Server Express installs use a self-signed certificate. Tick **Trust the
server certificate** for that site, or install a certificate from a trusted CA.

<a id="deployment-notes-connecting-to-site-sql-servers-tls-version"></a>
### TLS handshake failed (old SQL Server)

Current Linux images reject TLS 1.0/1.1. SQL Server 2016 and later are fine.
2014 needs SP1 CU5 or SP2+, 2012 needs SP3 with the TLS 1.2 update or SP4, and
2008/2008 R2 need a specific TLS 1.2 update. Test connection and Discovery
report the version and flag affected sites.

<a id="deployment-notes-connecting-to-site-sql-servers-driver-missing"></a>
### ODBC driver missing

This is a collector image packaging fault, not a site problem. Rebuild with
`docker compose build collector`.

## Development

```bash
# backend tests (need a throwaway PostgreSQL; MSSQL_TEST_* adds the SQL Server integration test)
cd backend
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql+psycopg://user:pw@127.0.0.1:5432/graintime_test pytest

# frontend
cd web && npm install && npm run build      # or: npm run dev (proxies /api to :8000)
# web/dist is committed (deployment never runs npm): after changing web/src,
# run `npm run build` and commit web/dist. A GitHub check fails if it is stale.
```
