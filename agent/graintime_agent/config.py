"""Where the agent keeps its settings, and protecting its secret.

Settings live in %LOCALAPPDATA%\\GrainTimeAgent\\agent.json for the Windows
account that runs the scale software. The signing secret is encrypted with
Windows DPAPI for that account, so it cannot be used from another account or
another PC. Nothing else in the file is secret.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path

APP_NAME = "GrainTimeAgent"


def app_dir() -> Path:
    base = os.environ.get("GRAINTIME_AGENT_HOME") or os.path.join(
        os.environ.get("LOCALAPPDATA") or str(Path.home()), APP_NAME)
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return app_dir() / "agent.json"


# --------------------------------------------------------------------------- #
# DPAPI (Windows). Tests on other systems set GRAINTIME_AGENT_TEST_NO_DPAPI=1.
# --------------------------------------------------------------------------- #

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        if os.environ.get("GRAINTIME_AGENT_TEST_NO_DPAPI") == "1":
            return data
        raise RuntimeError("The GrainTime Agent stores its key with Windows DPAPI and runs on Windows only.")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    entropy_bytes = b"GrainTimeAgent"
    ebuf = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    entropy = _Blob(len(entropy_bytes), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(out))
    if not ok:
        raise OSError("Windows could not protect or read the agent's key (DPAPI).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(secret: str) -> str:
    return base64.b64encode(_dpapi(secret.encode(), True)).decode()


def unprotect(blob: str) -> str:
    return _dpapi(base64.b64decode(blob), False).decode()


# --------------------------------------------------------------------------- #
# State file
# --------------------------------------------------------------------------- #

def load() -> dict | None:
    p = state_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def save(server: str, key_id: str, secret: str, site: dict) -> None:
    data = {"server": server.rstrip("/"), "key_id": key_id, "secret_protected": protect(secret), "site": site}
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, state_path())


def secret_of(state: dict) -> str:
    return unprotect(state["secret_protected"])


def forget() -> None:
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass
