"""Editing a site after setup: switches, archive/restore, permanent delete."""
from sqlalchemy import select

from graintime.common.db import get_sessionmaker
from graintime.common.models import AuditLog, CollectorJob, Site

from .conftest import SITE


def _new_site(c, **over):
    r = c.post("/api/admin/sites", json={**SITE, **over})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _actions():
    with get_sessionmaker()() as db:
        return [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "site")
                                             .order_by(AuditLog.id))]


def test_edit_details_and_switches(admin_client):
    sid = _new_site(admin_client)
    r = admin_client.patch(f"/api/admin/sites/{sid}", json={
        "name": " Celina North ", "address": "  ", "show_on_public": True, "show_on_dashboard": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "Celina North" and body["address"] is None
    assert body["show_on_public"] is True and body["show_on_dashboard"] is False
    assert _actions() == ["site.created", "site.updated"]


def test_polling_cannot_be_enabled_without_mapping(admin_client):
    sid = _new_site(admin_client)
    r = admin_client.patch(f"/api/admin/sites/{sid}", json={"polling_enabled": True})
    assert r.status_code == 409 and "mapping profile" in r.text
    assert admin_client.get(f"/api/admin/sites/{sid}").json()["polling_enabled"] is False


def test_archive_and_restore(admin_client):
    sid = _new_site(admin_client)
    admin_client.patch(f"/api/admin/sites/{sid}", json={"show_on_public": True})
    a = admin_client.post(f"/api/admin/sites/{sid}/archive").json()
    assert a["archived"] and not a["show_on_public"] and not a["show_on_dashboard"]
    # still listed (with its history), flagged archived
    assert [s["archived"] for s in admin_client.get("/api/admin/sites").json()] == [True]
    r = admin_client.patch(f"/api/admin/sites/{sid}", json={"show_on_public": True})
    assert r.status_code == 409
    # details stay editable while archived
    assert admin_client.patch(f"/api/admin/sites/{sid}", json={"port": 1500}).status_code == 200
    rs = admin_client.post(f"/api/admin/sites/{sid}/restore").json()
    assert not rs["archived"] and rs["show_on_dashboard"] and not rs["show_on_public"]
    assert _actions() == ["site.created", "site.updated", "site.archived", "site.updated",
                          "site.restored"]


def test_delete_requires_exact_name(admin_client):
    sid = _new_site(admin_client)
    admin_client.post(f"/api/admin/sites/{sid}/discovery", json={})
    r = admin_client.request("DELETE", f"/api/admin/sites/{sid}", json={"confirm_name": "celina"})
    assert r.status_code == 400
    assert admin_client.get(f"/api/admin/sites/{sid}").status_code == 200
    r = admin_client.request("DELETE", f"/api/admin/sites/{sid}", json={"confirm_name": "Celina"})
    assert r.status_code == 200
    assert admin_client.get(f"/api/admin/sites/{sid}").status_code == 404
    with get_sessionmaker()() as db:
        assert db.scalars(select(CollectorJob)).all() == []          # jobs went with it
        entry = db.scalars(select(AuditLog).where(AuditLog.action == "site.deleted")).one()
        assert db.get(Site, sid) is None
    assert entry.old_value["name"] == "Celina" and "password" not in entry.old_value
    # the short code is free again
    _new_site(admin_client)
