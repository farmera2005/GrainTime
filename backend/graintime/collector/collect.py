"""Ticket collection: incremental polls, Preview data, and backfill.

Polling (per site, every poll interval):
  1. new tickets after the high-water mark (bounded pages),
  2. a look-back re-read of tickets created in the last N hours and completed
     in the last M hours, to catch completions, edits and voids,
  3. once a night, a re-check of the last few days (late voids/edits),
then their weigh steps, normalization, and an idempotent upsert.

The first poll of a site does not read history: it starts the high-water mark
at the newest ticket and reads only the look-back window. History comes from
an admin-triggered backfill, in day-sized batches with pauses.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from ..common.normalize import Lookups, raw_key
from ..common.profiles import ProfileConfig
from ..common.tickets_store import upsert_tickets
from .mapping import SiteReader, local_now, window_bounds

NEW_PAGES_PER_POLL = 4          # up to 4 x 500 new tickets per poll, then continue next poll
BACKFILL_PAUSE_S = 1.0
RECHECK_PAUSE_S = 0.5


class MappingError(Exception):
    def __init__(self, code: str, cause: str, fix: str):
        super().__init__(cause)
        self.code, self.cause, self.fix = code, cause, fix

    def as_dict(self) -> dict:
        return {"code": self.code, "cause": self.cause, "fix": self.fix}


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def serialize(t: dict) -> dict:
    return {k: _iso(v) for k, v in t.items()}


def classify_mapping_error(exc: Exception) -> MappingError:
    """SQL errors from a wrong profile (bad table/column, missing permission)."""
    msg = str(exc)
    low = msg.lower()
    if "invalid object name" in low or "(208)" in low:
        return MappingError("mapping_table_missing", "A table in the mapping profile does not exist",
                            f"Check the table names in the mapping profile. Driver: {msg[:300]}")
    if "invalid column name" in low or "(207)" in low:
        return MappingError("mapping_column_missing", "A column in the mapping profile does not exist",
                            f"Check the column names in the mapping profile. Driver: {msg[:300]}")
    if "select permission was denied" in low or "(229)" in low or "(230)" in low:
        return MappingError("permission_denied",
                            "The SQL login is missing SELECT permission on a mapped table or column",
                            "Run the read-only login script from the mapping profile page at the "
                            f"site. Driver: {msg[:300]}")
    if "timeout" in low or "timed out" in low:
        return MappingError("query_timeout", "A query took longer than 15 seconds",
                            "The site database may be busy; GrainTime will retry with back-off. "
                            f"Driver: {msg[:300]}")
    return MappingError("query_failed", "Reading tickets failed", msg[:500])


# --------------------------------------------------------------------------- #
# Poll
# --------------------------------------------------------------------------- #

def poll(conn, profile: ProfileConfig, high_water, lookups: Lookups | None,
         recheck: bool) -> dict:
    """One incremental poll. Returns tickets to upsert and the new high-water mark."""
    reader = SiteReader(conn, profile)
    try:
        lookups = lookups or reader.lookups()
        rows: list[dict] = []
        new_hwm = high_water
        if high_water is None:
            top = reader.max_high_water()
            new_hwm = raw_key(top) if top is not None else None
        else:
            after = high_water
            for _ in range(NEW_PAGES_PER_POLL):
                page = reader.new_rows(after)
                rows += page
                if page:
                    after = raw_key(page[-1][profile.high_water_column])
                    new_hwm = after
                if len(page) < 500:
                    break
        created_since, completed_since = window_bounds(profile)
        rows += reader.window_rows(created_since, completed_since)
        rechecked = 0
        if recheck and profile.nightly_recheck_days > 0:
            today = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
            for d in range(profile.nightly_recheck_days, 0, -1):
                day = today - timedelta(days=d)
                got = reader.created_range(day, day + timedelta(days=1))
                rechecked += len(got)
                rows += got
                time.sleep(RECHECK_PAUSE_S)
        # De-duplicate (the same ticket can come from several queries)
        seen, unique = set(), []
        for r in rows:
            k = raw_key(r[profile.id_column])
            if k not in seen:
                seen.add(k)
                unique.append(r)
        tickets, merged = reader.normalize_rows(unique, lookups)
    except MappingError:
        raise
    except Exception as exc:
        from .sitedb import SiteConnectionError
        if isinstance(exc, SiteConnectionError):
            raise
        raise classify_mapping_error(exc) from None
    return {"tickets": tickets, "high_water": new_hwm, "lookups": lookups,
            "rows_read": len(unique), "merged": merged, "rechecked": rechecked}


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #

def preview(conn, profile: ProfileConfig, options: dict) -> dict:
    """Recent tickets, normalized, so a wrong mapping is caught before going live.
    Also lists example tickets for status values not mapped to open/completed,
    so their meaning can be checked in the scale software."""
    limit = max(1, min(int(options.get("limit") or 25), 100))
    reader = SiteReader(conn, profile)
    try:
        lookups = reader.lookups()
        rows = reader.recent_rows(limit)
        tickets, merged = reader.normalize_rows(rows, lookups)
        examples = {}
        check = [k for k, v in profile.status_map.items() if v not in ("open", "completed")]
        for raw in check:
            ex_rows = reader.status_examples(raw, 5)
            ex, _ = reader.normalize_rows(ex_rows, lookups)
            examples[raw] = [serialize(t) for t in ex]
    except Exception as exc:
        from .sitedb import SiteConnectionError
        if isinstance(exc, (SiteConnectionError, MappingError)):
            raise
        raise classify_mapping_error(exc) from None
    included = [t for t in tickets if t["included"]]
    return {
        "tickets": [serialize(t) for t in tickets],
        "status_examples": examples,
        "summary": {
            "read": len(tickets) + merged, "included": len(included), "merged_split_tickets": merged,
            "not_tracked": len(tickets) - len(included),
            "completed": sum(1 for t in included if t["status"] == "completed"),
            "open": sum(1 for t in included if t["status"] == "open"),
            "voided": sum(1 for t in included if t["status"] == "voided"),
            "unknown_status": sorted({t["raw_status"] for t in included if t["status"] == "unknown"}),
            "single_weigh": sum(1 for t in included if t["single_weigh"]),
            "types": {k: v.get("code") for k, v in list(lookups.types.items())[:50]},
            "products": len(lookups.products),
        },
    }


# --------------------------------------------------------------------------- #
# Backfill
# --------------------------------------------------------------------------- #

def backfill(conn, profile: ProfileConfig, site_id: int, options: dict, progress, should_cancel,
             Session) -> dict:
    """Admin-triggered history load: one day at a time (each query bounded),
    with a pause between days, progress, and cancel."""
    start = date.fromisoformat(options["date_from"])
    end = date.fromisoformat(options["date_to"])
    days = (end - start).days + 1
    reader = SiteReader(conn, profile)
    totals = {"days": days, "days_done": 0, "tickets_read": 0, "tickets_stored": 0,
              "merged_split_tickets": 0}
    try:
        lookups = reader.lookups()
        for i in range(days):
            if should_cancel():
                return {**totals, "cancelled": True}
            day = datetime.combine(start + timedelta(days=i), datetime.min.time())
            rows = reader.created_range(day, day + timedelta(days=1), should_cancel)
            tickets, merged = reader.normalize_rows(rows, lookups) if rows else ([], 0)
            with Session() as db:
                stored = upsert_tickets(db, site_id, tickets)
                db.commit()
            totals["days_done"] = i + 1
            totals["tickets_read"] += len(rows)
            totals["tickets_stored"] += stored
            totals["merged_split_tickets"] += merged
            progress({"step": f"{(start + timedelta(days=i)).isoformat()}", "done": i + 1,
                      "total": days, "tickets_stored": totals["tickets_stored"]})
            time.sleep(BACKFILL_PAUSE_S)
    except Exception as exc:
        from .sitedb import SiteConnectionError
        if isinstance(exc, (SiteConnectionError, MappingError)):
            raise
        raise classify_mapping_error(exc) from None
    return totals
