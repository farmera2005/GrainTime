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

from ..common import audit, settings_store
from ..common.config import get_settings
from ..common.models import CollectorJob, Site, User
from .schemas import AdminCreate, Defaults, LoginIn, UserOut
from .security import (end_session, get_db, hash_password, optional_user, require_admin,
                       start_session, throttle, verify_password)

router = APIRouter(prefix="/api")
STARTED_AT = datetime.now(timezone.utc)


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


@router.post("/auth/login", response_model=UserOut)
def login(body: LoginIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    key = body.username.strip().lower()
    if throttle.blocked(key):
        raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.username == key))
    if not user or not user.is_active or not verify_password(user.password_hash, body.password):
        throttle.fail(key)
        raise HTTPException(401, "Wrong username or password.")
    throttle.clear(key)
    start_session(db, user, request, response)
    db.commit()
    return _user_out(user)


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: DbSession = Depends(get_db)):
    end_session(db, request, response)
    db.commit()
    return {"ok": True}
