"""Silent install, update and removal (for remote deployment).

The agent must run as the Windows account that runs the scale software: its
key is DPAPI-protected for that account, it starts at that account's sign-in,
and only that account can reach AgVantagePC's private SQL Server instance.

Run as the scale software's account (PDQ "run as logged on user", Intune
install behaviour "User", a logon script, or by hand):

    GrainTimeAgent.exe install --server http://graintime:8080 --code ABCD-1234
    GrainTimeAgent.exe install            (already paired: update in place)
    GrainTimeAgent.exe uninstall

Run as an administrator or SYSTEM (PsExec, Intune "System", Invoke-Command):

    GrainTimeAgent.exe install --server ... --code ... --user MERCERLANDMARK\\nwscale

stages the program in C:\\ProgramData\\GrainTimeAgent and registers a task that
runs as that account (only while it is signed in, so no password is needed):
it finishes the install now if the account is signed in, otherwise at its next
sign-in. The pairing code waits in a file only that account and administrators
can read; it is deleted when used, and it works once and expires in 24 hours
anyway.

Exit codes: 0 done, 1 unexpected error, 2 wrong arguments, 3 GrainTime
refused the pairing code, 4 GrainTime unreachable, 5 wrong Windows account or
rights for this command, 6 nothing to update. Each run appends to install.log
(in %LOCALAPPDATA%\\GrainTimeAgent, or C:\\ProgramData\\GrainTimeAgent for --user).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import VERSION, config, winsetup
from .client import Server, ServerError
from .localdb import windows_user

OK, ERROR, USAGE, REFUSED, UNREACHABLE, WRONG_CONTEXT, NOTHING = 0, 1, 2, 3, 4, 5, 6
TASK_PREFIX = "GrainTime Agent setup"
NO_WINDOW = 0x08000000


class InstallError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def machine_dir() -> Path:
    base = os.environ.get("GRAINTIME_AGENT_PROGRAMDATA") or os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"), "GrainTimeAgent")
    return Path(base)


class Log:
    def __init__(self, path: Path):
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        print(line)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Who is running this
# --------------------------------------------------------------------------- #

def is_system_account() -> bool:
    user = os.environ.get("USERNAME", "")
    return user.endswith("$") or "systemprofile" in os.environ.get("LOCALAPPDATA", "").lower()


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _norm_user(user: str) -> str:
    u = user.strip()
    if not re.match(r"^[A-Za-z0-9._ -]{1,64}(\\[A-Za-z0-9._ $-]{1,64})?$", u):
        raise InstallError(USAGE, f"'{user}' is not a Windows account name (use DOMAIN\\name or name).")
    return u


def _safe_name(user: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", user.lower())


# --------------------------------------------------------------------------- #
# In the scale account's own context
# --------------------------------------------------------------------------- #

def _check_server(server: str) -> str:
    server = server.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", server):
        raise InstallError(USAGE, f"'{server}' is not a GrainTime address (e.g. http://graintime:8080).")
    return server


def pair_and_install(server: str, code: str, log=print, transport=None) -> dict:
    """Pair with GrainTime, install for this account, start the agent."""
    server = _check_server(server)
    try:
        kw = {"transport": transport} if transport else {}
        got = Server(server, **kw).pair(code.strip(), platform.node(), windows_user())
    except ServerError as exc:
        raise InstallError(REFUSED if exc.status in (401, 403, 409, 422, 429) else UNREACHABLE,
                           f"GrainTime refused the pairing: {exc.detail}") from None
    except OSError as exc:
        raise InstallError(UNREACHABLE, f"Could not reach GrainTime at {server}: {exc}") from None
    log(f"paired with {got['site']['name']} ({got['site']['code']}) at {server}")
    winsetup.stop_running_agents()
    config.save(server, got["key_id"], got["secret"], got["site"])
    _install_and_start(log)
    return got["site"]


def _install_and_start(log) -> None:
    exe = winsetup.install_copy()
    if sys.platform == "win32":
        winsetup.enable_autostart(exe)
        winsetup.start_background(exe)
    log(f"installed {VERSION} at {exe}; starts when {windows_user()} signs in; running now")


def update_in_place(log=print) -> dict:
    state = config.load()
    if not state:
        raise InstallError(NOTHING, "Not paired on this account yet: give --server and --code.")
    winsetup.stop_running_agents()
    _install_and_start(log)
    return state["site"]


def uninstall(log=print) -> None:
    winsetup.stop_running_agents()
    if sys.platform == "win32":
        winsetup.disable_autostart()
    config.forget()
    try:
        exe = winsetup.installed_exe()
        if exe.exists() and not winsetup.running_from_install():
            exe.unlink()
    except OSError:
        pass
    log(f"removed the agent for {windows_user()} (revoke it on the site's page in GrainTime too)")


# --------------------------------------------------------------------------- #
# From an administrator / SYSTEM context: hand over to the scale account
# --------------------------------------------------------------------------- #

def pending_path(user: str) -> Path:
    return machine_dir() / "pending" / f"{_safe_name(user)}.json"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW if sys.platform == "win32" else 0)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def stage_for_user(user: str, server: str, code: str | None, log=print) -> None:
    """Copy the program to ProgramData, leave the pairing for `user`, and
    register a task that finishes the install as `user`."""
    user = _norm_user(user)
    server = _check_server(server) if server else ""
    if not (is_admin() or is_system_account()):
        raise InstallError(WRONG_CONTEXT, "--user needs an administrator or SYSTEM. To install for yourself, "
                                          "leave --user out.")
    mdir = machine_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    exe = mdir / "GrainTimeAgent.exe"
    if winsetup.is_frozen():
        shutil.copy2(sys.executable, exe)
    if code:
        p = pending_path(user)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"server": server, "code": code.strip(), "for": user,
                                 "staged": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        # Only that account (read + delete when done) and administrators/SYSTEM.
        r = _run(["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:(R,D)",
                  "*S-1-5-18:F", "*S-1-5-32-544:F"])
        if r.returncode != 0:
            p.unlink(missing_ok=True)
            raise InstallError(USAGE, f"Could not protect the pairing file for {user}: {r.stdout.strip() or r.stderr.strip()} "
                                      "(is the account name right?)")
    elif not server:
        log(f"no --code: the task will update {user}'s existing install")
    task = f"{TASK_PREFIX} ({_safe_name(user)})"
    ps = (f"$a = New-ScheduledTaskAction -Execute {_ps_quote(str(exe))} -Argument 'setup-pending'; "
          f"$t = New-ScheduledTaskTrigger -AtLogOn -User {_ps_quote(user)}; "
          f"$p = New-ScheduledTaskPrincipal -UserId {_ps_quote(user)} -LogonType Interactive -RunLevel Limited; "
          f"$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
          f"-ExecutionTimeLimit (New-TimeSpan -Minutes 10); "
          f"Register-ScheduledTask -TaskName {_ps_quote(task)} -Action $a -Trigger $t -Principal $p -Settings $s "
          f"-Force | Out-Null; Start-ScheduledTask -TaskName {_ps_quote(task)} -ErrorAction SilentlyContinue")
    r = _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps])
    if r.returncode != 0:
        raise InstallError(ERROR, f"Could not register the setup task for {user}: {(r.stderr or r.stdout).strip()[:500]}")
    log(f"staged {VERSION} for {user} at {exe}; task '{task}' finishes the install now if {user} is signed in, "
        "otherwise at their next sign-in. Check the site's page in GrainTime for 'Agent: Online'.")


def setup_pending(log=print) -> int:
    """The task's action, running as the scale account: use the pairing left
    for this account, or update the install from ProgramData."""
    me = windows_user()
    p = pending_path(me)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        finally:
            try:
                p.unlink()                      # the code works once; don't leave it lying around
            except OSError:
                pass
        site = pair_and_install(data["server"], data["code"], log)
        log(f"finished the staged install for {me}: {site['name']}")
        return OK
    if config.load():
        if winsetup.is_frozen() and not winsetup.running_from_install():
            update_in_place(log)                 # staged without a code: update this account's copy
        return OK
    log(f"nothing staged for {me}")
    return OK


def remove_staged(user: str, log=print) -> None:
    user = _norm_user(user)
    task = f"{TASK_PREFIX} ({_safe_name(user)})"
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
          f"Unregister-ScheduledTask -TaskName {_ps_quote(task)} -Confirm:$false -ErrorAction SilentlyContinue"])
    pending_path(user).unlink(missing_ok=True)
    log(f"removed the setup task and any staged pairing for {user}. Run 'GrainTimeAgent.exe uninstall' as {user} "
        "to remove their copy, or revoke the agent in GrainTime to stop it sending data.")


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def cli(command: str, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog=f"GrainTimeAgent.exe {command}", add_help=False)
    ap.add_argument("--server")
    ap.add_argument("--code")
    ap.add_argument("--user")
    try:
        args, extra = ap.parse_known_args(argv)
    except SystemExit:
        return USAGE
    staged = bool(args.user)
    log = Log((machine_dir() if staged else config.app_dir()) / "install.log")
    log(f"GrainTime Agent {VERSION}: {command} {' '.join(a if a != args.code else '****-****' for a in argv)} "
        f"as {windows_user()}")
    try:
        if extra:
            raise InstallError(USAGE, f"Unknown option(s): {' '.join(extra)}")
        if command == "install":
            if staged:
                if args.code and not args.server:
                    raise InstallError(USAGE, "Give --server with --code.")
                stage_for_user(args.user, args.server or "", args.code, log)
            else:
                if is_system_account():
                    raise InstallError(WRONG_CONTEXT, "Running as SYSTEM: the agent must run as the scale software's "
                                                      "account. Add --user DOMAIN\\account.")
                if args.code:
                    if not args.server:
                        raise InstallError(USAGE, "Give --server with --code.")
                    pair_and_install(args.server, args.code, log)
                elif args.server:
                    raise InstallError(USAGE, "Give --code with --server (create one on the site's page in GrainTime).")
                else:
                    update_in_place(log)
        elif command == "uninstall":
            remove_staged(args.user, log) if staged else uninstall(log)
        elif command == "setup-pending":
            return setup_pending(log)
        log("done")
        return OK
    except InstallError as exc:
        log(f"FAILED ({exc.code}): {exc.message}")
        return exc.code
    except Exception as exc:                     # noqa: BLE001 - reported, not hidden
        log(f"FAILED ({ERROR}): {type(exc).__name__}: {exc}")
        return ERROR
