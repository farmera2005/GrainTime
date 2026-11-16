# GrainTime Agent (for AgVantagePC scale PCs)

AgVantagePC keeps its scale tickets in `LOCALSET.MDF`, which SQL Server
Express opens as a **user instance**: a private SQL Server that only programs
running on that PC, as the Windows account that runs AgVantagePC, can reach.
GrainTime's server can't connect to it over the network, so this small agent
runs on the scale PC, reads the tickets read-only, and sends them to GrainTime.

## What it does, and doesn't do

- **Reads only.** It runs only `SELECT` statements it builds itself from the
  site's mapping profile, and its database connection refuses anything else.
  GrainTime never sends it SQL. Same session rules as GrainTime's collector:
  `READ UNCOMMITTED`, 5 s lock timeout, low deadlock priority, 5 s connect and
  15 s command timeouts, one connection at a time.
- **Never starts or holds AgVantage's database.** It reads only while
  AgVantagePC already has it open, with one short connection per poll (every
  60 s by default), then disconnects. When AgVantagePC is closed it just
  waits, and GrainTime shows the site as out of date.
- **Reads only the columns GrainTime needs**: load and ticket number, status,
  type, direction, commodity name, the gross/tare weigh times, and whether a
  weight was keyed in. Never customer names, drivers, vehicles, weights or
  prices.
- **Uses no index-less scans on a timer.** Each poll reads new loads after the
  last one seen plus the newest 200 (by load number, the table's key).
- **Connects outbound only**, to your GrainTime server (`http://<server>:8080`).
  Nothing listens on the scale PC.
- **Signs every request** with a per-PC key stored encrypted with Windows
  DPAPI (only that Windows account on that PC can use it). The key is never
  sent after pairing, and a captured request can't be replayed.

## Install (per scale PC, about 2 minutes, no administrator rights)

1. In GrainTime: **Configuration → Sites → Add site → AgVantagePC scale PC
   (GrainTime Agent)**. Give it a name and short code. On the site's page,
   under **GrainTime Agent**, click **Create pairing code** (works once, for
   24 hours).
2. On the scale PC, **signed in as the account that runs AgVantagePC** (for
   New Weston, `MERCERLANDMARK\nwscale`), run **GrainTimeAgent.exe**. Enter the
   GrainTime address shown on the site's page and the pairing code, and click
   **Pair**.
3. The agent copies itself to `%LOCALAPPDATA%\GrainTimeAgent\`, starts now,
   and starts automatically whenever that account signs in (the account's
   *Run* entry: `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\GrainTimeAgent`).
4. Back in GrainTime, the site shows **Agent: Online**. Click **Preview data**
   to check the tickets look right, turn **Polling** on, and run a **Backfill**
   if you want history (the PC holds tickets back to 2023 at New Weston).

To update the agent, download the newer GrainTimeAgent.exe, open it on the
scale PC (as the same account) and click **Update this PC to <version>**: it
replaces the installed copy and restarts it, keeping the pairing. To move the
agent to another PC, create a new pairing code and pair there; to
stop it, click **Disconnect the PC** in GrainTime and **Remove from this PC**
in the agent window.

## Silent / remote install

Each scale PC needs a pairing code for **its own site** (Configuration →
Sites → the site → GrainTime Agent → Create pairing code; it works once,
within 24 hours). The agent must end up running as the **Windows account
that runs AgVantagePC**, so pick the form that matches how your tool runs
commands. Nothing is shown on screen either way.

**A. Your tool runs as the signed-in user** (PDQ Deploy "Run As: Logged on
User", Intune Win32 app with install behaviour *User*, a GPO user logon
script), with the scale account signed in:

```bat
GrainTimeAgent.exe install --server http://graintime:8080 --code ABCD-1234
```

**B. Your tool runs as an administrator or SYSTEM** (PsExec, Intune *System*,
`Invoke-Command`, PDQ "Deploy User"): name the scale account with `--user`:

```bat
GrainTimeAgent.exe install --server http://graintime:8080 --code ABCD-1234 --user MERCERLANDMARK\nwscale
```

This copies the program to `C:\ProgramData\GrainTimeAgent\`, leaves the
pairing code in a file only that account (and administrators) can read, and
registers the task **GrainTime Agent setup (…)**, which runs as that account
only while it is signed in, so no password is needed. The install finishes
straight away if the account is signed in, otherwise at its next sign-in.
With PowerShell remoting:

```powershell
Copy-Item .\GrainTimeAgent.exe \\SCALEPC\C$\Windows\Temp\
Invoke-Command -ComputerName SCALEPC -ScriptBlock {
  $p = Start-Process C:\Windows\Temp\GrainTimeAgent.exe -Wait -PassThru -ArgumentList `
       'install','--server','http://graintime:8080','--code','ABCD-1234','--user','MERCERLANDMARK\nwscale'
  $p.ExitCode
}
```

Either way, the site's page in GrainTime shows **Agent: Online** within a
minute of the install finishing.

**Updating:** run `GrainTimeAgent.exe install` with no code: as the scale
account (A) it replaces that account's copy and keeps the pairing; with
`--user` (B) the task does the same at the account's next sign-in (or at
once if signed in). **Removing:** `GrainTimeAgent.exe uninstall` as the scale
account; `uninstall --user …` (as admin) removes the setup task and any unused
pairing code. Also click **Disconnect the PC** in GrainTime.

| Exit code | Meaning |
|---|---|
| 0 | Done (for `--user`: staged; finishes as that account) |
| 2 | Wrong arguments (the log says which) |
| 3 | GrainTime refused the pairing code (wrong, used or older than 24 hours): create a new one |
| 4 | GrainTime unreachable from the PC (address, network, firewall) |
| 5 | Wrong account or rights: SYSTEM without `--user`, or `--user` without admin rights |
| 6 | `install` without a code, but this account has no agent to update |
| 1 | Anything else (see the log) |

The log is `install.log` in `%LOCALAPPDATA%\GrainTimeAgent\` (A), or
`C:\ProgramData\GrainTimeAgent\` (B; the staged task then logs in the scale
account's folder). The pairing code is never written to a log.

## Where things are on the PC

| | |
|---|---|
| Program | `%LOCALAPPDATA%\GrainTimeAgent\GrainTimeAgent.exe` |
| Settings | `%LOCALAPPDATA%\GrainTimeAgent\agent.json` (the key is DPAPI-encrypted) |
| Log | `%LOCALAPPDATA%\GrainTimeAgent\agent.log` (1 MB × 4, never contains the key) |
| Status | `GrainTimeAgent.exe status` |
| Self-test | `GrainTimeAgent.exe selftest` (time zones, DPAPI, ODBC drivers) |

It needs a SQL Server ODBC driver; it uses the best one installed (ODBC Driver
18/17, or the SQL Server Native Client 11.0 that SQL Server Express 2012
installs, as on the New Weston PC).

## Getting GrainTimeAgent.exe

Download it from the repository's **Releases** page
(`https://github.com/farmera2005/GrainTime/releases`), e.g.
**GrainTime Agent 1.0.0 → GrainTimeAgent.exe**. Each release also lists the
file's SHA-256 so IT can check the download.

Releases are made by the **GrainTime Agent (Windows build)** workflow
(`.github/workflows/agent.yml`): it tests the Windows-only parts, builds the
program and self-tests the built .exe. When `VERSION` in
`graintime_agent/__init__.py` has no release yet, it publishes one, tagged
`agent-v<version>`. To release a new version, bump `VERSION` and push. A
released version is never replaced by a later build (pushing its tag again
re-publishes it on purpose). Every build is also kept for 90 days as a run
artifact.

The program is not code-signed: Windows SmartScreen may ask for confirmation
the first time, and some antivirus products are wary of unsigned programs.
Signing it with your company's code-signing certificate avoids both.

## Troubleshooting

When something is wrong, the site's page in GrainTime says why under
**GrainTime Agent**. On the scale PC, **`GrainTimeAgent.exe diagnose`** (run
as the same Windows account) writes `diagnose.txt` and opens it: which
account the agent runs as, which SQL Server instances it found, and whether it
could open the scale database.

| GrainTime shows | Meaning / fix |
|---|---|
| Agent: Offline | The PC is off, signed out, or can't reach GrainTime (check `http://<server>:8080` opens in a browser on the PC). |
| Scale software database: Closed, "No SQL Server is running for <account>" | The agent runs as a different Windows account than the scale software. Check **Scale PC** on the site's page: it must be the account that runs AgVantagePC. Pair the agent again while signed in as that account. |
| Closed, "user instance is not running" | AgVantagePC is closed, or its database was closed after being idle. It's read again as soon as AgVantagePC opens it. |
| Closed, "LOCALSET.MDF is not open in any SQL Server" | Neither the account's own instance nor a SQL Server service on the PC has the database open. Run `diagnose` and send the report. |
| Windows refused the agent's sign-in | The agent is running as a different Windows account than AgVantagePC. Pair it while signed in as the scale account. |
| No SQL Server ODBC driver | Install Microsoft ODBC Driver 18 for SQL Server. |
| Clock is N minutes off | Ticket times come from the PC's clock; fix the PC's time sync. |

Where the agent looks, in order: the account's own SQL Server Express user
instance (as AgVantagePC uses at New Weston; found from its `error.log` and
named pipe), then any SQL Server service on the PC (e.g. `SQLEXPRESS`), in
case LOCALSET is attached there instead. It finds them by listing the PC's
named pipes, which never connects to anything, and only then opens one short
read-only connection to confirm where the database is.

## Development

`python -m graintime_agent selftest` with `PYTHONPATH=backend;agent`. Tests:
`backend/tests/test_agent_worker.py` (runs the agent against the real api and
a mock AgVantage database, `graintime.devtools.mock_agvantage`, started by
`docker compose --profile mock up`), and `agent/tests` (Windows only).
