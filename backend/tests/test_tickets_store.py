from datetime import datetime, timezone

from sqlalchemy import select

from graintime.common.db import get_sessionmaker
from graintime.common.models import Site, Ticket
from graintime.common.tickets_store import upsert_tickets


def _site():
    with get_sessionmaker()() as db:
        s = Site(name="S", code="S1", host="h", port=1433, database_name="d", username="u",
                 password_encrypted=b"x", encrypt="yes", trust_server_certificate=True,
                 polling_enabled=False, show_on_dashboard=True, show_on_public=False)
        db.add(s)
        db.commit()
        return s.id


def ticket(**over):
    t = {"source_ticket_id": "155202", "ticket_number": None, "status": "open", "raw_status": "0",
         "direction": "received", "transaction_type": "TRUCKIN", "commodity": "Corn",
         "inbound_at": datetime(2026, 10, 7, 14, 42, tzinfo=timezone.utc), "outbound_at": None,
         "duration_s": None, "single_weigh": False, "source_created_at": None, "included": True}
    return {**t, **over}


def test_upsert_is_idempotent_and_updates(db_clean):
    sid = _site()
    S = get_sessionmaker()
    for _ in range(3):
        with S() as db:
            upsert_tickets(db, sid, [ticket()])
            db.commit()
    with S() as db:
        upsert_tickets(db, sid, [ticket(status="completed", ticket_number="1082894", duration_s=623,
                                        outbound_at=datetime(2026, 10, 7, 14, 53, tzinfo=timezone.utc)),
                                 ticket(source_ticket_id="9", included=False)])
        db.commit()
        rows = db.scalars(select(Ticket)).all()
    assert len(rows) == 1
    assert rows[0].status == "completed" and rows[0].duration_s == 623 and rows[0].ticket_number == "1082894"
