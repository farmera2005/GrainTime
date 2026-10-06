"""Test-connection failure causes are named specifically, not generically."""
import socket

import pytest

from graintime.collector import sitedb


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_port_closed():
    with pytest.raises(sitedb.SiteConnectionError) as e:
        sitedb.tcp_probe("127.0.0.1", _free_port())
    assert e.value.code == "port_closed"


def test_dns_failed():
    with pytest.raises(sitedb.SiteConnectionError) as e:
        sitedb.tcp_probe("no-such-host.invalid", 1433)
    assert e.value.code == "dns_failed"


@pytest.mark.parametrize("exc", [TimeoutError(), socket.timeout(), OSError(113, "No route to host")])
def test_host_unreachable(monkeypatch, exc):
    # Simulated: how a real unroutable address fails depends on the local network.
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(sitedb.socket, "create_connection", boom)
    with pytest.raises(sitedb.SiteConnectionError) as e:
        sitedb.tcp_probe("192.0.2.1", 1433, timeout=0.5)
    assert e.value.code == "host_unreachable"


@pytest.mark.parametrize("message,code", [
    ("[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Login failed for user 'x'. (18456)",
     "login_failed"),
    ("[28000] Login failed. The login is from an untrusted domain and cannot be used with Integrated "
     "authentication. ... not associated with a trusted SQL Server connection. (18452)",
     "mixed_mode_disabled"),
    ("[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: [error:0A000086:SSL routines::"
     "certificate verify failed:self-signed certificate]", "cert_untrusted"),
    ("[08001] [Microsoft][ODBC Driver 18 for SQL Server]SSL Provider: [error:0A000102:SSL routines::"
     "unsupported protocol]", "tls_version"),
    ("[42000] [SQL Server]Cannot open database \"Nope\" requested by the login. The login failed. (4060)",
     "database_not_found"),
    ("[HYT00] [Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired (0)", "host_unreachable"),
    ("[01000] [unixODBC][Driver Manager]Can't open lib 'ODBC Driver 18 for SQL Server'", "driver_missing"),
    ("something nobody has seen", "unknown"),
])
def test_driver_error_classification(message, code):
    err = sitedb.classify_driver_error(Exception(message))
    assert err.code == code
    d = err.as_dict()
    assert d["fix"] or code == "unknown"
    assert d["docs"].startswith("README.md#")


def test_connection_spec_repr_hides_password():
    spec = sitedb.ConnectionSpec("h", 1433, "d", "u", "topsecret")
    assert "topsecret" not in repr(spec) and "topsecret" not in str(spec)


@pytest.mark.parametrize("version,affected", [
    ("16.0.4295.3", False), ("12.0.4100.1", True), ("12.0.5000.0", False),
    ("11.0.7001.0", False), ("10.50.6000.34", True)])
def test_tls_assessment(version, affected):
    assert sitedb.tls_assessment(version)["affected"] is affected
