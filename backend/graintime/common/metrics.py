"""Time-on-site statistics. The one place the brief's metric definitions live,
so the dashboard, wall display and public page never disagree.

* Time on site = outbound weigh - inbound weigh, for completed, non-voided
  tickets with both weighs ("valid" below).
* Excluded from statistics, and counted: voided, single-weigh (stored tare),
  and durations above the ceiling (default 4 h). Unknown statuses are counted
  separately.
* Current time on site = median of valid trucks that weighed out in the last
  60 minutes; with fewer than 3, the median of the most recent 5 valid trucks
  from the last 2 hours; with none, "no recent trucks".
* Trucks on site now = open tickets with an inbound weigh in the last 6 hours.
* Median and 90th percentile everywhere; received and shipped kept separate
  (received by default).
* Completed trucks are bucketed by their outbound weigh, in America/New_York;
  "trucks" counts are by inbound weigh, excluding voided tickets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from . import settings_store
from .normalize import SITE_TZ

TZ_NAME = "America/New_York"


def local_bounds(d_from: date, d_to: date) -> tuple[datetime, datetime]:
    """UTC [start, end) for local calendar days d_from..d_to inclusive."""
    start = datetime.combine(d_from, datetime.min.time(), SITE_TZ)
    end = datetime.combine(d_to + timedelta(days=1), datetime.min.time(), SITE_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def local_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(SITE_TZ).date()


@dataclass
class Filters:
    site_ids: list[int]
    d_from: date
    d_to: date
    direction: str = "received"
    commodity: str | None = None
    ceiling_s: int = 4 * 3600
    recent_window_min: int = 60
    open_cutoff_h: int = 6
    green_max_min: int = 20
    yellow_max_min: int = 40
    extra: dict = field(default_factory=dict)

    def params(self) -> dict:
        start, end = local_bounds(self.d_from, self.d_to)
        return {"sites": self.site_ids or [-1], "start": start, "end": end,
                "direction": self.direction, "commodity": self.commodity,
                "ceiling": self.ceiling_s, "tz": TZ_NAME}


BASE = """site_id = ANY(:sites) AND direction = :direction
          AND (CAST(:commodity AS text) IS NULL OR commodity = :commodity)"""
VALID = """status = 'completed' AND NOT single_weigh AND duration_s IS NOT NULL
           AND duration_s <= :ceiling"""


def globals_filters(db: DbSession, site_ids: list[int], d_from: date, d_to: date,
                    direction: str = "received", commodity: str | None = None) -> Filters:
    g = settings_store.get_globals(db)
    return Filters(site_ids=site_ids, d_from=d_from, d_to=d_to, direction=direction,
                   commodity=commodity, ceiling_s=int(g["duration_ceiling_hours"]) * 3600,
                   recent_window_min=int(g["recent_window_min"]),
                   open_cutoff_h=int(g["open_ticket_cutoff_hours"]),
                   green_max_min=int(g["threshold_green_max_min"]),
                   yellow_max_min=int(g["threshold_yellow_max_min"]))


def level(minutes: float | None, f: Filters) -> str | None:
    """good / warning / critical against the green and yellow limits."""
    if minutes is None:
        return None
    if minutes <= f.green_max_min:
        return "good"
    if minutes <= f.yellow_max_min:
        return "warning"
    return "critical"


def _min(seconds) -> float | None:
    return None if seconds is None else round(float(seconds) / 60.0, 1)


# --------------------------------------------------------------------------- #
# Live numbers
# --------------------------------------------------------------------------- #

def current_time_on_site(db: DbSession, site_id: int, f: Filters,
                         now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    p = {**f.params(), "sites": [site_id], "since": now - timedelta(minutes=f.recent_window_min),
         "since2": now - timedelta(hours=2), "now": now}
    rows = db.execute(text(f"""
        SELECT duration_s FROM tickets
        WHERE {BASE} AND {VALID} AND outbound_at >= :since AND outbound_at <= :now
    """), p).scalars().all()
    if len(rows) >= 3:
        med = _median(rows)
        return {"minutes": _min(med), "trucks": len(rows),
                "basis": f"median of {len(rows)} trucks out in the last {f.recent_window_min} min",
                "level": level(_min(med), f)}
    rows = db.execute(text(f"""
        SELECT duration_s FROM tickets
        WHERE {BASE} AND {VALID} AND outbound_at >= :since2 AND outbound_at <= :now
        ORDER BY outbound_at DESC LIMIT 5
    """), p).scalars().all()
    if rows:
        med = _median(rows)
        return {"minutes": _min(med), "trucks": len(rows),
                "basis": f"median of the last {len(rows)} trucks (last 2 hours)",
                "level": level(_min(med), f)}
    return {"minutes": None, "trucks": 0, "basis": "no recent trucks", "level": None}


def _median(values: list) -> float:
    v = sorted(values)
    n = len(v)
    return float(v[n // 2]) if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2.0


def on_site_now(db: DbSession, site_id: int, f: Filters, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = db.execute(text(f"""
        SELECT source_ticket_id, inbound_at, commodity FROM tickets
        WHERE {BASE} AND status = 'open' AND inbound_at >= :cutoff AND inbound_at <= :now
        ORDER BY inbound_at
    """), {**f.params(), "sites": [site_id], "cutoff": now - timedelta(hours=f.open_cutoff_h),
           "now": now}).all()
    return [{"id": r[0], "inbound_at": r[1], "commodity": r[2]} for r in rows]


def overview(db: DbSession, f: Filters, sites: list[dict], now: datetime | None = None) -> list[dict]:
    """One row per site: live numbers plus today's statistics."""
    now = now or datetime.now(timezone.utc)
    today = local_today(now)
    tf = Filters(**{**f.__dict__, "d_from": today, "d_to": today})
    p = tf.params()
    agg = {r[0]: r for r in db.execute(text(f"""
        SELECT site_id,
               COUNT(*) FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)
                   FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end) AS med,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s)
                   FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end) AS p90,
               COUNT(*) FILTER (WHERE status <> 'voided' AND inbound_at >= :start AND inbound_at < :end) AS trucks
        FROM tickets WHERE {BASE} GROUP BY site_id
    """), p).all()}
    hour = now.astimezone(SITE_TZ).hour
    typical = dict(db.execute(text(f"""
        SELECT site_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)
        FROM tickets
        WHERE {BASE} AND {VALID} AND outbound_at >= :hist_start AND outbound_at < :start
          AND EXTRACT(HOUR FROM outbound_at AT TIME ZONE :tz) = :hour
        GROUP BY site_id
    """), {**p, "hist_start": p["start"] - timedelta(days=28), "hour": hour}).all())
    out = []
    for s in sites:
        a = agg.get(s["id"])
        cur = current_time_on_site(db, s["id"], f, now)
        onsite = on_site_now(db, s["id"], f, now)
        out.append({
            **s,
            "current": cur,
            "on_site_now": len(onsite),
            "oldest_on_site_min": round((now - onsite[0]["inbound_at"]).total_seconds() / 60, 1)
            if onsite else None,
            "trucks_today": int(a[4]) if a else 0,
            "completed_today": int(a[1]) if a else 0,
            "median_today_min": _min(a[2]) if a else None,
            "p90_today_min": _min(a[3]) if a else None,
            "typical_this_hour_min": _min(typical.get(s["id"])),
        })
    return out


# --------------------------------------------------------------------------- #
# Range statistics
# --------------------------------------------------------------------------- #

def summary(db: DbSession, f: Filters) -> dict:
    r = db.execute(text(f"""
        SELECT
          COUNT(*) FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end),
          percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)
              FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end),
          percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s)
              FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end),
          AVG(duration_s) FILTER (WHERE {VALID} AND outbound_at >= :start AND outbound_at < :end),
          COUNT(*) FILTER (WHERE status <> 'voided' AND inbound_at >= :start AND inbound_at < :end),
          COUNT(*) FILTER (WHERE status = 'voided'
                           AND COALESCE(outbound_at, inbound_at, source_created_at) >= :start
                           AND COALESCE(outbound_at, inbound_at, source_created_at) < :end),
          COUNT(*) FILTER (WHERE status = 'completed' AND single_weigh
                           AND COALESCE(outbound_at, inbound_at) >= :start
                           AND COALESCE(outbound_at, inbound_at) < :end),
          COUNT(*) FILTER (WHERE status = 'completed' AND NOT single_weigh AND duration_s > :ceiling
                           AND outbound_at >= :start AND outbound_at < :end),
          COUNT(*) FILTER (WHERE status = 'unknown' AND inbound_at >= :start AND inbound_at < :end)
        FROM tickets WHERE {BASE}
    """), f.params()).one()
    return {"completed": int(r[0]), "median_min": _min(r[1]), "p90_min": _min(r[2]),
            "mean_min": _min(r[3]), "trucks": int(r[4]),
            "excluded": {"voided": int(r[5]), "single_weigh": int(r[6]),
                         "over_ceiling": int(r[7]), "unknown_status": int(r[8])},
            "ceiling_hours": f.ceiling_s / 3600}


def by_hour(db: DbSession, f: Filters) -> list[dict]:
    """Per local hour of day across the range: trucks in, completed, median, p90."""
    p = f.params()
    done = {int(r[0]): r for r in db.execute(text(f"""
        SELECT EXTRACT(HOUR FROM outbound_at AT TIME ZONE :tz) AS h, COUNT(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s),
               percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s)
        FROM tickets WHERE {BASE} AND {VALID} AND outbound_at >= :start AND outbound_at < :end
        GROUP BY h
    """), p).all()}
    arrivals = {int(r[0]): int(r[1]) for r in db.execute(text(f"""
        SELECT EXTRACT(HOUR FROM inbound_at AT TIME ZONE :tz) AS h, COUNT(*)
        FROM tickets WHERE {BASE} AND status <> 'voided' AND inbound_at >= :start AND inbound_at < :end
        GROUP BY h
    """), p).all()}
    return [{"hour": h, "trucks": arrivals.get(h, 0),
             "completed": int(done[h][1]) if h in done else 0,
             "median_min": _min(done[h][2]) if h in done else None,
             "p90_min": _min(done[h][3]) if h in done else None} for h in range(24)]


def trend(db: DbSession, f: Filters) -> list[dict]:
    """Per local day: completed trucks, median, p90 (days with no trucks included)."""
    rows = {r[0]: r for r in db.execute(text(f"""
        SELECT CAST(outbound_at AT TIME ZONE :tz AS date) AS d, COUNT(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s),
               percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s)
        FROM tickets WHERE {BASE} AND {VALID} AND outbound_at >= :start AND outbound_at < :end
        GROUP BY d ORDER BY d
    """), f.params()).all()}
    out, d = [], f.d_from
    while d <= f.d_to:
        r = rows.get(d)
        out.append({"date": d.isoformat(), "completed": int(r[1]) if r else 0,
                    "median_min": _min(r[2]) if r else None, "p90_min": _min(r[3]) if r else None})
        d += timedelta(days=1)
    return out


def heatmap(db: DbSession, f: Filters) -> list[dict]:
    """Hour of day x day of week (0 = Monday): completed trucks and median."""
    rows = db.execute(text(f"""
        SELECT EXTRACT(ISODOW FROM outbound_at AT TIME ZONE :tz) - 1 AS dow,
               EXTRACT(HOUR FROM outbound_at AT TIME ZONE :tz) AS h, COUNT(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s)
        FROM tickets WHERE {BASE} AND {VALID} AND outbound_at >= :start AND outbound_at < :end
        GROUP BY dow, h
    """), f.params()).all()
    return [{"dow": int(r[0]), "hour": int(r[1]), "completed": int(r[2]), "median_min": _min(r[3])}
            for r in rows]


def compare(db: DbSession, f: Filters, sites: list[dict]) -> list[dict]:
    rows = {r[0]: r for r in db.execute(text(f"""
        SELECT site_id, COUNT(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_s),
               percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_s)
        FROM tickets WHERE {BASE} AND {VALID} AND outbound_at >= :start AND outbound_at < :end
        GROUP BY site_id
    """), f.params()).all()}
    out = []
    for s in sites:
        r = rows.get(s["id"])
        out.append({"site_id": s["id"], "name": s["name"], "code": s["code"],
                    "completed": int(r[1]) if r else 0, "median_min": _min(r[2]) if r else None,
                    "p90_min": _min(r[3]) if r else None})
    return out


def distribution(db: DbSession, f: Filters, bin_min: int = 5) -> list[dict]:
    rows = {int(b): c for b, c in db.execute(text(f"""
        SELECT FLOOR(duration_s / (:bin * 60.0)) AS b, COUNT(*)
        FROM tickets WHERE {BASE} AND {VALID} AND outbound_at >= :start AND outbound_at < :end
        GROUP BY b
    """), {**f.params(), "bin": bin_min}).all()}
    nbins = max(1, int(f.ceiling_s / 60 // bin_min))
    last = max([int(b) for b in rows] + [0])
    nbins = min(nbins, max(last + 1, 12))
    return [{"from_min": i * bin_min, "to_min": (i + 1) * bin_min, "count": int(rows.get(i, 0))}
            for i in range(nbins)]
