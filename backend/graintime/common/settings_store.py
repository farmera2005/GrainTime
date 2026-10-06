"""Application settings kept in the central database (edited in the browser)."""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from .models import AppSetting

SETUP_KEY = "setup"
DEFAULTS_KEY = "defaults"

# Suggested starting values shown in the setup wizard; the admin confirms or
# changes them there. Minutes unless noted.
DEFAULT_GLOBALS = {
    "poll_interval_s": 60,
    "threshold_green_max_min": 20,
    "threshold_yellow_max_min": 40,
    "recent_window_min": 60,
    "open_ticket_cutoff_hours": 6,
    "duration_ceiling_hours": 4,
    "backfill_default_days": 730,
    "public_stale_after_min": 15,
    "public_min_trucks": 3,
}


def get_value(db: DbSession, key: str, default=None):
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_value(db: DbSession, key: str, value: dict) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def get_globals(db: DbSession) -> dict:
    return {**DEFAULT_GLOBALS, **(get_value(db, DEFAULTS_KEY) or {})}


def setup_state(db: DbSession) -> dict:
    return get_value(db, SETUP_KEY) or {}
