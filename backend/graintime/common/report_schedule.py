"""When scheduled reports run, what period they cover, and running them.

Times are Eastern (America/New_York). The collector calls run_due() about
every minute; due schedules are claimed with FOR UPDATE SKIP LOCKED, so two
processes never send the same report twice.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session as DbSession

from . import mailer, reports, settings_store
from .logging import get_logger
from .models import ReportRun, ReportSchedule
from .normalize import SITE_TZ

log = get_logger("reports.schedule")

PERIODS = {
    "yesterday": "Yesterday",
    "last_7_days": "Last 7 days",
    "last_week": "Last week (Mon–Sun)",
    "month_to_date": "Month to date",
    "last_month": "Last month",
    "season_to_date": "Harvest season to date (since Sep 1)",
}
FREQUENCIES = ("daily", "weekly", "monthly")


def period_range(period: str, run_day: date) -> tuple[date, date]:
    """The local dates a report sent on run_day covers (always complete days)."""
    end = run_day - timedelta(days=1)
    if period == "yesterday":
        return end, end
    if period == "last_7_days":
        return run_day - timedelta(days=7), end
    if period == "last_week":
        monday = run_day - timedelta(days=run_day.weekday()) - timedelta(days=7)
        return monday, monday + timedelta(days=6)
    if period == "month_to_date":
        return (end.replace(day=1), end)          # on the 1st: the whole previous month
    if period == "last_month":
        last = run_day.replace(day=1) - timedelta(days=1)
        return last.replace(day=1), last
    if period == "season_to_date":
        start = date(end.year if end >= date(end.year, 9, 1) else end.year - 1, 9, 1)
        return start, end
    raise ValueError(f"unknown period {period}")


def _local(d: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), SITE_TZ)


def next_run(s: ReportSchedule, after: datetime) -> datetime:
    """The first send time strictly after `after` (UTC)."""
    day = after.astimezone(SITE_TZ).date()
    for i in range(0, 400):
        d = day + timedelta(days=i)
        if s.frequency == "weekly" and d.weekday() != (s.weekday or 0):
            continue
        if s.frequency == "monthly" and d.day != (s.month_day or 1):
            continue
        at = _local(d, s.send_time).astimezone(timezone.utc)
        if at > after:
            return at
    raise ValueError("no next run")


def subject(s: ReportSchedule, label: str) -> str:
    return f"{s.name}: {label}"


def report_link(cfg: dict, s: ReportSchedule, d_from: date, d_to: date) -> str | None:
    base = (cfg.get("link_base_url") or "").rstrip("/")
    if not base:
        return None
    q = f"from={d_from.isoformat()}&to={d_to.isoformat()}&direction={s.direction}&compare={s.compare}"
    if s.site_ids:
        q += "&sites=" + ",".join(str(i) for i in s.site_ids)
    return f"{base}/reports?{q}"


def send_report(db: DbSession, s: ReportSchedule, d_from: date, d_to: date, send=mailer.send,
                now: datetime | None = None) -> None:
    """Build the report for the period and email it (raises on failure)."""
    cfg = mailer.config_with_defaults(settings_store.get_value(db, mailer.SETTINGS_KEY))
    p = reports.ReportParams(d_from=d_from, d_to=d_to, site_ids=list(s.site_ids or []), direction=s.direction,
                             compare=s.compare, title=s.name)
    r = reports.build_period_report(db, p, now=now)
    html = reports.render_html(r, email=True, link=report_link(cfg, s, d_from, d_to))
    attachments = []
    if s.include_trucks:
        rows = reports.truck_rows(db, p)
        attachments.append(mailer.Attachment(
            f"graintime-trucks-{d_from.isoformat()}-to-{d_to.isoformat()}.xlsx",
            reports.trucks_xlsx(rows, s.name, r),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    send(cfg, list(s.recipients or []), subject(s, r["period"]["label"]), html, reports.render_text(r), attachments)


def run_due(Session, send=mailer.send, now: datetime | None = None, limit: int = 5) -> int:
    """Send every due (or 'Send now') report. Returns how many were attempted."""
    now = now or datetime.now(timezone.utc)
    done = 0
    for _ in range(limit):
        with Session() as db:
            s = db.scalars(select(ReportSchedule).where(
                ReportSchedule.enabled.is_(True),
                or_(ReportSchedule.run_requested.is_(True), ReportSchedule.next_run_at <= now))
                .order_by(ReportSchedule.next_run_at).limit(1).with_for_update(skip_locked=True)).first()
            if s is None:
                return done
            manual = bool(s.run_requested) and not (s.next_run_at and s.next_run_at <= now)
            run_day = now.astimezone(SITE_TZ).date() if manual else s.next_run_at.astimezone(SITE_TZ).date()
            d_from, d_to = period_range(s.period, run_day)
            if manual:
                s.run_requested = False
            else:
                s.next_run_at = next_run(s, now)       # missed sends (server down) aren't repeated
                s.run_requested = False
            run = ReportRun(schedule_id=s.id, trigger="manual" if manual else "schedule", status="running",
                            period_from=d_from, period_to=d_to, recipients=list(s.recipients or []))
            db.add(run)
            db.commit()
            sid, rid = s.id, run.id
        status, error = "sent", None
        try:
            with Session() as db:
                send_report(db, db.get(ReportSchedule, sid), d_from, d_to, send=send, now=now)
        except mailer.MailError as exc:
            status, error = "failed", exc.message
        except Exception as exc:                       # noqa: BLE001 - shown in the run history
            status, error = "failed", f"{type(exc).__name__}: {str(exc)[:400]}"
        with Session() as db:
            run = db.get(ReportRun, rid)
            run.status, run.error, run.finished_at = status, error, datetime.now(timezone.utc)
            s = db.get(ReportSchedule, sid)
            s.last_run_at, s.last_status = run.finished_at, status
            db.commit()
        log.info("scheduled report", extra={"schedule_id": sid, "status": status})
        done += 1
    return done
