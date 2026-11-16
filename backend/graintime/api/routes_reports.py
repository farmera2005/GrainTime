"""Reports (any signed-in user), email settings and scheduled reports (admins)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..common import audit, crypto, mailer, metrics, reports, settings_store
from ..common.models import ReportRun, ReportSchedule, User
from ..common.report_schedule import FREQUENCIES, PERIODS, next_run, period_range
from .security import current_user, get_db, require_admin

router = APIRouter(prefix="/api")

MAX_DAYS = 3660


def _params(sites: str | None, d_from: date | None, d_to: date | None, direction: str, commodity: str | None,
            compare: str, title: str | None) -> reports.ReportParams:
    today = metrics.local_today()
    d_to = d_to or today
    d_from = d_from or d_to
    if d_from > d_to:
        raise HTTPException(422, "The start date is after the end date.")
    if (d_to - d_from).days > MAX_DAYS:
        raise HTTPException(422, "Choose at most ten years.")
    ids = [int(x) for x in (sites or "").split(",") if x.strip().isdigit()]
    return reports.ReportParams(d_from=d_from, d_to=d_to, site_ids=ids, direction=direction,
                                commodity=commodity or None, compare=compare, title=(title or None) and title[:120])


Q = dict(sites=Query(None), d_from=Query(None, alias="from"), d_to=Query(None, alias="to"),
         direction=Query("received", pattern="^(received|shipped)$"), commodity=Query(None, max_length=100),
         compare=Query("previous", pattern="^(previous|last_year|none)$"), title=Query(None, max_length=120))


@router.get("/reports/period")
def period_report(sites: str | None = Q["sites"], d_from: date | None = Q["d_from"], d_to: date | None = Q["d_to"],
                  direction: str = Q["direction"], commodity: str | None = Q["commodity"],
                  compare: str = Q["compare"], title: str | None = Q["title"],
                  format: str = Query("html", pattern="^(html|json)$"),
                  db: DbSession = Depends(get_db), _: User = Depends(current_user)):
    r = reports.build_period_report(db, _params(sites, d_from, d_to, direction, commodity, compare, title))
    if format == "json":
        return r
    return HTMLResponse(reports.render_html(r), headers={"Cache-Control": "no-store"})


@router.get("/reports/trucks")
def trucks_export(sites: str | None = Q["sites"], d_from: date | None = Q["d_from"], d_to: date | None = Q["d_to"],
                  direction: str = Q["direction"], commodity: str | None = Q["commodity"],
                  format: str = Query("xlsx", pattern="^(xlsx|csv)$"),
                  db: DbSession = Depends(get_db), _: User = Depends(current_user)):
    p = _params(sites, d_from, d_to, direction, commodity, "none", None)
    rows = reports.truck_rows(db, p)
    name = f"graintime-trucks-{p.d_from.isoformat()}-to-{p.d_to.isoformat()}"
    if format == "csv":
        return Response(reports.trucks_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    r = reports.build_period_report(db, p)
    return Response(reports.trucks_xlsx(rows, name, r),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})


# --------------------------------------------------------------------------- #
# Email settings (admin)
# --------------------------------------------------------------------------- #

class EmailSettings(BaseModel):
    method: Literal["graph", "smtp"] = "graph"
    from_address: str = Field(default="", max_length=254)
    from_name: str = Field(default="GrainTime", max_length=100)
    link_base_url: str = Field(default="", max_length=300)
    tenant_id: str = Field(default="", max_length=100)
    client_id: str = Field(default="", max_length=100)
    client_secret: str | None = Field(default=None, max_length=500)       # write-only; blank = keep
    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = Field(default="", max_length=254)
    smtp_password: str | None = Field(default=None, max_length=500)       # write-only; blank = keep
    ca_cert_pem: str | None = Field(default=None, max_length=20000)

    @field_validator("from_address")
    @classmethod
    def _from(cls, v):
        v = v.strip()
        if v and not mailer.valid_address(v):
            raise ValueError("Enter the sending mailbox's address, e.g. graintime@mercerlandmark.com.")
        return v

    @field_validator("link_base_url")
    @classmethod
    def _link(cls, v):
        v = v.strip().rstrip("/")
        if v and not re.match(r"^https?://[^\s/]+(/[^\s]*)?$", v):
            raise ValueError("Enter GrainTime's address as people open it, e.g. http://graintime:8080.")
        return v

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _guid(cls, v):
        v = v.strip()
        if v and not re.match(r"^[0-9a-fA-F-]{36}$|^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", v):
            raise ValueError("Use the ID from the app registration (a GUID like 1234abcd-...).")
        return v

    @field_validator("smtp_host")
    @classmethod
    def _host(cls, v):
        v = v.strip()
        if v and not re.match(r"^[A-Za-z0-9.-]+$", v):
            raise ValueError("Enter a host name, e.g. smtp.mercerlandmark.com.")
        return v

    @field_validator("ca_cert_pem")
    @classmethod
    def _pem(cls, v):
        v = (v or "").strip()
        if v and "-----BEGIN CERTIFICATE-----" not in v:
            raise ValueError("Paste the CA certificate in PEM form (-----BEGIN CERTIFICATE----- ...).")
        return v or None


def _stored(db: DbSession) -> dict:
    return mailer.config_with_defaults(settings_store.get_value(db, mailer.SETTINGS_KEY))


def _out(cfg: dict) -> dict:
    o = {k: v for k, v in cfg.items() if not k.endswith("_encrypted")}
    o["has_client_secret"] = bool(cfg.get("client_secret_encrypted"))
    o["has_smtp_password"] = bool(cfg.get("smtp_password_encrypted"))
    o["configured"] = mailer.configured(cfg)
    return o


def _merge(body: EmailSettings, current: dict) -> dict:
    new = body.model_dump(exclude={"client_secret", "smtp_password"})
    for field, key in (("client_secret", "client_secret_encrypted"), ("smtp_password", "smtp_password_encrypted")):
        val = getattr(body, field)
        new[key] = crypto.encrypt(val).decode() if val else current.get(key)
    return new


@router.get("/settings/email")
def get_email(db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    return _out(_stored(db))


@router.put("/settings/email")
def put_email(body: EmailSettings, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    current = _stored(db)
    new = _merge(body, current)
    settings_store.set_value(db, mailer.SETTINGS_KEY, new)
    old_c = {k: v for k, v in current.items() if new.get(k) != v}
    new_c = {k: new[k] for k in old_c}
    for key, label in (("client_secret_encrypted", "client_secret"), ("smtp_password_encrypted", "smtp_password")):
        if key in old_c:
            old_c.pop(key), new_c.pop(key)
            old_c[label], new_c[label] = True, True
    if new_c:
        audit.record(db, actor_id=user.id, actor_name=user.username, action="settings.email_updated",
                     entity_type="settings", entity_id="email", old=old_c, new=new_c)
    db.commit()
    return _out(new)


class EmailTestIn(BaseModel):
    settings: EmailSettings          # the form as it is now (may be unsaved)
    to: str = Field(max_length=254)


@router.post("/settings/email/test")
def test_email(body: EmailTestIn, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    """Send a test message with the settings in the form, without saving them."""
    cfg = _merge(body.settings, _stored(db))
    when = datetime.now(timezone.utc).astimezone(metrics.SITE_TZ).strftime("%b %d, %Y %I:%M %p")
    try:
        mailer.send(cfg, [body.to], "GrainTime test email",
                    f"<p>This is a test message from GrainTime, sent by {user.display_name} on {when}.</p>"
                    "<p>Scheduled reports will arrive from this address.</p>",
                    f"This is a test message from GrainTime, sent by {user.display_name} on {when}.")
    except mailer.MailError as exc:
        return {"ok": False, "code": exc.code, "message": exc.message}
    return {"ok": True, "message": f"Sent to {body.to}. Check the inbox (and junk folder)."}


# --------------------------------------------------------------------------- #
# Scheduled reports (admin)
# --------------------------------------------------------------------------- #

class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    frequency: Literal["daily", "weekly", "monthly"] = "daily"
    weekday: int | None = Field(default=None, ge=0, le=6)
    month_day: int | None = Field(default=None, ge=1, le=28)
    send_time: str = Field(default="06:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    period: Literal[tuple(PERIODS)] = "yesterday"                      # type: ignore[valid-type]
    compare: Literal["previous", "last_year", "none"] = "previous"
    site_ids: list[int] = Field(default_factory=list, max_length=200)
    direction: Literal["received", "shipped"] = "received"
    include_trucks: bool = False
    recipients: list[str] = Field(min_length=1, max_length=50)

    @field_validator("recipients")
    @classmethod
    def _rcpt(cls, v):
        out = []
        for a in v:
            for part in re.split(r"[;,\s]+", a or ""):
                if part:
                    if not mailer.valid_address(part):
                        raise ValueError(f"'{part}' is not an email address.")
                    if part.lower() not in (x.lower() for x in out):
                        out.append(part)
        if not out:
            raise ValueError("Add at least one recipient.")
        return out[:50]

    @model_validator(mode="after")
    def _when(self):
        if self.frequency == "weekly" and self.weekday is None:
            self.weekday = 0
        if self.frequency == "monthly" and self.month_day is None:
            self.month_day = 1
        return self


def _schedule_out(s: ReportSchedule, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    nxt = s.next_run_at
    preview = None
    if nxt:
        a, b = period_range(s.period, nxt.astimezone(metrics.SITE_TZ).date())
        preview = reports.fmt_range(a, b)
    return {"id": s.id, "name": s.name, "enabled": s.enabled, "frequency": s.frequency, "weekday": s.weekday,
            "month_day": s.month_day, "send_time": s.send_time, "period": s.period, "period_label": PERIODS[s.period],
            "compare": s.compare, "site_ids": s.site_ids, "direction": s.direction, "include_trucks": s.include_trucks,
            "recipients": s.recipients, "next_run_at": nxt, "next_period_label": preview,
            "run_requested": s.run_requested, "last_run_at": s.last_run_at, "last_status": s.last_status}


def _get_schedule(db: DbSession, sid: int) -> ReportSchedule:
    s = db.get(ReportSchedule, sid)
    if s is None:
        raise HTTPException(404, "Scheduled report not found")
    return s


@router.get("/admin/report-schedules")
def list_schedules(db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    rows = db.scalars(select(ReportSchedule).order_by(ReportSchedule.name)).all()
    cfg = _stored(db)
    return {"email_configured": mailer.configured(cfg), "periods": PERIODS, "frequencies": FREQUENCIES,
            "schedules": [_schedule_out(s) for s in rows]}


@router.post("/admin/report-schedules", status_code=201)
def create_schedule(body: ScheduleIn, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    s = ReportSchedule(**body.model_dump(), created_by=user.id)
    s.next_run_at = next_run(s, datetime.now(timezone.utc))
    db.add(s)
    db.flush()
    audit.record(db, actor_id=user.id, actor_name=user.username, action="report_schedule.created",
                 entity_type="report_schedule", entity_id=s.id, new=body.model_dump())
    db.commit()
    return _schedule_out(s)


@router.put("/admin/report-schedules/{sid}")
def update_schedule(sid: int, body: ScheduleIn, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    s = _get_schedule(db, sid)
    old = {k: getattr(s, k) for k in body.model_dump()}
    for k, v in body.model_dump().items():
        setattr(s, k, v)
    s.next_run_at = next_run(s, datetime.now(timezone.utc))
    changed_old = {k: v for k, v in old.items() if getattr(s, k) != v}
    if changed_old:
        audit.record(db, actor_id=user.id, actor_name=user.username, action="report_schedule.updated",
                     entity_type="report_schedule", entity_id=s.id, old=changed_old,
                     new={k: getattr(s, k) for k in changed_old})
    db.commit()
    return _schedule_out(s)


@router.delete("/admin/report-schedules/{sid}")
def delete_schedule(sid: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    s = _get_schedule(db, sid)
    audit.record(db, actor_id=user.id, actor_name=user.username, action="report_schedule.deleted",
                 entity_type="report_schedule", entity_id=s.id, old={"name": s.name, "recipients": s.recipients})
    db.delete(s)
    db.commit()
    return {"deleted": True}


@router.post("/admin/report-schedules/{sid}/send-now")
def send_now(sid: int, db: DbSession = Depends(get_db), user: User = Depends(require_admin)):
    s = _get_schedule(db, sid)
    if not mailer.configured(_stored(db)):
        raise HTTPException(409, "Set up email first (Configuration → Email).")
    if not s.enabled:
        raise HTTPException(409, "Turn this scheduled report on first.")
    s.run_requested = True
    audit.record(db, actor_id=user.id, actor_name=user.username, action="report_schedule.send_now",
                 entity_type="report_schedule", entity_id=s.id)
    db.commit()
    return _schedule_out(s)


@router.get("/admin/report-schedules/{sid}/runs")
def schedule_runs(sid: int, db: DbSession = Depends(get_db), _: User = Depends(require_admin)):
    _get_schedule(db, sid)
    runs = db.scalars(select(ReportRun).where(ReportRun.schedule_id == sid)
                      .order_by(ReportRun.started_at.desc()).limit(30)).all()
    return [{"id": r.id, "trigger": r.trigger, "status": r.status, "period_from": r.period_from,
             "period_to": r.period_to, "recipients": r.recipients, "error": r.error,
             "started_at": r.started_at, "finished_at": r.finished_at} for r in runs]

