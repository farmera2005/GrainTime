"""LDAP sign-in, its settings page, and user management.

Most tests replace the directory with a stub. The `real_ldap` tests run
against an actual LDAP server when LDAP_TEST_URL is set (see conftest notes):
  LDAP_TEST_URL=localhost:6636 LDAP_TEST_CA=/path/ca.pem LDAP_TEST_BASE=dc=corp,dc=example
with users alice (admin group), bob (viewer group), carol (no group) and the
service account uid=svc-graintime, as seeded by docs/dev/ldap-test-seed.ldif.
"""
import os

import pytest

from graintime.common import ldap_auth
from graintime.common.ldap_auth import LdapError, LdapUser

# --------------------------------------------------------------------------- #
# Pure checks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw,expected", [("adamf", "adamf"), ("MERCER\\AdamF", "adamf"),
                                          ("adamf@mercerlandmark.com", "adamf"), ("  bob.s ", "bob.s")])
def test_normalize_username(raw, expected):
    assert ldap_auth.normalize_username(raw) == expected


@pytest.mark.parametrize("raw", ["", "*", "a)(uid=*", "bob*", "x" * 101, "a b", "cn=admin,dc=x"])
def test_unsafe_usernames_refused_before_any_network(raw):
    with pytest.raises(LdapError) as e:
        ldap_auth.normalize_username(raw)
    assert e.value.code == "invalid_username"


def test_empty_password_never_reaches_the_directory(monkeypatch):
    # An empty simple bind is an anonymous bind that "succeeds"; it must be refused first.
    monkeypatch.setattr(ldap_auth, "_servers", lambda cfg: pytest.fail("connected with an empty password"))
    cfg = ldap_auth.config_with_defaults({"enabled": True, "servers": ["x"], "bind_mode": "direct",
                                          "direct_bind_template": "{username}@x", "allow_any_user": True})
    with pytest.raises(LdapError) as e:
        ldap_auth.authenticate(cfg, "alice", "")
    assert e.value.code == "invalid_credentials"


# --------------------------------------------------------------------------- #
# Settings API
# --------------------------------------------------------------------------- #

SETTINGS = {"enabled": True, "servers": ["dc1.corp.example"], "security": "ldaps", "verify_cert": True,
            "bind_mode": "service", "bind_dn": "CN=svc-graintime,OU=Service,DC=corp,DC=example",
            "bind_password": "Svc-Secret-Pw-1", "base_dn": "DC=corp,DC=example",
            "user_filter": "(&(objectClass=user)(sAMAccountName={username}))",
            "admin_groups": ["CN=GrainTime Admins,OU=Groups,DC=corp,DC=example"],
            "viewer_groups": ["CN=GrainTime Viewers,OU=Groups,DC=corp,DC=example"]}


def test_ldap_settings_password_is_write_only_and_audited_as_changed(admin_client, caplog):
    r = admin_client.put("/api/settings/ldap", json=SETTINGS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_bind_password"] is True
    assert "Svc-Secret-Pw-1" not in r.text and "bind_password_encrypted" not in body
    got = admin_client.get("/api/settings/ldap")
    assert "Svc-Secret-Pw-1" not in got.text and got.json()["servers"] == ["dc1.corp.example"]
    # Saving again with a blank password keeps the stored one.
    again = admin_client.put("/api/settings/ldap", json={**SETTINGS, "bind_password": "", "base_dn": "DC=corp2"})
    assert again.status_code == 200 and again.json()["has_bind_password"] is True
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import AppSetting, AuditLog
    with get_sessionmaker()() as db:
        stored = db.get(AppSetting, "ldap").value
        assert "Svc-Secret-Pw-1" not in str(stored) and stored["bind_password_encrypted"]
        entries = [a for a in db.query(AuditLog).all() if a.action == "settings.ldap_updated"]
        assert entries and entries[0].new_value["bind_password"] == "(changed)"
        assert "Svc-Secret-Pw-1" not in str([(a.old_value, a.new_value) for a in entries])
    assert "Svc-Secret-Pw-1" not in caplog.text


@pytest.mark.parametrize("patch,msg", [
    ({"servers": []}, "at least one directory server"),
    ({"base_dn": ""}, "base DN"),
    ({"user_filter": "(uid=x)"}, "{username}"),
    ({"admin_groups": [], "viewer_groups": []}, "at least one admin or viewer group"),
    ({"servers": ["bad host;"]}, "not a server name"),
    ({"ca_cert_pem": "hello"}, "PEM"),
    ({"bind_mode": "direct", "direct_bind_template": "corp\\user"}, "{username}"),
])
def test_ldap_settings_validation(admin_client, patch, msg):
    r = admin_client.put("/api/settings/ldap", json={**SETTINGS, **patch})
    assert r.status_code == 422 and msg in r.text


def test_service_password_required_when_enabling(admin_client):
    r = admin_client.put("/api/settings/ldap", json={**SETTINGS, "bind_password": None})
    assert r.status_code == 422 and "service account's password" in r.text


def test_ldap_test_endpoint_reports_cause_without_saving(admin_client, monkeypatch):
    def fake(cfg, raw, pw, trace=None):
        trace.append({"step": "service_bind", "ok": True, "detail": "ok"})
        raise LdapError("account_locked", "Wrong username or password.", "user_bind")
    monkeypatch.setattr(ldap_auth, "authenticate", fake)
    r = admin_client.post("/api/settings/ldap/test", json={"settings": SETTINGS, "username": "bob", "password": "x"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["code"] == "account_locked"
    assert r.json()["steps"][-1]["detail"] == "The account is locked."
    assert admin_client.get("/api/settings/ldap").json()["enabled"] is False     # nothing saved


# --------------------------------------------------------------------------- #
# Sign-in through LDAP (directory stubbed)
# --------------------------------------------------------------------------- #

@pytest.fixture
def ldap_on(admin_client, monkeypatch):
    assert admin_client.put("/api/settings/ldap", json=SETTINGS).status_code == 200
    admin_client.post("/api/auth/logout")
    directory = {"bob": ("Bob-Pass", "viewer", "Bob Smith"), "alice": ("Alice-Pass", "admin", "Alice Jones"),
                 "carol": ("Carol-Pass", None, "Carol")}

    def fake(cfg, raw, pw, trace=None):
        u = ldap_auth.normalize_username(raw)
        if u not in directory or directory[u][0] != pw:
            raise LdapError("invalid_credentials", "Wrong username or password.", "user_bind")
        if directory[u][1] is None:
            raise LdapError("not_in_group", "Your account is not allowed to use GrainTime.", "groups")
        return LdapUser(username=u, dn=f"CN={u}", display_name=directory[u][2], role=directory[u][1])
    monkeypatch.setattr(ldap_auth, "authenticate", fake)
    return directory


def test_directory_user_signs_in_and_gets_a_user_row(client, ldap_on):
    r = client.post("/api/auth/login", json={"username": "CORP\\Bob", "password": "Bob-Pass"})
    assert r.status_code == 200, r.text
    assert r.json() == {**r.json(), "username": "bob", "role": "viewer", "auth_source": "ldap",
                        "display_name": "Bob Smith"}
    assert client.get("/api/stats/options").status_code == 200        # signed in
    assert client.get("/api/admin/sites").status_code == 403          # but a viewer


def test_role_follows_groups_on_every_sign_in(client, ldap_on):
    client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"})
    client.post("/api/auth/logout")
    ldap_on["bob"] = ("Bob-Pass", "admin", "Bob Smith")
    r = client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"})
    assert r.json()["role"] == "admin"
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import AuditLog
    with get_sessionmaker()() as db:
        assert any(a.action == "user.role_from_ldap" for a in db.query(AuditLog).all())


def test_wrong_password_not_in_group_and_unreachable(client, ldap_on, monkeypatch):
    assert client.post("/api/auth/login", json={"username": "bob", "password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "carol", "password": "Carol-Pass"})
    assert r.status_code == 403 and "not allowed" in r.text

    def down(*a, **k):
        raise LdapError("server_unreachable", "x", "connect")
    monkeypatch.setattr(ldap_auth, "authenticate", down)
    r = client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"})
    assert r.status_code == 503 and "could not be reached" in r.text


def test_local_accounts_win_and_are_never_taken_over(client, ldap_on, make_user):
    # The break-glass admin keeps working whatever the directory says.
    assert client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"}).status_code == 200
    client.post("/api/auth/logout")
    # A local "bob" exists: a directory "bob" cannot sign in as that account.
    make_user("bob", "viewer", password="local-bob-password")
    assert client.post("/api/auth/login", json={"username": "CORP\\bob", "password": "Bob-Pass"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "bob", "password": "local-bob-password"}).status_code == 200


def test_disabled_directory_user_is_refused(client, ldap_on):
    client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"})
    client.post("/api/auth/logout")
    from graintime.common.db import get_sessionmaker
    from graintime.common.models import User
    with get_sessionmaker()() as db:
        db.query(User).filter_by(username="bob").one().is_active = False
        db.commit()
    assert client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"}).status_code == 403


def test_ldap_disabled_means_local_only(client, admin_client, monkeypatch):
    monkeypatch.setattr(ldap_auth, "authenticate", lambda *a, **k: pytest.fail("LDAP used while disabled"))
    admin_client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "bob", "password": "x"}).status_code == 401
    assert client.get("/api/auth/options").json() == {"ldap": False}


# --------------------------------------------------------------------------- #
# Users page
# --------------------------------------------------------------------------- #

def test_admin_manages_local_users(admin_client, client):
    r = admin_client.post("/api/admin/users", json={"username": "Dana", "display_name": "Dana",
                                                     "password": "dana-password-123", "role": "viewer"})
    assert r.status_code == 201 and r.json()["username"] == "dana"
    uid = r.json()["id"]
    assert admin_client.post("/api/admin/users", json={"username": "dana", "display_name": "x",
                                                        "password": "dana-password-123"}).status_code == 409
    assert admin_client.patch(f"/api/admin/users/{uid}", json={"role": "admin"}).json()["role"] == "admin"
    listed = admin_client.get("/api/admin/users").json()
    assert {u["username"] for u in listed} >= {"admin", "dana"}
    assert all("password" not in k for u in listed for k in u)
    assert admin_client.patch(f"/api/admin/users/{uid}", json={"password": "dana-new-password-1"}).status_code == 200


def test_cannot_lock_out_the_last_or_own_admin(admin_client):
    me = next(u for u in admin_client.get("/api/admin/users").json() if u["username"] == "admin")
    r = admin_client.patch(f"/api/admin/users/{me['id']}", json={"role": "viewer"})
    assert r.status_code == 409 and "your own" in r.text
    assert admin_client.patch(f"/api/admin/users/{me['id']}", json={"is_active": False}).status_code == 409


def test_disabling_a_user_signs_them_out(admin_client, client, make_user):
    pw = make_user("erin", "viewer")
    from fastapi.testclient import TestClient

    from graintime.api.main import app
    with TestClient(app, headers={"X-GrainTime": "1"}) as erin:
        assert erin.post("/api/auth/login", json={"username": "erin", "password": pw}).status_code == 200
        uid = next(u["id"] for u in admin_client.get("/api/admin/users").json() if u["username"] == "erin")
        assert admin_client.patch(f"/api/admin/users/{uid}", json={"is_active": False}).status_code == 200
        assert erin.get("/api/stats/options").status_code == 401


def test_directory_users_role_comes_from_groups(admin_client, client, ldap_on):
    client.post("/api/auth/login", json={"username": "bob", "password": "Bob-Pass"})
    admin_client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
    bob = next(u for u in admin_client.get("/api/admin/users").json() if u["username"] == "bob")
    assert bob["auth_source"] == "ldap"
    r = admin_client.patch(f"/api/admin/users/{bob['id']}", json={"role": "admin"})
    assert r.status_code == 409 and "directory groups" in r.text
    assert admin_client.patch(f"/api/admin/users/{bob['id']}", json={"is_active": False}).status_code == 200


# --------------------------------------------------------------------------- #
# Against a real LDAP server (optional)
# --------------------------------------------------------------------------- #

LDAP_URL = os.environ.get("LDAP_TEST_URL")
real_ldap = pytest.mark.skipif(not LDAP_URL, reason="LDAP_TEST_URL not set")


def real_cfg(**kw):
    from graintime.common import crypto
    base = os.environ.get("LDAP_TEST_BASE", "dc=corp,dc=example")
    ca = os.environ.get("LDAP_TEST_CA")
    return ldap_auth.config_with_defaults({
        "enabled": True, "servers": [LDAP_URL], "security": "ldaps",
        "ca_cert_pem": open(ca).read() if ca else None, "bind_dn": f"uid=svc-graintime,ou=People,{base}",
        "bind_password_encrypted": crypto.encrypt("Svc-Pass-2026!").decode(), "base_dn": base,
        "user_filter": "(&(objectClass=inetOrgPerson)(uid={username}))", "nested_groups": False,
        "admin_groups": [f"cn=GrainTime Admins,ou=Groups,{base}"],
        "viewer_groups": [f"CN=GrainTime Viewers, OU=Groups,{base.upper()}"], **kw})


@real_ldap
def test_real_directory_roles_and_failures(db_clean):
    cfg = real_cfg()
    assert ldap_auth.authenticate(cfg, "alice", "Alice-Pass-2026!").role == "admin"
    bob = ldap_auth.authenticate(cfg, "CORP\\Bob", "Bob-Pass-2026!")
    assert (bob.role, bob.display_name) == ("viewer", "Bob Viewer")     # group DN compared case-insensitively
    for user, pw, code in [("carol", "Carol-Pass-2026!", "not_in_group"), ("alice", "wrong", "invalid_credentials"),
                           ("nobody", "x", "user_not_found")]:
        with pytest.raises(LdapError) as e:
            ldap_auth.authenticate(cfg, user, pw)
        assert e.value.code == code
    assert ldap_auth.authenticate(real_cfg(allow_any_user=True), "carol", "Carol-Pass-2026!").role == "viewer"


@real_ldap
def test_real_directory_tls_and_service_account(db_clean):
    from graintime.common import crypto
    with pytest.raises(LdapError) as e:
        ldap_auth.authenticate(real_cfg(ca_cert_pem=None), "alice", "Alice-Pass-2026!")
    assert e.value.code == "certificate_untrusted"           # verification stays on without the CA
    with pytest.raises(LdapError) as e:
        ldap_auth.authenticate(real_cfg(bind_password_encrypted=crypto.encrypt("bad").decode()), "alice", "Alice-Pass-2026!")
    assert e.value.code == "service_bind_failed"             # never reported as the user's password
    with pytest.raises(LdapError) as e:
        ldap_auth.authenticate(real_cfg(servers=["127.0.0.1:1"]), "alice", "Alice-Pass-2026!")
    assert e.value.code == "server_unreachable"
    base = os.environ.get("LDAP_TEST_BASE", "dc=corp,dc=example")
    direct = real_cfg(bind_mode="direct", direct_bind_template=f"uid={{username}},ou=People,{base}")
    assert ldap_auth.authenticate(direct, "alice", "Alice-Pass-2026!").role == "admin"
    assert [s["step"] for s in ldap_auth.test_service(real_cfg())] == ["service_bind", "search"]


def test_certificate_ip_addresses_match():
    cert = {"subject": ((("commonName", "dc1"),),),
            "subjectAltName": (("DNS", "dc1.corp.example"), ("IP Address", "10.12.0.5"))}
    ldap_auth._match_hostname(cert, "10.12.0.5")
    ldap_auth._match_hostname(cert, "dc1.corp.example")
    for wrong in ("10.12.0.6", "dc2.corp.example"):
        with pytest.raises(Exception):
            ldap_auth._match_hostname(cert, wrong)


@real_ldap
def test_real_directory_by_ip_address_with_verification(db_clean):
    host, _, port = LDAP_URL.partition(":")
    if host != "localhost":
        pytest.skip("needs the test certificate's 127.0.0.1 entry")
    assert ldap_auth.authenticate(real_cfg(servers=[f"127.0.0.1:{port}"]), "alice", "Alice-Pass-2026!").role == "admin"
