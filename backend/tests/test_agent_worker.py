"""The GrainTime Agent (agent/graintime_agent), run here against the real api.

The end-to-end test needs a SQL Server seeded by graintime.devtools.mock_agvantage
(GMS_TEST_HOST / GMS_TEST_SA_PASSWORD, the same server as the GMS mock). The
Windows-only parts (finding the user instance's pipe, DPAPI) are covered by
unit tests here and by the Windows build workflow.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
os.environ.setdefault("GRAINTIME_AGENT_TEST_NO_DPAPI", "1")

from graintime_agent import config as agent_config  # noqa: E402
from graintime_agent.client import Server, ServerError  # noqa: E402
from graintime_agent.localdb import (LocalDbError, SelectOnlyConnection, explain,  # noqa: E402
                                     find_candidates, find_running_instance, latest_pipe_in_log,
                                     open_first, pipe_exists, read_log)
from graintime_agent.worker import Worker  # noqa: E402

from graintime.common.profiles import AGVANTAGE_PC  # noqa: E402

LOG = ("2026-10-15 15:23:18.23 spid11s     Server local connection provider is ready to accept connection on "
       "[ \\\\.\\pipe\\6E45AB3D-7514-4D\\tsql\\query ].\r\n"
       "2026-10-15 15:23:21.65 spid51      Starting up database 'C:\\PROGRAM FILES (X86)\\AGVANTAGE SOFTWARE\\"
       "AGVANTAGEPC GUI\\LOCALSET.MDF'.\r\n")


def test_pipe_found_in_utf16_error_log(tmp_path):
    inst = tmp_path / "SQLEXPRESS"
    inst.mkdir()
    (inst / "error.log").write_bytes(b"\xff\xfe" + LOG.encode("utf-16-le"))
    assert latest_pipe_in_log(read_log(inst / "error.log")) == "\\\\.\\pipe\\6E45AB3D-7514-4D\\tsql\\query"
    assert find_running_instance(tmp_path, exists=lambda p: True) == "\\\\.\\pipe\\6E45AB3D-7514-4D\\tsql\\query"
    # Pipe gone = the scale software closed its database: nothing to read, nothing started.
    assert find_running_instance(tmp_path, exists=lambda p: False) is None
    assert find_running_instance(tmp_path / "missing") is None


# --------------------------------------------------------------------------- #
# Finding where the scale database is open (pipes are listed, never opened)
# --------------------------------------------------------------------------- #

def _logs(tmp_path, pipe_line=LOG):
    inst = tmp_path / "SQLEXPRESS"
    inst.mkdir(exist_ok=True)
    (inst / "error.log").write_bytes(b"\xff\xfe" + pipe_line.encode("utf-16-le"))
    return tmp_path


def test_pipe_existence_is_checked_by_listing_case_insensitively():
    listed = ["InitShutdown", "6e45ab3d-7514-4d\\tsql\\query", "MSSQL$SQLEXPRESS\\sql\\query"]
    assert pipe_exists("\\\\.\\pipe\\6E45AB3D-7514-4D\\tsql\\query", listed)
    assert not pipe_exists("\\\\.\\pipe\\0000\\tsql\\query", listed)


def test_candidates_user_instance_first_then_services(tmp_path):
    base = _logs(tmp_path)
    cands, detail = find_candidates(base, ["6E45AB3D-7514-4D\\tsql\\query", "MSSQL$SQLEXPRESS\\sql\\query",
                                           "sql\\query", "lsass"])
    assert cands == ["\\\\.\\pipe\\6E45AB3D-7514-4D\\tsql\\query", "\\\\.\\pipe\\MSSQL$SQLEXPRESS\\sql\\query",
                     "\\\\.\\pipe\\sql\\query"]
    assert detail["user_instance_logs"][0]["running"] is True


def test_user_instance_not_running_is_explained(tmp_path):
    base = _logs(tmp_path)
    cands, detail = find_candidates(base, ["lsass"])          # its pipe is gone
    assert cands == [] and detail["user_instance_logs"][0]["running"] is False
    assert "user instance is not running" in explain(detail, "LOCALSET.MDF")


def test_wrong_windows_account_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("USERDOMAIN", "MERCERLANDMARK")
    monkeypatch.setenv("USERNAME", "itadmin")
    cands, detail = find_candidates(tmp_path / "none", ["lsass"])
    msg = explain(detail, "LOCALSET.MDF")
    assert cands == [] and "MERCERLANDMARK\\itadmin" in msg and "different Windows account" in msg


def test_open_first_skips_services_without_the_database_or_access():
    tried, refused = [], []

    def opener(c, match):
        tried.append(c)
        if c.endswith("A\\sql\\query"):
            raise LocalDbError("login_failed", "refused", "")
        if c.endswith("B\\sql\\query"):
            raise LocalDbError("database_not_found", "no db", "")
        return SelectOnlyConnection(_Conn(), "drv", "LOCALSET.MDF", c)
    svc = ["\\\\.\\pipe\\MSSQL$A\\sql\\query", "\\\\.\\pipe\\MSSQL$B\\sql\\query", "\\\\.\\pipe\\MSSQL$C\\sql\\query"]
    conn = open_first(svc, "LOCALSET.MDF", opener=opener, refused=refused.append)
    assert conn.target.endswith("C\\sql\\query") and refused == [svc[0]] and len(tried) == 3
    with pytest.raises(LocalDbError) as e:
        open_first(svc[:2], "LOCALSET.MDF", opener=opener)
    assert e.value.code == "database_not_found"
    # A user instance that refuses the account is a real problem, not "not here".
    with pytest.raises(LocalDbError) as e:
        open_first(["\\\\.\\pipe\\X\\tsql\\query"], "LOCALSET.MDF",
                   opener=lambda c, m: (_ for _ in ()).throw(LocalDbError("login_failed", "no", "")))
    assert e.value.code == "login_failed"


class _Cur:
    description = [("x",)]

    def __init__(self):
        self.ran = []

    def execute(self, sql, *params):
        self.ran.append(sql)

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.cur = _Cur()

    def cursor(self):
        return self.cur

    def close(self):
        pass


@pytest.mark.parametrize("sql", ["UPDATE dbo.scale_ticket SET st_status = 'V'", "DELETE FROM x",
                                 "SELECT 1; DROP TABLE dbo.scale_ticket", "EXEC sp_who", "  insert into x values (1)",
                                 "WITH x AS (SELECT 1) DELETE FROM y"])
def test_connection_refuses_anything_but_select(sql):
    conn = SelectOnlyConnection(_Conn(), "test", "db")
    with pytest.raises(PermissionError):
        conn.cursor().execute(sql)
    conn.cursor().execute("SELECT TOP (1) [st_load] FROM [dbo].[scale_ticket] WHERE 1 = 1 ORDER BY [st_load]")


def test_state_file_never_holds_the_plain_secret_on_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAINTIME_AGENT_HOME", str(tmp_path))
    agent_config.save("http://gt:8080/", "k1", "s3cret", {"id": 1, "name": "New Weston", "code": "NEWW"})
    st = agent_config.load()
    assert st["server"] == "http://gt:8080" and agent_config.secret_of(st) == "s3cret"
    monkeypatch.delenv("GRAINTIME_AGENT_TEST_NO_DPAPI")
    if sys.platform != "win32":
        with pytest.raises(RuntimeError):            # refuses to store a key without DPAPI
            agent_config.protect("x")


# --------------------------------------------------------------------------- #
# End to end: agent -> api, with the mock AgVantage database
# --------------------------------------------------------------------------- #

GMS_HOST = os.environ.get("GMS_TEST_HOST")
needs_mock = pytest.mark.skipif(not GMS_HOST, reason="GMS_TEST_HOST not set (mock SQL Server)")


def mock_db_opener(pipe, database_match):
    import pyodbc
    from graintime.devtools.mock_agvantage import DB_NAME
    assert DB_NAME.upper().endswith(database_match.upper())
    conn = pyodbc.connect(
        f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=tcp:{GMS_HOST},1433;UID=sa;"
        f"PWD={{{os.environ['GMS_TEST_SA_PASSWORD']}}};TrustServerCertificate=yes;DATABASE={{{DB_NAME}}}",
        autocommit=True, timeout=5, readonly=True)
    conn.cursor().execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET LOCK_TIMEOUT 5000;")
    return SelectOnlyConnection(conn, "ODBC Driver 18 for SQL Server", DB_NAME, pipe)


def transport_for(client):
    def t(method, url, headers, body):
        r = client.request(method, url.replace("http://graintime", ""), content=body, headers=headers)
        return r.status_code, r.content
    return t


@needs_mock
def test_agent_end_to_end(admin_client):
    prof = admin_client.post("/api/admin/mapping-profiles", json={"name": "AgVantagePC", "description": None,
                                                                   "config": AGVANTAGE_PC}).json()
    site = admin_client.post("/api/admin/sites/agent", json={"name": "New Weston", "code": "NEWW"}).json()
    assert site["mapping_profile_id"] == prof["id"]
    code = admin_client.post(f"/api/admin/sites/{site['id']}/agent/pairing-code").json()["code"]
    t = transport_for(admin_client)
    with pytest.raises(ServerError):
        Server("http://graintime", transport=t).pair("WRNG-CODE", "DESKTOP-512", "MERCERLANDMARK\\nwscale")
    got = Server("http://graintime", transport=t).pair(code, "DESKTOP-512", "MERCERLANDMARK\\nwscale")
    server = Server("http://graintime", got["key_id"], got["secret"], transport=t)
    running = {"pipe": "\\\\.\\pipe\\X\\tsql\\query"}
    w = Worker(server, find_instance=lambda: ([running["pipe"]] if running["pipe"] else [], {"account": "X\\nwscale"}),
               open_db=mock_db_opener, sleep=lambda s: None)

    assert w.cycle() == 1                              # first sync: learns the database name, re-checks now
    assert w.cycle() > 1                               # polling is off: status only
    assert admin_client.get(f"/api/admin/sites/{site['id']}/agent").json()["instance"] == "running"
    admin_client.patch(f"/api/admin/sites/{site['id']}", json={"polling_enabled": True})
    w.cycle()                                          # gets the profile
    w.cycle()                                          # first poll: sets the mark, reads the newest 200
    col = admin_client.get(f"/api/admin/sites/{site['id']}/collection").json()
    assert col["state"]["high_water_mark"] and col["state"]["last_error"] is None and not col["stale"]
    first = col["tickets"]["total"]
    assert 100 < first <= 200

    # Preview through the agent
    job = admin_client.post(f"/api/admin/sites/{site['id']}/preview", json={"limit": 20}).json()
    w.cycle()
    pv = admin_client.get(f"/api/admin/jobs/{job['id']}").json()
    assert pv["status"] == "succeeded", pv
    assert pv["result"]["summary"]["read"] == 20 and pv["result"]["summary"]["included"] > 10
    assert all("st_name" not in t and "MOCK PRODUCER" not in str(t) for t in pv["result"]["tickets"])

    # Backfill 30 days through the agent (key-ordered walk; transfers left out)
    from datetime import date, timedelta
    job = admin_client.post(f"/api/admin/sites/{site['id']}/backfill", json={
        "date_from": (date.today() - timedelta(days=31)).isoformat(), "date_to": date.today().isoformat()}).json()
    w.cycle()
    bf = admin_client.get(f"/api/admin/jobs/{job['id']}").json()
    assert bf["status"] == "succeeded", bf
    total = admin_client.get(f"/api/admin/sites/{site['id']}/collection").json()["tickets"]["total"]
    assert total > first

    from graintime.common.db import get_sessionmaker
    from graintime.common.models import Ticket
    with get_sessionmaker()() as db:
        types = {t for (t,) in db.query(Ticket.transaction_type).filter_by(site_id=site["id"]).distinct()}
        assert types <= {"P", "S"}
        assert db.query(Ticket).filter(Ticket.site_id == site["id"], Ticket.duration_s.isnot(None)).count() > 0

    # Scale software closed: nothing read, site goes stale on its own, no error,
    # and the page says why the database wasn't found.
    running["pipe"] = None
    w.cycle()
    st = admin_client.get(f"/api/admin/sites/{site['id']}/agent").json()
    assert st["instance"] == "not_running" and st["detail"]["reason"]
    assert admin_client.get(f"/api/admin/sites/{site['id']}/collection").json()["state"]["last_error"] is None

    # Revoked: the agent is locked out.
    admin_client.post(f"/api/admin/sites/{site['id']}/agent/revoke")
    running["pipe"] = "\\\\.\\pipe\\X\\tsql\\query"
    with pytest.raises(ServerError):
        w.cycle()



@needs_mock
def test_service_instance_without_the_database_reports_not_running(admin_client):
    """A PC whose SQLEXPRESS service doesn't have LOCALSET attached: checked,
    not mistaken for the database, explained on the page."""
    prof = admin_client.post("/api/admin/mapping-profiles", json={"name": "AgVantagePC", "description": None,
                                                                   "config": AGVANTAGE_PC}).json()
    site = admin_client.post("/api/admin/sites/agent", json={"name": "Coldwater", "code": "COLD"}).json()
    assert site["mapping_profile_id"] == prof["id"]
    code = admin_client.post(f"/api/admin/sites/{site['id']}/agent/pairing-code").json()["code"]
    t = transport_for(admin_client)
    got = Server("http://graintime", transport=t).pair(code, "SCALEPC", "MERCERLANDMARK\\itadmin")
    svc = "\\\\.\\pipe\\MSSQL$SQLEXPRESS\\sql\\query"

    def no_db(c, match):
        raise LocalDbError("database_not_found", "none", "")
    w = Worker(Server("http://graintime", got["key_id"], got["secret"], transport=t),
               find_instance=lambda: ([svc], {"account": "MERCERLANDMARK\\itadmin", "user_instance_logs": [],
                                              "service_pipes": [svc], "user_instance_folder": "C:\\x"}),
               open_db=no_db, sleep=lambda s: None)
    w.cycle()
    w.cycle()
    st = admin_client.get(f"/api/admin/sites/{site['id']}/agent").json()
    assert st["instance"] == "not_running" and st["error"] is None
    assert "LOCALSET.MDF is not open" in st["detail"]["reason"] and st["detail"]["service_pipes"] == [svc]
