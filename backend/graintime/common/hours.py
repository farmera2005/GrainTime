"""Site operating hours: a weekly schedule plus date-range overrides (harvest hours).

Stored on the site as JSON:

    {"weekly": [day, ... 7 entries, Monday first],
     "overrides": [{"label": "Harvest", "date_from": "2026-09-15", "date_to": "2026-11-30",
                    "weekly": [day, ...]}]}

where a day is {"open": "07:00", "close": "18:00"} or null (closed). Times are
local (America/New_York). The last override whose dates cover a day wins.
A site with no hours stored has an unknown open state.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .normalize import SITE_TZ

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def schedule_for(hours: dict, d: date) -> dict | None:
    """The opening hours that apply on local date d (None = closed)."""
    weekly = hours.get("weekly") or [None] * 7
    for o in hours.get("overrides") or []:
        if o["date_from"] <= d.isoformat() <= o["date_to"]:
            weekly = o.get("weekly") or [None] * 7
    return weekly[d.weekday()] if len(weekly) == 7 else None


def fmt_time(t: time) -> str:
    h = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    return f"{h} {suffix}" if t.minute == 0 else f"{h}:{t.minute:02d} {suffix}"


def status(hours: dict | None, now: datetime) -> dict:
    """{"open": True/False/None, "text": "Open until 6 PM" | "Closed · opens 7 AM Thu" | None}."""
    if not hours:
        return {"open": None, "text": None}
    local = now.astimezone(SITE_TZ)
    today = local.date()
    day = schedule_for(hours, today)
    if day and _t(day["open"]) <= local.time() < _t(day["close"]):
        return {"open": True, "text": f"Open until {fmt_time(_t(day['close']))}"}
    for i in range(0, 15):
        d = today + timedelta(days=i)
        s = schedule_for(hours, d)
        if not s:
            continue
        opens = _t(s["open"])
        if i == 0 and local.time() >= opens:
            continue
        when = "" if i == 0 else " tomorrow" if i == 1 else f" {DAYS[d.weekday()]}" if i < 7 \
            else f" {d.strftime('%b')} {d.day}"
        return {"open": False, "text": f"Closed · opens {fmt_time(opens)}{when}"}
    return {"open": False, "text": "Closed"}
