"""Installing for the current Windows account (no administrator rights needed).

- The program copies itself to %LOCALAPPDATA%\\GrainTimeAgent\\GrainTimeAgent.exe
  so it no longer depends on where it was downloaded.
- It starts at sign-in through the account's Run key
  (HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run), like other per-user
  programs: it runs whenever the scale software's account is signed in, which
  is exactly when the scale software can run.
- A named mutex keeps a second copy from running.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import APP_NAME, app_dir

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_mutex = None


def installed_exe() -> Path:
    return app_dir() / f"{APP_NAME}.exe"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_copy() -> Path:
    """Copy the running .exe into the app folder (frozen builds only)."""
    target = installed_exe()
    if is_frozen():
        src = Path(sys.executable)
        if src.resolve() != target.resolve():
            shutil.copy2(src, target)
        return target
    return Path(sys.executable)


def enable_autostart(exe: Path) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" run')


def disable_autostart() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except FileNotFoundError:
        pass


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def start_background(exe: Path) -> None:
    flags = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    args = [str(exe), "run"] if is_frozen() else [sys.executable, "-m", "graintime_agent", "run"]
    subprocess.Popen(args, creationflags=flags, close_fds=True)


def single_instance() -> bool:
    """True if no other agent is running for this account."""
    global _mutex
    if sys.platform != "win32":
        return True
    import ctypes
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\GrainTimeAgent")
    return ctypes.windll.kernel32.GetLastError() != 183    # ERROR_ALREADY_EXISTS


def stop_running_agents() -> None:
    """Stop other copies of the agent (used before removing or re-pairing)."""
    if sys.platform == "win32":
        me = os.getpid()
        subprocess.run(["taskkill", "/F", "/FI", f"PID ne {me}", "/IM", f"{APP_NAME}.exe"],
                       capture_output=True, creationflags=0x08000000)
