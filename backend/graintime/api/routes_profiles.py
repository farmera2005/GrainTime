"""Mapping profiles (admin only): list, create, clone, edit, delete, login script."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..common import audit
from ..common.login_script import login_script
from ..common.models import MappingProfile, Site, User
from ..common.profiles import COMPUWEIGH_GMS, ProfileConfig
from .security import get_db, require_admin

router = APIRouter(prefix="/api/admin/mapping-profiles", dependencies=[Depends(require_admin)])


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    config: ProfileConfig
    confirm_in_use: bool = False


class CloneIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)


def _sites_using(db: DbSession, pid: int) -> list[dict]:
    return [{"id": s.id, "name": s.name, "polling_enabled": s.polling_enabled}
            for s in db.scalars(select(Site).where(Site.mapping_profile_id == pid).order_by(Site.name))]


def profile_out(db: DbSession, p: MappingProfile) -> dict:
    return {"id": p.id, "name": p.name, "description": p.description, "config": p.config,
            "sites": _sites_using(db, p.id), "updated_at": p.updated_at}


def _get(db: DbSession, pid: int) -> MappingProfile:
    p = db.get(MappingProfile, pid)
    if p is None:
        raise HTTPException(404, "Mapping profile not found")
    return p


def _config_dict(cfg: ProfileConfig) -> dict:
    return cfg.model_dump(mode="json", exclude_none=False)


@router.get("")
def list_profiles(db: DbSession = Depends(get_db)):
    return [profile_out(db, p) for p in db.scalars(select(MappingProfile).order_by(MappingProfile.name))]


@router.get("/template")
def template():
    """Starting point for a new profile (the CompuWeigh GMS shape)."""
    return {"config": _config_dict(ProfileConfig(**COMPUWEIGH_GMS))}


@router.get("/{pid}")
def get_profile(pid: int, db: DbSession = Depends(get_db)):
    return profile_out(db, _get(db, pid))


@router.post("", status_code=201)
def create_profile(body: ProfileIn, db: DbSession = Depends(get_db),
                   user: User = Depends(require_admin)):
    p = MappingProfile(name=body.name.strip(), description=body.description,
                       config=_config_dict(body.config))
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A profile named '{body.name}' already exists.")
    audit.record(db, actor_id=user.id, actor_name=user.username, action="profile.created",
                 entity_type="mapping_profile", entity_id=p.id,
                 new={"name": p.name, "config": p.config})
    db.commit()
    return profile_out(db, p)


@router.put("/{pid}")
def update_profile(pid: int, body: ProfileIn, db: DbSession = Depends(get_db),
                   user: User = Depends(require_admin)):
    p = _get(db, pid)
    sites = _sites_using(db, pid)
    new_config = _config_dict(body.config)
    if sites and not body.confirm_in_use and new_config != p.config:
        names = ", ".join(s["name"] for s in sites)
        raise HTTPException(409, f"This profile is used by {len(sites)} site(s): {names}. "
                                 "Confirm to apply the change to them on their next poll.")
    old = {"name": p.name, "description": p.description, "config": p.config}
    p.name, p.description, p.config = body.name.strip(), body.description, new_config
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A profile named '{body.name}' already exists.")
    audit.record(db, actor_id=user.id, actor_name=user.username, action="profile.updated",
                 entity_type="mapping_profile", entity_id=p.id, old=old,
                 new={"name": p.name, "description": p.description, "config": p.config})
    db.commit()
    return profile_out(db, p)


@router.post("/{pid}/clone", status_code=201)
def clone_profile(pid: int, body: CloneIn, db: DbSession = Depends(get_db),
                  user: User = Depends(require_admin)):
    src = _get(db, pid)
    base = (body.name or f"{src.name} (copy)").strip()
    name, n = base, 2
    while db.scalar(select(MappingProfile.id).where(MappingProfile.name == name)):
        name, n = f"{base} {n}", n + 1
    p = MappingProfile(name=name, description=src.description, config=dict(src.config))
    db.add(p)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="profile.cloned",
                 entity_type="mapping_profile", entity_id=p.id,
                 new={"name": p.name, "from_profile": src.id})
    db.commit()
    return profile_out(db, p)


@router.delete("/{pid}")
def delete_profile(pid: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    p = _get(db, pid)
    sites = _sites_using(db, pid)
    if sites:
        raise HTTPException(409, f"Used by {len(sites)} site(s); choose another profile for them first.")
    audit.record(db, actor_id=user.id, actor_name=user.username, action="profile.deleted",
                 entity_type="mapping_profile", entity_id=p.id, old={"name": p.name, "config": p.config})
    db.delete(p)
    db.commit()
    return {"deleted": True}


@router.get("/{pid}/login-script", response_class=PlainTextResponse)
def get_login_script(pid: int, database: str, login: str = "graintime", windows: bool = False,
                     db: DbSession = Depends(get_db)):
    p = _get(db, pid)
    for label, v in (("database", database), ("login", login)):
        if not v or len(v) > 128 or any(ch in v for ch in "[]'\";\n\r"):
            raise HTTPException(422, f"Invalid {label} name.")
    try:
        cfg = ProfileConfig(**p.config)
    except ValidationError as exc:
        raise HTTPException(422, f"The profile is invalid: {exc.errors()[0]['msg']}")
    return PlainTextResponse(login_script(cfg, database=database, login=login,
                                          windows_account=windows))
