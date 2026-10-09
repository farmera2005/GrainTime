# GrainTime: Grain Elevator Truck Times

Truck time on site (inbound weigh to outbound weigh) for Mercer Landmark grain
elevators: scale house wall displays, a management dashboard, and a public
farmer page.

**Status:** Phase 1. Setup, site management, Test connection, Discovery,
mapping profiles, Preview data, backfill, live ticket collection and the
customizable statistics dashboard and the public wait-times page are in
place. The wall display comes next.

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

### Configuration

Administrators find every setting under **Configuration** in the top menu,
grouped in a side menu:

| Group | Page | What it holds |
|---|---|---|
| Scale sites | **Sites** | Connections, data collection, where each site appears, operating hours, discovery, archive/delete |
| | **Mapping profiles** | How tickets and weigh times are read from each kind of scale database |
| Statistics | **Collection & metrics** | Poll interval, green/yellow limits, current-time window, open-ticket cutoff, duration ceiling, backfill default |
| | **Public page** | When to hold back a number (minimum trucks, staleness) and which sites are listed |
| Access | **Users** | Local accounts and directory users: roles, enable/disable, passwords |
| | **Sign-in & LDAP** | Directory (Active Directory / LDAP) sign-in |

Old `/admin/...` bookmarks redirect to the matching Configuration page.

### Signing in with LDAP / Active Directory

Under **Configuration → Sign-in & LDAP**, staff can sign in with their network
username and password. Local GrainTime accounts are always checked first and
keep working when the directory is down, so keep at least one local
administrator (the one created in the setup wizard).

1. **Servers**: one or more domain controllers, tried in order.
   **LDAPS** (port 636) is recommended; **StartTLS** (389) also works. If the
   servers' certificates come from your own certificate authority (AD
   Certificate Services), paste that CA certificate in PEM form. Use the name
   on the certificate (usually the DC's host name).
2. **Finding users**: a read-only **service account** looks users up
   (recommended; any domain user account can read the directory), or GrainTime
   signs in **directly** as each user with a template such as
   `{username}@mercerlandmark.com`. For Active Directory the user filter is
   `(&(objectClass=user)(sAMAccountName={username}))`.
3. **Groups**: members of an *administrator group* become administrators;
   members of a *viewer group* can see the dashboard. Everyone else is refused
   (unless you allow any directory user as a viewer). With *Include nested
   groups* (Active Directory only) groups inside these groups count too.
4. **Test** with a real username and password: each step (connection,
   service account, finding the user, password, groups and role) is shown with
   the exact cause of any failure. Nothing is saved until you click **Save**.

Users can type `adamf`, `MERCER\adamf` or `adamf@mercerlandmark.com`. A
directory user gets a GrainTime user row at first sign-in (listed under
**Users**); the role is worked out from the groups at every sign-in, and an
administrator can disable the account in GrainTime at any time (which signs
them out at once). A local account is never taken over by a directory user
with the same name. Sign-in sessions last up to 12 hours, so a group change
takes effect at the next sign-in.

The service-account password is stored encrypted like site passwords, never
shown again, and recorded in the audit log only as "(changed)".

### Managing sites after setup

Under **Configuration → Sites** is a list of every site, with an **Edit** button on each.
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

### Collecting tickets from a site (admin guide)

1. **Prepare the site's SQL Server** (see the deployment notes below): TCP/IP
   on, a static port (or the SQL Server Browser running), and a firewall rule
   for the Docker host.
2. **Create GrainTime's read-only login.** Mapping profiles → open the profile
   → *Read-only login script*. Enter the database name and run the script at
   the site in SSMS as a sysadmin. It grants `SELECT` on only the columns the
   profile reads, so GrainTime cannot read customer, driver, plate, weight or
   price columns.
3. **Add the site** (Sites → Add site) with that login, and click **Test
   connection**.
4. On the site's page, under **Data collection**, choose the **mapping
   profile** and click **Preview data**. Check that types, statuses,
   commodities, weigh times and times on site look right. Preview also lists
   example tickets for status values treated as voided, so you can look them
   up in CompuWeigh.
5. **Backfill history**: pick a date range and run it. It loads one day at a
   time with pauses, shows progress, can be cancelled, and is safe to repeat.
6. Turn **Polling** on. The first poll starts within seconds; then every poll
   interval (default 60 s, per-site override).

How polling works: each poll reads new tickets after the high-water mark
(`tid_pk` for CompuWeigh GMS), re-reads tickets created in the last 24 h and
completed in the last 2 h (to catch completions, edits and voids), and once a
night re-checks the last 7 days. Every query is a bounded, read-only `SELECT`
at `READ UNCOMMITTED`. An unreachable site backs off (up to 30 minutes), is
marked **Stale** in the Sites list, and catches up from the high-water mark
when it returns. Other sites are never affected.

### The dashboard

Everyone who signs in lands on the **Dashboard**. Each user has their own
dashboards (up to 20, shown as tabs); the first one, *Overview*, is created
automatically.

- **Filters** (one row, saved with the dashboard): date range (today,
  yesterday, last 7/30/90 days, harvest season since Sep 1, year to date, or
  custom), sites, received or shipped (received by default), commodity.
- **Widgets**: number tiles (current time on site, trucks on site now, trucks
  today, median, 90th percentile, completed trucks), *Sites right now*
  (sortable, flags out-of-date sites), time on site by day, by hour of day,
  day × hour heatmap, compare sites, how long trucks stay, and tickets
  included/excluded.
- **Customize** adds, removes, reorders and resizes widgets (small, half,
  full), renames them, pins one to a single site, and renames, resets or
  deletes the dashboard. Changes save immediately.
- Every chart has a hover tooltip, a **Table** view and a **CSV** download.
  Live numbers refresh every minute. Dark mode follows the device.
- Statistics follow the metric definitions exactly; each range widget shows
  how many tickets were counted and how many were left out (voided, single
  weigh, over the ceiling).

Admins choose which sites appear with **Show on dashboard** on the site page.

### The public wait-times page

A mobile-first page for farmers, with no sign-in: every site that has
**Public page** switched on, with its current time on site, trucks on site now,
open or closed (from the site's **Operating hours**), and when the data was
last updated. The same data is served as a JSON feed (`feed.json`) for the
website or member portal.

- **Preview it** at `http://<this-host>:8080/public/` (also linked as
  *Public page* in the top menu). The public service itself listens on the
  Docker host's `127.0.0.1:8081` only (`PUBLIC_PORT` to change it).
  **Nothing is exposed to the internet**: publishing it (reverse proxy,
  tunnel or similar) is a separate decision.
- **Isolated by design.** It runs in its own container, built from only
  `graintime/public` (no api or collector code), on a network shared only
  with the database and the internal web server. It logs in as
  `graintime_public`, a role that can `SELECT` one table,
  `public_site_status`, and nothing else; its password is generated into a
  separate volume, the only secret it can see. It cannot reach the api, the
  collector or any site database.
- **Aggregates only.** The collector recomputes `public_site_status` every
  30 s: no ticket numbers, no individual trucks, no customers. Received grain
  only.
- **Honest numbers.** With fewer recent trucks than the *public minimum
  trucks* setting (default 3), the page says *Light traffic* or *No recent
  trucks* instead of a number. When a site's data is older than the *public
  staleness* setting (default 15 min), it says *Data delayed* and hides the
  numbers. Both limits are under Configuration → Public page.
- Cached (15 s in the service, 30 s for browsers), rate limited per client
  (60 requests a minute), GET only, with strict security headers.
  `X-Forwarded-For` is trusted only from private-network peers such as your
  reverse proxy.

**Operating hours** (site page → Operating hours): regular hours per weekday,
plus date-range overrides such as harvest hours. Without hours the page shows
wait times but not open or closed.

### Mapping profiles

A mapping profile says where tickets and weigh times live in a scale
database: tables, columns and value translations, never free-form SQL.
GrainTime builds every query from it. Sites on the same CompuWeigh version
share a profile; profiles can be created, cloned and edited in the browser,
and saving a profile that sites use asks for confirmation.

The **CompuWeigh GMS** profile (from the first site's discovery):

| GrainTime | CompuWeigh GMS |
|---|---|
| Ticket | `dbo.TransactionID` (`tid_pk`; printed number `tid_ticket`) |
| Inbound / outbound weigh | First / last finished weigh step in `dbo.TransactionLog` (`tlg_FinishTime`, `tlg_status = 1`, `tlg_WeightType` set) |
| Single weigh (stored tare) | Only one kind of weight (gross or tare) recorded: excluded and counted |
| Status | `tid_status`: 0 open, 1 completed, 2 and 4 voided (to be confirmed) |
| Type, direction | `tid_trtfk` → `dbo.TransactionType` (`trt_code`, `trt_direction` 1 received / 2 shipped). Only `TRUCKIN` is tracked |
| Commodity | `tid_prdfk` → `dbo.Product.prd_description` |
| Split tickets | `tid_parenttidfk`: a linked ticket for a split counts once, merged into its truck |

Times are converted from US Eastern local time to UTC; in the fall-back hour
the interpretation giving the shortest non-negative stay is used.

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
| ldap3 | Pure-Python LDAP client for directory sign-in (LDAPS, StartTLS, service-account or direct bind); no system libraries needed |
| React + TypeScript + Vite, React Router | Required stack; Vite for fast builds, no other UI dependencies |
| nginx (unprivileged image) | Serves the built frontend and proxies the api |

## Security notes

- Site passwords are write-only: the api never returns them, the UI never shows
  them, and logs pass through a redaction filter as a second line of defence.
  A password typed for a one-off Test connection is stored only as ciphertext
  and is wiped from the job when it finishes.
- Sessions are server-side, in HttpOnly SameSite=Strict cookies. Every
  state-changing request also needs an `X-GrainTime` header (CSRF guard).
  Failed sign-ins are rate-limited (local and directory alike). Directory
  sign-in refuses empty passwords (an empty LDAP bind is anonymous), escapes
  usernames before they go into a search filter, and verifies the server's
  certificate unless an administrator explicitly turns that off.
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

<a id="deployment-notes-connecting-to-site-sql-servers-windows-credentials-rejected"></a>
#### "Windows on the SQL Server PC did not accept the account" (SQL error 18452)

SQL Server words this as *"The login is from an untrusted domain and cannot
be used with Integrated authentication"*, but it almost always means the
Windows sign-in itself failed, before SQL Server looked for a login. Check, in
order:

1. **Domain, user name and password.** A wrong password gives exactly this
   message. The domain is normally the short NetBIOS name (`MERCER`), not the
   DNS name (`mercer.local`). Failed attempts count toward account lockout,
   so check before testing repeatedly.
2. **Is the SQL Server PC joined to that domain** and able to reach a domain
   controller? A workgroup PC can't check domain accounts. Use the PC's own
   computer name as the domain with a local Windows account, or a SQL login.
3. **NTLM restrictions or Extended Protection.** Some domains block NTLM
   ("Network security: Restrict NTLM"), and SQL Server's Extended Protection
   set to *Required* rejects it. Use a SQL Server login in that case.

The exact reason is in **Event Viewer → Windows Logs → Security** on the SQL
Server PC: event **4625** at the time of the test. *Failure Reason* and *Sub
Status* say which: `0xC000006A` wrong password, `0xC0000064` unknown user,
`0xC0000234` account locked, `0xC000005E` no domain controller reachable.

<a id="deployment-notes-connecting-to-site-sql-servers-windows-no-sql-login"></a>
#### "Windows accepted the account, but SQL Server would not let it in" (18456)

The Windows sign-in worked, but SQL Server has no login for the account or it
has no access to the database: `CREATE LOGIN [DOMAIN\user] FROM WINDOWS;` then
a database user in `db_datareader` (see `docs/sql/discovery_login.sql`).

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

# integration tests: MSSQL_TEST_* (any SQL Server), GMS_TEST_HOST + GMS_TEST_SA_PASSWORD
# (a SQL Server seeded by graintime.devtools.mock_gms)

# a mock CompuWeigh GMS site with live truck traffic (development only)
docker compose --profile mock up -d --build
# then add a site: host mock-mssql, port 1433, database GMS, SQL login graintime,
# password Mock-Collector-2026!, trust server certificate on

# frontend
cd web && npm install && npm run build      # or: npm run dev (proxies /api to :8000)
# web/dist is committed (deployment never runs npm): after changing web/src,
# run `npm run build` and commit web/dist. A GitHub check fails if it is stale.
```
