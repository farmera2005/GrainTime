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
| python-tds + pyspnego + pyOpenSSL | Windows (domain) account sign-in over NTLM, which ODBC Driver 18 on Linux lacks; used only for sites set to Windows accounts |
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
### Port closed, or the wrong port

SQL Server Express ships with TCP/IP **disabled**: in SQL Server Configuration
Manager → SQL Server Network Configuration → Protocols for *INSTANCE*, enable
**TCP/IP** and restart the SQL Server service.

**Named instances** (what Windows users type as `SERVER\SQLEXPRESS`) usually
listen on a dynamic port, not 1433. Enter the server name and the **Instance
name**, leave **TCP port** blank, and click **Test connection**. GrainTime asks
the site's SQL Server Browser service (UDP 1434) for the port, as Windows
clients do, and fills it in. You can also paste `SERVER\INSTANCE` into the
server field. When a port doesn't answer, the error lists every instance the
server reports, with a button to use its port.

Dynamic ports can change when SQL Server restarts. If a saved port stops
answering, GrainTime looks the instance up again and reconnects. For
reliability, still give each instance a static port: IP Addresses → **IPAll**,
clear *TCP Dynamic Ports*, set *TCP Port*.

<a id="deployment-notes-connecting-to-site-sql-servers-browser-unreachable"></a>
### Couldn't look up the instance's port

SQL Server Browser didn't answer on UDP 1434. Start the **SQL Server Browser**
service at the site (Services, or SQL Server Configuration Manager), or enter
the instance's TCP port directly.

<a id="deployment-notes-connecting-to-site-sql-servers-instance-not-found"></a>
### No instance with that name

The server's SQL Server Browser doesn't list that instance. The error shows the
instances it does have; correct the name or use one of their ports.

<a id="deployment-notes-connecting-to-site-sql-servers-host-unreachable"></a>
### No answer from the SQL port

When the SQL port doesn't answer, Test connection also checks whether the
machine answers on a few standard Windows ports (file sharing, RPC, Remote
Desktop and so on). These are one-time connect-only checks, run only when you
click Test. The result is shown as a checklist under the error and points to
one of the two sections below.

<a id="deployment-notes-connecting-to-site-sql-servers-sql-port-blocked"></a>
### Server reachable, but the SQL port doesn't answer

The machine is on the network, but nothing answers on the SQL port. Usually:

1. **Windows Firewall on the SQL Server PC itself.** It's on by default even
   where the network has no firewall. Programs on that same PC (CompuWeigh,
   SSMS run locally) are never blocked, so "other systems connect" can still
   be true. Add an inbound rule for TCP on the SQL port (and UDP 1434 if you
   use the instance name), from the Docker host's address.
2. **SQL Server isn't on that port.** Named instances normally use a dynamic
   port. Enter the instance name and leave the port blank, or read the port in
   SQL Server Configuration Manager → TCP/IP → IP Addresses → IPAll. Check
   TCP/IP is enabled there too.

<a id="deployment-notes-connecting-to-site-sql-servers-no-route"></a>
### Nothing at that address answers

The machine didn't respond on any port. Check that the address is the SQL
Server PC's own IP (`ipconfig` on that PC), and that the Docker host can reach
that site's network. Other PCs connecting only shows the site is up, not that
this server has a route to it, so ask IT about VLAN or VPN routing. Windows
Firewall on a "Public" network profile can also drop everything.

<a id="deployment-notes-connecting-to-site-sql-servers-docker-network-overlap"></a>
### Address inside Docker's internal network

Docker gives its containers addresses in 172.17.0.0/16, 172.18.0.0/16 and so
on. If a site's SQL Server uses an address in the same range, the connection
never leaves the Docker host. GrainTime detects this and says so. Fix it on
the Docker host (an IT task, not in GrainTime): set `default-address-pools` in
`/etc/docker/daemon.json` to a range your network doesn't use, restart Docker,
and run `docker compose up -d` again.

<a id="deployment-notes-connecting-to-site-sql-servers-dns-failed"></a>
### Host name does not resolve

The Docker host can't resolve the name. Windows PCs often find short server
names through NetBIOS/WINS, which Linux doesn't use, so a name that works in
SSMS can still fail here. Use the server's IP address, or ask IT to add the
name to DNS.

<a id="deployment-notes-connecting-to-site-sql-servers-mixed-mode-disabled"></a>
### SQL authentication (mixed mode) disabled

A SQL Server login only works when mixed mode is on. In SSMS: Server
Properties → Security → *SQL Server and Windows Authentication mode*, then
restart the service. Or skip SQL logins entirely: set the site to **Sign in
with: Windows account (domain)** (see below).

<a id="deployment-notes-connecting-to-site-sql-servers-windows-login-failed"></a>
### Windows (domain) accounts

A site can sign in with a domain account instead of a SQL Server login. In the
site form choose **Sign in with → Windows account (domain)**, then enter the
domain and user name (or type `DOMAIN\user`) and the password. The password is
stored encrypted and is never shown again, like SQL login passwords.

How it works: Microsoft's ODBC driver on Linux can only use Windows accounts
through Kerberos, which needs a registered SPN, a server name rather than an
IP, domain DNS and synced clocks. Windows PCs fall back to **NTLM** when
Kerberos isn't available, so GrainTime does the same. Windows-account sites
connect with the open-source `python-tds` driver and NTLM (`pyspnego`). This
works by IP address and needs nothing set up on the GrainTime server.
Encryption and *Trust the server certificate* behave as for SQL logins.
*Strict (TDS 8)* encryption isn't available with this driver.

If SQL Server rejects the sign-in:

- The account needs a login and read access on the scale database:
  `CREATE LOGIN [DOMAIN\user] FROM WINDOWS;` then a database user in
  `db_datareader` (see `docs/sql/discovery_login.sql`).
- Check the domain, user name and password. A locked or expired domain account
  fails here too.
- Some domains turn NTLM off by policy. Use a SQL Server login for GrainTime
  there.

Windows sign-in doesn't get past a firewall: if Test connection reports that
the SQL port doesn't answer, fix that first.

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
