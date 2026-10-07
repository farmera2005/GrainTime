"""Connections to site SQL Server databases. Only the collector imports this.

Every connection follows the site-database rules: 5 s connect timeout, 15 s
command timeout, READ UNCOMMITTED, low deadlock priority, autocommit, and
read-only intent. The password and the connection string are never logged or
returned.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
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
    auth_method: str = "sql"      # "sql" (SQL Server login) or "windows" (domain account, NTLM)
    domain: str | None = None     # Windows domain, e.g. MERCER (windows auth only)

    def __repr__(self) -> str:  # keep the password out of any accidental repr/log
        return (f"ConnectionSpec(host={self.host!r}, port={self.port}, "
                f"instance_name={self.instance_name!r}, database={self.database!r}, "
                f"auth_method={self.auth_method!r}, domain={self.domain!r}, "
                f"username={self.username!r}, encrypt={self.encrypt!r}, "
                f"trust_server_certificate={self.trust_server_certificate})")


class SiteConnectionError(Exception):
    """A classified connection failure: code, plain-language cause, and the fix."""

    def __init__(self, code: str, cause: str, fix: str, detail: str = "",
                 instances: list[dict] | None = None, checks: list[dict] | None = None):
        super().__init__(cause)
        self.code, self.cause, self.fix, self.detail = code, cause, fix, detail
        self.instances = instances  # SQL Server instances the host reported, if asked
        self.checks = checks        # network checks run while diagnosing

    def as_dict(self) -> dict:
        d = {"code": self.code, "cause": self.cause, "fix": self.fix,
             "docs": f"{DOCS}-{self.code.replace('_', '-')}", "detail": self.detail}
        if self.instances is not None:
            d["instances"] = self.instances
        if self.checks is not None:
            d["checks"] = self.checks
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
    ("host_unreachable", ("login timeout expired", "timeout", "timed out", "server is not found",
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


# Standard Windows services, used only to tell "machine reachable, SQL port
# blocked" apart from "machine not reachable at all". Connect-only, once per
# diagnosis, never sends data.
REACHABILITY_PORTS = [(445, "Windows file sharing (TCP 445)"), (135, "Windows RPC (TCP 135)"),
                      (3389, "Remote Desktop (TCP 3389)"), (5985, "Windows Remote Management (TCP 5985)"),
                      (80, "Web server (TCP 80)")]
PROBE_TIMEOUT_S = 2
DOCKER_DEFAULT_POOLS = [ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16")]


def probe_tcp(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> str:
    """'open', 'refused' (the machine answered), 'no answer', or 'unreachable'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "no answer"
    except OSError as exc:
        return f"unreachable ({exc.strerror or exc})"


def _resolve_ipv4(host: str) -> str | None:
    try:
        return socket.getaddrinfo(host, None, family=socket.AF_INET)[0][4][0]
    except socket.gaierror:
        return None


def _source_address(ip: str) -> str | None:
    """The address this container uses to reach ip (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((ip, 9))
            return s.getsockname()[0]
    except OSError:
        return None


def _network_checks(host: str, port: int | None, ip: str | None) -> tuple[list[dict], bool]:
    """Run the reachability checks. Returns (checks, machine_answered)."""
    checks = [{"check": "Address", "result": f"{host} → {ip}" if ip and ip != host else (ip or host),
               "ok": ip is not None}]
    if ip:
        src = _source_address(ip)
        checks.append({"check": "GrainTime collector's address (inside Docker)",
                       "result": f"{src or '?'}; the site sees the Docker host's own network "
                                 "address instead", "ok": True})
    checks.append({"check": f"SQL Server (TCP {port})", "result": "no answer", "ok": False})
    checks.append({"check": "SQL Server Browser (UDP 1434)", "result": "no answer", "ok": False})
    with ThreadPoolExecutor(max_workers=len(REACHABILITY_PORTS)) as pool:
        results = list(pool.map(lambda p: probe_tcp(host, p[0]), REACHABILITY_PORTS))
    answered = False
    for (_, label), r in zip(REACHABILITY_PORTS, results):
        ok = r in ("open", "refused")
        answered |= ok
        checks.append({"check": label, "ok": ok,
                       "result": {"open": "answered (open)",
                                  "refused": "answered (closed, but the machine replied)"}.get(r, r)})
    return checks, answered


def diagnose(spec: ConnectionSpec, exc: SiteConnectionError) -> SiteConnectionError:
    """Add what we can learn when the SQL port does not answer: which instances
    (and ports) the host's SQL Server Browser reports, whether the address
    collides with Docker's own network, and whether the machine answers at all."""
    if exc.code not in ("port_closed", "host_unreachable"):
        return exc
    instances = exc.instances if exc.instances is not None else browse_instances(spec.host)
    if instances:
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
    if exc.code != "host_unreachable":
        return exc  # refused: the machine answered; the message already says why

    ip = _resolve_ipv4(spec.host)
    checks, answered = _network_checks(spec.host, spec.port, ip)
    # Only silence looks like an overlap: if anything answered, a machine is really
    # there (e.g. a dev site on Docker's own network).
    overlap = None if answered else _docker_overlap(spec.host)
    if overlap:
        oip, net = overlap
        return SiteConnectionError(
            "docker_network_overlap",
            f"The site's address {oip} is inside Docker's internal network {net}",
            "Docker on this server uses the same address range as the site, so the connection "
            "never leaves the server. Ask IT to move Docker's address pools to a range the "
            "company network does not use (see the deployment notes).", checks=checks)
    where = ip or spec.host
    if answered:
        return SiteConnectionError(
            "sql_port_blocked",
            f"The server {where} is reachable, but SQL Server did not answer on TCP port "
            f"{spec.port}",
            f"Port {spec.port} was silently dropped, not refused: a firewall on that machine is "
            "filtering it (a Windows PC without one refuses an unused port instantly). Almost "
            "always this is Windows Firewall on the SQL Server PC itself, which is on by default "
            "even where the network has no firewall and never blocks programs on that same PC "
            "(such as CompuWeigh), so they still connect. On that PC: (1) find SQL Server's port "
            "in SQL Server Configuration Manager > Protocols > TCP/IP > IP Addresses > IPAll "
            "(TCP Port, or TCP Dynamic Ports for a named instance) and enter it here; "
            "(2) add a Windows Firewall inbound rule allowing TCP on that port from the Docker "
            "host's own IP address (not the 172.x Docker address listed below). To use the "
            "instance name instead of a port, also allow UDP 1434 and run the SQL Server Browser "
            "service.",
            exc.detail, checks=checks)
    hint = ""
    if ip and any(ipaddress.ip_address(ip) in n for n in DOCKER_DEFAULT_POOLS):
        hint = (f" {ip} is also in an address range Docker uses for its own networks by "
                "default; if another Docker network on this server uses it, traffic never leaves "
                "the server (see the deployment notes).")
    return SiteConnectionError(
        "no_route",
        f"Nothing at {where} answered the GrainTime server on any port",
        "The machine did not respond on the SQL port or on any standard Windows port. Check: "
        "the address is the SQL Server PC's own IP (run ipconfig on that PC); the GrainTime "
        "server is on a network that can reach the site (other PCs connecting proves the site "
        "is up, not that this server has a route to it, so ask IT about VLAN/VPN routing); and "
        "Windows Firewall on that PC, which can drop all traffic on a 'Public' network "
        "profile." + hint, exc.detail, checks=checks)


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
    tcp_probe(spec.host, spec.port)
    if spec.auth_method == "windows":
        conn = _connect_windows(spec)
    else:
        conn = _connect_odbc(spec)
    cur = conn.cursor()
    cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED; "
                "SET LOCK_TIMEOUT 5000; SET DEADLOCK_PRIORITY LOW;")
    cur.close()
    return conn


def _connect_odbc(spec: ConnectionSpec):
    """SQL Server logins: Microsoft ODBC Driver 18."""
    import pyodbc

    try:
        conn = pyodbc.connect(_connection_string(spec), timeout=CONNECT_TIMEOUT_S,
                              autocommit=True, readonly=True)
    except pyodbc.Error as exc:
        raise classify_driver_error(exc) from None
    conn.timeout = COMMAND_TIMEOUT_S
    return conn


# --- Windows (domain) accounts ------------------------------------------------ #
# ODBC Driver 18 on Linux can only sign in with Windows accounts through Kerberos,
# which needs a registered SPN, a server *name* (not an IP), domain DNS and synced
# clocks. Windows PCs fall back to NTLM when Kerberos is unavailable, so for
# Windows accounts we use python-tds with NTLM (pyspnego), which works by IP and
# needs nothing configured on this server.

SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
_TLS_PATCH_LOCK = threading.Lock()


def windows_principal(spec: ConnectionSpec) -> str:
    """DOMAIN\\user, or user@domain.tld when given as a UPN."""
    user = spec.username.strip()
    if "\\" in user or "@" in user or not spec.domain:
        return user
    return f"{spec.domain.strip()}\\{user}"


def _connect_windows(spec: ConnectionSpec):
    import pytds.login

    if spec.encrypt == "strict":
        raise SiteConnectionError(
            "encrypt_unsupported", "Strict (TDS 8) encryption isn't available with Windows sign-in",
            "Choose 'Encrypt (recommended)' for this site.")
    auth = pytds.login.SpnegoAuth(username=windows_principal(spec), password=spec.password,
                                  hostname=spec.host, service="MSSQLSvc", protocol="ntlm")
    return _connect_pytds(spec, auth=auth)


def _connect_pytds(spec: ConnectionSpec, **login):
    """python-tds connection with the same rules as ODBC: timeouts, read-only intent,
    autocommit, TLS per the site's encrypt / trust-certificate settings."""
    import pytds
    import pytds.tls
    from OpenSSL import SSL

    kwargs = dict(dsn=spec.host, port=spec.port, database=spec.database,
                  login_timeout=CONNECT_TIMEOUT_S, timeout=COMMAND_TIMEOUT_S, autocommit=True,
                  readonly=True, appname="GrainTime-Collector", disable_connect_retry=True,
                  **login)
    if spec.encrypt != "no":
        kwargs["cafile"] = SYSTEM_CA_BUNDLE          # turns TLS on, verified against system CAs
        kwargs["validate_host"] = not spec.trust_server_certificate
    try:
        if spec.encrypt != "no" and spec.trust_server_certificate:
            # Self-signed server certificate: python-tds always verifies, so hand it a
            # non-verifying TLS 1.2 context for this one login (same meaning as
            # TrustServerCertificate=yes in ODBC).
            def _trusting_context(_cafile):
                ctx = SSL.Context(SSL.TLSv1_2_METHOD)
                ctx.set_verify(SSL.VERIFY_NONE, lambda *a: True)
                return ctx
            with _TLS_PATCH_LOCK:
                original = pytds.tls.create_context
                pytds.tls.create_context = _trusting_context
                try:
                    return pytds.connect(**kwargs)
                finally:
                    pytds.tls.create_context = original
        return pytds.connect(**kwargs)
    except SiteConnectionError:
        raise
    except Exception as exc:  # pytds, OpenSSL and pyspnego raise various types
        raise classify_windows_error(exc, spec) from None


LOCKOUT_WARNING = ("Each failed attempt counts toward the domain's account lockout, so check "
                   "the details before testing again.")


def classify_windows_error(exc: Exception, spec: ConnectionSpec | None = None) -> SiteConnectionError:
    msg = str(exc)
    low = msg.lower()
    principal = windows_principal(spec) if spec else "the account"
    if "untrusted domain" in low or "18452" in low:
        # SSPI/NTLM handshake refused by Windows before SQL Server looks up a login.
        domain_hint = ""
        if spec and spec.domain and "." in spec.domain:
            domain_hint = (f" The domain '{spec.domain}' looks like a DNS name; NTLM normally "
                           f"expects the short (NetBIOS) name, e.g. '{spec.domain.split('.')[0].upper()}'.")
        return SiteConnectionError(
            "windows_credentials_rejected",
            f"Windows on the SQL Server PC did not accept {principal}",
            "SQL Server reports this as 'untrusted domain', but it almost always means the Windows "
            "sign-in itself failed, before SQL Server checks for a login. In order of likelihood: "
            "(1) the domain, user name or password is wrong (a wrong password gives exactly this "
            "message);" + domain_hint + " (2) the SQL Server PC is not joined to that domain, or "
            "cannot reach a domain controller (on a workgroup PC, use its own computer name as the "
            "domain and a local Windows account); (3) the domain restricts NTLM, or SQL Server "
            "requires Extended Protection. The exact reason is in the SQL Server PC's Windows "
            "Event Viewer > Security log, event 4625 at the time of the test (Failure Reason "
            "and Sub Status). " + LOCKOUT_WARNING, msg[:500])
    if "login failed" in low or "18456" in low:
        return SiteConnectionError(
            "windows_no_sql_login",
            f"Windows accepted {principal}, but SQL Server would not let it in",
            "The account signed in to Windows correctly, but SQL Server has no login for it or "
            "it has no access to this database. On the SQL Server: CREATE LOGIN [DOMAIN\\user] "
            "FROM WINDOWS; then add a user for it in the scale database with db_datareader "
            "(docs/sql/discovery_login.sql). The SQL Server error log shows the exact reason "
            "(error 18456 and its state number).", msg[:500])
    if "spnego" in low or "ntlm" in low:
        return SiteConnectionError(
            "windows_login_failed", "The Windows sign-in could not be completed",
            "Check the domain and user name (DOMAIN\\user or user@domain), and the password.",
            msg[:500])
    err = classify_driver_error(exc)
    if err.code == "mixed_mode_disabled":  # not relevant to Windows sign-in
        err = SiteConnectionError("windows_login_failed", "SQL Server rejected the Windows sign-in",
                                  "Check the domain, user name and password.", err.detail)
    return err


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
