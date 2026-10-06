"""Every admin endpoint refuses anonymous callers (401) and viewers (403)."""
import re

import pytest

from graintime.api.main import app

ADMIN_PREFIXES = ("/api/admin/", "/api/settings/")
ADMIN_EXACT = {"/api/setup/complete"}


def _walk(routes):
    for r in routes:
        inner = getattr(r, "original_router", None)  # routers included lazily
        if inner is not None:
            yield from _walk(inner.routes)
        else:
            yield r


def _admin_routes():
    out = []
    for r in _walk(app.routes):
        path = getattr(r, "path", "")
        if path.startswith(ADMIN_PREFIXES) or path in ADMIN_EXACT:
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out.append((m, re.sub(r"\{[^}]+\}", "1", path)))
    return out


ROUTES = _admin_routes()


def test_admin_routes_discovered():
    assert len(ROUTES) >= 10


@pytest.mark.parametrize("method,path", ROUTES)
def test_anonymous_rejected(client, method, path):
    assert client.request(method, path, json={}).status_code == 401


@pytest.mark.parametrize("method,path", ROUTES)
def test_viewer_rejected(client, make_user, method, path):
    pw = make_user("viewer1", "viewer")
    assert client.post("/api/auth/login", json={"username": "viewer1", "password": pw}).status_code == 200
    assert client.request(method, path, json={}).status_code == 403
