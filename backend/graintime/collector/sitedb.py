"""Connections to site SQL Server databases. Only the collector imports this.

Every connection follows the site-database rules: 5 s connect timeout, 15 s
command timeout, READ UNCOMMITTED, low deadlock priority, autocommit, and
read-only intent. The password and the connection string are never logged or
returned.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

CONNECT_TIMEOUT_S = 5
COMMAND_TIMEOUT_S = 15
DRIVER = "ODBC Driver 18 for SQL Server"
DOCS = "README.md#deployment-notes-connecting-to-site-sql-servers"


@dataclass(frozen=True)
class ConnectionSpec:
    host: str
    port: int
    database: str
    username: str
    password: str
    encrypt: str = "yes"
    trust_server_certificate: bool = False

    def __repr__(self) -> str:  # keep the password out of any accidental repr/log
        return (f"ConnectionSpec(host={self.host!r}, port={self.port}, database={self.database!r}, "
                f"username={self.username!r}, encrypt={self.encrypt!r}, "
                f"trust_server_certificate={self.trust_server_certificate})")


class SiteConnectionError(Exception):
    """A classified connection failure: code, plain-language cause, and the fix."""

    def __init__(self, code: str, cause: str, fix: str, detail: str = ""):
        super().__init__(cause)
        self.code, self.cause, self.fix, self.detail = code, cause, fix, detail

    def as_dict(self) -> dict:
        return {"code": self.code, "cause": self.cause, "fix": self.fix,
                "docs": f"{DOCS}-{self.code.replace('_', '-')}", "detail": self.detail}


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
