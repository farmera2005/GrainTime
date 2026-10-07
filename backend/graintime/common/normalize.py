"""Turn raw site rows into GrainTime tickets. Pure functions, no I/O.

Times: site databases store local Eastern time with no offset. They are
converted to UTC with America/New_York. In the fall-back hour a local time
like 01:30 happens twice; for a truck's inbound/outbound pair we pick the
interpretation that gives the shortest non-negative duration, so a truck on
site across the repeated hour is neither negative nor one hour too long.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .profiles import ProfileConfig

SITE_TZ = ZoneInfo("America/New_York")


def raw_key(v) -> str:
    """Stable string form of a raw code value (1, 1.0, '1', True -> '1')."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def to_utc(local: datetime | None, fold: int = 0) -> datetime | None:
    if local is None:
        return None
    if local.tzinfo is not None:
        return local.astimezone(timezone.utc)
    return local.replace(tzinfo=SITE_TZ, fold=fold).astimezone(timezone.utc)


def pair_to_utc(inbound: datetime | None, outbound: datetime | None
                ) -> tuple[datetime | None, datetime | None]:
    """Convert an inbound/outbound pair of local times, resolving the fall-back
    hour so the duration is the shortest non-negative one."""
    if inbound is None or outbound is None:
        return to_utc(inbound), to_utc(outbound)
    best = None
    for fin in (0, 1):
        for fout in (0, 1):
            if fout < fin:
                continue  # can't go from the second 01:xx back to the first
            i, o = to_utc(inbound, fin), to_utc(outbound, fout)
            d = (o - i).total_seconds()
            if d >= 0 and (best is None or d < best[0]):
                best = (d, i, o)
    if best is None:  # outbound before inbound in the data itself
        return to_utc(inbound), to_utc(outbound)
    return best[1], best[2]


def _two_weigh_pair(a: tuple, b: tuple) -> tuple[datetime, datetime]:
    """Inbound/outbound for a ticket with two kinds of weigh (gross and tare).

    Normally the earlier local time is the inbound weigh. In the fall-back hour
    local times repeat, so a weigh at the second 01:10 can look earlier than one
    at the first 01:50; try both orders and keep the shortest non-negative stay
    (ties keep the natural order)."""
    natural = (a[0], b[1]) if a[0] <= b[0] else (b[0], a[1])
    reverse = (b[0], a[1]) if natural == (a[0], b[1]) else (a[0], b[1])
    best = None
    for i, (fi, lo) in enumerate((natural, reverse)):
        iu, ou = pair_to_utc(fi, lo)
        d = (ou - iu).total_seconds()
        if d >= 0 and (best is None or d < best[0]):
            best = (d, iu, ou)
    if best is None:
        return pair_to_utc(*natural)
    return best[1], best[2]


@dataclass
class WeighSummary:
    """Recorded weigh steps for one ticket, grouped by weight type."""
    by_type: dict[str, tuple[datetime, datetime, int]] = field(default_factory=dict)

    def add(self, weight_type, first_at: datetime, last_at: datetime, n: int) -> None:
        k = raw_key(weight_type)
        if k in self.by_type:
            f, l, c = self.by_type[k]
            self.by_type[k] = (min(f, first_at), max(l, last_at), c + n)
        else:
            self.by_type[k] = (first_at, last_at, n)

    def merge(self, other: "WeighSummary") -> None:
        for k, (f, l, n) in other.by_type.items():
            self.add(k, f, l, n)


@dataclass
class Lookups:
    types: dict[str, dict] = field(default_factory=dict)      # key -> {code, description, direction}
    products: dict[str, dict] = field(default_factory=dict)   # key -> {code, description}


def normalize(profile: ProfileConfig, row: dict, weighs: WeighSummary | None,
              lookups: Lookups) -> dict:
    """One raw ticket row -> normalized ticket dict, with 'included' False and a
    'note' when the profile filters it out (e.g. a transaction type not tracked)."""
    p = profile
    raw_status = raw_key(row.get(p.status_column))
    status = p.status_map.get(raw_status, "unknown")
    if p.void_column and row.get(p.void_column) not in (None, 0, False, "", "0"):
        status = "voided"

    type_info = lookups.types.get(raw_key(row.get(p.type_column))) if p.type_column else None
    type_code = (type_info or {}).get("code")
    direction = "received"
    if type_info and p.type_lookup and p.type_lookup.direction_column:
        direction = p.direction_map.get(raw_key(type_info.get("direction")), "unknown")
    prod = lookups.products.get(raw_key(row.get(p.product_column))) if p.product_column else None
    commodity = None
    if prod:
        commodity = (prod.get("description") or prod.get("code") or "").strip() or None

    single_weigh = False
    if p.weigh_steps is not None:
        types = weighs.by_type if weighs else {}
        groups = list(types.values())
        if len(groups) == 2:
            inbound_at, outbound_at = _two_weigh_pair(groups[0], groups[1])
        elif groups:
            in_local = min(f for f, _, _ in groups)
            out_local = max(l for _, l, _ in groups) if len(groups) > 2 else None
            inbound_at, outbound_at = pair_to_utc(in_local, out_local)
        else:
            inbound_at = outbound_at = None
        # Completed with only one kind of weight (gross or tare): stored tare.
        single_weigh = status == "completed" and len(groups) < 2
    else:
        in_local, out_local = row.get(p.inbound_column), row.get(p.outbound_column)
        single_weigh = status == "completed" and (in_local is None or out_local is None
                                                  or in_local == out_local)
        inbound_at, outbound_at = pair_to_utc(in_local, out_local)
    duration = None
    if inbound_at and outbound_at and not single_weigh:
        duration = int((outbound_at - inbound_at).total_seconds())
        if duration < 0:
            duration = None

    included, note = True, None
    if p.included_type_codes and type_code not in p.included_type_codes:
        included, note = False, f"type {type_code or '?'} not tracked"
    elif p.filter_column and raw_key(row.get(p.filter_column)) != raw_key(p.filter_value):
        included, note = False, "outside the profile filter"

    created = row.get(p.created_column)
    return {
        "source_ticket_id": raw_key(row.get(p.id_column)),
        "ticket_number": (raw_key(row.get(p.ticket_number_column)) or None)
        if p.ticket_number_column else None,
        "raw_status": raw_status,
        "status": status,
        "direction": direction,
        "transaction_type": type_code,
        "commodity": commodity,
        "inbound_at": inbound_at,
        "outbound_at": outbound_at if not single_weigh else None,
        "duration_s": duration,
        "single_weigh": single_weigh,
        "source_created_at": to_utc(created) if isinstance(created, datetime) else None,
        "included": included,
        "note": note,
    }


def resolve_families(profile: ProfileConfig, rows: list[dict],
                     weighs: dict[str, WeighSummary]) -> tuple[list[dict], dict[str, WeighSummary], int]:
    """Linked (split) tickets: a row whose parent column points to another ticket
    is the same truck. It is never counted on its own; when the parent is in the
    batch its weigh steps are merged into the parent's. (Parents and children are
    created minutes apart, so the look-back window always reads both.)
    Returns (rows, weighs, merged_count)."""
    if not profile.parent_column:
        return rows, weighs, 0
    by_id = {raw_key(r.get(profile.id_column)): r for r in rows}
    keep, merged = [], 0
    for r in rows:
        parent = raw_key(r.get(profile.parent_column))
        rid = raw_key(r.get(profile.id_column))
        if parent and parent != rid:
            if parent in by_id and rid in weighs:
                weighs.setdefault(parent, WeighSummary()).merge(weighs[rid])
            merged += 1
            continue
        keep.append(r)
    return keep, weighs, merged


def ceiling_excluded(duration_s: int | None, ceiling_hours: float) -> bool:
    return duration_s is not None and duration_s > ceiling_hours * 3600


__all__ = ["normalize", "resolve_families", "pair_to_utc", "to_utc", "WeighSummary", "Lookups",
           "raw_key", "ceiling_excluded", "SITE_TZ"]
