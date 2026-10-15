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

To move the agent to another PC, create a new pairing code and pair there; to
stop it, click **Disconnect the PC** in GrainTime and **Remove from this PC**
in the agent window.

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

The GitHub workflow **GrainTime Agent (Windows build)**
(`.github/workflows/agent.yml`) tests the Windows-only parts and builds the
program on every change; download **GrainTimeAgent** from the run's artifacts.
It is not code-signed: Windows SmartScreen may ask for confirmation the first
time, and some antivirus products are wary of unsigned programs. Signing it
with your company's code-signing certificate avoids both.

## Troubleshooting

| GrainTime shows | Meaning / fix |
|---|---|
| Agent: Offline | The PC is off, signed out, or can't reach GrainTime (check `http://<server>:8080` opens in a browser on the PC). |
| Scale software database: Closed | AgVantagePC isn't open. Nothing to read until it is. |
| Windows refused the agent's sign-in | The agent is running as a different Windows account than AgVantagePC. Pair it while signed in as the scale account. |
| No SQL Server ODBC driver | Install Microsoft ODBC Driver 18 for SQL Server. |
| Clock is N minutes off | Ticket times come from the PC's clock; fix the PC's time sync. |

## Development

`python -m graintime_agent selftest` with `PYTHONPATH=backend;agent`. Tests:
`backend/tests/test_agent_worker.py` (runs the agent against the real api and
a mock AgVantage database, `graintime.devtools.mock_agvantage`, started by
`docker compose --profile mock up`), and `agent/tests` (Windows only).
