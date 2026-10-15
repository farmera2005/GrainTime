"""The AgVantagePC mapping: weigh order by direction, statuses, types, manual
weights, decoding agent uploads, and polling by key (no table scans)."""
from datetime import datetime
from decimal import Decimal

import pytest

from graintime.collector.mapping import SiteReader, encode_value, read_poll_rows
from graintime.common.normalize import Lookups, decode_rows, normalize, raw_key
from graintime.common.profiles import AGVANTAGE_PC, ProfileConfig

P = ProfileConfig(**AGVANTAGE_PC)


def row(**kw):
    base = {"st_load": "24591R", "st_scaleticket": "679934", "st_status": "CPT", "st_date": datetime(2026, 10, 7),
            "st_completedDate": datetime(2026, 10, 7, 14, 4), "st_type": "P", "st_desc": "BEANS",
            "st_grsdate": datetime(2026, 10, 7, 13, 50, 51), "st_tardate": datetime(2026, 10, 7, 14, 3, 38),
            "st_inout": "I", "st_grsmanual": Decimal("0"), "st_tarmanual": Decimal("0")}
    return {**base, **kw}


def n(r, profile=P):
    return normalize(profile, r, None, Lookups())


def test_profile_is_valid_and_agent_ready():
    assert P.agent_compatible() is None
    assert "st_name" not in P.ticket_columns() and "st_account" not in P.ticket_columns()   # no customer data
    assert not any("lbs" in c or "price" in c for c in P.ticket_columns())                  # no weights or prices


def test_inbound_purchase_gross_first():
    t = n(row())
    assert (t["status"], t["direction"], t["transaction_type"], t["commodity"]) == ("completed", "received", "P", "BEANS")
    assert t["duration_s"] == 12 * 60 + 47 and t["included"] and t["ticket_number"] == "679934"
    assert t["inbound_at"].hour == 17          # 13:50 EDT -> 17:50 UTC


def test_outbound_sale_tare_first():
    t = n(row(st_inout="O", st_type="S", st_tardate=datetime(2026, 10, 7, 9, 0), st_grsdate=datetime(2026, 10, 7, 9, 25)))
    assert t["direction"] == "shipped" and t["duration_s"] == 25 * 60
    assert t["inbound_at"] < t["outbound_at"]


def test_fall_back_hour_gives_the_short_stay():
    # Nov 1 2026: 01:00-02:00 EDT then 01:00-02:00 EST. Gross at first 01:50, tare at second 01:10.
    t = n(row(st_grsdate=datetime(2026, 11, 1, 1, 50), st_tardate=datetime(2026, 11, 1, 1, 10)))
    assert t["duration_s"] == 20 * 60


def test_statuses_and_types():
    assert n(row(st_status="APP"))["status"] == "completed"
    assert n(row(st_status="V"))["status"] == "voided"
    assert n(row(st_status="XYZ"))["status"] == "unknown"
    tr = n(row(st_type="T"))
    assert tr["included"] is False and "not tracked" in tr["note"]       # transfers left out


def test_truck_on_site_has_one_weigh():
    t = n(row(st_status="TAR", st_tardate=None, st_completedDate=None))
    assert t["status"] == "open" and t["inbound_at"] and t["outbound_at"] is None and t["duration_s"] is None


def test_manual_weights_kept_or_excluded():
    kept = n(row(st_grsmanual=Decimal("1"), st_tarmanual=Decimal("1")))
    assert kept["duration_s"] and not kept["single_weigh"] and kept["note"] == "weight keyed in by hand"
    excl = n(row(st_tarmanual=1), ProfileConfig(**{**AGVANTAGE_PC, "manual_rule": "exclude"}))
    assert excl["single_weigh"] and excl["duration_s"] is None


def test_raw_keys_for_sql_numerics():
    assert raw_key(Decimal("45.0000")) == "45" and raw_key(Decimal("1.50")) == "1.5"


def test_agent_upload_round_trip():
    r = row()
    sent = {k: encode_value(v) for k, v in {**r, "st_name": "SECRET FARMER"}.items()}
    back = decode_rows(P, [sent])[0]
    assert "st_name" not in back                                       # extra columns dropped
    assert back["st_grsdate"] == r["st_grsdate"] and isinstance(back["st_date"], datetime)
    assert n(back)["duration_s"] == n(r)["duration_s"]


class FakeConn:
    """Records SQL; returns canned rows."""
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def cursor(self):
        conn = self

        class C:
            description = None

            def execute(self, sql, params=()):
                conn.sql.append(sql)
                self.description = [(c,) for c in (conn.rows[0].keys() if conn.rows else ["st_load"])]
                self._rows = [tuple(r.values()) for r in conn.rows] if "TOP" in sql else []

            def fetchall(self):
                return self._rows

            def close(self):
                pass
        return C()


def test_polls_seek_by_key_never_by_date(monkeypatch):
    monkeypatch.setattr("graintime.collector.mapping.PAUSE_S", 0)
    conn = FakeConn([row()])
    got = read_poll_rows(SiteReader(conn, P), "24590R", recheck=True)
    assert got["rows"] and got["high_water"] == "24591R"
    for sql in conn.sql:
        assert sql.startswith("SELECT TOP (")
        assert "st_date" not in sql.split("WHERE", 1)[1] and "st_grsdate" not in sql.split("WHERE", 1)[1]
        assert "[st_load]" in sql.split("WHERE", 1)[1] or "1 = 1" in sql


@pytest.mark.parametrize("bad", [{"weigh_pair_columns": ["st_grsdate"]},
                                 {"weigh_pair_columns": ["a", "a"]},
                                 {"direction_map": {}},
                                 {"agent_database_match": "%LOCALSET%"}])
def test_profile_validation(bad):
    with pytest.raises(ValueError):
        ProfileConfig(**{**AGVANTAGE_PC, **bad})
