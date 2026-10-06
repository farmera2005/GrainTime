# Phase 0: site database discovery

`discover.py` connects to **one** CompuWeigh site database, reads its schema, and
writes `report.md` (for people to read) and `report.json` (for designing the
mapping profile). Send both files back. Nothing is written to the site database.

## What it does to the site database

- `SELECT` only: it creates, writes and alters nothing. Apart from `SET`
  statements for session options, every query is a `SELECT`.
- One connection, 5 s connect timeout, 15 s command timeout, 0.1 s pause between queries.
- `READ UNCOMMITTED`, `LOCK_TIMEOUT 5000`, `DEADLOCK_PRIORITY LOW`: it never blocks a weigh.
- Row counts come from `sys.partitions` metadata. No `COUNT(*)`, no full scans.
- Sample rows: `TOP 5`, newest first by clustered key.
- Value distributions (status, void, direction, commodity codes): from the newest
  2,000 rows only (`--distribution-rows`).
- Oldest/newest dates: only for date columns that lead an index (two index seeks each).

Run it outside peak scale hours if you can. It takes a few seconds.

## Privacy

Text in columns that look like names, plates, addresses, phone numbers or notes
is replaced with `<masked len=N>` in the samples. Codes and lookups such as
commodity, status and location are left alone. Large text and binary columns are
never read. Look through `report.md` before sending it. `--no-samples` leaves out
sample rows completely.

## Steps

1. **Prepare the login.** On the site SQL Server, edit and run
   `discovery_login.sql` as a sysadmin. It creates a temporary
   `graintime_discovery` login with read-only access to the one database. Mixed
   mode (SQL authentication) must be on.
2. **Run the script.** Running it from the Linux Docker host is best, because
   that also proves the network path and the TLS settings the collector will use:

   ```bash
   cd discovery
   docker build -t graintime-discovery .
   mkdir -p out
   docker run --rm -it -v "$PWD/out:/out" graintime-discovery \
       --host 10.x.x.x --port 1433 --database <DbName> --user graintime_discovery
   # prompts for the password; add --trust-server-certificate if the server
   # uses a self-signed certificate (most SQL Express installs do)
   ```

   Or run it on any machine with Python 3.9+ and
   [Microsoft ODBC Driver 18](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server):

   ```bash
   pip install pyodbc
   python discover.py --host 10.x.x.x --port 1433 --database <DbName> --user graintime_discovery
   ```

   To skip the prompt, put the password in `DISCOVERY_PASSWORD`. The script never
   prints the password or the connection string.
3. **Send back** `out/report.md` and `out/report.json`. If the connection fails,
   send the console output instead. It names the cause (port closed, host
   unreachable, login failed, certificate not trusted, TLS version, database not
   found) and the fix.
4. **Clean up** after the mapping is confirmed: run the DROP section at the bottom
   of `discovery_login.sql`.

## Options

| Option | Default | Purpose |
|---|---|---|
| `--port` | 1433 | Static TCP port of the instance |
| `--encrypt` | yes | `yes`, `no` or `strict` (ODBC 18 encrypts by default) |
| `--trust-server-certificate` | off | Accept a self-signed certificate |
| `--table NAME` | | Force a table into the detailed section (repeatable) |
| `--max-candidates` | 25 | Number of ticket-like tables inspected in detail |
| `--sample-rows` | 5 | Sample rows per candidate table (max 20) |
| `--distribution-rows` | 2000 | Number of recent rows used for value distributions |
| `--no-samples` | off | Leave out sample rows and distributions |
| `--no-mask` | off | Turn off masking of name-like text |
