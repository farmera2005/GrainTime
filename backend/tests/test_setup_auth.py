from datetime import datetime, timedelta, timezone

from graintime.api import routes_setup

PW = "correct-horse-battery"


def test_fresh_install_status(client):
    s = client.get("/api/setup/status").json()
    assert s == {**s, "setup_complete": False, "admin_exists": False, "admin_window_open": True,
                 "user": None}


def test_first_admin_created_once_and_signed_in(client):
    r = client.post("/api/setup/admin", json={"username": "IT.Admin", "display_name": "IT",
                                              "password": PW})
    assert r.status_code == 200
    assert r.json()["username"] == "it.admin" and r.json()["role"] == "admin"
    s = client.get("/api/setup/status").json()
    assert s["user"]["username"] == "it.admin" and s["admin_exists"]
    assert s["steps"] == {"admin": True, "defaults": False, "site": False, "discovery": False}
    r2 = client.post("/api/setup/admin", json={"username": "other", "display_name": "x",
                                               "password": PW})
    assert r2.status_code == 409


def test_short_password_rejected(client):
    r = client.post("/api/setup/admin", json={"username": "a1b", "display_name": "x",
                                              "password": "short"})
    assert r.status_code == 422


def test_first_run_window_closes(client, monkeypatch):
    monkeypatch.setattr(routes_setup, "STARTED_AT",
                        datetime.now(timezone.utc) - timedelta(minutes=61))
    assert client.get("/api/setup/status").json()["admin_window_open"] is False
    r = client.post("/api/setup/admin", json={"username": "late", "display_name": "x",
                                              "password": PW})
    assert r.status_code == 403


def test_login_logout_and_throttle(admin_client):
    c = admin_client
    c.post("/api/auth/logout")
    assert c.get("/api/setup/status").json()["user"] is None
    assert c.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "ADMIN", "password": PW}).status_code == 200
    c.post("/api/auth/logout")
    for _ in range(10):
        c.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW}).status_code == 429


def test_csrf_header_required(admin_client):
    r = admin_client.post("/api/setup/complete", headers={"X-GrainTime": ""})
    assert r.status_code == 403


def test_defaults_validation_and_audit(admin_client):
    c = admin_client
    d = c.get("/api/settings/defaults").json()
    assert d["threshold_green_max_min"] < d["threshold_yellow_max_min"]
    bad = {**d, "threshold_green_max_min": 50, "threshold_yellow_max_min": 40}
    assert c.put("/api/settings/defaults", json=bad).status_code == 422
    good = {**d, "threshold_green_max_min": 15, "threshold_yellow_max_min": 30}
    assert c.put("/api/settings/defaults", json=good).status_code == 200
    assert c.get("/api/settings/defaults").json()["threshold_green_max_min"] == 15
    assert c.get("/api/setup/status").json()["steps"]["defaults"] is True

    from sqlalchemy import select

    from graintime.common.db import get_sessionmaker
    from graintime.common.models import AuditLog
    with get_sessionmaker()() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
        entry = db.scalars(select(AuditLog).where(AuditLog.action == "settings.defaults_updated")).one()
    assert actions == ["setup.admin_created", "settings.defaults_updated"]
    assert entry.old_value["threshold_green_max_min"] == 20
    assert entry.new_value["threshold_green_max_min"] == 15
    assert entry.actor_name == "admin"


def test_complete_setup(admin_client):
    assert admin_client.post("/api/setup/complete").json() == {"setup_complete": True}
    assert admin_client.get("/api/setup/status").json()["setup_complete"] is True
