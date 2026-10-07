"""Duration, exclusions, linked tickets and daylight-saving handling."""
from datetime import datetime, timedelta, timezone

import pytest

from graintime.common.normalize import (Lookups, WeighSummary, ceiling_excluded, normalize,
                                        pair_to_utc, resolve_families)
from graintime.common.profiles import COMPUWEIGH_GMS, ProfileConfig

P = ProfileConfig(**COMPUWEIGH_GMS)
LK = Lookups(types={"1": {"code": "TRUCKIN", "direction": 1}, "2": {"code": "TRUCKOUT", "direction": 2}},
             products={"9201597": {"code": "CORN", "description": "Yellow Corn #2"}})


def row(pk=1, status=1, trt=1, created=datetime(2026, 10, 7, 10, 0), parent=None):
    return {"tid_pk": pk, "tid_ticket": 1082894 if status == 1 else None, "tid_status": status,
            "tid_createdate": created, "tid_completetime": None, "tid_trtfk": trt,
            "tid_prdfk": 9201597, "tid_parenttidfk": parent}


def weighs(*steps):
    w = WeighSummary()
    for wtype, at in steps:
        w.add(wtype, at, at, 1)
    return w


def test_completed_ticket_duration_and_fields():
    t = normalize(P, row(), weighs((0, datetime(2026, 10, 7, 10, 1)), (1, datetime(2026, 10, 7, 10, 23))), LK)
    assert t["status"] == "completed" and t["direction"] == "received"
    assert t["transaction_type"] == "TRUCKIN" and t["commodity"] == "Yellow Corn #2"
    assert t["inbound_at"] == datetime(2026, 10, 7, 14, 1, tzinfo=timezone.utc)   # EDT = UTC-4
    assert t["duration_s"] == 22 * 60 and t["included"] and not t["single_weigh"]
    assert t["ticket_number"] == "1082894" and t["source_ticket_id"] == "1"


def test_open_ticket_has_inbound_only():
    t = normalize(P, row(status=0), weighs((0, datetime(2026, 10, 7, 10, 1))), LK)
    assert t["status"] == "open" and t["inbound_at"] and t["outbound_at"] is None
    assert t["duration_s"] is None and not t["single_weigh"]


def test_stored_tare_single_weigh_is_flagged():
    t = normalize(P, row(), weighs((0, datetime(2026, 10, 7, 10, 1))), LK)
    assert t["single_weigh"] and t["duration_s"] is None and t["outbound_at"] is None


@pytest.mark.parametrize("raw,expected", [(2, "voided"), (4, "voided"), (9, "unknown")])
def test_status_translation(raw, expected):
    t = normalize(P, row(status=raw), weighs((0, datetime(2026, 10, 7, 10)), (1, datetime(2026, 10, 7, 10, 30))), LK)
    assert t["status"] == expected and t["raw_status"] == str(raw)


def test_void_flag_column_overrides_status():
    p = ProfileConfig(**{**COMPUWEIGH_GMS, "void_column": "tid_void"})
    t = normalize(p, {**row(), "tid_void": True}, weighs((0, datetime(2026, 10, 7, 10)), (1, datetime(2026, 10, 7, 10, 30))), LK)
    assert t["status"] == "voided"


def test_untracked_type_is_not_included():
    t = normalize(P, row(trt=2), weighs((1, datetime(2026, 10, 7, 10)), (0, datetime(2026, 10, 7, 10, 30))), LK)
    assert not t["included"] and "TRUCKOUT" in t["note"] and t["direction"] == "shipped"


def test_outbound_order_does_not_matter_for_shipped():
    p = ProfileConfig(**{**COMPUWEIGH_GMS, "included_type_codes": []})
    t = normalize(p, row(trt=2), weighs((1, datetime(2026, 10, 7, 10)), (0, datetime(2026, 10, 7, 10, 30))), LK)
    assert t["included"] and t["duration_s"] == 1800


def test_split_child_merged_into_parent_and_not_counted():
    rows = [row(pk=10), row(pk=11, parent=10)]
    ws = {"10": weighs((0, datetime(2026, 10, 7, 10))), "11": weighs((1, datetime(2026, 10, 7, 10, 25)))}
    kept, ws, merged = resolve_families(P, rows, ws)
    assert [r["tid_pk"] for r in kept] == [10] and merged == 1
    t = normalize(P, kept[0], ws["10"], LK)
    assert t["duration_s"] == 25 * 60 and not t["single_weigh"]


def test_split_child_without_parent_in_batch_is_still_not_counted():
    kept, _, merged = resolve_families(P, [row(pk=11, parent=10)], {})
    assert kept == [] and merged == 1


def test_ceiling_exclusion():
    assert ceiling_excluded(4 * 3600 + 1, 4) and not ceiling_excluded(4 * 3600, 4)
    assert not ceiling_excluded(None, 4)


def test_columns_mode_profile():
    cfg = {**COMPUWEIGH_GMS, "weigh_steps": None, "inbound_column": "tin", "outbound_column": "tout",
           "included_type_codes": []}
    p = ProfileConfig(**cfg)
    t = normalize(p, {**row(), "tin": datetime(2026, 10, 7, 9), "tout": datetime(2026, 10, 7, 9, 40)}, None, LK)
    assert t["duration_s"] == 2400
    t2 = normalize(p, {**row(), "tin": datetime(2026, 10, 7, 9), "tout": None}, None, LK)
    assert t2["single_weigh"]


# --- daylight saving --------------------------------------------------------- #
# 2026-11-01: clocks go from 01:59:59 EDT back to 01:00:00 EST; 01:xx happens twice.

def _dur(i, o):
    a, b = pair_to_utc(i, o)
    return (b - a).total_seconds()


def test_fall_back_truck_across_repeated_hour_is_not_negative():
    # In at 01:50 EDT (first 01:50), out at 01:10 EST (second 01:10): 20 minutes on site.
    assert _dur(datetime(2026, 11, 1, 1, 50), datetime(2026, 11, 1, 1, 10)) == 20 * 60


def test_fall_back_truck_within_repeated_hour_is_not_inflated():
    # In 01:10, out 01:40, both in the same occurrence: 30 minutes, not 1 h 30.
    assert _dur(datetime(2026, 11, 1, 1, 10), datetime(2026, 11, 1, 1, 40)) == 30 * 60


def test_fall_back_truck_spanning_whole_night():
    # In 00:50 EDT, out 02:10 EST: 2 h 20 real minutes.
    assert _dur(datetime(2026, 11, 1, 0, 50), datetime(2026, 11, 1, 2, 10)) == 2 * 3600 + 20 * 60


def test_spring_forward():
    # 2026-03-08: 02:00 EST jumps to 03:00 EDT. In 01:50, out 03:10 = 20 minutes.
    assert _dur(datetime(2026, 3, 8, 1, 50), datetime(2026, 3, 8, 3, 10)) == 20 * 60


def test_normalize_uses_dst_safe_pair():
    t = normalize(P, row(created=datetime(2026, 11, 1, 1, 45)),
                  weighs((0, datetime(2026, 11, 1, 1, 50)), (1, datetime(2026, 11, 1, 1, 10))), LK)
    assert t["duration_s"] == 20 * 60


def test_profile_validation():
    with pytest.raises(ValueError):
        ProfileConfig(**{**COMPUWEIGH_GMS, "weigh_steps": None})                # no weigh times
    with pytest.raises(ValueError):
        ProfileConfig(**{**COMPUWEIGH_GMS, "ticket_table": "dbo.T; DROP TABLE x"})
    with pytest.raises(ValueError):
        ProfileConfig(**{**COMPUWEIGH_GMS, "status_map": {}})
    assert timedelta  # keep import
