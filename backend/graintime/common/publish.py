"""Precompute the public page's aggregates (run by the collector every 30 s).

The public service reads only the public_site_status table this writes. Rows
hold aggregates only: no ticket numbers, no individual truck times. A current
time on site is published only when at least `public_min_trucks` trucks back
it; otherwise the row says light traffic or no recent trucks. Received grain
only (the farmer-facing number).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from . import hours, metrics, settings_store
from .models import PublicSiteStatus, Site, SiteCollectorState


def site_row(db: DbSession, site: Site, state: SiteCollectorState | None, g: dict,
             now: datetime) -> PublicSiteStatus:
    today = metrics.local_today(now)
    f = metrics.globals_filters(db, [site.id], today, today, "received")
    cur = metrics.current_time_on_site(db, site.id, f, now)
    onsite = len(metrics.on_site_now(db, site.id, f, now))
    min_trucks = max(1, int(g["public_min_trucks"]))
    if cur["minutes"] is None:
        traffic, minutes = "none", None
    elif cur["trucks"] < min_trucks:
        traffic, minutes = "light", None
    else:
        traffic, minutes = "ok", int(round(cur["minutes"]))
    st = hours.status(site.hours, now)
    return PublicSiteStatus(
        code=site.code, name=site.name, address=site.address, map_url=site.map_url,
        open_state=st["open"], hours_text=st["text"], traffic=traffic, minutes=minutes,
        level=metrics.level(minutes, f), trucks_on_site=onsite,
        data_as_of=state.last_success_at if state else None, computed_at=now,
        stale_after_min=int(g["public_stale_after_min"]))


def publish(db: DbSession, now: datetime | None = None) -> int:
    """Replace the public table in one transaction (readers see old or new, never half)."""
    now = now or datetime.now(timezone.utc)
    g = settings_store.get_globals(db)
    sites = db.scalars(select(Site).where(Site.show_on_public.is_(True), Site.archived_at.is_(None))
                       .order_by(Site.name)).all()
    states = {s.site_id: s for s in db.scalars(select(SiteCollectorState))}
    rows = [site_row(db, s, states.get(s.id), g, now) for s in sites]
    for i, r in enumerate(rows):
        r.position = i
    db.execute(delete(PublicSiteStatus))
    db.add_all(rows)
    db.commit()
    return len(rows)
