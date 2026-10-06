"""Sites and the collector jobs run against them (admin only).

The api never connects to a site database: Test connection and Discovery are
queued as collector jobs and the browser polls for the result.
"""

from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..common import audit, crypto
from ..common.models import CollectorJob, Site, User
from .schemas import DiscoveryIn, SiteCreate, SiteOut, SiteUpdate, TestConnectionIn
from .security import get_db, require_admin

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

# Fields shown in the audit log (the password is recorded only as "(changed)").
AUDIT_FIELDS = ("name", "code", "address", "map_url", "host", "port", "database_name", "username",
                "encrypt", "trust_server_certificate", "polling_enabled", "show_on_dashboard",
                "show_on_public")


def site_out(s: Site) -> SiteOut:
    return SiteOut(id=s.id, name=s.name, code=s.code, address=s.address, map_url=s.map_url,
                   host=s.host, port=s.port, database=s.database_name, username=s.username,
                   has_password=bool(s.password_encrypted), encrypt=s.encrypt,
                   trust_server_certificate=s.trust_server_certificate,
                   polling_enabled=s.polling_enabled, show_on_dashboard=s.show_on_dashboard,
                   show_on_public=s.show_on_public, archived=s.archived_at is not None)


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


@router.get("/sites", response_model=list[SiteOut])
def list_sites(db: DbSession = Depends(get_db)):
    return [site_out(s) for s in db.scalars(select(Site).order_by(Site.name))]


@router.post("/sites", response_model=SiteOut, status_code=201)
def create_site(body: SiteCreate, db: DbSession = Depends(get_db),
                user: User = Depends(require_admin)):
    if _code_taken(db, body.code):
        raise HTTPException(409, f"Short code {body.code} is already used by another site.")
    site = Site(name=body.name.strip(), code=body.code, address=body.address,
                map_url=body.map_url, host=body.host, port=body.port,
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
    for k, v in data.items():
        if v is None and k in ("name", "code", "host", "port", "database_name", "username",
                               "encrypt", "trust_server_certificate"):
            continue  # required fields cannot be cleared
        setattr(site, k, v)
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
            "host": c.host, "port": c.port, "database": c.database, "username": c.username,
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
