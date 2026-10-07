"""Collector polling: state, back-off, recovery, isolation, picking up changes."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from graintime.collector import collect, poller as poller_mod, sitedb
from graintime.collector.jobs import JobRunner
from graintime.collector.poller import Poller, backoff_seconds
from graintime.common.db import get_sessionmaker
from graintime.common.models import SiteCollectorState, Ticket

from .conftest import SITE


@pytest.fixture
def sites(admin_client):
    c = admin_client
    pid = c.get("/api/admin/mapping-profiles").json()[0]["id"]
    ids = []
    for code in ("AAA", "BBB"):
        sid = c.post("/api/admin/sites", json={**SITE, "code": code, "name": code}).json()["id"]
        c.patch(f"/api/admin/sites/{sid}", json={"mapping_profile_id": pid, "polling_enabled": True})
        ids.append(sid)
    return c, ids


class FakeConn:
    def close(self):
        pass


def ticket(tid, status="open"):
    return {"source_ticket_id": str(tid), "ticket_number": None, "status": status, "raw_status": "0",
            "direction": "received", "transaction_type": "TRUCKIN", "commodity": "Corn",
            "inbound_at": datetime.now(timezone.utc) - timedelta(minutes=5), "outbound_at": None,
            "duration_s": None, "single_weigh": False, "source_created_at": None, "included": True}


@pytest.fixture
def fake_site(monkeypatch):
    """Site AAA answers; BBB is unreachable unless told otherwise."""
    behaviour = {"down": {"BBB"}, "calls": []}

    def open_connection(spec):
        behaviour["calls"].append(spec.username)
        return FakeConn(), {}

    def poll(conn, profile, hwm, lookups, recheck):
        site = behaviour["current"]
        if site in behaviour["down"]:
            raise sitedb.SiteConnectionError("host_unreachable", "Host unreachable", "fix")
        return {"tickets": [ticket(100), ticket(101)], "high_water": "101", "lookups": object(),
                "rows_read": 2, "merged": 0, "rechecked": 0}

    real_poll_site = Poller.poll_site

    def poll_site(self, site_id, interval):
        from graintime.common.models import Site
        with get_sessionmaker()() as db:
            behaviour["current"] = db.get(Site, site_id).code
        return real_poll_site(self, site_id, interval)

    monkeypatch.setattr(poller_mod.sitedb, "open_connection", open_connection)
    monkeypatch.setattr(poller_mod.collect, "poll", poll)
    monkeypatch.setattr(Poller, "poll_site", poll_site)
    return behaviour


def _state(sid):
    with get_sessionmaker()() as db:
        return db.get(SiteCollectorState, sid)


def test_success_and_failure_are_isolated(sites, fake_site):
    c, (a, b) = sites
    p = Poller(JobRunner())
    due = dict(p.due_sites())
    assert set(due) == {a, b}
    for sid in (a, b):
        p.poll_site(sid, due[sid])
    sa, sb = _state(a), _state(b)
    assert sa.high_water_mark == "101" and sa.consecutive_failures == 0 and sa.last_error is None
    assert sa.rows_last_poll == 2 and sa.next_poll_at > datetime.now(timezone.utc)
    assert sb.consecutive_failures == 1 and sb.last_error["code"] == "host_unreachable"
    with get_sessionmaker()() as db:
        assert len(db.scalars(select(Ticket).where(Ticket.site_id == a)).all()) == 2
    sites_out = {s["id"]: s for s in c.get("/api/admin/sites").json()}
    assert sites_out[a]["stale"] is False and sites_out[a]["trucks_today"] == 2
    assert sites_out[b]["stale"] is True and sites_out[b]["last_error"]["code"] == "host_unreachable"


def test_backoff_grows_then_recovers(sites, fake_site):
    _, (_, b) = sites
    p = Poller(JobRunner())
    gaps = []
    for _ in range(3):
        before = datetime.now(timezone.utc)
        p.poll_site(b, 60)
        gaps.append((_state(b).next_poll_at - before).total_seconds())
    assert gaps[0] < gaps[1] < gaps[2]
    assert backoff_seconds(60, 20) == 30 * 60                     # capped
    fake_site["down"].clear()                                      # site comes back
    p.poll_site(b, 60)
    st = _state(b)
    assert st.consecutive_failures == 0 and st.last_error is None and st.high_water_mark == "101"


def test_repolling_is_idempotent(sites, fake_site):
    _, (a, _) = sites
    p = Poller(JobRunner())
    for _ in range(3):
        p.poll_site(a, 60)
    with get_sessionmaker()() as db:
        assert len(db.scalars(select(Ticket)).all()) == 2
    assert _state(a).rows_total == 6       # rows written each poll; ticket count unchanged


def test_admin_changes_picked_up_without_restart(sites, fake_site):
    c, (a, b) = sites
    p = Poller(JobRunner())
    c.patch(f"/api/admin/sites/{b}", json={"polling_enabled": False})
    assert [s for s, _ in p.due_sites()] == [a]
    c.post(f"/api/admin/sites/{a}/archive")
    assert p.due_sites() == []
    c.post(f"/api/admin/sites/{a}/restore")
    c.patch(f"/api/admin/sites/{a}", json={"polling_enabled": True, "poll_interval_s": 120})
    assert p.due_sites() == [(a, 120)]
    p.poll_site(a, 120)
    assert p.due_sites() == []             # not due again until its interval passes


def test_busy_site_is_not_polled_twice(sites, fake_site):
    _, (a, b) = sites
    runner = JobRunner()
    runner.busy.add(f"site:{a}")           # e.g. a backfill is running
    p = Poller(runner)
    started = p.tick()
    p.pool.shutdown(wait=True)
    assert started == 1 and _state(a) is None


def test_mapping_errors_are_classified():
    err = collect.classify_mapping_error(Exception("[42S02] Invalid object name 'dbo.TransactionIDX'. (208)"))
    assert err.code == "mapping_table_missing"
    err = collect.classify_mapping_error(Exception("The SELECT permission was denied on the column 'x' (230)"))
    assert err.code == "permission_denied" and "login script" in err.fix
