"""GrainTimeAgent.exe

  (no arguments)   open the pairing window
  run              run the agent (started automatically at sign-in)
  status           print pairing and database status
  selftest         check this PC can run the agent (writes selftest.txt; used by the build)
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


def status() -> int:
    from .localdb import find_running_instance
    state = config.load()
    print(f"GrainTime Agent {VERSION}")
    print("Paired with:", f"{state['site']['name']} at {state['server']}" if state else "nothing yet")
    print("Scale database running:", "yes" if find_running_instance() else "no")
    print("Log:", config.app_dir() / "agent.log")
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
