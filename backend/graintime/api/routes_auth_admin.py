"""Configuration -> Sign-in & LDAP, and Configuration -> Users (admin only)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from ..common import audit, crypto, ldap_auth, settings_store
from ..common.models import Session, User
from .security import get_db, hash_password, require_admin

router = APIRouter(prefix="/api")

# --------------------------------------------------------------------------- #
# LDAP settings
# --------------------------------------------------------------------------- #

DN_LIST = Field(default_factory=list, max_length=50)


class LdapSettings(BaseModel):
    enabled: bool = False
    servers: list[str] = Field(default_factory=list, max_length=10)
    security: Literal["ldaps", "starttls", "none"] = "ldaps"
    verify_cert: bool = True
    ca_cert_pem: str | None = Field(default=None, max_length=20000)
    bind_mode: Literal["service", "direct"] = "service"
    bind_dn: str | None = Field(default=None, max_length=500)
    bind_password: str | None = Field(default=None, max_length=256)   # write-only; blank = keep
    direct_bind_template: str | None = Field(default=None, max_length=200)
    base_dn: str = Field(default="", max_length=500)
    user_filter: str = Field(default=ldap_auth.DEFAULTS["user_filter"], max_length=500)
    display_name_attr: str = Field(default="displayName", max_length=100)
    admin_groups: list[str] = DN_LIST
    viewer_groups: list[str] = DN_LIST
    allow_any_user: bool = False
    nested_groups: bool = True

    @field_validator("servers")
    @classmethod
    def _servers(cls, v):
        out = []
        for s in (x.strip() for x in v):
            if not s:
                continue
            if not re.match(r"^(ldaps?://)?[A-Za-z0-9.\-]+(:\d{1,5})?$", s):
                raise ValueError(f"'{s}' is not a server name, e.g. dc1.corp.example or ldaps://dc1.corp.example:636.")
            out.append(s)
        return out

    @field_validator("admin_groups", "viewer_groups")
    @classmethod
    def _groups(cls, v):
        return [g.strip() for g in v if g.strip()]

    @field_validator("ca_cert_pem")
    @classmethod
    def _pem(cls, v):
        v = (v or "").strip()
        if v and "-----BEGIN CERTIFICATE-----" not in v:
            raise ValueError("Paste the CA certificate in PEM form (-----BEGIN CERTIFICATE----- ...).")
        return v or None

    @model_validator(mode="after")
    def _complete(self):
        if not self.enabled:
            return self
        if not self.servers:
            raise ValueError("Enter at least one directory server.")
        if not self.base_dn.strip():
            raise ValueError("Enter the base DN to search for users, e.g. DC=corp,DC=example.")
        if "{username}" not in self.user_filter:
            raise ValueError("The user filter must contain {username}.")
        if self.bind_mode == "service" and not (self.bind_dn or "").strip():
            raise ValueError("Enter the service account (bind DN), or choose direct sign-in.")
        if self.bind_mode == "direct" and "{username}" not in (self.direct_bind_template or ""):
            raise ValueError("The sign-in name template must contain {username}, e.g. {username}@corp.example.")
        if not self.admin_groups and not self.viewer_groups and not self.allow_any_user:
            raise ValueError("Choose at least one admin or viewer group, or allow any directory user.")
        return self


def _stored(db: DbSession) -> dict:
    return ldap_auth.config_with_defaults(settings_store.get_value(db, ldap_auth.SETTINGS_KEY))


def _out(cfg: dict) -> dict:
    out = {k: v for k, v in cfg.items() if k != "bind_password_encrypted"}
    out["has_bind_password"] = bool(cfg.get("bind_password_encrypted"))
    return out


def _merge(body: LdapSettings, current: dict) -> dict:
    new = body.model_dump(exclude={"bind_password"})
    new["bind_password_encrypted"] = current.get("bind_password_encrypted")
    if body.bind_password:
        new["bind_password_encrypted"] = crypto.encrypt(body.bind_password).decode()
    if body.bind_mode == "direct":
        new["bind_password_encrypted"] = None
    return new


@router.get("/settings/ldap")
def get_ldap(db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    return _out(_stored(db))


@router.put("/settings/ldap")
def put_ldap(body: LdapSettings, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    current = _stored(db)
    if body.enabled and body.bind_mode == "service" and not (body.bind_password or current.get("bind_password_encrypted")):
        raise HTTPException(422, "Enter the service account's password.")
    new = _merge(body, current)
    settings_store.set_value(db, ldap_auth.SETTINGS_KEY, new)
    old_c = {k: v for k, v in current.items() if new.get(k) != v}
    new_c = {k: new[k] for k in old_c}
    if "bind_password_encrypted" in old_c:
        old_c["bind_password"], new_c["bind_password"] = True, True
        old_c.pop("bind_password_encrypted"), new_c.pop("bind_password_encrypted")
    if new_c:
        audit.record(db, actor_id=user.id, actor_name=user.username, action="settings.ldap_updated",
                     entity_type="settings", entity_id="ldap", old=old_c, new=new_c)
    db.commit()
    return _out(new)


class LdapTestIn(BaseModel):
    settings: LdapSettings              # the form as it is now (may be unsaved)
    username: str | None = Field(default=None, max_length=200)
    password: str | None = Field(default=None, max_length=256)


@router.post("/settings/ldap/test")
def test_ldap(body: LdapTestIn, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    """Try the settings in the form without saving them. Nothing is stored."""
    cfg = _merge(body.settings, _stored(db))
    trace: list[dict] = []
    try:
        if body.username:
            u = ldap_auth.authenticate(cfg, body.username, body.password or "", trace)
            result = {"username": u.username, "display_name": u.display_name, "dn": u.dn, "role": u.role}
        else:
            trace = ldap_auth.test_service(cfg)
            result = None
        return {"ok": True, "steps": trace, "user": result}
    except ldap_auth.LdapError as exc:
        # On the test page the real cause helps; the sign-in page stays generic.
        detail = {"invalid_credentials": "The password was not accepted.",
                  "user_not_found": "No directory entry matches this username with the current filter.",
                  "account_locked": "The account is locked.", "account_disabled": "The account is disabled.",
                  "password_expired": "The password has expired.", "account_expired": "The account has expired.",
                  "password_change_required": "The user must change their password first.",
                  "invalid_username": "Usernames may contain letters, digits, '.', '_' and '-'.",
                  "not_in_group": "Password accepted, but the user is not in any of the admin or viewer groups"
                                  + (" (nested group checks work on Active Directory only; turn them off for other "
                                     "directories)" if cfg.get("nested_groups") else "") + ".",
                  }.get(exc.code, exc.message)
        trace.append({"step": exc.step or "error", "ok": False, "detail": detail})
        return {"ok": False, "code": exc.code, "steps": trace, "user": None}


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #

class UserAdminOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    auth_source: str
    is_active: bool
    created_at: datetime | None
    last_login_at: datetime | None


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=200)
    role: Literal["viewer", "admin"] = "viewer"

    @field_validator("username")
    @classmethod
    def _u(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9._@-]+$", v):
            raise ValueError("Use letters, digits, and . _ @ - only.")
        return v


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Literal["viewer", "admin"] | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=200)


def _active_admins(db: DbSession) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True)))


@router.get("/admin/users", response_model=list[UserAdminOut])
def list_users(db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    return db.scalars(select(User).order_by(User.auth_source, User.username)).all()


@router.post("/admin/users", response_model=UserAdminOut, status_code=201)
def create_user(body: UserCreate, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(409, f"The username {body.username} is already taken.")
    u = User(username=body.username, display_name=body.display_name.strip(), role=body.role,
             auth_source="local", password_hash=hash_password(body.password), is_active=True)
    db.add(u)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="user.created", entity_type="user",
                 entity_id=u.id, new={"username": u.username, "role": u.role, "auth_source": "local"})
    db.commit()
    return u


@router.patch("/admin/users/{uid}", response_model=UserAdminOut)
def update_user(uid: int, body: UserPatch, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404, "User not found")
    data = body.model_dump(exclude_unset=True)
    if u.auth_source != "local":
        if "role" in data and data["role"] != u.role:
            raise HTTPException(409, "This user's role comes from their directory groups. Change their group "
                                     "membership, or the group settings under Sign-in & LDAP.")
        if data.get("password"):
            raise HTTPException(409, "Directory users sign in with their directory password.")
    losing_admin = (data.get("role") == "viewer" or data.get("is_active") is False) and u.role == "admin" and u.is_active
    if losing_admin and u.id == user.id:
        raise HTTPException(409, "You cannot remove your own administrator access.")
    if losing_admin and _active_admins(db) <= 1:
        raise HTTPException(409, "GrainTime needs at least one active administrator.")
    old, new = {}, {}
    for k in ("display_name", "role", "is_active"):
        if k in data and data[k] is not None and getattr(u, k) != data[k]:
            old[k], new[k] = getattr(u, k), data[k]
            setattr(u, k, data[k])
    if data.get("password"):
        u.password_hash = hash_password(data["password"])
        old["password"], new["password"] = True, True
    if "is_active" in new and not u.is_active or "password" in new:
        db.execute(delete(Session).where(Session.user_id == u.id))     # sign them out everywhere
    if new:
        audit.record(db, actor_id=user.id, actor_name=user.username, action="user.updated", entity_type="user",
                     entity_id=u.id, old=old, new=new)
    db.commit()
    return u
