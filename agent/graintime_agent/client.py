"""Talking to GrainTime: pairing, and HMAC-signed requests.

Every request after pairing is signed with the agent's secret over the method,
path, a millisecond timestamp and the body's SHA-256. The server rejects old,
replayed or altered requests; the secret itself is never sent again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from . import VERSION

TIMEOUT_S = 20


class ServerError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


def urllib_transport(method: str, url: str, headers: dict, body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _detail(raw: bytes) -> str:
    try:
        d = json.loads(raw).get("detail")
        return d if isinstance(d, str) else json.dumps(d)[:300]
    except (ValueError, AttributeError):
        return raw[:300].decode(errors="replace")


class Server:
    def __init__(self, base_url: str, key_id: str | None = None, secret: str | None = None,
                 transport=urllib_transport):
        self.base = base_url.rstrip("/")
        self.key_id, self.secret, self.transport = key_id, secret, transport
        self._last_ms = 0

    def _send(self, path: str, payload: dict, signed: bool) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "X-GrainTime": "1", "User-Agent": f"GrainTimeAgent/{VERSION}"}
        if signed:
            ts = max(int(time.time() * 1000), self._last_ms + 1)
            self._last_ms = ts
            msg = f"POST\n{path}\n{ts}\n{hashlib.sha256(body).hexdigest()}"
            headers.update({"X-GT-Agent": self.key_id, "X-GT-Time": str(ts),
                            "X-GT-Signature": hmac.new(self.secret.encode(), msg.encode(),
                                                       hashlib.sha256).hexdigest()})
        status, raw = self.transport("POST", self.base + path, headers, body)
        if status >= 400:
            raise ServerError(status, _detail(raw))
        return json.loads(raw or b"{}")

    def pair(self, code: str, hostname: str, windows_user: str) -> dict:
        return self._send("/api/agent/pair", {"code": code, "hostname": hostname,
                                              "windows_user": windows_user, "version": VERSION}, signed=False)

    def post(self, path: str, payload: dict) -> dict:
        return self._send(path, payload, signed=True)
