"""End to end against the mock CompuWeigh GMS database (graintime.devtools.mock_gms).

Needs GMS_TEST_HOST (+ GMS_TEST_PORT, GMS_TEST_SA_PASSWORD) pointing at a SQL
Server seeded by mock_gms; skipped otherwise. Uses the restricted `graintime`
login created from the profile's login script.
"""
import os
import random
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from graintime.collector.jobs import JobRunner
from graintime.collector.poller import Poller
from graintime.common.db import get_sessionmaker
from graintime.common.models import SiteCollectorState, Ticket

HOST = os.environ.get("GMS_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="GMS_TEST_HOST not set")


def mock_conn():
    os.environ.setdefault("MOCK_SQL_HOST", HOST)
    os.environ.setdefault("MOCK_SA_PASSWORD", os.environ.get("GMS_TEST_SA_PASSWORD", ""))
    from graintime.devtools import mock_gms
    return mock_gms, mock_gms.connect("GMS")


@pytest.fixture
def site(admin_client):
    c = admin_client
    pid = c.get("/api/admin/mapping-profiles").json()[0]["id"]
    r = c.post("/api/admin/sites", json={
        "name": "Mock GMS", "code": "MOCK", "host": HOST, "port": int(os.environ.get("GMS_TEST_PORT", 1433)),
        "database": "GMS", "username": "graintime", "password": "Mock-Collector-2026!",
        "encrypt": "yes", "trust_server_certificate": True})
    sid = r.json()["id"]
    c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": pid, "polling_enabled": True})
    return c, sid


def _count(sid):
    with get_sessionmaker()() as db:
        return db.scalar(select(func.count()).select_from(Ticket).where(Ticket.site_id == sid))


def test_preview_poll_live_backfill(site):
    c, sid = site
    runner = JobRunner(max_workers=1)

    # Preview with the restricted login
    j = c.post(f"/api/admin/sites/{sid}/preview", json={"limit": 50}).json()["id"]
    runner.run(j)
    pv = c.get(f"/api/admin/jobs/{j}").json()
    assert pv["status"] == "succeeded", pv
    res = pv["result"]
    assert res["summary"]["types"]["1"] == "TRUCKIN" and res["summary"]["included"] > 0
    assert any(not t["included"] for t in res["tickets"])           # other types read but not tracked
    assert set(res["status_examples"]) == {"2", "4"}

    # First poll: high-water mark starts at the newest ticket; only the look-back window is read
    p = Poller(runner)
    p.poll_site(sid, 60)
    st = c.get(f"/api/admin/sites/{sid}/collection").json()
    assert st["state"]["last_error"] is None and st["state"]["high_water_mark"]
    first = _count(sid)
    assert first > 0 and st["stale"] is False

    # A new truck arrives at the scale and another finishes
    mock_gms, conn = mock_conn()
    gen = mock_gms.Gen(conn, random.Random(7))
    now = mock_gms.local_now()
    gen.truck(now - timedelta(minutes=1), now)
    gen.live_step(now + timedelta(hours=2), speedup=1)               # completes trucks on site
    p.poll_site(sid, 60)
    st2 = c.get(f"/api/admin/sites/{sid}/collection").json()
    assert int(st2["state"]["high_water_mark"]) > int(st["state"]["high_water_mark"])
    assert _count(sid) >= first

    # Backfill the last 7 days, then again: idempotent
    body = {"date_from": (date.today() - timedelta(days=7)).isoformat(), "date_to": date.today().isoformat()}
    j = c.post(f"/api/admin/sites/{sid}/backfill", json=body).json()["id"]
    runner.run(j)
    bf = c.get(f"/api/admin/jobs/{j}").json()
    assert bf["status"] == "succeeded", bf
    after = _count(sid)
    assert after > first and bf["result"]["days_done"] == 8
    j = c.post(f"/api/admin/sites/{sid}/backfill", json=body).json()["id"]
    runner.run(j)
    assert _count(sid) == after

    with get_sessionmaker()() as db:
        statuses = dict(db.execute(select(Ticket.status, func.count()).where(Ticket.site_id == sid)
                                   .group_by(Ticket.status)).all())
        types = set(db.scalars(select(Ticket.transaction_type).where(Ticket.site_id == sid)))
        singles = db.scalar(select(func.count()).select_from(Ticket).where(
            Ticket.site_id == sid, Ticket.single_weigh.is_(True)))
        durations = db.scalars(select(Ticket.duration_s).where(
            Ticket.site_id == sid, Ticket.duration_s.is_not(None))).all()
    assert types == {"TRUCKIN"}
    assert statuses.get("completed") and statuses.get("voided")
    assert singles > 0
    assert 3 * 60 < sorted(durations)[len(durations) // 2] < 60 * 60      # median stay is plausible

    # Split children never stored as their own tickets
    cur = conn.cursor()
    cur.execute("SELECT CAST(tid_pk AS varchar(20)) FROM dbo.TransactionID WHERE tid_parenttidfk IS NOT NULL")
    children = {r[0] for r in cur.fetchall()}
    with get_sessionmaker()() as db:
        stored = set(db.scalars(select(Ticket.source_ticket_id).where(Ticket.site_id == sid)))
    assert children and not (children & stored)
    with get_sessionmaker()() as db:
        assert db.get(SiteCollectorState, sid).consecutive_failures == 0
