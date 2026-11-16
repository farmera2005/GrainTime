"""Checks that only make sense on Windows (run by the agent build workflow)."""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_dpapi_round_trip_and_ciphertext(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAINTIME_AGENT_HOME", str(tmp_path))
    from graintime_agent import config
    blob = config.protect("not-in-the-file")
    assert "not-in-the-file" not in blob and config.unprotect(blob) == "not-in-the-file"
    config.save("http://gt:8080", "k", "not-in-the-file", {"id": 1, "name": "x", "code": "X"})
    assert "not-in-the-file" not in (tmp_path / "agent.json").read_text()


def test_time_zone_data_available():
    from datetime import datetime

    from graintime.common.normalize import to_utc
    assert to_utc(datetime(2026, 1, 15, 12)).hour == 17 and to_utc(datetime(2026, 7, 15, 12)).hour == 16


def test_autostart_set_and_removed():
    from graintime_agent import winsetup
    winsetup.enable_autostart(winsetup.installed_exe())
    assert winsetup.autostart_enabled()
    winsetup.disable_autostart()
    assert not winsetup.autostart_enabled()


def test_missing_pipe_is_not_running():
    from graintime_agent.localdb import pipe_exists
    assert pipe_exists("\\\\.\\pipe\\graintime-test-does-not-exist\\tsql\\query") is False


def test_single_instance_guard():
    from graintime_agent import winsetup
    assert winsetup.single_instance() is True


def test_pipes_are_listed_without_opening_them():
    from graintime_agent.localdb import find_candidates, list_pipes
    pipes = list_pipes()
    assert pipes is not None and len(pipes) > 0          # Windows always has some named pipes
    cands, detail = find_candidates()
    assert detail["pipes_listed"] is True and isinstance(cands, list)


def _whoami():
    import os
    return f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"


def test_staged_install_for_another_account_registers_task_and_locks_the_code(tmp_path, monkeypatch):
    """--user from an administrator: pairing file only readable by that account
    and admins, a logon task running as that account, then removal."""
    import subprocess

    from graintime_agent import installer
    monkeypatch.setenv("GRAINTIME_AGENT_PROGRAMDATA", str(tmp_path / "pd"))
    assert installer.is_admin()                       # the build runner is an administrator
    user = _whoami()
    rc = installer.cli("install", ["--user", user, "--server", "http://127.0.0.1:9", "--code", "TEST-CODE"])
    try:
        assert rc == installer.OK, (tmp_path / "pd" / "install.log").read_text()
        pending = installer.pending_path(user)
        assert pending.exists()
        acl = subprocess.run(["icacls", str(pending)], capture_output=True, text=True).stdout
        assert "BUILTIN\\Users" not in acl and "Everyone" not in acl
        task = f"{installer.TASK_PREFIX} ({installer._safe_name(user)})"
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-ScheduledTask -TaskName '{task}').Principal.LogonType"],
                             capture_output=True, text=True).stdout
        assert "Interactive" in out
        assert "TEST-CODE" not in (tmp_path / "pd" / "install.log").read_text()
    finally:
        assert installer.cli("uninstall", ["--user", user]) == installer.OK
    assert not installer.pending_path(user).exists()


def test_bad_account_name_is_refused(tmp_path, monkeypatch):
    from graintime_agent import installer
    monkeypatch.setenv("GRAINTIME_AGENT_PROGRAMDATA", str(tmp_path / "pd"))
    assert installer.cli("install", ["--user", "bad;name", "--server", "http://x:1", "--code", "C"]) == installer.USAGE
