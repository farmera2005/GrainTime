import json
import logging

from sqlalchemy import select

from graintime.common import crypto
from graintime.common.db import get_sessionmaker
from graintime.common.models import AuditLog, CollectorJob, Site

from .conftest import SITE


def _all_text(client):
    """Every site-related response body, to prove the password never comes back."""
    out = client.get("/api/admin/sites").text
    for s in client.get("/api/admin/sites").json():
        out += client.get(f"/api/admin/sites/{s['id']}").text
    return out


def test_create_site_password_write_only_and_encrypted(admin_client, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    r = admin_client.post("/api/admin/sites", json=SITE)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "CELINA" and body["has_password"] is True
    assert body["polling_enabled"] is False and body["show_on_public"] is False
    assert "password" not in body
    assert SITE["password"] not in r.text + _all_text(admin_client)

    with get_sessionmaker()() as db:
        site = db.scalars(select(Site)).one()
        assert SITE["password"].encode() not in site.password_encrypted
        assert crypto.decrypt(site.password_encrypted) == SITE["password"]
        audits = db.scalars(select(AuditLog).where(AuditLog.entity_type == "site")).all()
    assert [a.action for a in audits] == ["site.created"]
    assert SITE["password"] not in json.dumps(audits[0].new_value)
    assert audits[0].new_value["password"] == "(changed)"

    logged = caplog.text + capsys.readouterr().out
    assert SITE["password"] not in logged


def test_duplicate_code_rejected(admin_client):
    assert admin_client.post("/api/admin/sites", json=SITE).status_code == 201
    r = admin_client.post("/api/admin/sites", json={**SITE, "name": "Other", "code": "CELINA"})
    assert r.status_code == 409


def test_validation_messages(admin_client):
    r = admin_client.post("/api/admin/sites", json={**SITE, "host": "SCALE01\\SQLEXPRESS"})
    assert r.status_code == 422 and "static TCP port" in r.text
    assert admin_client.post("/api/admin/sites", json={**SITE, "port": 70000}).status_code == 422
    assert admin_client.post("/api/admin/sites", json={**SITE, "password": ""}).status_code == 422


def test_update_keeps_password_unless_given(admin_client):
    sid = admin_client.post("/api/admin/sites", json=SITE).json()["id"]
    r = admin_client.patch(f"/api/admin/sites/{sid}", json={"port": 50123, "password": ""})
    assert r.status_code == 200 and r.json()["port"] == 50123
    with get_sessionmaker()() as db:
        assert crypto.decrypt(db.get(Site, sid).password_encrypted) == SITE["password"]
    admin_client.patch(f"/api/admin/sites/{sid}", json={"password": "new-pw-456"})
    with get_sessionmaker()() as db:
        assert crypto.decrypt(db.get(Site, sid).password_encrypted) == "new-pw-456"
        upd = db.scalars(select(AuditLog).where(AuditLog.action == "site.updated")
                         .order_by(AuditLog.id)).all()
    assert upd[0].old_value == {"port": 1433} and upd[0].new_value == {"port": 50123}
    assert upd[1].new_value == {"password": "(changed)"}
    assert "new-pw-456" not in json.dumps([u.new_value for u in upd])


def test_test_connection_job_stores_only_ciphertext(admin_client):
    conn = {k: SITE[k] for k in ("host", "port", "database", "username", "password", "encrypt",
                                 "trust_server_certificate")}
    r = admin_client.post("/api/admin/jobs/test-connection", json={"connection": conn})
    assert r.status_code == 202 and SITE["password"] not in r.text
    with get_sessionmaker()() as db:
        job = db.get(CollectorJob, r.json()["id"])
        assert job.status == "queued"
        assert SITE["password"] not in json.dumps(job.params)
        assert job.params["connection"]["password_encrypted"]


def test_test_connection_needs_password_when_unsaved(admin_client):
    conn = {k: SITE[k] for k in ("host", "port", "database", "username")}
    assert admin_client.post("/api/admin/jobs/test-connection",
                             json={"connection": conn}).status_code == 422


def test_discovery_job_queued_and_audited(admin_client):
    sid = admin_client.post("/api/admin/sites", json=SITE).json()["id"]
    r = admin_client.post(f"/api/admin/sites/{sid}/discovery", json={})
    assert r.status_code == 202 and r.json()["status"] == "queued"
    jobs = admin_client.get(f"/api/admin/sites/{sid}/jobs?kind=discovery").json()
    assert [j["id"] for j in jobs] == [r.json()["id"]]
    assert admin_client.get(f"/api/admin/jobs/{r.json()['id']}/report.md").status_code == 404
    with get_sessionmaker()() as db:
        assert db.scalars(select(AuditLog.action).where(
            AuditLog.action == "site.discovery_started")).one()
