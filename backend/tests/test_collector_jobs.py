import json

import pytest
from sqlalchemy import select

from graintime.collector import jobs as jobs_mod
from graintime.collector import sitedb
from graintime.collector.jobs import JobRunner
from graintime.common.db import get_sessionmaker
from graintime.common.models import CollectorJob

from .conftest import SITE


@pytest.fixture
def site_id(admin_client):
    return admin_client.post("/api/admin/sites", json=SITE).json()["id"]


def _queue(kind, site_id=None, params=None):
    with get_sessionmaker()() as db:
        j = CollectorJob(kind=kind, site_id=site_id, params=params or {}, status="queued")
        db.add(j)
        db.commit()
        return j.id


def _job(job_id):
    with get_sessionmaker()() as db:
        return db.get(CollectorJob, job_id)


def test_failure_is_classified_and_params_wiped(admin_client, monkeypatch):
    seen = {}

    def fake_connect(spec):
        seen["password"] = spec.password
        raise sitedb.SiteConnectionError("port_closed", "Port closed", "Enable TCP/IP")

    monkeypatch.setattr(jobs_mod.sitedb, "connect", fake_connect)
    conn = {k: SITE[k] for k in ("host", "port", "database", "username", "password")}
    job_id = admin_client.post("/api/admin/jobs/test-connection", json={"connection": conn}).json()["id"]
    runner = JobRunner(max_workers=1)
    runner.run(job_id)
    job = _job(job_id)
    assert seen["password"] == SITE["password"]  # decrypted only inside the collector
    assert job.status == "failed" and job.error["code"] == "port_closed"
    assert job.error["docs"].endswith("port-closed")
    assert job.params == {}
    assert SITE["password"] not in json.dumps(job.error)


def test_saved_site_uses_stored_password(site_id, monkeypatch):
    seen = {}

    class FakeConn:
        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(jobs_mod.sitedb, "connect", lambda spec: seen.update(spec=spec) or FakeConn())
    monkeypatch.setattr(jobs_mod.sitedb, "server_info",
                        lambda c: {"product_version": "16.0.1", "edition": "Express Edition"})
    job_id = _queue("test_connection", site_id)
    JobRunner(max_workers=1).run(job_id)
    job = _job(job_id)
    assert job.status == "succeeded" and job.result["edition"] == "Express Edition"
    assert seen["spec"].password == SITE["password"] and seen["closed"]


def test_one_job_per_site_at_a_time(site_id):
    a = _queue("test_connection", site_id)
    b = _queue("discovery", site_id)
    runner = JobRunner(max_workers=4)
    first = runner._claim()
    assert first == (a, f"site:{site_id}")
    runner.busy.add(first[1])
    assert runner._claim() is None          # same site: waits
    assert _job(b).status == "queued"
    runner.busy.clear()
    assert runner._claim()[0] == b


def test_cancelled_before_start(site_id):
    j = _queue("discovery", site_id)
    with get_sessionmaker()() as db:
        db.get(CollectorJob, j).cancel_requested = True
        db.commit()
    assert JobRunner()._claim() is None
    assert _job(j).status == "cancelled"


def test_interrupted_jobs_failed_on_restart(site_id):
    j = _queue("discovery", site_id)
    with get_sessionmaker()() as db:
        db.get(CollectorJob, j).status = "running"
        db.commit()
    JobRunner().recover_interrupted()
    assert _job(j).status == "failed" and _job(j).error["code"] == "interrupted"


def test_unreadable_password_reported(site_id):
    from cryptography.fernet import Fernet
    with get_sessionmaker()() as db:
        from graintime.common.models import Site
        db.get(Site, site_id).password_encrypted = Fernet(Fernet.generate_key()).encrypt(b"x")
        db.commit()
    j = _queue("test_connection", site_id)
    JobRunner().run(j)
    assert _job(j).error["code"] == "password_unreadable"


def test_job_status_endpoint_hides_full_report(admin_client, site_id):
    j = _queue("discovery", site_id)
    with get_sessionmaker()() as db:
        job = db.get(CollectorJob, j)
        job.status = "succeeded"
        job.result = {"summary": {"tables": 3}, "report": {"tables": []}, "markdown": "# r"}
        db.commit()
    body = admin_client.get(f"/api/admin/jobs/{j}").json()
    assert body["result"] == {"summary": {"tables": 3}}
    assert admin_client.get(f"/api/admin/jobs/{j}/report.md").text == "# r"
    assert admin_client.get(f"/api/admin/jobs/{j}/report.json").json() == {"tables": []}
    assert len(select(CollectorJob).compile().params) == 0
