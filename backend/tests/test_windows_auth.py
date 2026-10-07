"""Windows (domain) accounts: NTLM sign-in through python-tds."""
import os

import pytest

from graintime.collector import sitedb
from graintime.collector.sitedb import ConnectionSpec, SiteConnectionError


def spec(**over):
    base = dict(host="10.12.131.100", port=1433, database="CW", username="svc_graintime",
                password="pw", auth_method="windows", domain="MERCER",
                trust_server_certificate=True)
    return ConnectionSpec(**{**base, **over})


@pytest.mark.parametrize("user,domain,expected", [
    ("svc", "MERCER", "MERCER\\svc"),
    ("MERCER\\svc", "OTHER", "MERCER\\svc"),
    ("svc@mercer.local", "MERCER", "svc@mercer.local"),
    ("svc", None, "svc"),
])
def test_windows_principal(user, domain, expected):
    assert sitedb.windows_principal(spec(username=user, domain=domain)) == expected


def test_repr_hides_password():
    assert "pw'" not in repr(spec()) and "MERCER" in repr(spec())


@pytest.fixture
def captured(monkeypatch):
    import pytds
    import pytds.tls
    calls = {}

    def fake_connect(**kw):
        calls["kw"] = kw
        calls["ctx_factory"] = pytds.tls.create_context
        return "CONN"
    monkeypatch.setattr(pytds, "connect", fake_connect)
    return calls


def test_ntlm_with_trusted_self_signed_cert(captured):
    import pytds.tls
    original = pytds.tls.create_context
    assert sitedb._connect_windows(spec()) == "CONN"
    kw = captured["kw"]
    assert kw["dsn"] == "10.12.131.100" and kw["port"] == 1433 and kw["database"] == "CW"
    assert kw["readonly"] and kw["autocommit"]
    assert kw["login_timeout"] == sitedb.CONNECT_TIMEOUT_S and kw["timeout"] == sitedb.COMMAND_TIMEOUT_S
    assert kw["cafile"] == sitedb.SYSTEM_CA_BUNDLE and kw["validate_host"] is False
    assert "user" not in kw and "password" not in kw          # NTLM, not SQL login
    assert type(kw["auth"]).__name__ == "SpnegoAuth"
    assert captured["ctx_factory"] is not original              # non-verifying for this login
    assert pytds.tls.create_context is original                 # and restored afterwards


def test_verified_cert_uses_system_cas(captured):
    import pytds.tls
    original = pytds.tls.create_context
    sitedb._connect_windows(spec(trust_server_certificate=False))
    assert captured["kw"]["validate_host"] is True and captured["ctx_factory"] is original


def test_no_encryption(captured):
    sitedb._connect_windows(spec(encrypt="no"))
    assert "cafile" not in captured["kw"]


def test_strict_rejected():
    with pytest.raises(SiteConnectionError) as e:
        sitedb._connect_windows(spec(encrypt="strict"))
    assert e.value.code == "encrypt_unsupported"


def test_ntlm_token_is_produced():
    """pyspnego builds an NTLM NEGOTIATE message for the login packet."""
    import pytds.login
    auth = pytds.login.SpnegoAuth(username="MERCER\\svc", password="pw", hostname="h",
                                  service="MSSQLSvc", protocol="ntlm")
    assert auth.create_packet().startswith(b"NTLMSSP\x00\x01")


@pytest.mark.parametrize("message,code", [
    ("('Login failed. The login is from an untrusted domain and cannot be used with Integrated authentication.', None)",
     "windows_credentials_rejected"),
    ("Login failed for user 'MERCER\\svc'. (18456)", "windows_no_sql_login"),
    ("SpnegoError: something", "windows_login_failed"),
    ("[('SSL routines', '', 'certificate verify failed')]", "cert_untrusted"),
    ("timed out", "host_unreachable"),
])
def test_classify_windows_error(message, code):
    assert sitedb.classify_windows_error(Exception(message)).code == code


def test_connect_routes_by_auth_method(monkeypatch):
    seen = []
    monkeypatch.setattr(sitedb, "tcp_probe", lambda *a, **k: None)
    monkeypatch.setattr(sitedb, "_connect_windows", lambda sp: seen.append("windows") or FakeConn())
    monkeypatch.setattr(sitedb, "_connect_odbc", lambda sp: seen.append("odbc") or FakeConn())
    sitedb.connect(spec())
    sitedb.connect(spec(auth_method="sql"))
    assert seen == ["windows", "odbc"]


class FakeConn:
    def cursor(self):
        return self

    def execute(self, *_):
        pass

    def close(self):
        pass


# --- against a real SQL Server (MSSQL_TEST_*) -------------------------------- #

ENV = {k: os.environ.get(f"MSSQL_TEST_{k.upper()}") for k in ("host", "port", "database", "user", "password")}
needs_mssql = pytest.mark.skipif(not all(ENV.values()), reason="MSSQL_TEST_* not set")


@needs_mssql
def test_pytds_path_tls_and_discovery_on_real_server():
    """The python-tds path used for Windows accounts: TLS with a trusted self-signed
    cert, session settings, server info and discovery (SQL login, as the test server
    has no domain)."""
    from graintime.collector import discovery
    sp = ConnectionSpec(host=ENV["host"], port=int(ENV["port"]), database=ENV["database"],
                        username=ENV["user"], password=ENV["password"],
                        trust_server_certificate=True)
    conn = sitedb._connect_pytds(sp, user=sp.username, password=sp.password)
    try:
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; SET LOCK_TIMEOUT 5000;")
        cur.execute("SELECT transaction_isolation_level FROM sys.dm_exec_sessions WHERE session_id = @@SPID")
        assert cur.fetchone()[0] == 1          # READ UNCOMMITTED
        info = sitedb.server_info(conn)
        assert info["edition"] and info["database"] == ENV["database"]
        rep = discovery.run_discovery(conn, "t", discovery.DiscoveryOptions())
        assert rep["tables"] and rep["candidates"]
    finally:
        conn.close()


@needs_mssql
def test_ntlm_reaches_real_server_and_is_rejected_cleanly():
    """The NTLM exchange runs end to end; a server without a domain rejects it, and
    the failure is reported as a Windows sign-in problem."""
    sp = ConnectionSpec(host=ENV["host"], port=int(ENV["port"]), database=ENV["database"],
                        username="svc", password="pw", auth_method="windows", domain="MERCER",
                        trust_server_certificate=True)
    with pytest.raises(SiteConnectionError) as e:
        sitedb.connect(sp)
    assert e.value.code == "windows_credentials_rejected", e.value.as_dict()
    assert "MERCER\\svc" in e.value.cause and "event 4625" in e.value.fix


def test_credentials_rejected_explains_causes_and_lockout():
    err = sitedb.classify_windows_error(
        Exception("Login failed. The login is from an untrusted domain and cannot be used with "
                  "Integrated authentication."), spec(domain="mercer.local"))
    assert err.code == "windows_credentials_rejected"
    assert "MERCER\\svc_graintime" not in err.cause and "mercer.local\\svc_graintime" in err.cause
    assert "NetBIOS" in err.fix and "'MERCER'" in err.fix
    assert "lockout" in err.fix and "4625" in err.fix
    assert err.as_dict()["docs"].endswith("windows-credentials-rejected")


def test_short_domain_gets_no_dns_hint():
    err = sitedb.classify_windows_error(Exception("untrusted domain"), spec())
    assert "NetBIOS" not in err.fix
