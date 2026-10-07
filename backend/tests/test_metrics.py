"""The brief's metric definitions, computed from the central store."""
from datetime import date, datetime, timedelta, timezone

import pytest

from graintime.common import metrics
from graintime.common.db import get_sessionmaker
from graintime.common.models import Site
from graintime.common.tickets_store import upsert_tickets

NOW = datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc)          # 12:00 noon Eastern (EDT)
TODAY = date(2026, 10, 7)


def site(code="AAA"):
    with get_sessionmaker()() as db:
        s = Site(name=code, code=code, host="h", port=1433, database_name="d", username="u",
                 password_encrypted=b"x", encrypt="yes", trust_server_certificate=True,
                 polling_enabled=True, show_on_dashboard=True, show_on_public=False)
        db.add(s)
        db.commit()
        return s.id


_n = [0]


def t(out_ago_min=None, dur_min=None, status="completed", single=False, direction="received",
      commodity="Corn", in_ago_min=None):
    _n[0] += 1
    inbound = outbound = None
    if out_ago_min is not None:
        outbound = NOW - timedelta(minutes=out_ago_min)
        inbound = outbound - timedelta(minutes=dur_min)
    elif in_ago_min is not None:
        inbound = NOW - timedelta(minutes=in_ago_min)
    return {"source_ticket_id": str(_n[0]), "ticket_number": None, "status": status,
            "raw_status": "x", "direction": direction, "transaction_type": "TRUCKIN",
            "commodity": commodity, "inbound_at": inbound, "outbound_at": outbound if not single else None,
            "duration_s": int(dur_min * 60) if (dur_min is not None and not single and status == "completed") else None,
            "single_weigh": single, "source_created_at": inbound, "included": True}


def load(sid, tickets):
    with get_sessionmaker()() as db:
        upsert_tickets(db, sid, tickets)
        db.commit()


def F(db, sid, d_from=TODAY, d_to=TODAY, **kw):
    return metrics.globals_filters(db, [sid], d_from, d_to, **kw)


def test_current_uses_last_hour_when_three_or_more(db_clean):
    sid = site()
    load(sid, [t(10, 20), t(20, 30), t(50, 40), t(90, 99)])
    with get_sessionmaker()() as db:
        cur = metrics.current_time_on_site(db, sid, F(db, sid), NOW)
    assert cur["minutes"] == 30 and cur["trucks"] == 3 and "last 60 min" in cur["basis"]
    assert cur["level"] == "warning"          # 30 min: above green (20), within yellow (40)


def test_current_falls_back_to_last_five_in_two_hours(db_clean):
    sid = site()
    load(sid, [t(10, 12), t(70, 14), t(80, 16), t(90, 18), t(100, 20), t(110, 22), t(130, 50)])
    with get_sessionmaker()() as db:
        cur = metrics.current_time_on_site(db, sid, F(db, sid), NOW)
    # only one in the last hour -> last 5 within 2 h: 12, 14, 16, 18, 20 -> median 16
    assert cur["minutes"] == 16 and cur["trucks"] == 5 and "last 5" in cur["basis"]
    assert cur["level"] == "good"


def test_no_recent_trucks(db_clean):
    sid = site()
    load(sid, [t(150, 20)])
    with get_sessionmaker()() as db:
        cur = metrics.current_time_on_site(db, sid, F(db, sid), NOW)
    assert cur["minutes"] is None and cur["basis"] == "no recent trucks" and cur["level"] is None


def test_exclusions_never_count_toward_time_on_site(db_clean):
    sid = site()
    load(sid, [t(10, 20), t(15, 22), t(20, 24),
               t(5, 5, status="voided"), t(25, 1, single=True), t(30, 300),       # 5 h > 4 h ceiling
               t(35, 2, direction="shipped")])
    with get_sessionmaker()() as db:
        f = F(db, sid)
        cur = metrics.current_time_on_site(db, sid, f, NOW)
        s = metrics.summary(db, f)
    assert cur["minutes"] == 22
    assert s["completed"] == 3 and s["median_min"] == 22
    assert s["excluded"] == {"voided": 1, "single_weigh": 1, "over_ceiling": 1, "unknown_status": 0}


def test_on_site_now_respects_six_hour_cutoff(db_clean):
    sid = site()
    load(sid, [t(status="open", in_ago_min=30), t(status="open", in_ago_min=200),
               t(status="open", in_ago_min=7 * 60),                    # abandoned: not counted
               t(status="open", in_ago_min=10, direction="shipped")])
    with get_sessionmaker()() as db:
        onsite = metrics.on_site_now(db, sid, F(db, sid), NOW)
    assert len(onsite) == 2 and onsite[0]["inbound_at"] < onsite[1]["inbound_at"]


def test_p90_and_direction_and_commodity(db_clean):
    sid = site()
    load(sid, [t(10 + i, m) for i, m in enumerate(range(10, 110, 10))] +        # 10..100 min
         [t(5, 60, direction="shipped"), t(6, 70, commodity="Soybeans")])
    with get_sessionmaker()() as db:
        s = metrics.summary(db, F(db, sid, commodity="Corn"))
        shipped = metrics.summary(db, F(db, sid, direction="shipped"))
    assert s["completed"] == 10 and s["median_min"] == 55 and s["p90_min"] == 91
    assert shipped["completed"] == 1 and shipped["median_min"] == 60


def test_local_day_boundaries(db_clean):
    sid = site()
    # 2026-10-07 23:30 Eastern = 03:30 UTC on the 8th: belongs to the 7th
    late = datetime(2026, 10, 8, 3, 30, tzinfo=timezone.utc)
    tk = t(0, 15)
    tk.update(outbound_at=late, inbound_at=late - timedelta(minutes=15))
    load(sid, [tk])
    with get_sessionmaker()() as db:
        days = metrics.trend(db, F(db, sid, d_from=date(2026, 10, 6), d_to=date(2026, 10, 8)))
        hours = metrics.by_hour(db, F(db, sid))
    assert [d["completed"] for d in days] == [0, 1, 0]
    assert hours[23]["completed"] == 1 and hours[23]["median_min"] == 15


def test_overview_heatmap_compare_distribution(db_clean):
    a, b = site("AAA"), site("BBB")
    load(a, [t(10, 20), t(20, 30), t(30, 40), t(status="open", in_ago_min=15)])
    load(b, [t(15, 10)])
    with get_sessionmaker()() as db:
        f = metrics.globals_filters(db, [a, b], TODAY, TODAY)
        sites = [{"id": a, "name": "AAA", "code": "AAA"}, {"id": b, "name": "BBB", "code": "BBB"}]
        ov = {r["id"]: r for r in metrics.overview(db, f, sites, NOW)}
        hm = metrics.heatmap(db, f)
        cmp_ = {r["site_id"]: r for r in metrics.compare(db, f, sites)}
        dist = metrics.distribution(db, f)
    assert ov[a]["current"]["minutes"] == 30 and ov[a]["on_site_now"] == 1 and ov[a]["completed_today"] == 3
    assert ov[a]["trucks_today"] == 4 and ov[b]["current"]["minutes"] == 10
    assert sum(c["completed"] for c in hm) == 4 and all(c["dow"] == 2 for c in hm)   # Wednesday
    assert cmp_[a]["median_min"] == 30 and cmp_[b]["completed"] == 1
    assert sum(x["count"] for x in dist) == 4 and dist[0]["from_min"] == 0


@pytest.mark.parametrize("m,expected", [(20, "good"), (20.1, "warning"), (40, "warning"), (41, "critical")])
def test_levels(m, expected):
    f = metrics.Filters(site_ids=[], d_from=TODAY, d_to=TODAY)
    assert metrics.level(m, f) == expected
