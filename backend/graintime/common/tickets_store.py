"""Idempotent ticket upserts into the central store, keyed on (site, source ticket)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session as DbSession

from .models import Ticket

STORED = ("ticket_number", "status", "raw_status", "direction", "transaction_type", "commodity",
          "inbound_at", "outbound_at", "duration_s", "single_weigh", "source_created_at")


def upsert_tickets(db: DbSession, site_id: int, tickets: list[dict]) -> int:
    """Insert or update included tickets. Re-reading the same rows is a no-op
    apart from updated_at. Returns the number of rows written."""
    rows = [{"site_id": site_id, "source_ticket_id": t["source_ticket_id"],
             **{k: t.get(k) for k in STORED}}
            for t in tickets if t.get("included", True) and t.get("source_ticket_id")]
    if not rows:
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    for i in range(0, len(rows), 500):
        stmt = insert(Ticket).values(rows[i:i + 500])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_tickets_site_source",
            set_={**{k: getattr(stmt.excluded, k) for k in STORED}, "updated_at": now}
        ).returning(Ticket.id)
        written += len(db.execute(stmt).all())
    return written
