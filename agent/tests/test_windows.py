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
