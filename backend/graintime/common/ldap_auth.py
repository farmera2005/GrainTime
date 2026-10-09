"""LDAP / Active Directory sign-in.

Configured in the browser (Configuration -> Sign-in & LDAP) and stored in
app_settings under "ldap". The service-account password is Fernet-encrypted
like site passwords and never returned or logged.

Two ways to find and check a user:
- "service": bind as a read-only service account, search base_dn with
  user_filter for exactly one entry, then bind as that entry's DN with the
  user's password.
- "direct": bind straight away with a template such as
  "{username}@mercerlandmark.com" or "MERCER\\{username}", then read the
  user's own entry.

The role comes from group membership on every sign-in: any admin group makes
an administrator; any viewer group (or "allow any user") makes a viewer;
otherwise sign-in is refused. With nested_groups, membership is checked with
Active Directory's LDAP_MATCHING_RULE_IN_CHAIN so groups inside groups count.

Safety: an empty password is always refused (an empty simple bind is an
anonymous bind and would "succeed"); user input is escaped before it goes
into a filter; usernames are restricted to a plain character set; every
operation has a 5 s connect and 10 s response timeout.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from dataclasses import dataclass, field

from ldap3 import BASE, NONE, SIMPLE, SUBTREE, Connection, Server, ServerPool, FIRST, Tls
from ldap3.core import exceptions as lx
from ldap3.core import tls as _ldap3_tls
from ldap3.utils.conv import escape_filter_chars

from . import crypto

# ldap3 checks the certificate's name itself. On Python 3.12+ (no
# ssl.match_hostname) it falls back to a copy that only knows DNS names, so a
# server given by IP address always failed. Match IP SANs here and defer
# everything else to ldap3. Verification itself is unchanged.
_ldap3_match_hostname = _ldap3_tls.match_hostname


def _match_hostname(cert: dict, hostname: str) -> None:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return _ldap3_match_hostname(cert, hostname)
    for key, value in cert.get("subjectAltName", ()):
        if key == "IP Address":
            try:
                if ipaddress.ip_address(value.strip()) == ip:
                    return None
            except ValueError:
                continue
    raise _ldap3_tls.CertificateError(f"certificate is not valid for {hostname}")


_ldap3_tls.match_hostname = _match_hostname

SETTINGS_KEY = "ldap"
CONNECT_TIMEOUT_S = 5
RECEIVE_TIMEOUT_S = 10
IN_CHAIN = "1.2.840.113556.1.4.1941"
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

DEFAULTS: dict = {
    "enabled": False,
    "servers": [],                 # "dc1.corp.example" or "ldaps://dc1.corp.example:636"
    "security": "ldaps",           # ldaps | starttls | none
    "verify_cert": True,
    "ca_cert_pem": None,           # optional: the directory's CA certificate (PEM)
    "bind_mode": "service",        # service | direct
    "bind_dn": None,
    "bind_password_encrypted": None,
    "direct_bind_template": None,  # e.g. "{username}@mercerlandmark.com"
    "base_dn": "",
    "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
    "display_name_attr": "displayName",
    "admin_groups": [],
    "viewer_groups": [],
    "allow_any_user": False,
    "nested_groups": True,
}


class LdapError(Exception):
    """A sign-in or test failure with a specific, admin-readable cause."""

    def __init__(self, code: str, message: str, step: str = ""):
        super().__init__(message)
        self.code, self.message, self.step = code, message, step


@dataclass
class LdapUser:
    username: str
    dn: str
    display_name: str
    role: str
    admin_groups: list[str] = field(default_factory=list)
    viewer_groups: list[str] = field(default_factory=list)


def normalize_username(raw: str) -> str:
    """"MERCER\\adamf", "adamf@mercerlandmark.com" and "AdamF" all become "adamf"."""
    u = (raw or "").strip()
    if "\\" in u:
        u = u.split("\\", 1)[1]
    if "@" in u:
        u = u.split("@", 1)[0]
    u = u.lower()
    if not USERNAME_RE.match(u):
        raise LdapError("invalid_username", "Wrong username or password.", "input")
    return u


def config_with_defaults(stored: dict | None) -> dict:
    return {**DEFAULTS, **(stored or {})}


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

def _servers(cfg: dict) -> ServerPool:
    if not cfg["servers"]:
        raise LdapError("not_configured", "No directory server is configured.", "connect")
    tls = None
    if cfg["security"] in ("ldaps", "starttls"):
        tls = Tls(validate=ssl.CERT_REQUIRED if cfg["verify_cert"] else ssl.CERT_NONE,
                  ca_certs_data=cfg["ca_cert_pem"] or None, version=ssl.PROTOCOL_TLS_CLIENT)
    servers = []
    for s in cfg["servers"]:
        s = s.strip()
        if "://" in s:
            scheme, rest = s.split("://", 1)
        else:
            scheme, rest = ("ldaps" if cfg["security"] == "ldaps" else "ldap"), s
        host, _, port = rest.partition(":")
        port_n = int(port) if port.isdigit() else (636 if scheme == "ldaps" else 389)
        servers.append(Server(host, port=port_n, use_ssl=scheme == "ldaps", tls=tls, get_info=NONE,
                              connect_timeout=CONNECT_TIMEOUT_S))
    return ServerPool(servers, FIRST, active=1, exhaust=True)


def _bind(cfg: dict, user: str, password: str, step: str) -> Connection:
    """Open, optionally StartTLS, and simple-bind. Raises LdapError with the cause."""
    if not password:
        raise LdapError("invalid_credentials", "Wrong username or password.", step)
    conn = Connection(_servers(cfg), user=user, password=password, authentication=SIMPLE,
                      receive_timeout=RECEIVE_TIMEOUT_S, auto_referrals=False, raise_exceptions=False,
                      read_only=True)
    try:
        conn.open()
        if cfg["security"] == "starttls" and not conn.start_tls():
            raise LdapError("tls_failed", "StartTLS was refused by the directory server.", "tls")
        if conn.bind():
            return conn
        desc = (conn.result or {}).get("description", "")
        msg = (conn.result or {}).get("message", "")
        conn.unbind()
        if desc == "invalidCredentials":
            # AD puts a sub-code in the message: 773 must change password, 775 locked, 533 disabled
            sub = re.search(r"data ([0-9a-f]{3})", msg or "")
            code = {"773": "password_change_required", "775": "account_locked", "533": "account_disabled",
                    "532": "password_expired", "701": "account_expired"}.get(sub.group(1) if sub else "", "invalid_credentials")
            raise LdapError(code, "Wrong username or password.", step)
        if desc == "confidentialityRequired" or desc == "strongerAuthRequired":
            raise LdapError("tls_required", "The directory requires an encrypted connection. Use LDAPS or StartTLS.", step)
        raise LdapError("bind_failed", f"The directory refused the sign-in ({desc or 'no reason given'}).", step)
    except LdapError:
        raise
    except lx.LDAPSocketOpenError as exc:
        text = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
            raise LdapError("certificate_untrusted",
                            "The directory server's certificate is not trusted, or does not match the server "
                            "name. Paste the directory's CA certificate in the box above, and use the name on the certificate.", "tls") from None
        raise LdapError("server_unreachable", "The directory server could not be reached (check the "
                        "server name, port and firewall).", "connect") from None
    except lx.LDAPServerPoolExhaustedError:
        raise LdapError("server_unreachable", "No directory server could be reached (check the server "
                        "names, ports and firewall).", "connect") from None
    except (lx.LDAPStartTLSError, ssl.SSLError) as exc:
        raise LdapError("tls_failed", f"The encrypted connection failed: {str(exc)[:160]}", "tls") from None
    except (lx.LDAPSocketReceiveError, lx.LDAPResponseTimeoutError):
        raise LdapError("timeout", "The directory server did not answer in time.", "connect") from None
    except lx.LDAPException as exc:
        raise LdapError("ldap_error", f"Directory error: {type(exc).__name__}", step) from None


def _attr(entry: dict, name: str) -> list[str]:
    v = entry.get("attributes", {}).get(name)
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def _find_user(conn: Connection, cfg: dict, username: str) -> dict:
    flt = cfg["user_filter"].replace("{username}", escape_filter_chars(username))
    attrs = [cfg["display_name_attr"], "memberOf"]
    ok = conn.search(cfg["base_dn"], flt, SUBTREE, attributes=attrs, size_limit=2,
                     time_limit=RECEIVE_TIMEOUT_S)
    entries = [e for e in conn.response or [] if e.get("type") == "searchResEntry"]
    if not ok and not entries:
        desc = (conn.result or {}).get("description", "")
        if desc not in ("success", "noSuchObject", ""):
            raise LdapError("search_failed", f"The user search failed ({desc}). Check the base DN and filter.", "search")
    if not entries:
        raise LdapError("user_not_found", "Wrong username or password.", "search")
    if len(entries) > 1:
        raise LdapError("multiple_users", "More than one directory entry matches this username. Make the "
                        "user filter more specific.", "search")
    return entries[0]


def _norm_dn(dn: str) -> str:
    return ",".join(p.strip() for p in dn.lower().split(","))


def _groups_matched(conn: Connection, cfg: dict, entry: dict, groups: list[str]) -> list[str]:
    if not groups:
        return []
    if cfg["nested_groups"]:
        found = []
        for g in groups:
            flt = f"(memberOf:{IN_CHAIN}:={escape_filter_chars(g)})"
            conn.search(entry["dn"], flt, BASE, attributes=[], size_limit=1, time_limit=RECEIVE_TIMEOUT_S)
            desc = (conn.result or {}).get("description", "")
            if desc not in ("success", "noSuchObject"):
                raise LdapError("nested_unsupported", "This directory does not support nested group checks "
                                "(an Active Directory feature). Turn off \"Include nested groups\".", "groups")
            if any(e.get("type") == "searchResEntry" for e in conn.response or []):
                found.append(g)
        return found
    member_of = {_norm_dn(g) for g in _attr(entry, "memberOf")}
    return [g for g in groups if _norm_dn(g) in member_of]


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #

def service_password(cfg: dict) -> str:
    enc = cfg.get("bind_password_encrypted")
    return crypto.decrypt(enc.encode()) if enc else ""


CREDENTIAL_CODES = {"invalid_credentials", "account_locked", "account_disabled", "password_expired",
                    "account_expired", "password_change_required"}


def _service_bind(cfg: dict) -> Connection:
    """Bind as the service account. Its credential problems are configuration
    errors, never reported as the signing-in user's wrong password."""
    try:
        return _bind(cfg, cfg["bind_dn"] or "", service_password(cfg), "service_bind")
    except LdapError as exc:
        if exc.code in CREDENTIAL_CODES:
            raise LdapError("service_bind_failed", "The service account's DN or password was not accepted "
                            f"({exc.code.replace('_', ' ')}).", "service_bind") from None
        raise


def authenticate(cfg: dict, raw_username: str, password: str, trace: list | None = None) -> LdapUser:
    """Check a username and password against the directory and work out the role.

    `trace`, if given, collects (step, ok, detail) for the admin test page."""
    def note(step: str, ok: bool, detail: str):
        if trace is not None:
            trace.append({"step": step, "ok": ok, "detail": detail})

    username = normalize_username(raw_username)
    if not password:
        raise LdapError("invalid_credentials", "Wrong username or password.", "input")
    if cfg["bind_mode"] == "service":
        svc = _service_bind(cfg)
        note("service_bind", True, f"Signed in as the service account {cfg['bind_dn']}")
        try:
            entry = _find_user(svc, cfg, username)
            note("search", True, f"Found {entry['dn']}")
            user_conn = _bind(cfg, entry["dn"], password, "user_bind")
            user_conn.unbind()
            note("user_bind", True, "Password accepted")
            admin = _groups_matched(svc, cfg, entry, cfg["admin_groups"])
            viewer = _groups_matched(svc, cfg, entry, cfg["viewer_groups"])
        finally:
            svc.unbind()
    else:
        template = cfg["direct_bind_template"] or "{username}"
        conn = _bind(cfg, template.replace("{username}", username), password, "user_bind")
        note("user_bind", True, "Password accepted")
        try:
            entry = _find_user(conn, cfg, username)
            note("search", True, f"Found {entry['dn']}")
            admin = _groups_matched(conn, cfg, entry, cfg["admin_groups"])
            viewer = _groups_matched(conn, cfg, entry, cfg["viewer_groups"])
        finally:
            conn.unbind()
    if admin:
        role = "admin"
    elif viewer or cfg["allow_any_user"]:
        role = "viewer"
    else:
        note("groups", False, "Not in any GrainTime admin or viewer group")
        raise LdapError("not_in_group", "Your account is not allowed to use GrainTime. Ask an "
                        "administrator to add you to the right group.", "groups")
    note("groups", True, f"Role: {role}" + (f" (admin groups: {', '.join(admin)})" if admin else "")
         + (f" (viewer groups: {', '.join(viewer)})" if viewer and not admin else ""))
    names = _attr(entry, cfg["display_name_attr"])
    return LdapUser(username=username, dn=entry["dn"], display_name=(names[0] if names else username)[:200],
                    role=role, admin_groups=admin, viewer_groups=viewer)


def test_service(cfg: dict) -> list[dict]:
    """Connect (and bind with the service account, if used) without any user."""
    trace: list[dict] = []
    if cfg["bind_mode"] == "service":
        conn = _service_bind(cfg)
        trace.append({"step": "service_bind", "ok": True, "detail": f"Signed in as {cfg['bind_dn']}"})
        ok = conn.search(cfg["base_dn"], "(objectClass=*)", BASE, attributes=[], size_limit=1)
        conn.unbind()
        if not ok:
            raise LdapError("base_dn_not_found", "The base DN was not found or cannot be read.", "search")
        trace.append({"step": "search", "ok": True, "detail": f"Base DN {cfg['base_dn']} is readable"})
    else:
        conn = Connection(_servers(cfg), receive_timeout=RECEIVE_TIMEOUT_S, raise_exceptions=False)
        try:
            conn.open()
            if cfg["security"] == "starttls" and not conn.start_tls():
                raise LdapError("tls_failed", "StartTLS was refused by the directory server.", "tls")
        except lx.LDAPSocketOpenError as exc:
            if "certificate" in str(exc).lower():
                raise LdapError("certificate_untrusted", "The directory server's certificate is not trusted.", "tls") from None
            raise LdapError("server_unreachable", "The directory server could not be reached (check the "
                            "server name, port and firewall).", "connect") from None
        finally:
            conn.unbind()
        trace.append({"step": "connect", "ok": True, "detail": "Connected. Enter a test user to check a sign-in."})
    return trace
