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

from ..common.normalize import Lookups
from ..common.profiles import ProfileConfig
from ..common.tickets_store import upsert_tickets
from .mapping import (MappingError, SiteReader, classify_mapping_error, read_poll_rows,  # noqa: F401
                      read_preview_rows)

BACKFILL_PAUSE_S = 1.0


def _iso(v):
    return v.isoformat() if isinstance(v, (datetime, date)) else v


def serialize(t: dict) -> dict:
    return {k: _iso(v) for k, v in t.items()}


# --------------------------------------------------------------------------- #
# Poll
# --------------------------------------------------------------------------- #

def poll(conn, profile: ProfileConfig, high_water, lookups: Lookups | None,
         recheck: bool) -> dict:
    """One incremental poll. Returns tickets to upsert and the new high-water mark."""
    reader = SiteReader(conn, profile)
    try:
        lookups = lookups or reader.lookups()
        got = read_poll_rows(reader, high_water, recheck)
        tickets, merged = reader.normalize_rows(got["rows"], lookups)
    except MappingError:
        raise
    except Exception as exc:
        from .sitedb import SiteConnectionError
        if isinstance(exc, SiteConnectionError):
            raise
        raise classify_mapping_error(exc) from None
    return {"tickets": tickets, "high_water": got["high_water"], "lookups": lookups,
            "rows_read": len(got["rows"]), "merged": merged, "rechecked": got["rechecked"]}


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
        got = read_preview_rows(reader, limit)
        tickets, merged = reader.normalize_rows(got["rows"], lookups)
        examples = {raw: reader.normalize_rows(rows, lookups)[0]
                    for raw, rows in got["status_examples"].items()}
    except Exception as exc:
        from .sitedb import SiteConnectionError
        if isinstance(exc, (SiteConnectionError, MappingError)):
            raise
        raise classify_mapping_error(exc) from None
    return preview_result(tickets, merged, examples, lookups)


def preview_result(tickets: list[dict], merged: int, examples: dict[str, list[dict]],
                   lookups: Lookups) -> dict:
    """The Preview data report (shared with agent sites, normalized on the server)."""
    included = [t for t in tickets if t["included"]]
    return {
        "tickets": [serialize(t) for t in tickets],
        "status_examples": {raw: [serialize(t) for t in ex] for raw, ex in examples.items()},
        "summary": {
            "read": len(tickets) + merged, "included": len(included), "merged_split_tickets": merged,
            "not_tracked": len(tickets) - len(included),
            "completed": sum(1 for t in included if t["status"] == "completed"),
            "open": sum(1 for t in included if t["status"] == "open"),
            "voided": sum(1 for t in included if t["status"] == "voided"),
            "unknown_status": sorted({t["raw_status"] for t in included if t["status"] == "unknown"}),
            "single_weigh": sum(1 for t in included if t["single_weigh"]),
            "manual_weights": sum(1 for t in included if t.get("note") == "weight keyed in by hand"),
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
