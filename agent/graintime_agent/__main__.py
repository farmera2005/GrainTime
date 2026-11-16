"""GrainTimeAgent.exe

  (no arguments)   open the pairing window
  run              run the agent (started automatically at sign-in)
  status           pairing and database status (status.txt, opened in Notepad)
  diagnose         where the agent looked for the scale database and why (diagnose.txt, opened in Notepad)
  selftest         check this PC can run the agent (writes selftest.txt; used by the build)
  install --server URL --code CODE [--user DOMAIN\\account]   silent install (see installer.py)
  install          update an existing install in place, keeping its pairing
  uninstall [--user DOMAIN\\account]
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import VERSION, config, winsetup


def setup_logging() -> None:
    h = RotatingFileHandler(config.app_dir() / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)


def run() -> int:
    setup_logging()
    log = logging.getLogger("graintime_agent")
    if not winsetup.single_instance():
        log.info("another copy of the agent is already running; exiting")
        return 0
    state = config.load()
    if not state:
        log.warning("not paired: open GrainTimeAgent.exe to pair it with a GrainTime site")
        return 1
    from .client import Server
    from .worker import Worker
    Worker(Server(state["server"], state["key_id"], config.secret_of(state))).run_forever()
    return 0


def _report(name: str, lines: list[str]) -> None:
    """Write a report next to the log and show it (a windowed .exe has no console)."""
    path = config.app_dir() / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        import os
        os.startfile(str(path))      # opens in Notepad


def diagnose_lines(open_db=None) -> list[str]:
    from .localdb import explain, find_candidates, list_pipes, open_first, windows_user
    state = config.load()
    match = "LOCALSET.MDF"
    lines = [f"GrainTime Agent {VERSION} diagnosis", f"Windows account: {windows_user()}",
             f"Paired with: {state['site']['name'] + ' at ' + state['server'] if state else 'nothing yet'}",
             f"Looking for a database ending in: {match}", ""]
    cands, detail = find_candidates()
    lines.append(f"User-instance folder: {detail['user_instance_folder']}")
    if not detail["user_instance_logs"]:
        lines.append("  (no SQL Server user instance has run for this account)")
    for e in detail["user_instance_logs"]:
        lines.append(f"  {e.get('instance')}: pipe {e.get('pipe') or 'none in error.log'} -> "
                     f"{'RUNNING' if e.get('running') else 'not running'}{'  ' + e['error'] if e.get('error') else ''}")
    lines.append(f"SQL Server services on this PC: {', '.join(detail['service_pipes']) or 'none'}")
    pipes = list_pipes()
    sql = [p for p in (pipes or []) if "sql" in p.lower()]
    lines.append(f"SQL-related pipes: {', '.join(sql) if sql else 'none'}"
                 + ("" if pipes is not None else "  (could not list pipes)"))
    lines.append("")
    if not cands:
        lines.append("RESULT: not found. " + explain(detail, match))
        return lines
    try:
        conn = open_first(cands, match, opener=open_db) if open_db else open_first(cands, match)
        lines.append(f"RESULT: found {conn.database} via {conn.target} ({conn.driver})")
        conn.close()
    except Exception as exc:
        cause = getattr(exc, "cause", None) or str(exc)
        fix = getattr(exc, "fix", "")
        lines.append(f"RESULT: not opened. {cause}. {fix}".strip())
        lines.append(explain(detail, match))
    return lines


def diagnose() -> int:
    _report("diagnose.txt", diagnose_lines())
    return 0


def status() -> int:
    from .localdb import find_running_instance, windows_user
    state = config.load()
    _report("status.txt", [f"GrainTime Agent {VERSION}", f"Windows account: {windows_user()}",
                           "Paired with: " + (f"{state['site']['name']} at {state['server']}" if state else "nothing yet"),
                           "Scale software's user instance running: " + ("yes" if find_running_instance() else "no"),
                           "Starts at sign-in: " + ("yes" if sys.platform == "win32" and winsetup.autostart_enabled() else "no"),
                           f"Log: {config.app_dir() / 'agent.log'}", "For details run: GrainTimeAgent.exe diagnose"])
    return 0


def selftest() -> int:
    """Windows checks that need no GrainTime server: time zones, DPAPI, ODBC."""
    lines, ok = [f"GrainTime Agent {VERSION}"], True
    try:
        from graintime.common.normalize import to_utc  # noqa: F401  (loads America/New_York)
        from datetime import datetime
        lines.append(f"time zone: ok ({to_utc(datetime(2026, 7, 1, 12)).isoformat()})")
    except Exception as exc:
        ok = False
        lines.append(f"time zone: FAILED {exc}")
    try:
        assert config.unprotect(config.protect("check")) == "check"
        lines.append("DPAPI: ok")
    except Exception as exc:
        ok = False
        lines.append(f"DPAPI: FAILED {exc}")
    try:
        import pyodbc
        from .localdb import choose_driver
        lines.append(f"ODBC drivers: {', '.join(pyodbc.drivers()) or 'none'}; would use: "
                     f"{choose_driver(pyodbc.drivers()) or 'none'}")
    except Exception as exc:
        ok = False
        lines.append(f"pyodbc: FAILED {exc}")
    from .localdb import find_running_instance
    lines.append(f"scale database running: {'yes' if find_running_instance() else 'no'}")
    lines.append("RESULT: " + ("ok" if ok else "FAILED"))
    (config.app_dir() / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "run":
        return run()
    if args and args[0] == "status":
        return status()
    if args and args[0] == "diagnose":
        return diagnose()
    if args and args[0] in ("install", "uninstall", "setup-pending"):
        from .installer import cli
        return cli(args[0], args[1:])
    if args and args[0] == "selftest":
        return selftest()
    if args and args[0] in ("version", "--version"):
        print(VERSION)
        return 0
    from .ui import run_setup_window
    run_setup_window()
    return 0


if __name__ == "__main__":
    sys.exit(main())
