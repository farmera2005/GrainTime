from sqlalchemy import select

from graintime.common.db import get_sessionmaker
from graintime.common.models import AuditLog
from graintime.common.profiles import COMPUWEIGH_GMS

from .conftest import SITE


def _profiles(c):
    return c.get("/api/admin/mapping-profiles").json()


def test_seeded_profile_listed(admin_client):
    ps = _profiles(admin_client)
    assert [p["name"] for p in ps] == ["CompuWeigh GMS"]
    assert ps[0]["config"]["included_type_codes"] == ["TRUCKIN"] and ps[0]["sites"] == []


def test_create_validate_clone_delete(admin_client):
    c = admin_client
    bad = {"name": "Bad", "config": {**COMPUWEIGH_GMS, "id_column": "x;--"}}
    assert c.post("/api/admin/mapping-profiles", json=bad).status_code == 422
    r = c.post("/api/admin/mapping-profiles", json={"name": "GMS v2", "config": COMPUWEIGH_GMS})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert c.post("/api/admin/mapping-profiles", json={"name": "GMS v2", "config": COMPUWEIGH_GMS}).status_code == 409
    clone = c.post(f"/api/admin/mapping-profiles/{pid}/clone", json={}).json()
    assert clone["name"] == "GMS v2 (copy)"
    assert c.delete(f"/api/admin/mapping-profiles/{clone['id']}").status_code == 200
    with get_sessionmaker()() as db:
        actions = [a.action for a in db.scalars(select(AuditLog).where(
            AuditLog.entity_type == "mapping_profile").order_by(AuditLog.id))]
    assert actions == ["profile.created", "profile.cloned", "profile.deleted"]


def test_editing_profile_in_use_needs_confirmation(admin_client):
    c = admin_client
    pid = _profiles(c)[0]["id"]
    sid = c.post("/api/admin/sites", json=SITE).json()["id"]
    assert c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": pid}).json()["mapping_profile_id"] == pid
    assert _profiles(c)[0]["sites"][0]["id"] == sid
    changed = {"name": "CompuWeigh GMS", "config": {**COMPUWEIGH_GMS, "lookback_hours": 12}}
    r = c.put(f"/api/admin/mapping-profiles/{pid}", json=changed)
    assert r.status_code == 409 and "Celina" in r.text
    r = c.put(f"/api/admin/mapping-profiles/{pid}", json={**changed, "confirm_in_use": True})
    assert r.status_code == 200 and r.json()["config"]["lookback_hours"] == 12
    assert c.delete(f"/api/admin/mapping-profiles/{pid}").status_code == 409


def test_polling_needs_a_profile_and_clearing_it_stops_polling(admin_client):
    c = admin_client
    pid = _profiles(c)[0]["id"]
    sid = c.post("/api/admin/sites", json=SITE).json()["id"]
    r = c.patch(f"/api/admin/sites/{sid}", json={"polling_enabled": True})
    assert r.status_code == 409 and "mapping profile" in r.text
    r = c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": pid, "polling_enabled": True,
                                                 "poll_interval_s": 30})
    assert r.status_code == 200 and r.json()["polling_enabled"] and r.json()["poll_interval_s"] == 30
    r = c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": None})
    assert r.json()["polling_enabled"] is False and r.json()["mapping_profile_id"] is None
    assert c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": 999}).status_code == 422


def test_login_script(admin_client):
    c = admin_client
    pid = _profiles(c)[0]["id"]
    r = c.get(f"/api/admin/mapping-profiles/{pid}/login-script?database=GMS")
    assert r.status_code == 200
    assert "GRANT SELECT ON [dbo].[TransactionID] ([tid_pk]" in r.text and "tid_driver" not in r.text
    assert "INSERT" not in r.text.upper().replace("INSERTED", "")
    w = c.get(f"/api/admin/mapping-profiles/{pid}/login-script?database=GMS&login=MERCER%5Csvc&windows=true")
    assert "CREATE LOGIN [MERCER\\svc] FROM WINDOWS" in w.text
    assert c.get(f"/api/admin/mapping-profiles/{pid}/login-script?database=GMS%5D;DROP").status_code == 422


def test_preview_and_backfill_queueing(admin_client):
    c = admin_client
    pid = _profiles(c)[0]["id"]
    sid = c.post("/api/admin/sites", json=SITE).json()["id"]
    assert c.post(f"/api/admin/sites/{sid}/preview", json={}).status_code == 409   # no profile yet
    assert c.post(f"/api/admin/sites/{sid}/preview", json={"profile_id": pid}).status_code == 202
    assert c.post(f"/api/admin/sites/{sid}/backfill", json={"date_from": "2026-01-01",
                                                            "date_to": "2026-01-31"}).status_code == 409
    c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": pid})
    assert c.post(f"/api/admin/sites/{sid}/backfill", json={"date_from": "2026-02-01",
                                                            "date_to": "2026-01-01"}).status_code == 422
    assert c.post(f"/api/admin/sites/{sid}/backfill", json={"date_from": "2026-01-01",
                                                            "date_to": "2026-01-31"}).status_code == 202
    assert c.post(f"/api/admin/sites/{sid}/backfill", json={"date_from": "2026-01-01",
                                                            "date_to": "2026-01-31"}).status_code == 409
    st = c.get(f"/api/admin/sites/{sid}/collection").json()
    assert st["tickets"]["total"] == 0 and st["last_backfill"]["status"] == "queued"
