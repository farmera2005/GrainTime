"""End to end against a real SQL Server: api queues, collector runs, report comes back.

Set MSSQL_TEST_HOST, MSSQL_TEST_PORT, MSSQL_TEST_DATABASE, MSSQL_TEST_USER and
MSSQL_TEST_PASSWORD (a read-only login) to run; skipped otherwise.
"""
import os

import pytest

from graintime.collector.jobs import JobRunner

ENV = {k: os.environ.get(f"MSSQL_TEST_{k.upper()}") for k in
       ("host", "port", "database", "user", "password")}
pytestmark = pytest.mark.skipif(not all(ENV.values()), reason="MSSQL_TEST_* not set")


def _site(**over):
    return {"name": "Integration", "code": "INTEG", "host": ENV["host"], "port": int(ENV["port"]),
            "database": ENV["database"], "username": ENV["user"], "password": ENV["password"],
            "encrypt": "yes", "trust_server_certificate": True, **over}


def test_test_connection_and_discovery(admin_client):
    c = admin_client
    sid = c.post("/api/admin/sites", json=_site()).json()["id"]
    runner = JobRunner(max_workers=1)

    j = c.post("/api/admin/jobs/test-connection", json={"site_id": sid}).json()["id"]
    runner.run(j)
    res = c.get(f"/api/admin/jobs/{j}").json()
    assert res["status"] == "succeeded", res
    assert res["result"]["edition"] and res["result"]["tls"]["affected"] is False

    j = c.post(f"/api/admin/sites/{sid}/discovery", json={}).json()["id"]
    runner.run(j)
    res = c.get(f"/api/admin/jobs/{j}").json()
    assert res["status"] == "succeeded", res
    assert res["result"]["summary"]["tables"] > 0
    md = c.get(f"/api/admin/jobs/{j}/report.md").text
    assert "# GrainTime site discovery report" in md
    assert ENV["password"] not in md
    # Sample columns keep the table's column order through JSONB storage.
    report = c.get(f"/api/admin/jobs/{j}/report.json").json()
    for cand in report["candidates"]:
        if cand.get("sample"):
            table_order = [col["column"] for col in cand["columns"]]
            assert cand["sample"]["columns"] == [x for x in table_order if x in cand["sample"]["columns"]]


def test_wrong_password_and_untrusted_cert(admin_client):
    c = admin_client
    runner = JobRunner(max_workers=1)
    conn = {k: v for k, v in _site().items() if k not in ("name", "code")}
    cases = [({**conn, "password": "wrong-password"}, "login_failed"),
             ({**conn, "trust_server_certificate": False}, "cert_untrusted"),
             ({**conn, "database": "NoSuchDb"}, "database_not_found")]
    for connection, code in cases:
        j = c.post("/api/admin/jobs/test-connection", json={"connection": connection}).json()["id"]
        runner.run(j)
        res = c.get(f"/api/admin/jobs/{j}").json()
        assert res["status"] == "failed" and res["error"]["code"] == code, res
