"""Agent sites: pairing, signed requests, sync, jobs, and keeping the collector away."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from graintime.api.routes_agents import signature
from graintime.collector.mapping import encode_value
from graintime.common.profiles import AGVANTAGE_PC


@pytest.fixture
def agv_profile_id(admin_client):
    r = admin_client.post("/api/admin/mapping-profiles", json={"name": "AgVantagePC", "description": None,
                                                                "config": AGVANTAGE_PC})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def agent_site(admin_client, agv_profile_id):
    r = admin_client.post("/api/admin/sites/agent", json={"name": "New Weston", "code": "NEWW"})
    assert r.status_code == 201, r.text
    assert r.json()["source"] == "agent" and r.json()["mapping_profile_id"] == agv_profile_id
    assert r.json()["host"] is None and r.json()["agent"]["paired"] is False
    return r.json()["id"]


class Agent:
    def __init__(self, client, key_id, secret):
        self.c, self.key_id, self.secret, self.last = client, key_id, secret, 0

    def post(self, path, payload, ts=None, sig=None):
        body = json.dumps(payload).encode()
        ts = ts if ts is not None else max(int(time.time() * 1000), self.last + 1)
        self.last = ts
        headers = {"X-GT-Agent": self.key_id, "X-GT-Time": str(ts), "Content-Type": "application/json",
                   "X-GT-Signature": sig or signature(self.secret, "POST", path, str(ts), body)}
        return self.c.post(path, content=body, headers=headers)


@pytest.fixture
def paired(admin_client, agent_site):
    code = admin_client.post(f"/api/admin/sites/{agent_site}/agent/pairing-code").json()["code"]
    r = admin_client.post("/api/agent/pair", json={"code": code.lower().replace("-", " "), "hostname": "DESKTOP-512",
                                                   "windows_user": "MERCERLANDMARK\\nwscale", "version": "1.0.0"})
    assert r.status_code == 200, r.text
    assert r.json()["site"]["code"] == "NEWW"
    # The code works once.
    assert admin_client.post("/api/agent/pair", json={"code": code}).status_code == 401
    return Agent(admin_client, r.json()["key_id"], r.json()["secret"])


def row(load, grs, tar, **kw):
    r = {"st_load": load, "st_scaleticket": "6799" + load[:2], "st_status": "CPT", "st_date": grs.replace(hour=0, minute=0),
         "st_completedDate": tar, "st_type": "P", "st_desc": "BEANS", "st_grsdate": grs, "st_tardate": tar,
         "st_inout": "I", "st_grsmanual": 0, "st_tarmanual": 0, **kw}
    return {k: encode_value(v) for k, v in r.items()}


def sync(agent, poll=None, **status):
    return agent.post("/api/agent/sync", {"agent": {"version": "1.0.0", "clock_utc": datetime.now(timezone.utc).isoformat()},
                                          "status": {"instance": "running", "database": "C:\\...\\LOCALSET.MDF", **status},
                                          **({"poll": poll} if poll else {})})


def test_status_shows_paired_agent(admin_client, paired, agent_site):
    s = admin_client.get(f"/api/admin/sites/{agent_site}/agent").json()
    assert s["paired"] and s["hostname"] == "DESKTOP-512" and s["windows_user"] == "MERCERLANDMARK\\nwscale"


def test_signature_timestamp_and_replay_are_checked(paired):
    assert sync(paired).status_code == 200
    assert paired.post("/api/agent/sync", {}, sig="0" * 64).status_code == 401                 # bad signature
    old = int((time.time() - 600) * 1000)
    assert paired.post("/api/agent/sync", {}, ts=old).status_code == 401                       # 10 min old
    ts = int(time.time() * 1000) + 5
    assert paired.post("/api/agent/sync", {}, ts=ts).status_code == 200
    assert paired.post("/api/agent/sync", {}, ts=ts).status_code == 401                        # replay


def test_expired_or_unknown_codes_refused(admin_client, agent_site):
    assert admin_client.post("/api/agent/pair", json={"code": "ABCD-EFGH"}).status_code == 401
    code = admin_client.post(f"/api/admin/sites/{agent_site}/agent/pairing-code").json()["code"]
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import SiteAgent
    with get_sessionmaker()() as db:
        db.get(SiteAgent, agent_site).pairing_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert admin_client.post("/api/agent/pair", json={"code": code}).status_code == 401


def test_sync_polls_store_tickets_and_keep_the_high_water_mark(admin_client, paired, agent_site):
    first = sync(paired).json()
    assert first["poll"] is None and first["site"]["code"] == "NEWW"         # polling off until an admin turns it on
    assert admin_client.patch(f"/api/admin/sites/{agent_site}", json={"polling_enabled": True}).status_code == 200
    instr = sync(paired).json()
    assert instr["poll"]["high_water"] is None and instr["poll"]["profile"]["ticket_table"] == "dbo.scale_ticket"
    assert instr["database_match"] == "LOCALSET.MDF"
    fp = instr["poll"]["fingerprint"]
    rows = [row("24590R", datetime(2026, 10, 7, 9, 0), datetime(2026, 10, 7, 9, 18)),
            row("24591R", datetime(2026, 10, 7, 13, 50), datetime(2026, 10, 7, 14, 3), st_name="FARMER JOE"),
            row("24592R", datetime(2026, 10, 7, 15, 0), datetime(2026, 10, 7, 15, 30), st_type="T")]
    r = sync(paired, {"fingerprint": fp, "high_water": "24592R", "rows": rows}).json()
    assert r["poll"]["high_water"] == "24592R"
    col = admin_client.get(f"/api/admin/sites/{agent_site}/collection").json()
    assert col["tickets"]["total"] == 2 and col["stale"] is False                # the transfer isn't stored
    assert col["state"]["last_error"] is None
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import Ticket
    with get_sessionmaker()() as db:
        t = db.query(Ticket).filter_by(source_ticket_id="24590R").one()
        assert t.duration_s == 18 * 60 and t.direction == "received" and t.commodity == "BEANS"
    # Rows read under an older profile are ignored, not misread.
    sync(paired, {"fingerprint": "stale", "high_water": "99999R", "rows": rows})
    assert sync(paired).json()["poll"]["high_water"] == "24592R"


def test_agent_errors_back_off_and_show(admin_client, paired, agent_site):
    admin_client.patch(f"/api/admin/sites/{agent_site}", json={"polling_enabled": True})
    r1 = sync(paired, error={"code": "query_failed", "cause": "Reading tickets failed", "fix": "x"}).json()
    r2 = sync(paired, error={"code": "query_failed", "cause": "Reading tickets failed", "fix": "x"}).json()
    assert r2["next_sync_s"] > r1["next_sync_s"]
    col = admin_client.get(f"/api/admin/sites/{agent_site}/collection").json()
    assert col["state"]["consecutive_failures"] == 2 and col["state"]["last_error"]["code"] == "query_failed"
    s = admin_client.get(f"/api/admin/sites/{agent_site}/agent").json()
    assert s["error"]["code"] == "query_failed" and s["instance"] == "running"


def test_preview_runs_through_the_agent(admin_client, paired, agent_site):
    job = admin_client.post(f"/api/admin/sites/{agent_site}/preview", json={"limit": 5}).json()
    from graintime.collector.jobs import JobRunner
    assert JobRunner()._claim() is None                       # the collector never takes agent jobs
    got = sync(paired).json()["job"]
    assert got["id"] == job["id"] and got["kind"] == "preview" and got["profile"]["ticket_table"] == "dbo.scale_ticket"
    rows = [row("24591R", datetime(2026, 10, 7, 13, 50), datetime(2026, 10, 7, 14, 3)),
            row("24593R", datetime(2026, 10, 7, 15, 0), datetime(2026, 10, 7, 15, 30), st_status="V")]
    r = paired.post(f"/api/agent/jobs/{job['id']}", {"rows": rows, "status_examples": {"V": [rows[1]]}, "done": True})
    assert r.status_code == 200, r.text
    res = admin_client.get(f"/api/admin/jobs/{job['id']}").json()
    assert res["status"] == "succeeded" and res["result"]["summary"]["completed"] == 1
    assert res["result"]["summary"]["voided"] == 1 and "V" in res["result"]["status_examples"]


def test_backfill_runs_through_the_agent_and_can_be_cancelled(admin_client, paired, agent_site):
    job = admin_client.post(f"/api/admin/sites/{agent_site}/backfill",
                            json={"date_from": "2026-10-06", "date_to": "2026-10-07"}).json()
    got = sync(paired).json()["job"]
    assert got["kind"] == "backfill" and got["options"]["date_from"] == "2026-10-06"
    day1 = [row("24580R", datetime(2026, 10, 6, 8, 0), datetime(2026, 10, 6, 8, 20))]
    r = paired.post(f"/api/agent/jobs/{job['id']}", {"rows": day1, "progress": {"step": "2026-10-06", "done": 1, "total": 2}})
    assert r.json() == {"cancel": False, "status": "running"}
    assert admin_client.get(f"/api/admin/jobs/{job['id']}").json()["progress"]["tickets_stored"] == 1
    admin_client.post(f"/api/admin/jobs/{job['id']}/cancel")
    r = paired.post(f"/api/agent/jobs/{job['id']}", {"rows": [], "progress": {"done": 1, "total": 2}})
    assert r.json()["cancel"] is True
    paired.post(f"/api/agent/jobs/{job['id']}", {"done": True})
    assert admin_client.get(f"/api/admin/jobs/{job['id']}").json()["status"] == "cancelled"


def test_job_left_running_by_a_restarted_agent_fails(admin_client, paired, agent_site):
    job = admin_client.post(f"/api/admin/sites/{agent_site}/preview", json={"limit": 5}).json()
    assert sync(paired).json()["job"]["id"] == job["id"]
    sync(paired)                                   # the agent no longer reports it as active
    assert admin_client.get(f"/api/admin/jobs/{job['id']}").json()["status"] == "failed"


def test_sql_only_actions_and_profiles_refused(admin_client, agent_site):
    assert admin_client.post("/api/admin/jobs/test-connection", json={"site_id": agent_site}).status_code == 409
    assert admin_client.post(f"/api/admin/sites/{agent_site}/discovery",
                             json={"extra_tables": [], "include_samples": False, "mask": True}).status_code == 409
    gms = next(p["id"] for p in admin_client.get("/api/admin/mapping-profiles").json() if p["name"] == "CompuWeigh GMS")
    r = admin_client.patch(f"/api/admin/sites/{agent_site}", json={"mapping_profile_id": gms})
    assert r.status_code == 422 and "weigh steps" in r.text
    assert admin_client.patch(f"/api/admin/sites/{agent_site}", json={"host": "10.0.0.5"}).status_code == 409


def test_revoked_agent_is_locked_out(admin_client, paired, agent_site):
    assert sync(paired).status_code == 200
    admin_client.post(f"/api/admin/sites/{agent_site}/agent/revoke")
    assert sync(paired).status_code == 401


def test_agent_routes_need_signatures(client):
    assert client.post("/api/agent/sync", json={}).status_code == 401
    assert client.post("/api/agent/jobs/1", json={}).status_code == 401
