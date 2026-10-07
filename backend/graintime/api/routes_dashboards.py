"""Customizable dashboards, saved per user."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from ..common.models import Dashboard, User
from .security import current_user, get_db

router = APIRouter(prefix="/api/dashboards")

WidgetType = Literal["kpi", "sites_table", "by_hour", "trend", "heatmap", "compare", "distribution",
                     "exclusions"]
KpiMetric = Literal["current", "on_site_now", "trucks_today", "median", "p90", "completed"]


class WidgetSettings(BaseModel):
    site_id: int | None = None          # one site; None = every site in the dashboard filter
    metric: KpiMetric | None = None     # KPI tiles
    show_p90: bool = True               # trend


class Widget(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10], max_length=40)
    type: WidgetType
    title: str | None = Field(default=None, max_length=80)
    size: Literal["S", "M", "L"] = "M"
    settings: WidgetSettings = Field(default_factory=WidgetSettings)


class DashFilters(BaseModel):
    range: Literal["today", "yesterday", "7d", "30d", "90d", "season", "ytd", "custom"] = "7d"
    date_from: str | None = None        # custom range (YYYY-MM-DD)
    date_to: str | None = None
    site_ids: list[int] = Field(default_factory=list, max_length=200)   # empty = all sites
    direction: Literal["received", "shipped"] = "received"
    commodity: str | None = Field(default=None, max_length=100)


class DashIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    filters: DashFilters = Field(default_factory=DashFilters)
    widgets: list[Widget] = Field(default_factory=list, max_length=40)

    @field_validator("name")
    @classmethod
    def _n(cls, v):
        return v.strip()


def default_dashboard() -> DashIn:
    w = [
        Widget(type="kpi", size="S", settings=WidgetSettings(metric="current")),
        Widget(type="kpi", size="S", settings=WidgetSettings(metric="on_site_now")),
        Widget(type="kpi", size="S", settings=WidgetSettings(metric="trucks_today")),
        Widget(type="kpi", size="S", settings=WidgetSettings(metric="median")),
        Widget(type="sites_table", size="L"),
        Widget(type="trend", size="L"),
        Widget(type="by_hour", size="M"),
        Widget(type="heatmap", size="M"),
        Widget(type="compare", size="M"),
        Widget(type="distribution", size="M"),
        Widget(type="exclusions", size="M"),
    ]
    return DashIn(name="Overview", filters=DashFilters(range="7d"), widgets=w)


def out(d: Dashboard) -> dict:
    return {"id": d.id, "name": d.name, "position": d.position, "filters": d.filters,
            "widgets": d.widgets, "updated_at": d.updated_at}


def _mine(db: DbSession, user: User, did: int) -> Dashboard:
    d = db.get(Dashboard, did)
    if d is None or d.user_id != user.id:
        raise HTTPException(404, "Dashboard not found")
    return d


@router.get("")
def list_dashboards(db: DbSession = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Dashboard).where(Dashboard.user_id == user.id)
                      .order_by(Dashboard.position, Dashboard.id)).all()
    if not rows:
        d = default_dashboard()
        row = Dashboard(user_id=user.id, name=d.name, position=0,
                        filters=d.filters.model_dump(), widgets=[w.model_dump() for w in d.widgets])
        db.add(row)
        db.commit()
        rows = [row]
    return [out(d) for d in rows]


@router.post("", status_code=201)
def create_dashboard(body: DashIn | None = None, db: DbSession = Depends(get_db),
                     user: User = Depends(current_user)):
    if body is None:
        body = default_dashboard()
    elif not body.widgets:
        body = DashIn(name=body.name, filters=body.filters, widgets=default_dashboard().widgets)
    n = len(db.scalars(select(Dashboard.id).where(Dashboard.user_id == user.id)).all())
    if n >= 20:
        raise HTTPException(409, "You can have up to 20 dashboards.")
    row = Dashboard(user_id=user.id, name=body.name, position=n, filters=body.filters.model_dump(),
                    widgets=[w.model_dump() for w in body.widgets])
    db.add(row)
    db.commit()
    return out(row)


@router.put("/{did}")
def save_dashboard(did: int, body: DashIn, db: DbSession = Depends(get_db),
                   user: User = Depends(current_user)):
    d = _mine(db, user, did)
    d.name, d.filters, d.widgets = body.name, body.filters.model_dump(), [w.model_dump() for w in body.widgets]
    db.commit()
    return out(d)


@router.post("/{did}/reset")
def reset_dashboard(did: int, db: DbSession = Depends(get_db), user: User = Depends(current_user)):
    d = _mine(db, user, did)
    dflt = default_dashboard()
    d.filters, d.widgets = dflt.filters.model_dump(), [w.model_dump() for w in dflt.widgets]
    db.commit()
    return out(d)


@router.delete("/{did}")
def delete_dashboard(did: int, db: DbSession = Depends(get_db), user: User = Depends(current_user)):
    d = _mine(db, user, did)
    db.delete(d)
    db.commit()
    return {"deleted": True}
