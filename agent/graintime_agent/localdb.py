"""The scale software's database on this PC, opened read-only.

AgVantagePC attaches LOCALSET.MDF to a SQL Server Express *user instance*: a
private SQL Server started for the signed-in Windows account, reachable only
on this PC through a named pipe whose name changes each time it starts. The
instance writes that pipe name to its error log, in the account's profile:

  %LOCALAPPDATA%\\Microsoft\\Microsoft SQL Server Data\\<INSTANCE>\\error.log
  "Server local connection provider is ready to accept connection on [ \\\\.\\pipe\\...\\tsql\\query ]"

Rules (the same as GrainTime's collector, plus):
- We never start the instance or attach the database ourselves. If the pipe
  isn't there, the scale software isn't running and there is nothing to read.
- One short connection per poll, closed straight after, so we never keep the
  instance or LOCALSET.MDF open on our own.
- READ UNCOMMITTED, LOCK_TIMEOUT 5000, DEADLOCK_PRIORITY LOW, 5 s connect and
  15 s command timeouts.
- The connection wrapper refuses any statement that isn't a single SELECT.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

CONNECT_TIMEOUT_S = 5
COMMAND_TIMEOUT_S = 15
DRIVER_PREFERENCE = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                     "SQL Server Native Client 11.0", "SQL Server Native Client 10.0", "SQL Server"]
PIPE_RE = re.compile(r"(\\\\\.\\pipe\\[^\s\]\[]+\\tsql\\query)", re.IGNORECASE)
SESSION_SQL = ("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET LOCK_TIMEOUT 5000; "
               "SET DEADLOCK_PRIORITY LOW;")


class LocalDbError(Exception):
    def __init__(self, code: str, cause: str, fix: str):
        super().__init__(cause)
        self.code, self.cause, self.fix = code, cause, fix

    def as_dict(self) -> dict:
        return {"code": self.code, "cause": self.cause, "fix": self.fix}


# --------------------------------------------------------------------------- #
# Finding the running user instance
# --------------------------------------------------------------------------- #

def sql_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Microsoft SQL Server Data"


def read_log(path: Path) -> str:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if len(raw) > 1 and raw[1:2] == b"\x00":           # UTF-16 LE without a BOM
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def latest_pipe_in_log(text: str) -> str | None:
    found = PIPE_RE.findall(text)
    return found[-1] if found else None


def pipe_exists(pipe: str) -> bool:
    return os.path.exists(pipe)


def find_running_instance(base: Path | None = None, exists=pipe_exists) -> str | None:
    """The named pipe of this account's running user instance, or None."""
    base = base or sql_data_dir()
    if not base.is_dir():
        return None
    best = None
    for inst in base.iterdir():
        log = inst / "error.log"
        if not log.is_file():
            continue
        try:
            pipe = latest_pipe_in_log(read_log(log))
        except OSError:
            continue
        if pipe and exists(pipe):
            mtime = log.stat().st_mtime
            if best is None or mtime > best[0]:
                best = (mtime, pipe)
    return best[1] if best else None


# --------------------------------------------------------------------------- #
# Read-only connection
# --------------------------------------------------------------------------- #

class SelectOnlyCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params=()):
        stmt = sql.strip()
        if not stmt.upper().startswith("SELECT ") or ";" in stmt:
            raise PermissionError("The GrainTime Agent only runs single SELECT statements.")
        return self._cur.execute(stmt, *([tuple(params)] if params else []))

    @property
    def description(self):
        return self._cur.description

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        self._cur.close()


class SelectOnlyConnection:
    """What the mapping code gets: SELECT or nothing."""

    def __init__(self, conn, driver: str, database: str):
        self._conn, self.driver, self.database = conn, driver, database

    def cursor(self):
        return SelectOnlyCursor(self._conn.cursor())

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def choose_driver(installed: list[str]) -> str | None:
    for d in DRIVER_PREFERENCE:
        if d in installed:
            return d
    return None


def _odbc_str(server: str, driver: str, database: str | None, extra: str = "") -> str:
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", "Trusted_Connection=yes", "APP=GrainTime Agent"]
    if database:
        parts.append("DATABASE={" + database.replace("}", "}}") + "}")
    if driver.startswith("ODBC Driver 18"):
        # A named pipe on this same PC: no network to encrypt against, and the
        # user instance has no certificate to verify.
        parts.append("Encrypt=Optional")
    return ";".join(parts) + ";" + extra


def _open(conn_str: str):
    import pyodbc
    conn = pyodbc.connect(conn_str, timeout=CONNECT_TIMEOUT_S, autocommit=True, readonly=True)
    conn.timeout = COMMAND_TIMEOUT_S
    cur = conn.cursor()
    cur.execute(SESSION_SQL)
    cur.close()
    return conn


def classify(exc: Exception) -> LocalDbError:
    msg = str(exc)
    low = msg.lower()
    if "login failed" in low or "18456" in low:
        return LocalDbError("login_failed", "Windows refused the agent's sign-in to the scale database",
                            "The agent must run as the same Windows account as the scale software "
                            "(for AgVantagePC, the account that signs in to the scale PC).")
    if "cannot open database" in low or "4060" in low:
        return LocalDbError("database_unavailable", "The scale database could not be opened",
                            f"Check the scale software is running. Driver: {msg[:300]}")
    if "timeout" in low or "named pipes provider" in low or "could not open a connection" in low:
        return LocalDbError("instance_unreachable", "The scale software's database did not answer",
                            "It may have just closed; the agent will try again.")
    return LocalDbError("connect_failed", "Connecting to the scale database failed", msg[:500])


def open_scale_database(pipe: str, database_match: str, driver: str | None = None) -> SelectOnlyConnection:
    """Connect over the instance's pipe, find the database whose name ends with
    `database_match` (e.g. LOCALSET.MDF), and reconnect to it read-only."""
    import pyodbc
    driver = driver or choose_driver(pyodbc.drivers())
    if driver is None:
        raise LocalDbError("no_driver", "No SQL Server ODBC driver is installed on this PC",
                           "Install Microsoft ODBC Driver 18 for SQL Server, or use a PC where the "
                           "scale software's SQL Server tools are installed.")
    server = "np:" + pipe
    try:
        conn = _open(_odbc_str(server, driver, None))
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sys.databases WHERE UPPER(name) LIKE ? ESCAPE '!'",
                        "%" + database_match.upper().replace("!", "!!").replace("%", "!%").replace("_", "!_")
                        .replace("[", "![") )
            names = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except pyodbc.Error as exc:
        raise classify(exc) from None
    if not names:
        raise LocalDbError("database_not_found", f"No database ending in {database_match} is open",
                           "Open the scale software on this PC; the agent reads only while it runs.")
    try:
        return SelectOnlyConnection(_open(_odbc_str(server, driver, names[0])), driver, names[0])
    except pyodbc.Error as exc:
        raise classify(exc) from None
