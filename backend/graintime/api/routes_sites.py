"""Sites and the collector jobs run against them (admin only).

The api never connects to a site database: Test connection and Discovery are
queued as collector jobs and the browser polls for the result.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..common import audit, crypto
from ..common.models import CollectorJob, MappingProfile, Site, SiteCollectorState, Ticket, User
from ..common import settings_store
from ..common.normalize import SITE_TZ
from .schemas import check_auth
from .schemas import (BackfillIn, DiscoveryIn, PreviewIn, SiteCreate, SiteDelete, SiteOut,
                      SiteUpdate, TestConnectionIn)
from .security import get_db, require_admin

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

# Fields shown in the audit log (the password is recorded only as "(changed)").
AUDIT_FIELDS = ("name", "code", "address", "map_url", "host", "port", "instance_name",
                "database_name", "auth_method", "domain", "username",
                "encrypt", "trust_server_certificate", "polling_enabled", "show_on_dashboard",
                "mapping_profile_id", "poll_interval_s",
                "show_on_public", "hours")


def site_out(s: Site) -> SiteOut:
    return SiteOut(id=s.id, name=s.name, code=s.code, address=s.address, map_url=s.map_url,
                   host=s.host, port=s.port, instance_name=s.instance_name,
                   database=s.database_name, auth_method=s.auth_method, domain=s.domain,
                   username=s.username,
                   has_password=bool(s.password_encrypted), encrypt=s.encrypt,
                   trust_server_certificate=s.trust_server_certificate,
                   polling_enabled=s.polling_enabled, show_on_dashboard=s.show_on_dashboard,
                   show_on_public=s.show_on_public, archived=s.archived_at is not None,
                   hours=s.hours,
                   mapping_profile_id=s.mapping_profile_id, poll_interval_s=s.poll_interval_s)


def _snapshot(s: Site) -> dict:
    return {f: getattr(s, f) for f in AUDIT_FIELDS}


def _get_site(db: DbSession, site_id: int) -> Site:
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(404, "Site not found")
    return site


def _code_taken(db: DbSession, code: str, exclude_id: int | None = None) -> bool:
    q = select(Site.id).where(Site.code == code)
    if exclude_id is not None:
        q = q.where(Site.id != exclude_id)
    return db.scalar(q) is not None


def local_day_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(SITE_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _interval(db: DbSession, site: Site) -> int:
    return site.poll_interval_s or int(settings_store.get_globals(db)["poll_interval_s"])


def is_stale(site: Site, state: SiteCollectorState | None, interval: int,
             now: datetime | None = None) -> bool:
    if not site.polling_enabled or site.archived_at is not None:
        return False
    now = now or datetime.now(timezone.utc)
    if state is None or state.last_success_at is None:
        return True
    return (now - state.last_success_at).total_seconds() > max(3 * interval, 180)


@router.get("/sites", response_model=list[SiteOut])
def list_sites(db: DbSession = Depends(get_db)):
    sites = db.scalars(select(Site).order_by(Site.name)).all()
    states = {st.site_id: st for st in db.scalars(select(SiteCollectorState))}
    day0 = local_day_start_utc()
    counts = dict(db.execute(
        select(Ticket.site_id, func.count()).where(Ticket.inbound_at >= day0,
                                                   Ticket.status != "voided")
        .group_by(Ticket.site_id)).all())
    default = int(settings_store.get_globals(db)["poll_interval_s"])
    out = []
    for s in sites:
        o = site_out(s)
        st = states.get(s.id)
        o.last_success_at = st.last_success_at.isoformat() if st and st.last_success_at else None
        o.last_error = st.last_error if st else None
        o.stale = is_stale(s, st, s.poll_interval_s or default)
        o.trucks_today = counts.get(s.id, 0)
        out.append(o)
    return out


@router.post("/sites", response_model=SiteOut, status_code=201)
def create_site(body: SiteCreate, db: DbSession = Depends(get_db),
                user: User = Depends(require_admin)):
    if _code_taken(db, body.code):
        raise HTTPException(409, f"Short code {body.code} is already used by another site.")
    site = Site(name=body.name.strip(), code=body.code, address=body.address,
                map_url=body.map_url, host=body.host, port=body.port,
                instance_name=body.instance_name, auth_method=body.auth_method,
                domain=body.domain,
                database_name=body.database, username=body.username,
                password_encrypted=crypto.encrypt(body.password), encrypt=body.encrypt,
                trust_server_certificate=body.trust_server_certificate,
                # New sites never start polling or appear publicly on their own.
                polling_enabled=False, show_on_dashboard=True, show_on_public=False)
    db.add(site)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Short code {body.code} is already used by another site.")
    audit.record(db, actor_id=user.id, actor_name=user.username, action="site.created",
                 entity_type="site", entity_id=site.id, new={**_snapshot(site), "password": True})
    db.commit()
    return site_out(site)


@router.get("/sites/{site_id}", response_model=SiteOut)
def get_site(site_id: int, db: DbSession = Depends(get_db)):
    return site_out(_get_site(db, site_id))


@router.patch("/sites/{site_id}", response_model=SiteOut)
def update_site(site_id: int, body: SiteUpdate, db: DbSession = Depends(get_db),
                user: User = Depends(require_admin)):
    site = _get_site(db, site_id)
    before = _snapshot(site)
    data = body.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    if "database" in data:
        data["database_name"] = data.pop("database")
    if data.get("code") and _code_taken(db, data["code"], exclude_id=site.id):
        raise HTTPException(409, f"Short code {data['code']} is already used by another site.")
    if "mapping_profile_id" in data and data["mapping_profile_id"] is not None \
            and db.get(MappingProfile, data["mapping_profile_id"]) is None:
        raise HTTPException(422, "That mapping profile does not exist.")
    profile_after = data["mapping_profile_id"] if "mapping_profile_id" in data else site.mapping_profile_id
    polling_after = data.get("polling_enabled", site.polling_enabled)
    if polling_after and profile_after is None:
        if data.get("polling_enabled"):
            raise HTTPException(409, "Choose a mapping profile for this site before turning "
                                     "polling on.")
        data["polling_enabled"] = False      # clearing the profile stops polling
    if data.get("polling_enabled") and site.archived_at is not None:
        raise HTTPException(409, "Restore this site before turning polling on.")
    if site.archived_at is not None and (data.get("show_on_dashboard") or data.get("show_on_public")):
        raise HTTPException(409, "Restore this site before showing it on the dashboard or "
                                 "public page.")
    if "hours" in data:
        data["hours"] = body.hours.stored() if body.hours else None
    for k in ("address", "map_url", "domain"):
        if k in data and isinstance(data[k], str):
            data[k] = data[k].strip() or None
    try:
        check_auth(data.get("auth_method", site.auth_method),
                   data["domain"] if "domain" in data else site.domain,
                   data.get("username") or site.username, data.get("encrypt") or site.encrypt)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    for k, v in data.items():
        if v is None and k in ("name", "code", "host", "port", "database_name", "username",
                               "auth_method",
                               "encrypt", "trust_server_certificate", "polling_enabled",
                               "show_on_dashboard", "show_on_public"):
            continue  # required fields cannot be cleared
        setattr(site, k, v.strip() if k == "name" else v)
    if password:
        site.password_encrypted = crypto.encrypt(password)
    after = _snapshot(site)
    old = {k: v for k, v in before.items() if after[k] != v}
    new = {k: after[k] for k in old}
    if password:
        old["password"], new["password"] = True, True
    if new:
        audit.record(db, actor_id=user.id, actor_name=user.username, action="site.updated",
                     entity_type="site", entity_id=site.id, old=old, new=new)
    db.commit()
    return site_out(site)


@router.post("/sites/{site_id}/archive", response_model=SiteOut)
def archive_site(site_id: int, db: DbSession = Depends(get_db),
                 user: User = Depends(require_admin)):
    """Hide the site everywhere and stop polling; all history is kept."""
    site = _get_site(db, site_id)
    if site.archived_at is None:
        before = _snapshot(site)
        site.archived_at = datetime.now(timezone.utc)
        site.polling_enabled = site.show_on_dashboard = site.show_on_public = False
        after = _snapshot(site)
        old = {k: v for k, v in before.items() if after[k] != v}
        audit.record(db, actor_id=user.id, actor_name=user.username, action="site.archived",
                     entity_type="site", entity_id=site.id, old={**old, "archived": False},
                     new={**{k: after[k] for k in old}, "archived": True})
        db.commit()
    return site_out(site)


@router.post("/sites/{site_id}/restore", response_model=SiteOut)
def restore_site(site_id: int, db: DbSession = Depends(get_db),
                 user: User = Depends(require_admin)):
    """Bring an archived site back. It returns with polling off and hidden from the
    public page; switch those back on deliberately."""
    site = _get_site(db, site_id)
    if site.archived_at is not None:
        site.archived_at = None
        site.show_on_dashboard = True
        audit.record(db, actor_id=user.id, actor_name=user.username, action="site.restored",
                     entity_type="site", entity_id=site.id, old={"archived": True},
                     new={"archived": False, "show_on_dashboard": True})
        db.commit()
    return site_out(site)


@router.delete("/sites/{site_id}")
def delete_site(site_id: int, body: SiteDelete, db: DbSession = Depends(get_db),
                user: User = Depends(require_admin)):
    """Permanently delete a site and everything stored for it. The admin must type
    the site's name exactly."""
    site = _get_site(db, site_id)
    if body.confirm_name.strip() != site.name:
        raise HTTPException(400, "The name typed does not match the site's name. Nothing was "
                                 "deleted.")
    snapshot = _snapshot(site)
    audit.record(db, actor_id=user.id, actor_name=user.username, action="site.deleted",
                 entity_type="site", entity_id=site.id, old=snapshot)
    db.delete(site)  # collector jobs cascade
    db.commit()
    return {"deleted": True}


# --- collector jobs -------------------------------------------------------- #

def job_out(j: CollectorJob, include_result: bool = True) -> dict:
    result = j.result
    if result and j.kind == "discovery":
        result = {"summary": result.get("summary")}  # full report via the download endpoints
    return {"id": j.id, "kind": j.kind, "site_id": j.site_id, "status": j.status,
            "progress": j.progress, "error": j.error,
            "result": result if include_result else None,
            "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at}


@router.post("/jobs/test-connection", status_code=202)
def queue_test_connection(body: TestConnectionIn, db: DbSession = Depends(get_db),
                          user: User = Depends(require_admin)):
    params: dict = {}
    if body.site_id is not None:
        _get_site(db, body.site_id)
    if body.connection is not None:
        c = body.connection
        params["connection"] = {
            "host": c.host, "port": c.port, "instance_name": c.instance_name,
            "auth_method": c.auth_method, "domain": c.domain,
            "database": c.database, "username": c.username,
            "encrypt": c.encrypt, "trust_server_certificate": c.trust_server_certificate,
            # Ciphertext only, wiped by the collector when the job ends.
            "password_encrypted": (base64.b64encode(crypto.encrypt(c.password)).decode()
                                   if c.password else None),
        }
    job = CollectorJob(kind="test_connection", site_id=body.site_id, params=params,
                       status="queued", requested_by=user.id)
    db.add(job)
    db.commit()
    return job_out(job)


@router.post("/sites/{site_id}/discovery", status_code=202)
def queue_discovery(site_id: int, body: DiscoveryIn, db: DbSession = Depends(get_db),
                    user: User = Depends(require_admin)):
    _get_site(db, site_id)
    job = CollectorJob(kind="discovery", site_id=site_id, params={"options": body.model_dump()},
                       status="queued", requested_by=user.id)
    db.add(job)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="site.discovery_started",
                 entity_type="site", entity_id=site_id, new={"job_id": job.id, **body.model_dump()})
    db.commit()
    return job_out(job)


@router.get("/sites/{site_id}/jobs")
def site_jobs(site_id: int, kind: str | None = None, limit: int = 10,
              db: DbSession = Depends(get_db)):
    q = select(CollectorJob).where(CollectorJob.site_id == site_id)
    if kind:
        q = q.where(CollectorJob.kind == kind)
    q = q.order_by(CollectorJob.id.desc()).limit(max(1, min(limit, 50)))
    return [job_out(j) for j in db.scalars(q)]


def _get_job(db: DbSession, job_id: int) -> CollectorJob:
    job = db.get(CollectorJob, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: DbSession = Depends(get_db)):
    return job_out(_get_job(db, job_id))


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: DbSession = Depends(get_db)):
    job = _get_job(db, job_id)
    if job.status in ("queued", "running"):
        job.cancel_requested = True
        db.commit()
    return job_out(job)


def _discovery_result(db: DbSession, job_id: int) -> tuple[CollectorJob, dict]:
    job = _get_job(db, job_id)
    if job.kind != "discovery" or job.status != "succeeded" or not job.result:
        raise HTTPException(404, "No discovery report for this job")
    return job, job.result


@router.get("/jobs/{job_id}/report.md")
def discovery_markdown(job_id: int, download: bool = False, db: DbSession = Depends(get_db)):
    job, result = _discovery_result(db, job_id)
    headers = ({"Content-Disposition": f'attachment; filename="discovery-site{job.site_id}-'
                                       f'job{job.id}.md"'} if download else {})
    return Response(result.get("markdown", ""), media_type="text/markdown; charset=utf-8",
                    headers=headers)


@router.get("/jobs/{job_id}/report.json")
def discovery_json(job_id: int, db: DbSession = Depends(get_db)):
    job, result = _discovery_result(db, job_id)
    return Response(json.dumps(result.get("report", {}), indent=2, default=str),
                    media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="discovery-site'
                                                    f'{job.site_id}-job{job.id}.json"'})


# --- ticket collection: preview, backfill, status ------------------------- #

@router.post("/sites/{site_id}/preview", status_code=202)
def queue_preview(site_id: int, body: PreviewIn, db: DbSession = Depends(get_db),
                  user: User = Depends(require_admin)):
    site = _get_site(db, site_id)
    pid = body.profile_id or site.mapping_profile_id
    if pid is None or db.get(MappingProfile, pid) is None:
        raise HTTPException(409, "Choose a mapping profile first.")
    job = CollectorJob(kind="preview", site_id=site_id, status="queued", requested_by=user.id,
                       params={"options": {"profile_id": pid, "limit": body.limit}})
    db.add(job)
    db.commit()
    return job_out(job)


@router.post("/sites/{site_id}/backfill", status_code=202)
def queue_backfill(site_id: int, body: BackfillIn, db: DbSession = Depends(get_db),
                   user: User = Depends(require_admin)):
    site = _get_site(db, site_id)
    if site.mapping_profile_id is None:
        raise HTTPException(409, "Choose a mapping profile for this site first.")
    active = db.scalar(select(CollectorJob.id).where(
        CollectorJob.site_id == site_id, CollectorJob.kind == "backfill",
        CollectorJob.status.in_(("queued", "running"))))
    if active:
        raise HTTPException(409, "A backfill is already running for this site.")
    opts = {"date_from": body.date_from.isoformat(), "date_to": body.date_to.isoformat()}
    job = CollectorJob(kind="backfill", site_id=site_id, status="queued", requested_by=user.id,
                       params={"options": opts})
    db.add(job)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="site.backfill_started",
                 entity_type="site", entity_id=site_id, new={"job_id": job.id, **opts})
    db.commit()
    return job_out(job)


@router.get("/sites/{site_id}/collection")
def collection_status(site_id: int, db: DbSession = Depends(get_db)):
    site = _get_site(db, site_id)
    st = db.get(SiteCollectorState, site_id)
    interval = _interval(db, site)
    day0 = local_day_start_utc()
    total = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.site_id == site_id))
    today = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.site_id == site_id, Ticket.inbound_at >= day0, Ticket.status != "voided"))
    open_now = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.site_id == site_id, Ticket.status == "open",
        Ticket.inbound_at >= datetime.now(timezone.utc) - timedelta(hours=6)))
    recent = db.scalars(select(Ticket).where(Ticket.site_id == site_id)
                        .order_by(Ticket.inbound_at.desc().nulls_last()).limit(20)).all()
    running = db.scalars(select(CollectorJob).where(
        CollectorJob.site_id == site_id, CollectorJob.kind == "backfill")
        .order_by(CollectorJob.id.desc()).limit(1)).first()
    return {
        "polling_enabled": site.polling_enabled,
        "interval_s": interval,
        "stale": is_stale(site, st, interval),
        "state": None if st is None else {
            "high_water_mark": st.high_water_mark, "last_poll_at": st.last_poll_at,
            "last_success_at": st.last_success_at, "next_poll_at": st.next_poll_at,
            "consecutive_failures": st.consecutive_failures, "last_error": st.last_error,
            "rows_last_poll": st.rows_last_poll, "rows_total": st.rows_total,
            "last_recheck_at": st.last_recheck_at},
        "tickets": {"total": total, "today": today, "on_site_now": open_now},
        "recent": [{"source_ticket_id": t.source_ticket_id, "ticket_number": t.ticket_number,
                    "status": t.status, "commodity": t.commodity, "inbound_at": t.inbound_at,
                    "outbound_at": t.outbound_at, "duration_s": t.duration_s,
                    "single_weigh": t.single_weigh} for t in recent],
        "last_backfill": job_out(running) if running else None,
    }
