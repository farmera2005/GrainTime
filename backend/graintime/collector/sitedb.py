"""Connections to site SQL Server databases. Only the collector imports this.

Every connection follows the site-database rules: 5 s connect timeout, 15 s
command timeout, READ UNCOMMITTED, low deadlock priority, autocommit, and
read-only intent. The password and the connection string are never logged or
returned.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, replace

CONNECT_TIMEOUT_S = 5
COMMAND_TIMEOUT_S = 15
SQL_BROWSER_PORT = 1434      # SQL Server Browser (UDP): maps instance names to ports
BROWSER_TIMEOUT_S = 2
DRIVER = "ODBC Driver 18 for SQL Server"
DOCS = "README.md#deployment-notes-connecting-to-site-sql-servers"


@dataclass(frozen=True)
class ConnectionSpec:
    host: str
    port: int | None          # None: look it up from instance_name via SQL Server Browser
    database: str
    username: str
    password: str
    encrypt: str = "yes"
    trust_server_certificate: bool = False
    instance_name: str | None = None

    def __repr__(self) -> str:  # keep the password out of any accidental repr/log
        return (f"ConnectionSpec(host={self.host!r}, port={self.port}, "
                f"instance_name={self.instance_name!r}, database={self.database!r}, "
                f"username={self.username!r}, encrypt={self.encrypt!r}, "
                f"trust_server_certificate={self.trust_server_certificate})")


class SiteConnectionError(Exception):
    """A classified connection failure: code, plain-language cause, and the fix."""

    def __init__(self, code: str, cause: str, fix: str, detail: str = "",
                 instances: list[dict] | None = None):
        super().__init__(cause)
        self.code, self.cause, self.fix, self.detail = code, cause, fix, detail
        self.instances = instances  # SQL Server instances the host reported, if asked

    def as_dict(self) -> dict:
        d = {"code": self.code, "cause": self.cause, "fix": self.fix,
             "docs": f"{DOCS}-{self.code.replace('_', '-')}", "detail": self.detail}
        if self.instances is not None:
            d["instances"] = self.instances
        return d


def _odbc_quote(value: str) -> str:
    return "{" + value.replace("}", "}}") + "}"


def _connection_string(spec: ConnectionSpec) -> str:
    return ";".join([
        f"DRIVER={{{DRIVER}}}",
        f"SERVER=tcp:{spec.host},{int(spec.port)}",
        f"DATABASE={_odbc_quote(spec.database)}",
        f"UID={_odbc_quote(spec.username)}",
        f"PWD={_odbc_quote(spec.password)}",
        f"Encrypt={spec.encrypt}",
        f"TrustServerCertificate={'yes' if spec.trust_server_certificate else 'no'}",
        "ApplicationIntent=ReadOnly",
        "APP=GrainTime-Collector",
    ])


def tcp_probe(host: str, port: int, timeout: float = CONNECT_TIMEOUT_S) -> None:
    """Plain TCP connect first, so a closed port is told apart from an
    unreachable host (the ODBC driver reports both as a login timeout)."""
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SiteConnectionError(
            "dns_failed", "Host name does not resolve",
            "Check the host name, or enter the server's IP address instead.", str(exc)) from None
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except ConnectionRefusedError:
        raise SiteConnectionError(
            "port_closed", "Port closed: the host answered but nothing is listening on that port",
            "Enable TCP/IP for the instance in SQL Server Configuration Manager, set a static TCP "
            "port (IPAll > TCP Port, clear TCP Dynamic Ports), restart the SQL Server service, and "
            "check the port number entered here.") from None
    except (socket.timeout, TimeoutError):
        raise SiteConnectionError(
            "host_unreachable", f"Host unreachable or port filtered (no answer within {timeout:g} s)",
            "Check the network path from the Docker host to the site, and add a Windows Firewall "
            "inbound rule at the site allowing TCP on the SQL port from the Docker host.") from None
    except OSError as exc:
        raise SiteConnectionError(
            "host_unreachable", f"Host unreachable ({exc.strerror or exc})",
            "Check the host address and the network route/VPN from the Docker host to the site.",
            str(exc)) from None


_DRIVER_RULES = [
    ("driver_missing", ("can't open lib", "data source name not found", "file not found"),
     "ODBC Driver 18 is not installed in the collector image",
     "Rebuild the collector image; this is a packaging fault, not a site problem."),
    ("database_not_found", ("cannot open database", "(4060)"),
     "Database not found, or the login has no user in that database",
     "Check the database name (it is case-sensitive on some collations) and that the login "
     "is mapped to a user in that database."),
    ("cert_untrusted", ("certificate verify failed", "certificate chain", "self signed",
                        "self-signed", "the certificate"),
     "The server's TLS certificate is not trusted",
     "Most SQL Server Express installs use a self-signed certificate. Turn on "
     "'Trust server certificate' for this site, or install a certificate from a trusted CA."),
    ("tls_version", ("unsupported protocol", "protocol version", "wrong version number",
                     "no protocols available", "ssl routines", "dh key too small",
                     "ssl provider", "tls"),
     "TLS handshake failed; the server probably offers only TLS 1.0/1.1",
     "Install the SQL Server update that adds TLS 1.2 (2012 SP4, 2014 SP2/SP1 CU5, or later), "
     "or upgrade SQL Server. Note the version and tell the project team."),
    ("mixed_mode_disabled", ("not associated with a trusted sql server connection",),
     "SQL Server authentication (mixed mode) is disabled on this instance",
     "In SSMS: Server Properties > Security > 'SQL Server and Windows Authentication mode', "
     "then restart the SQL Server service."),
    ("login_failed", ("login failed", "(18456)"),
     "Login failed",
     "Check the login name and password, that the login is enabled, and that mixed mode "
     "authentication is on. The SQL Server error log at the site shows the exact reason."),
    ("host_unreachable", ("login timeout expired", "timeout", "server is not found",
                          "no route to host", "network is unreachable"),
     "The server did not complete the login in time",
     "The port is open but SQL Server did not answer in 5 s. Check the port is SQL Server's "
     "(not another service), and that the instance is running."),
]


def classify_driver_error(exc: Exception) -> SiteConnectionError:
    msg = str(exc)
    low = msg.lower()
    for code, needles, cause, fix in _DRIVER_RULES:
        if any(n in low for n in needles):
            return SiteConnectionError(code, cause, fix, msg[:500])
    return SiteConnectionError("unknown", "Connection failed", "See the driver message.", msg[:500])


# --- SQL Server Browser (instance name -> port) ------------------------------ #

def parse_browser_response(data: bytes) -> list[dict]:
    """Parse an SSRP SVR_RESP: 0x05, 2-byte length, then
    'ServerName;X;InstanceName;Y;IsClustered;No;Version;V;tcp;PORT;;' per instance."""
    if len(data) < 3 or data[0] != 0x05:
        return []
    length = int.from_bytes(data[1:3], "little")
    text = data[3:3 + length].decode("latin-1", errors="replace")
    out = []
    for chunk in text.split(";;"):
        parts = chunk.split(";")
        kv = dict(zip(parts[0::2], parts[1::2]))
        if not kv.get("InstanceName"):
            continue
        tcp = kv.get("tcp", "")
        out.append({"server": kv.get("ServerName"), "instance": kv["InstanceName"],
                    "version": kv.get("Version"),
                    "tcp_port": int(tcp) if tcp.isdigit() else None})
    return out


def browse_instances(host: str, port: int = SQL_BROWSER_PORT,
                     timeout: float = BROWSER_TIMEOUT_S) -> list[dict] | None:
    """Ask the host's SQL Server Browser which instances it has and their TCP
    ports, as Windows clients do for HOST\\INSTANCE. None if nothing answers."""
    try:
        family, _, _, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    except socket.gaierror:
        return None
    with socket.socket(family, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(b"\x03", addr)          # CLNT_UCAST_EX: list all instances
            data, _ = s.recvfrom(65535)
        except OSError:
            return None
    return parse_browser_response(data)


def _instance_list(instances: list[dict]) -> str:
    return ", ".join(f"{i['instance']} (port {i['tcp_port'] or 'TCP off'})" for i in instances)


def resolve_instance_port(spec: ConnectionSpec) -> int:
    """Current TCP port of spec.instance_name, from SQL Server Browser."""
    name = spec.instance_name or ""
    instances = browse_instances(spec.host)
    if instances is None:
        raise SiteConnectionError(
            "browser_unreachable", f"Could not look up the port for instance {name}",
            "SQL Server Browser (UDP 1434) did not answer. Either start the 'SQL Server Browser' "
            "service at the site, or enter the instance's TCP port (SQL Server Configuration "
            "Manager > Protocols > TCP/IP > IP Addresses > IPAll).")
    match = next((i for i in instances if i["instance"].lower() == name.lower()), None)
    if match is None:
        raise SiteConnectionError(
            "instance_not_found", f"This server has no instance named {name}",
            "Instances found on this server: " + (_instance_list(instances) or "none") +
            ". Correct the instance name, or enter the port directly.", instances=instances)
    if not match["tcp_port"]:
        raise SiteConnectionError(
            "port_closed", f"Instance {match['instance']} has TCP/IP turned off",
            "Enable TCP/IP for the instance in SQL Server Configuration Manager and restart the "
            "SQL Server service.", instances=instances)
    return match["tcp_port"]


# --- diagnosis when the port does not answer -------------------------------- #

def _local_networks() -> list[ipaddress.IPv4Network]:
    """Networks directly attached to this container (Docker's internal ranges)."""
    nets = []
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                fields = line.split()
                dest, gateway, mask = fields[1], fields[2], fields[7]
                if dest == "00000000" or gateway != "00000000":
                    continue
                to_ip = lambda h: ipaddress.IPv4Address(bytes.fromhex(h)[::-1])  # noqa: E731
                nets.append(ipaddress.IPv4Network(f"{to_ip(dest)}/{to_ip(mask)}", strict=False))
    except (OSError, ValueError, IndexError, StopIteration):
        pass
    return nets


def _docker_overlap(host: str) -> tuple[str, str] | None:
    try:
        ips = {ai[4][0] for ai in socket.getaddrinfo(host, None, family=socket.AF_INET)}
    except socket.gaierror:
        return None
    for ip in ips:
        for net in _local_networks():
            if ipaddress.IPv4Address(ip) in net:
                return ip, str(net)
    return None


def diagnose(spec: ConnectionSpec, exc: SiteConnectionError) -> SiteConnectionError:
    """Add what we can learn when the SQL port does not answer: whether the
    address collides with Docker's own network, and which instances (and
    ports) the host's SQL Server Browser reports."""
    if exc.code not in ("port_closed", "host_unreachable"):
        return exc
    instances = exc.instances if exc.instances is not None else browse_instances(spec.host)
    if not instances:
        # Only silence looks like an overlap: if the port was refused or SQL Server
        # Browser answered, a machine is really there (e.g. a dev site on Docker's
        # own network).
        overlap = _docker_overlap(spec.host) if exc.code == "host_unreachable" else None
        if overlap:
            ip, net = overlap
            return SiteConnectionError(
                "docker_network_overlap",
                f"The site's address {ip} is inside Docker's internal network {net}",
                "Docker on this server uses the same address range as the site, so the "
                "connection never leaves the server. Ask IT to move Docker's address pools to "
                "a range the company network does not use (see the deployment notes).")
        return exc
    port = spec.port
    listed = _instance_list(instances)
    if exc.code == "host_unreachable":
        cause = f"The server is reachable, but nothing answered on TCP port {port}"
    else:
        cause = f"Nothing is listening on TCP port {port}"
    return SiteConnectionError(
        exc.code, cause,
        f"SQL Server Browser on this server reports: {listed}. Use one of those ports, or "
        "enter the instance name and leave the port blank so it is looked up.",
        exc.detail, instances=instances)


def open_connection(spec: ConnectionSpec) -> tuple[object, dict]:
    """Connect, looking up the port from the instance name when needed. Returns
    (connection, notes); notes record a port that was looked up."""
    notes: dict = {}
    if spec.port is None:
        if not spec.instance_name:
            raise SiteConnectionError("port_closed", "No TCP port entered",
                                      "Enter the port, or the instance name to look it up.")
        port = resolve_instance_port(spec)
        spec = replace(spec, port=port)
        notes["resolved_port"] = port
    try:
        return connect(spec), notes
    except SiteConnectionError as exc:
        first = exc
    # A named instance on a dynamic port may have moved since the port was saved.
    if spec.instance_name and first.code in ("port_closed", "host_unreachable") \
            and "resolved_port" not in notes:
        try:
            port = resolve_instance_port(spec)
        except SiteConnectionError:
            port = None
        if port and port != spec.port:
            try:
                conn = connect(replace(spec, port=port))
            except SiteConnectionError as exc:
                raise diagnose(replace(spec, port=port), exc) from None
            return conn, {"resolved_port": port, "port_entered": spec.port}
    raise diagnose(spec, first) from None


def connect(spec: ConnectionSpec):
    """Open one read-only connection, or raise SiteConnectionError."""
    import pyodbc

    tcp_probe(spec.host, spec.port)
    try:
        conn = pyodbc.connect(_connection_string(spec), timeout=CONNECT_TIMEOUT_S,
                              autocommit=True, readonly=True)
    except pyodbc.Error as exc:
        raise classify_driver_error(exc) from None
    conn.timeout = COMMAND_TIMEOUT_S
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
                "SET LOCK_TIMEOUT 5000; SET DEADLOCK_PRIORITY LOW;")
    cur.close()
    return conn


def tls_assessment(product_version: str) -> dict:
    """Whether this SQL Server build supports TLS 1.2 (current Linux images
    reject older TLS)."""
    try:
        major, _minor, build = (int(x) for x in product_version.split(".")[:3])
    except Exception:
        return {"affected": None, "note": "Could not parse the version."}
    if major >= 13:
        return {"affected": False, "note": "SQL Server 2016 or later supports TLS 1.2 natively."}
    if major == 12:
        ok = build >= 4439
        return {"affected": not ok, "note": "SQL Server 2014: TLS 1.2 needs SP1 CU5 or SP2+. "
                + ("This build has it." if ok else "This build may not.")}
    if major == 11:
        ok = build >= 6020
        return {"affected": not ok, "note": "SQL Server 2012: TLS 1.2 needs SP3 with the TLS "
                "update, or SP4. " + ("This build has it." if ok else "This build may not.")}
    if major == 10:
        return {"affected": True, "note": "SQL Server 2008/2008 R2: TLS 1.2 only with a specific "
                "post-SP update. Flag this site."}
    return {"affected": True, "note": "SQL Server older than 2008 cannot be reached from Linux."}


def server_info(conn) -> dict:
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)),
                   CAST(SERVERPROPERTY('ProductLevel') AS nvarchar(128)),
                   CAST(SERVERPROPERTY('ProductUpdateLevel') AS nvarchar(128)),
                   CAST(SERVERPROPERTY('Edition') AS nvarchar(128)),
                   CAST(SERVERPROPERTY('EngineEdition') AS int),
                   DB_NAME(),
                   CONVERT(nvarchar(33), SYSDATETIME(), 126),
                   DATENAME(TZOFFSET, SYSDATETIMEOFFSET())
        """)
        r = cur.fetchone()
    finally:
        cur.close()
    info = {"product_version": r[0], "product_level": r[1], "product_update_level": r[2],
            "edition": r[3], "engine_edition": r[4], "database": r[5],
            "server_local_time": r[6], "server_utc_offset": r[7]}
    info["tls"] = tls_assessment(info["product_version"] or "")
    return info
