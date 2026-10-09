"""First-run setup wizard and sign-in.

Everything needed to start using the system is entered here, in the browser:
the first administrator, the global defaults, and the first site. Nothing is
typed into a terminal or a config file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..common import audit, ldap_auth, settings_store
from ..common.config import get_settings
from ..common.logging import get_logger
from ..common.models import CollectorJob, Site, User
from .schemas import AdminCreate, Defaults, LoginIn, UserOut
from .security import (end_session, get_db, hash_password, optional_user, require_admin,
                       start_session, throttle, verify_password)

router = APIRouter(prefix="/api")
STARTED_AT = datetime.now(timezone.utc)
log = get_logger("api.auth")


def _user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, display_name=u.display_name, role=u.role,
                   auth_source=u.auth_source)


def _window_closes_at() -> datetime:
    return STARTED_AT + timedelta(minutes=get_settings().setup_window_minutes)


@router.get("/setup/status")
def setup_status(db: DbSession = Depends(get_db), user: User | None = Depends(optional_user)):
    state = settings_store.setup_state(db)
    admin_exists = db.scalar(select(func.count()).select_from(User)
                             .where(User.role == "admin")) > 0
    out = {
        "setup_complete": bool(state.get("completed_at")),
        "admin_exists": admin_exists,
        "admin_window_open": not admin_exists and datetime.now(timezone.utc) < _window_closes_at(),
        "admin_window_closes_at": None if admin_exists else _window_closes_at().isoformat(),
        "user": _user_out(user) if user else None,
    }
    if user and user.role == "admin":
        out["steps"] = {
            "admin": admin_exists,
            "defaults": bool(state.get("defaults_confirmed_at")),
            "site": db.scalar(select(func.count()).select_from(Site)) > 0,
            "discovery": db.scalar(select(func.count()).select_from(CollectorJob).where(
                CollectorJob.kind == "discovery", CollectorJob.status == "succeeded")) > 0,
        }
    return out


@router.post("/setup/admin", response_model=UserOut)
def create_first_admin(body: AdminCreate, request: Request, response: Response,
                       db: DbSession = Depends(get_db)):
    # Serialise concurrent attempts so only one first admin can ever be created.
    db.execute(select(func.pg_advisory_xact_lock(7340001)))
    if db.scalar(select(func.count()).select_from(User)) > 0:
        raise HTTPException(409, "An administrator already exists. Sign in instead.")
    if datetime.now(timezone.utc) >= _window_closes_at():
        raise HTTPException(403, "The first-run window has closed. Restart the api container "
                                 "(for example from your Docker management tool) to reopen it.")
    user = User(username=body.username, display_name=body.display_name,
                password_hash=hash_password(body.password), auth_source="local", role="admin",
                is_active=True)
    db.add(user)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="setup.admin_created",
                 entity_type="user", entity_id=user.id,
                 new={"username": user.username, "role": "admin", "auth_source": "local"})
    start_session(db, user, request, response)
    db.commit()
    return _user_out(user)


@router.get("/settings/defaults", response_model=Defaults)
def get_defaults(db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    return Defaults(**settings_store.get_globals(db))


@router.put("/settings/defaults", response_model=Defaults)
def put_defaults(body: Defaults, db: DbSession = Depends(get_db),
                 user: User = Depends(require_admin)):
    old = settings_store.get_globals(db)
    new = body.model_dump()
    settings_store.set_value(db, settings_store.DEFAULTS_KEY, new)
    state = dict(settings_store.setup_state(db))
    state.setdefault("defaults_confirmed_at", datetime.now(timezone.utc).isoformat())
    settings_store.set_value(db, settings_store.SETUP_KEY, state)
    changed_old = {k: v for k, v in old.items() if new.get(k) != v}
    changed_new = {k: new[k] for k in changed_old}
    audit.record(db, actor_id=user.id, actor_name=user.username, action="settings.defaults_updated",
                 entity_type="settings", entity_id="defaults", old=changed_old, new=changed_new)
    db.commit()
    return body


@router.post("/setup/complete")
def complete_setup(db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    state = dict(settings_store.setup_state(db))
    if not state.get("completed_at"):
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        settings_store.set_value(db, settings_store.SETUP_KEY, state)
        audit.record(db, actor_id=user.id, actor_name=user.username, action="setup.completed",
                     entity_type="settings", entity_id="setup")
        db.commit()
    return {"setup_complete": True}


@router.get("/auth/options")
def auth_options(db: DbSession = Depends(get_db)):
    """What the sign-in page offers (no secrets)."""
    cfg = ldap_auth.config_with_defaults(settings_store.get_value(db, ldap_auth.SETTINGS_KEY))
    return {"ldap": bool(cfg["enabled"])}


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    """Local accounts first (the break-glass admin always works), then LDAP if enabled."""
    key = body.username.strip().lower()
    if throttle.blocked(key):
        raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.username == key))
    if user is not None and user.auth_source == "local":
        if not user.is_active or not verify_password(user.password_hash, body.password):
            throttle.fail(key)
            raise HTTPException(401, "Wrong username or password.")
    else:
        user = _ldap_login(db, body.username, body.password, key)
    throttle.clear(key)
    start_session(db, user, request, response)
    db.commit()
    return _user_out(user)


def _ldap_login(db: DbSession, raw: str, password: str, key: str) -> User:
    cfg = ldap_auth.config_with_defaults(settings_store.get_value(db, ldap_auth.SETTINGS_KEY))
    if not cfg["enabled"]:
        throttle.fail(key)
        raise HTTPException(401, "Wrong username or password.")
    try:
        found = ldap_auth.authenticate(cfg, raw, password)
    except ldap_auth.LdapError as exc:
        if exc.code in ("server_unreachable", "timeout", "tls_failed", "certificate_untrusted", "not_configured",
                        "service_bind_failed", "bind_failed", "multiple_users",
                        "nested_unsupported", "base_dn_not_found", "search_failed", "tls_required", "ldap_error"):
            log.warning("ldap sign-in unavailable", extra={"code": exc.code})
            raise HTTPException(503, "The directory server could not be reached. Try again, or ask an "
                                     "administrator.")
        throttle.fail(key)
        if exc.code == "not_in_group":
            raise HTTPException(403, exc.message)
        raise HTTPException(401, "Wrong username or password.")
    user = db.scalar(select(User).where(User.username == found.username))
    if user is not None and user.auth_source == "local":
        # A local account with the same name is never taken over by the directory.
        throttle.fail(key)
        raise HTTPException(401, "Wrong username or password.")
    if user is None:
        user = User(username=found.username, display_name=found.display_name, role=found.role,
                    auth_source="ldap", password_hash=None, is_active=True)
        db.add(user)
        db.flush()
        audit.record(db, actor_id=user.id, actor_name=user.username, action="user.created_from_ldap",
                     entity_type="user", entity_id=user.id,
                     new={"username": user.username, "role": user.role, "auth_source": "ldap"})
    elif not user.is_active:
        throttle.fail(key)
        raise HTTPException(403, "Your GrainTime account has been disabled. Ask an administrator.")
    else:
        if user.role != found.role:
            audit.record(db, actor_id=user.id, actor_name=user.username, action="user.role_from_ldap",
                         entity_type="user", entity_id=user.id, old={"role": user.role}, new={"role": found.role})
            user.role = found.role
        user.display_name = found.display_name
    return user


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: DbSession = Depends(get_db)):
    end_session(db, request, response)
    db.commit()
    return {"ok": True}
