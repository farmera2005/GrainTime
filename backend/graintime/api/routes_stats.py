"""Statistics for dashboards (any signed-in user). Reads only the central store."""

from __future__ import annotations

import csv
import io
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..common import metrics
from ..common.models import Site, SiteCollectorState, Ticket
from ..common import settings_store
from .routes_sites import is_stale
from .security import current_user, get_db

router = APIRouter(prefix="/api/stats", dependencies=[Depends(current_user)])


def visible_sites(db: DbSession) -> list[Site]:
    return db.scalars(select(Site).where(Site.show_on_dashboard.is_(True), Site.archived_at.is_(None))
                      .order_by(Site.name)).all()


def _filters(db: DbSession, sites: str | None, d_from: date | None, d_to: date | None,
             direction: str, commodity: str | None) -> tuple[metrics.Filters, list[dict]]:
    vis = visible_sites(db)
    wanted = {int(x) for x in sites.split(",") if x.strip().isdigit()} if sites else None
    chosen = [s for s in vis if wanted is None or s.id in wanted]
    today = metrics.local_today()
    d_to = d_to or today
    d_from = d_from or d_to
    if d_from > d_to:
        raise HTTPException(422, "The start date is after the end date.")
    if (d_to - d_from).days > 3660:
        raise HTTPException(422, "Choose at most ten years.")
    f = metrics.globals_filters(db, [s.id for s in chosen], d_from, d_to, direction, commodity or None)
    return f, [{"id": s.id, "name": s.name, "code": s.code} for s in chosen]


def _respond(rows: list[dict] | dict, fmt: str, name: str):
    if fmt != "csv":
        return rows
    data = rows if isinstance(rows, list) else [rows]
    flat = [{k: (v if not isinstance(v, dict) else None) for k, v in r.items()} for r in data]
    buf = io.StringIO()
    if flat:
        w = csv.DictWriter(buf, fieldnames=list(flat[0].keys()), extrasaction="ignore")
        w.writeheader()
        w.writerows(flat)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="graintime-{name}.csv"'})


Common = dict(sites=Query(None), d_from=Query(None, alias="from"), d_to=Query(None, alias="to"),
              direction=Query("received", pattern="^(received|shipped)$"), commodity=Query(None),
              format=Query("json", pattern="^(json|csv)$"))


@router.get("/options")
def options(db: DbSession = Depends(get_db)):
    """What the filter row offers: sites, commodities, data range, thresholds."""
    vis = visible_sites(db)
    ids = [s.id for s in vis] or [-1]
    commodities = sorted(c for c in db.scalars(select(Ticket.commodity).where(
        Ticket.site_id.in_(ids), Ticket.commodity.is_not(None)).distinct()) if c)
    first = db.scalar(select(func.min(Ticket.inbound_at)).where(Ticket.site_id.in_(ids)))
    g = settings_store.get_globals(db)
    return {"sites": [{"id": s.id, "name": s.name, "code": s.code} for s in vis],
            "commodities": commodities,
            "first_date": first.astimezone(metrics.SITE_TZ).date().isoformat() if first else None,
            "today": metrics.local_today().isoformat(),
            "thresholds": {"green_max_min": g["threshold_green_max_min"],
                           "yellow_max_min": g["threshold_yellow_max_min"]},
            "ceiling_hours": g["duration_ceiling_hours"]}


@router.get("/overview")
def overview(sites: str | None = Common["sites"], direction: str = Common["direction"],
             commodity: str | None = Common["commodity"], format: str = Common["format"],
             db: DbSession = Depends(get_db)):
    f, chosen = _filters(db, sites, None, None, direction, commodity)
    states = {st.site_id: st for st in db.scalars(select(SiteCollectorState))}
    default = int(settings_store.get_globals(db)["poll_interval_s"])
    by_id = {s.id: s for s in visible_sites(db)}
    for c in chosen:
        site, st = by_id[c["id"]], states.get(c["id"])
        c["last_update"] = st.last_success_at.isoformat() if st and st.last_success_at else None
        c["stale"] = is_stale(site, st, site.poll_interval_s or default)
        c["polling"] = site.polling_enabled
    rows = metrics.overview(db, f, chosen)
    if format == "csv":
        rows = [{"site": r["name"], "current_min": r["current"]["minutes"],
                 "current_basis": r["current"]["basis"], "on_site_now": r["on_site_now"],
                 "trucks_today": r["trucks_today"], "median_today_min": r["median_today_min"],
                 "p90_today_min": r["p90_today_min"], "typical_this_hour_min": r["typical_this_hour_min"],
                 "last_update": r["last_update"], "stale": r["stale"]} for r in rows]
    return _respond(rows, format, "overview")


@router.get("/summary")
def summary(sites: str | None = Common["sites"], d_from: date | None = Common["d_from"],
            d_to: date | None = Common["d_to"], direction: str = Common["direction"],
            commodity: str | None = Common["commodity"], format: str = Common["format"],
            db: DbSession = Depends(get_db)):
    f, _ = _filters(db, sites, d_from, d_to, direction, commodity)
    s = metrics.summary(db, f)
    if format == "csv":
        s = {**{k: v for k, v in s.items() if k != "excluded"},
             **{f"excluded_{k}": v for k, v in s["excluded"].items()}}
    return _respond(s, format, "summary")


ChartKind = Literal["by-hour", "trend", "heatmap", "compare", "distribution"]


@router.get("/{kind}")
def chart(kind: ChartKind, sites: str | None = Common["sites"], d_from: date | None = Common["d_from"],
          d_to: date | None = Common["d_to"], direction: str = Common["direction"],
          commodity: str | None = Common["commodity"], format: str = Common["format"],
          db: DbSession = Depends(get_db)):
    f, chosen = _filters(db, sites, d_from, d_to, direction, commodity)
    if kind == "by-hour":
        rows = metrics.by_hour(db, f)
    elif kind == "trend":
        rows = metrics.trend(db, f)
    elif kind == "heatmap":
        rows = metrics.heatmap(db, f)
    elif kind == "compare":
        rows = metrics.compare(db, f, chosen)
    else:
        rows = metrics.distribution(db, f)
    return _respond(rows, format, kind)

