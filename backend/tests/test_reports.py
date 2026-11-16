"""Reports: the period report, its comparison, HTML for browser and email, and the truck export."""
from datetime import date, datetime, timedelta, timezone

import pytest

from graintime.common import reports
from graintime.common.db import get_sessionmaker
from graintime.common.models import Site

from .test_metrics import NOW, load, t

TODAY = date(2026, 10, 7)


def site(code, dash=True):
    with get_sessionmaker()() as db:
        s = Site(name=code.title(), code=code, host="h", port=1433, database_name="d", username="u",
                 password_encrypted=b"x", encrypt="yes", trust_server_certificate=True, polling_enabled=True,
                 show_on_dashboard=dash, show_on_public=False)
        db.add(s)
        db.commit()
        return s.id


def tick(days_ago, out_ago_min, dur, **kw):
    r = t(out_ago_min, dur, **kw)
    shift = timedelta(days=days_ago)
    for k in ("inbound_at", "outbound_at", "source_created_at"):
        if r.get(k):
            r[k] -= shift
    return r


@pytest.fixture
def data(db_clean):
    a, b, hidden = site("AAA"), site("BBB"), site("HID", dash=False)
    load(a, [tick(0, 30, 20), tick(0, 60, 30), tick(0, 90, 40, commodity="Beans"),
             tick(0, 100, 10, status="voided"), tick(0, 120, 300),          # over the 4 h ceiling
             tick(0, 130, 15, single=True), tick(0, None, None, status="open", in_ago_min=20),
             tick(1, 30, 10), tick(1, 60, 12)])                              # previous day (comparison)
    load(b, [tick(0, 45, 50), tick(0, 75, 70)])
    load(hidden, [tick(0, 30, 99)])
    return a, b


def params(**kw):
    return reports.ReportParams(d_from=TODAY, d_to=TODAY, **kw)


def test_period_report_numbers_match_metrics(data):
    a, b = data
    with get_sessionmaker()() as db:
        r = reports.build_period_report(db, params(), now=NOW)
    assert [s["code"] for s in r["sites"]] == ["AAA", "BBB"]               # hidden site never reported
    by = {s["code"]: s for s in r["per_site"]}
    assert by["AAA"]["completed"] == 3 and by["AAA"]["median_min"] == 30.0
    assert by["AAA"]["excluded"] == {"voided": 1, "single_weigh": 1, "over_ceiling": 1, "unknown_status": 0}
    assert by["BBB"]["median_min"] == 60.0
    assert [s["code"] for s in r["per_site"]] == ["BBB", "AAA"]             # longest median first
    # comparison with the previous day: AAA median 11 min yesterday -> +19 min
    assert by["AAA"]["median_delta"]["diff"] == 19.0 and by["AAA"]["prev"]["completed"] == 2
    assert r["compare"]["from"] == "2026-10-06" and r["compare"]["label"].startswith("previous period")
    assert {c["commodity"] for c in r["commodities"]} >= {"Corn", "Beans"}


def test_last_year_comparison_handles_leap_day():
    p = reports.ReportParams(d_from=date(2028, 2, 29), d_to=date(2028, 3, 6), compare="last_year")
    assert reports.compare_range(p) == (date(2027, 2, 28), date(2027, 3, 6))
    assert reports.compare_range(reports.ReportParams(d_from=TODAY, d_to=TODAY, compare="none")) is None


def test_html_for_browser_and_email(data):
    with get_sessionmaker()() as db:
        r = reports.build_period_report(db, params(title="Daily report <AAA>"), now=NOW)
    page = reports.render_html(r)
    assert "Daily report &lt;AAA&gt;" in page and "<script" not in page
    assert "Busiest hour" in page and "Median" in page and "inbound scale" in page
    mail = reports.render_html(r, email=True, link="http://graintime:8080/reports")
    assert "<svg" not in mail and "<style" not in mail                     # Outlook-safe
    assert "Open the full report in GrainTime" in mail and "Bbb" in mail
    assert "Median: 30 min" in reports.render_text(r) or "Median:" in reports.render_text(r)


def test_truck_export_explains_every_truck(data):
    with get_sessionmaker()() as db:
        rows = reports.truck_rows(db, params())
    why = {(r[0], r[8]): (r[9], r[10]) for r in rows}
    reasons = [r[10] for r in rows if r[9] == "no"]
    assert len(rows) == 9                                                   # 7 at AAA + 2 at BBB, none hidden
    assert sorted(reasons) == sorted(["voided", "over 4 h (probably left open)", "single weigh (stored tare)",
                                      "not weighed out yet"])
    assert why[("Aaa", 20.0)] == ("yes", None)
    x = reports.trucks_xlsx(rows, "test", None)
    assert x[:2] == b"PK"                                                   # an .xlsx (zip)
    c = reports.trucks_csv(rows).decode("utf-8-sig")
    assert c.splitlines()[0].startswith("Site,Site code,Ticket") and "voided" in c
