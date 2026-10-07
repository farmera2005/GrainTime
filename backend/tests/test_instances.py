"""Named instances: port lookup via SQL Server Browser, and diagnosis of a
port that does not answer (what Windows clients do for HOST\\INSTANCE)."""
import ipaddress
import socket
import threading

import pytest
from pydantic import ValidationError

from graintime.api.schemas import ConnectionFields, SiteCreate, TestConnectionIn
from graintime.collector import sitedb
from graintime.collector.sitedb import ConnectionSpec, SiteConnectionError


def ssrp(*instances):
    body = "".join(f"ServerName;SCALE01;InstanceName;{n};IsClustered;No;Version;15.0.2000.5;"
                   + (f"tcp;{p};;" if p else "np;\\\\SCALE01\\pipe\\sql\\query;;")
                   for n, p in instances).encode()
    return b"\x05" + len(body).to_bytes(2, "little") + body


def test_parse_browser_response():
    out = sitedb.parse_browser_response(ssrp(("SQLEXPRESS", 49721), ("OLD", None)))
    assert out == [
        {"server": "SCALE01", "instance": "SQLEXPRESS", "version": "15.0.2000.5", "tcp_port": 49721},
        {"server": "SCALE01", "instance": "OLD", "version": "15.0.2000.5", "tcp_port": None}]
    assert sitedb.parse_browser_response(b"junk") == []


def test_browse_instances_over_udp():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def answer():
        data, addr = srv.recvfrom(16)
        assert data == b"\x03"
        srv.sendto(ssrp(("SQLEXPRESS", 49721)), addr)
    t = threading.Thread(target=answer)
    t.start()
    got = sitedb.browse_instances("127.0.0.1", port=port, timeout=2)
    t.join()
    srv.close()
    assert got[0]["instance"] == "SQLEXPRESS" and got[0]["tcp_port"] == 49721


def test_browse_instances_no_answer():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        assert sitedb.browse_instances("127.0.0.1", port=port, timeout=0.3) is None
    finally:
        s.close()


def spec(**over):
    base = dict(host="scale01", port=None, database="CW", username="u", password="p",
                instance_name="SQLEXPRESS")
    return ConnectionSpec(**{**base, **over})


@pytest.fixture
def fake(monkeypatch):
    state = {"instances": [{"instance": "SQLEXPRESS", "tcp_port": 49721, "server": "S", "version": "v"}],
             "listening": {49721}, "connects": []}
    state["probe"] = {}  # port -> result; default "no answer"
    monkeypatch.setattr(sitedb, "browse_instances", lambda host, **k: state["instances"])
    monkeypatch.setattr(sitedb, "_local_networks", lambda: [])
    monkeypatch.setattr(sitedb, "probe_tcp", lambda host, port, **k: state["probe"].get(port, "no answer"))

    def connect(sp):
        state["connects"].append(sp.port)
        if sp.port not in state["listening"]:
            raise SiteConnectionError("port_closed", "Port closed", "fix")
        return "CONN"
    monkeypatch.setattr(sitedb, "connect", connect)
    return state


def test_port_looked_up_from_instance(fake):
    conn, notes = sitedb.open_connection(spec())
    assert conn == "CONN" and notes == {"resolved_port": 49721} and fake["connects"] == [49721]


def test_instance_name_case_insensitive(fake):
    assert sitedb.open_connection(spec(instance_name="sqlexpress"))[1]["resolved_port"] == 49721


def test_saved_port_moved_reconnects_on_new_port(fake):
    conn, notes = sitedb.open_connection(spec(port=50000))
    assert conn == "CONN" and notes == {"resolved_port": 49721, "port_entered": 50000}
    assert fake["connects"] == [50000, 49721]


def test_instance_not_found_lists_instances(fake):
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(instance_name="NOPE"))
    assert e.value.code == "instance_not_found" and "SQLEXPRESS (port 49721)" in e.value.fix
    assert e.value.as_dict()["instances"][0]["tcp_port"] == 49721


def test_browser_unreachable(fake):
    fake["instances"] = None
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec())
    assert e.value.code == "browser_unreachable"


def test_tcp_disabled_on_instance(fake):
    fake["instances"] = [{"instance": "SQLEXPRESS", "tcp_port": None, "server": "S", "version": "v"}]
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec())
    assert e.value.code == "port_closed" and "TCP/IP turned off" in e.value.cause


def test_wrong_port_without_instance_lists_what_the_server_has(fake, monkeypatch):
    def unreachable(sp):
        raise SiteConnectionError("host_unreachable", "Host unreachable or port filtered", "firewall")
    monkeypatch.setattr(sitedb, "connect", unreachable)
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(instance_name=None, port=1433))
    err = e.value
    assert err.code == "host_unreachable"
    assert "server is reachable" in err.cause and "port 1433" in err.cause
    assert "SQLEXPRESS (port 49721)" in err.fix


def test_docker_network_overlap(fake, monkeypatch):
    fake["instances"] = None  # nothing at that address answers at all (probes default to silence)
    monkeypatch.setattr(sitedb, "_local_networks", lambda: [ipaddress.IPv4Network("172.18.0.0/16")])
    monkeypatch.setattr(sitedb, "connect", lambda sp: (_ for _ in ()).throw(
        SiteConnectionError("host_unreachable", "x", "y")))
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="172.18.5.10", instance_name=None, port=1433))
    assert e.value.code == "docker_network_overlap" and "172.18.0.0/16" in e.value.cause


def test_local_networks_reads_route_table():
    nets = sitedb._local_networks()
    assert all(isinstance(n, ipaddress.IPv4Network) for n in nets)


# --- validation ----------------------------------------------------------- #

BASE = {"host": "scale01", "database": "CW", "username": "u", "password": "p"}


def test_port_optional_with_instance_for_testing():
    c = ConnectionFields(**BASE, instance_name=" SQLEXPRESS ")
    assert c.port is None and c.instance_name == "SQLEXPRESS"
    TestConnectionIn(connection=c)


def test_port_or_instance_required():
    with pytest.raises(ValidationError, match="TCP port, or the instance name"):
        ConnectionFields(**BASE)


def test_default_instance_name_means_none():
    assert ConnectionFields(**BASE, port=1433, instance_name="MSSQLSERVER").instance_name is None


def test_saving_a_site_needs_the_port():
    with pytest.raises(ValidationError):
        SiteCreate(**BASE, name="n", code="AB", instance_name="SQLEXPRESS")
    assert SiteCreate(**BASE, name="n", code="AB", port=49721, instance_name="SQLEXPRESS").port == 49721


def test_no_overlap_reported_when_something_answers(fake, monkeypatch):
    """A server really on Docker's network (e.g. the dev mock site) is not an overlap."""
    monkeypatch.setattr(sitedb, "_local_networks", lambda: [ipaddress.IPv4Network("172.18.0.0/16")])
    # Browser answers -> instance diagnosis, not overlap
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="172.18.5.10", instance_name=None, port=1500))
    assert e.value.code == "port_closed" and e.value.instances
    # Port refused and no browser -> a machine is there; plain port_closed
    fake["instances"] = None
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="172.18.5.10", instance_name=None, port=1500))
    assert e.value.code == "port_closed"


def _unreachable(monkeypatch):
    def connect(sp):
        raise SiteConnectionError("host_unreachable", "Host unreachable or port filtered", "firewall")
    monkeypatch.setattr(sitedb, "connect", connect)


def test_machine_answers_but_sql_port_silent(fake, monkeypatch):
    """The reported case: no answer on the SQL port, but the PC is there."""
    fake["instances"] = None
    fake["probe"] = {445: "open", 135: "refused"}
    _unreachable(monkeypatch)
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="10.5.1.20", instance_name=None, port=1433))
    err = e.value.as_dict()
    assert err["code"] == "sql_port_blocked"
    assert "reachable" in err["cause"] and "TCP port 1433" in err["cause"]
    assert "Windows Firewall on the SQL Server machine" in err["fix"]
    labels = {c["check"]: c for c in err["checks"]}
    assert labels["Windows file sharing (TCP 445)"]["ok"] is True
    assert labels["SQL Server (TCP 1433)"]["ok"] is False
    assert err["docs"].endswith("sql-port-blocked")


def test_nothing_answers_at_all(fake, monkeypatch):
    fake["instances"] = None
    _unreachable(monkeypatch)
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="10.5.1.20", instance_name=None, port=1433))
    err = e.value.as_dict()
    assert err["code"] == "no_route" and "any port" in err["cause"]
    assert all(not c["ok"] for c in err["checks"] if c["check"].startswith(("SQL", "Windows", "Remote", "Web")))
    assert "Docker uses" not in err["fix"]


def test_nothing_answers_in_docker_default_range_hints(fake, monkeypatch):
    fake["instances"] = None
    _unreachable(monkeypatch)
    with pytest.raises(SiteConnectionError) as e:
        sitedb.open_connection(spec(host="172.20.4.9", instance_name=None, port=1433))
    assert e.value.code == "no_route" and "Docker uses" in e.value.fix
