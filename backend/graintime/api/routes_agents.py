"""Agent sites: tickets sent by a GrainTime Agent on the scale PC.

Some scale software keeps its tickets in a database only programs on that PC
can open (AgVantagePC: a SQL Server Express user instance). The GrainTime
Agent runs there, reads read-only with the same rules as the collector, and
sends the profile's columns to this api. The server never sends it SQL: only
the mapping profile (structured, validated), the high-water mark, and jobs.

Admin side (admin role):
  POST /api/admin/sites/agent                  create an agent site
  GET  /api/admin/sites/{id}/agent             pairing and agent status
  POST /api/admin/sites/{id}/agent/pairing-code  one-time code (24 h), shown once
  POST /api/admin/sites/{id}/agent/revoke      disconnect the paired PC

Agent side (no session; every request signed, see `verify`):
  POST /api/agent/pair                         exchange a pairing code for a key
  POST /api/agent/sync                         status + poll rows in; instructions out
  POST /api/agent/jobs/{id}                    Preview data / backfill rows and results

Signing: HMAC-SHA256 with the agent's secret over
  "{METHOD}\\n{path}\\n{X-GT-Time}\\n{sha256(body) hex}"
X-GT-Time is milliseconds since the epoch, within 5 minutes of the server's
clock and strictly increasing per agent, so a captured request cannot be
replayed. The secret itself is never sent after pairing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from ..collector.collect import preview_result
from ..collector.poller import RECHECK_EVERY, RECHECK_LOCAL_HOURS, backoff_seconds, fingerprint
from ..common import audit, crypto, settings_store
from ..common.db import get_sessionmaker
from ..common.logging import get_logger
from ..common.models import CollectorJob, MappingProfile, Site, SiteAgent, SiteCollectorState, User
from ..common.normalize import SITE_TZ, Lookups, decode_rows, normalize
from ..common.profiles import ProfileConfig
from ..common.tickets_store import upsert_tickets
from .agent_status import agent_summary
from .routes_sites import _code_taken, _get_site, _snapshot, site_out
from .schemas import AgentSiteCreate, SiteOut
from .security import LoginThrottle, get_db, require_admin

log = get_logger("api.agents")

admin = APIRouter(prefix="/api/admin")
agent_api = APIRouter(prefix="/api/agent")

PAIRING_HOURS = 24
MAX_SKEW_MS = 5 * 60 * 1000
MAX_BODY = 8 * 1024 * 1024
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I
pair_throttle = LoginThrottle(limit=10, window_s=900)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _code_hash(code: str) -> str:
    norm = "".join(ch for ch in code.upper() if ch.isalnum())
    return hashlib.sha256(("graintime-pair:" + norm).encode()).hexdigest()


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


# --------------------------------------------------------------------------- #
# Admin side
# --------------------------------------------------------------------------- #

def _profile_for_agent(db: DbSession, profile_id: int | None) -> MappingProfile | None:
    if profile_id is None:
        return None
    prof = db.get(MappingProfile, profile_id)
    if prof is None:
        raise HTTPException(422, "That mapping profile does not exist.")
    why = ProfileConfig(**prof.config).agent_compatible()
    if why:
        raise HTTPException(422, f"The profile {prof.name} can't be used by an agent site: {why}.")
    return prof


@admin.post("/sites/agent", response_model=SiteOut, status_code=201)
def create_agent_site(body: AgentSiteCreate, db: DbSession = Depends(get_db),
                      user: User = Depends(require_admin)):
    if _code_taken(db, body.code):
        raise HTTPException(409, f"Short code {body.code} is already used by another site.")
    pid = body.mapping_profile_id
    if pid is None:   # default to the AgVantagePC profile when it exists
        pid = db.scalar(select(MappingProfile.id).where(MappingProfile.name == "AgVantagePC"))
    _profile_for_agent(db, pid)
    site = Site(name=body.name.strip(), code=body.code, address=body.address, map_url=body.map_url,
                source="agent", mapping_profile_id=pid, auth_method="sql", encrypt="yes",
                trust_server_certificate=False,
                polling_enabled=False, show_on_dashboard=True, show_on_public=False)
    db.add(site)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"Short code {body.code} is already used by another site.")
    db.add(SiteAgent(site_id=site.id))
    audit.record(db, actor_id=user.id, actor_name=user.username, action="site.created",
                 entity_type="site", entity_id=site.id, new={**_snapshot(site), "source": "agent"})
    db.commit()
    out = site_out(site)
    out.agent = agent_summary(db.get(SiteAgent, site.id))
    return out


def _agent_site(db: DbSession, site_id: int) -> tuple[Site, SiteAgent]:
    site = _get_site(db, site_id)
    if site.source != "agent":
        raise HTTPException(409, "This site is read over SQL Server, not by an agent.")
    a = db.get(SiteAgent, site.id)
    if a is None:
        a = SiteAgent(site_id=site.id)
        db.add(a)
        db.flush()
    return site, a


@admin.get("/sites/{site_id}/agent")
def get_agent(site_id: int, db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    _, a = _agent_site(db, site_id)
    db.commit()
    return agent_summary(a)


@admin.post("/sites/{site_id}/agent/pairing-code")
def pairing_code(site_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    """A new one-time code. If a PC is already paired it keeps working until the
    new code is used, which then replaces it (moving the agent to a new PC)."""
    site, a = _agent_site(db, site_id)
    if site.archived_at is not None:
        raise HTTPException(409, "Restore this site before pairing an agent.")
    code = new_pairing_code()
    a.pairing_code_hash = _code_hash(code)
    a.pairing_expires_at = _now() + timedelta(hours=PAIRING_HOURS)
    audit.record(db, actor_id=user.id, actor_name=user.username, action="agent.pairing_code_created",
                 entity_type="site", entity_id=site.id, new={"expires_at": a.pairing_expires_at.isoformat()})
    db.commit()
    return {"code": code, "expires_at": a.pairing_expires_at.isoformat(), "agent": agent_summary(a)}


@admin.post("/sites/{site_id}/agent/revoke")
def revoke_agent(site_id: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    site, a = _agent_site(db, site_id)
    was = a.hostname
    a.key_id = a.secret_encrypted = a.pairing_code_hash = a.pairing_expires_at = None
    a.paired_at = None
    audit.record(db, actor_id=user.id, actor_name=user.username, action="agent.revoked",
                 entity_type="site", entity_id=site.id, old={"hostname": was})
    db.commit()
    return agent_summary(a)


# --------------------------------------------------------------------------- #
# Agent side: pairing
# --------------------------------------------------------------------------- #

class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=20)
    hostname: str | None = Field(default=None, max_length=100)
    windows_user: str | None = Field(default=None, max_length=150)
    version: str | None = Field(default=None, max_length=40)


@agent_api.post("/pair")
def pair(body: PairIn, request: Request, db: DbSession = Depends(get_db)):
    ip = request.client.host if request.client else "?"
    key = f"pair:{ip}"
    if pair_throttle.blocked(key):
        raise HTTPException(429, "Too many pairing attempts. Try again in 15 minutes.")
    a = db.scalar(select(SiteAgent).where(SiteAgent.pairing_code_hash == _code_hash(body.code)))
    site = db.get(Site, a.site_id) if a else None
    if a is None or a.pairing_expires_at is None or a.pairing_expires_at < _now() \
            or site is None or site.archived_at is not None or site.source != "agent":
        pair_throttle.fail(key)
        raise HTTPException(401, "That pairing code is not valid or has expired. Create a new one "
                                 "on the site's page in GrainTime.")
    secret = secrets.token_urlsafe(32)
    a.key_id = secrets.token_hex(12)
    a.secret_encrypted = crypto.encrypt(secret)
    a.pairing_code_hash = a.pairing_expires_at = None        # works once
    a.paired_at = a.last_seen_at = _now()
    a.last_request_ms = 0
    a.hostname, a.windows_user, a.version = body.hostname, body.windows_user, body.version
    audit.record(db, actor_id=None, actor_name=f"agent@{body.hostname or ip}", action="agent.paired",
                 entity_type="site", entity_id=site.id,
                 new={"hostname": body.hostname, "windows_user": body.windows_user, "version": body.version})
    db.commit()
    pair_throttle.clear(key)
    log.info("agent paired", extra={"site_id": site.id, "hostname": body.hostname})
    return {"key_id": a.key_id, "secret": secret,
            "site": {"id": site.id, "name": site.name, "code": site.code}}


# --------------------------------------------------------------------------- #
# Agent side: request signing
# --------------------------------------------------------------------------- #

def signature(secret: str, method: str, path: str, ts_ms: str, body: bytes) -> str:
    msg = f"{method.upper()}\n{path}\n{ts_ms}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _verify_sync(db: DbSession, method: str, path: str, headers: dict, body: bytes) -> tuple[SiteAgent, Site]:
    key_id, ts, sig = headers.get("x-gt-agent"), headers.get("x-gt-time"), headers.get("x-gt-signature")
    if not key_id or not ts or not sig or not ts.isdigit():
        raise HTTPException(401, "Unsigned agent request.")
    a = db.scalar(select(SiteAgent).where(SiteAgent.key_id == key_id).with_for_update())
    if a is None or a.secret_encrypted is None:
        raise HTTPException(401, "This agent is not paired (it may have been disconnected). Pair it again.")
    expected = signature(crypto.decrypt(a.secret_encrypted), method, path, ts, body)
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "Bad agent signature.")
    ts_ms = int(ts)
    if abs(ts_ms - int(time.time() * 1000)) > MAX_SKEW_MS:
        raise HTTPException(401, "The PC's clock is more than 5 minutes off. Correct the PC's time.")
    if ts_ms <= (a.last_request_ms or 0):
        raise HTTPException(401, "Replayed agent request.")
    a.last_request_ms = ts_ms
    site = db.get(Site, a.site_id)
    if site is None or site.archived_at is not None or site.source != "agent":
        raise HTTPException(403, "This site is archived or no longer read by an agent.")
    return a, site


async def _signed(request: Request, handler, *args):
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Request too large.")
    headers = {k.lower(): v for k, v in request.headers.items()}
    method, path = request.method, request.url.path

    def work():
        Session = get_sessionmaker()
        with Session() as db:
            a, site = _verify_sync(db, method, path, headers, body)
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                raise HTTPException(422, "Body is not JSON.")
            return handler(db, a, site, payload, *args)
    return await run_in_threadpool(work)


# --------------------------------------------------------------------------- #
# Agent side: sync
# --------------------------------------------------------------------------- #

class AgentInfo(BaseModel):
    version: str | None = Field(default=None, max_length=40)
    hostname: str | None = Field(default=None, max_length=100)
    windows_user: str | None = Field(default=None, max_length=150)
    clock_utc: datetime | None = None


class AgentStatus(BaseModel):
    instance: str | None = Field(default=None, max_length=20)     # running | not_running
    database: str | None = Field(default=None, max_length=300)
    driver: str | None = Field(default=None, max_length=100)
    error: dict | None = None                                     # {code, cause, fix}
    active_job_id: int | None = None


class PollIn(BaseModel):
    fingerprint: str = Field(max_length=64)
    high_water: str | None = Field(default=None, max_length=64)
    rows: list[dict] = Field(default_factory=list, max_length=10000)
    rechecked: bool = False


class SyncIn(BaseModel):
    agent: AgentInfo = Field(default_factory=AgentInfo)
    status: AgentStatus = Field(default_factory=AgentStatus)
    poll: PollIn | None = None


def _error(e: dict | None) -> dict | None:
    if not e:
        return None
    return {k: str(e.get(k))[:500] for k in ("code", "cause", "fix") if e.get(k) is not None}


def _recheck_due(state: SiteCollectorState | None, now: datetime) -> bool:
    if now.astimezone(SITE_TZ).hour not in RECHECK_LOCAL_HOURS:
        return False
    return state is None or state.last_recheck_at is None or now - state.last_recheck_at > RECHECK_EVERY


def _sync(db: DbSession, a: SiteAgent, site: Site, payload: dict) -> dict:
    try:
        body = SyncIn(**payload)
    except ValidationError as exc:
        raise HTTPException(422, f"Bad sync request: {exc.errors()[:3]}")
    now = _now()
    a.last_seen_at = now
    a.version = body.agent.version or a.version
    a.hostname = body.agent.hostname or a.hostname
    a.windows_user = body.agent.windows_user or a.windows_user
    if body.agent.clock_utc:
        cu = body.agent.clock_utc if body.agent.clock_utc.tzinfo else body.agent.clock_utc.replace(tzinfo=timezone.utc)
        a.clock_skew_s = int((cu - now).total_seconds())
    err = _error(body.status.error)
    a.status = {"instance": body.status.instance, "database": body.status.database,
                "driver": body.status.driver, "error": err, "at": now.isoformat()}

    default = int(settings_store.get_globals(db)["poll_interval_s"])
    interval = site.poll_interval_s or default
    state = db.get(SiteCollectorState, site.id)
    if state is None:
        state = SiteCollectorState(site_id=site.id, rows_total=0, consecutive_failures=0)
        db.add(state)
    prof = db.get(MappingProfile, site.mapping_profile_id) if site.mapping_profile_id else None
    profile = ProfileConfig(**prof.config) if prof else None
    fp = fingerprint(prof.config) if prof else None
    usable = profile is not None and profile.agent_compatible() is None

    stored = None
    if site.polling_enabled and usable and body.poll is not None and body.poll.fingerprint == fp:
        rows = decode_rows(profile, body.poll.rows)
        tickets = [normalize(profile, r, None, Lookups()) for r in rows]
        stored = upsert_tickets(db, site.id, tickets)
        state.high_water_mark = body.poll.high_water
        state.profile_fingerprint = fp
        state.last_poll_at = state.last_success_at = now
        state.consecutive_failures = 0
        state.last_error = None
        state.rows_last_poll = stored
        state.rows_total = (state.rows_total or 0) + stored
        if body.poll.rechecked:
            state.last_recheck_at = now
    elif site.polling_enabled and err:
        state.consecutive_failures = (state.consecutive_failures or 0) + 1
        state.last_poll_at = now
        state.last_error = {**err, "at": now.isoformat()}

    wait = backoff_seconds(interval, state.consecutive_failures) if state.consecutive_failures else interval
    state.next_poll_at = now + timedelta(seconds=wait)

    # A job the server thinks is running but the agent no longer has (agent
    # restarted, PC rebooted) can't resume: fail it so it can be run again.
    running = db.scalars(select(CollectorJob).where(CollectorJob.site_id == site.id,
                                                    CollectorJob.status == "running")).all()
    for j in running:
        if j.id != body.status.active_job_id:
            j.status, j.finished_at, j.params = "failed", now, {}
            j.error = {"code": "interrupted", "cause": "The agent stopped while this job was running",
                       "fix": "Run it again."}
    job = None
    if body.status.active_job_id is None and body.status.instance == "running":
        job = _claim_job(db, site, now)

    hwm = state.high_water_mark if state.profile_fingerprint in (None, fp) else None
    db.commit()
    if stored is not None:
        log.info("agent poll stored", extra={"site_id": site.id, "rows": len(body.poll.rows), "stored": stored})
    return {
        "site": {"id": site.id, "name": site.name, "code": site.code},
        "server_time": now.isoformat(),
        "next_sync_s": max(15, min(wait, 1800)),
        "poll": ({"profile": prof.config, "fingerprint": fp, "high_water": hwm,
                  "recheck": _recheck_due(state, now)}
                 if site.polling_enabled and usable else None),
        "database_match": profile.agent_database_match if usable else None,
        "job": job,
        "message": None if usable else (
            "Choose a mapping profile for this site in GrainTime." if profile is None
            else f"The mapping profile can't be used by an agent: {profile.agent_compatible()}."),
    }


def _claim_job(db: DbSession, site: Site, now: datetime) -> dict | None:
    j = db.scalars(select(CollectorJob).where(
        CollectorJob.site_id == site.id, CollectorJob.status == "queued",
        CollectorJob.kind.in_(("preview", "backfill"))).order_by(CollectorJob.id)
        .limit(1).with_for_update(skip_locked=True)).first()
    if j is None:
        return None
    if j.cancel_requested:
        j.status, j.finished_at, j.params = "cancelled", now, {}
        return None
    opts = (j.params or {}).get("options", {})
    pid = opts.get("profile_id") or site.mapping_profile_id
    prof = db.get(MappingProfile, pid) if pid else None
    if prof is None or ProfileConfig(**prof.config).agent_compatible():
        j.status, j.finished_at, j.params = "failed", now, {}
        j.error = {"code": "profile_unusable", "cause": "The mapping profile can't be used by an agent site",
                   "fix": "Choose an agent-compatible profile, such as AgVantagePC."}
        return None
    j.status, j.started_at = "running", now
    return {"id": j.id, "kind": j.kind, "options": opts, "profile": prof.config,
            "fingerprint": fingerprint(prof.config)}


@agent_api.post("/sync")
async def sync(request: Request):
    return await _signed(request, _sync)


# --------------------------------------------------------------------------- #
# Agent side: job results (Preview data, backfill)
# --------------------------------------------------------------------------- #

class JobIn(BaseModel):
    rows: list[dict] = Field(default_factory=list, max_length=10000)
    status_examples: dict[str, list[dict]] = Field(default_factory=dict)
    progress: dict | None = None             # {step, done, total}
    done: bool = False
    error: dict | None = None


def _job(db: DbSession, a: SiteAgent, site: Site, payload: dict, job_id: int) -> dict:
    try:
        body = JobIn(**payload)
    except ValidationError as exc:
        raise HTTPException(422, f"Bad job update: {exc.errors()[:3]}")
    j = db.get(CollectorJob, job_id)
    if j is None or j.site_id != site.id:
        raise HTTPException(404, "Job not found")
    now = _now()
    a.last_seen_at = now
    if j.status != "running":
        db.commit()
        return {"cancel": True, "status": j.status}
    opts = (j.params or {}).get("options", {})
    prof = db.get(MappingProfile, opts.get("profile_id") or site.mapping_profile_id)
    profile = ProfileConfig(**prof.config)
    if body.error:
        j.status, j.finished_at, j.params, j.error = "failed", now, {}, _error(body.error)
        db.commit()
        return {"cancel": False, "status": j.status}

    def norm(rows):
        return [normalize(profile, r, None, Lookups()) for r in decode_rows(profile, rows)]

    if j.kind == "preview":
        if body.done:
            examples = {raw: norm(rows) for raw, rows in body.status_examples.items()}
            j.result = preview_result(norm(body.rows), 0, examples, Lookups())
            j.status, j.finished_at, j.params = "succeeded", now, {}
    elif j.kind == "backfill":
        stored = upsert_tickets(db, site.id, norm(body.rows)) if body.rows else 0
        prog = dict(j.progress or {})
        prog["tickets_stored"] = int(prog.get("tickets_stored", 0)) + stored
        prog["tickets_read"] = int(prog.get("tickets_read", 0)) + len(body.rows)
        if body.progress:
            prog.update({k: body.progress[k] for k in ("step", "done", "total") if k in body.progress})
        j.progress = prog
        if body.done:
            finished = "cancelled" if j.cancel_requested else "succeeded"
            try:   # the agent may walk the table by key in pages; report the requested range
                days = (date.fromisoformat(opts["date_to"]) - date.fromisoformat(opts["date_from"])).days + 1
            except (KeyError, ValueError):
                days = prog.get("total")
            j.result = {"days": days, "days_done": days if not j.cancel_requested else prog.get("done"),
                        "tickets_read": prog["tickets_read"], "tickets_stored": prog["tickets_stored"],
                        "merged_split_tickets": 0, **({"cancelled": True} if j.cancel_requested else {})}
            j.status, j.finished_at, j.params = finished, now, {}
    db.commit()
    return {"cancel": bool(j.cancel_requested), "status": j.status}


@agent_api.post("/jobs/{job_id}")
async def job_update(job_id: int, request: Request):
    return await _signed(request, _job, job_id)


__all__ = ["admin", "agent_api", "signature", "agent_summary", "new_pairing_code"]
