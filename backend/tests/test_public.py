"""The public page: operating hours, the publisher, the isolated role and the service."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError

from graintime.common import hours
from graintime.common.db import get_sessionmaker
from graintime.common.models import PublicSiteStatus, Site, SiteCollectorState
from graintime.common.publish import publish

from .conftest import TEST_DB
from .test_metrics import NOW, load, t

WEEK = [{"open": "07:00", "close": "17:00"}] * 5 + [{"open": "08:00", "close": "12:00"}, None]
HOURS = {"weekly": WEEK, "overrides": []}


# --------------------------------------------------------------------------- #
# Operating hours (pure)
# --------------------------------------------------------------------------- #

def at(local: str) -> datetime:
    """A local Eastern time in Oct 2026 (EDT, UTC-4)."""
    return datetime.fromisoformat(local).replace(tzinfo=timezone(timedelta(hours=-4)))


def test_hours_open_and_closed_texts():
    # Wed 2026-10-07
    assert hours.status(HOURS, at("2026-10-07T12:00")) == {"open": True, "text": "Open until 5 PM"}
    assert hours.status(HOURS, at("2026-10-07T06:30"))["text"] == "Closed · opens 7 AM"
    assert hours.status(HOURS, at("2026-10-07T17:00"))["text"] == "Closed · opens 7 AM tomorrow"
    # Saturday afternoon -> closed Sunday -> opens Monday
    assert hours.status(HOURS, at("2026-10-10T13:00"))["text"] == "Closed · opens 7 AM Mon"
    assert hours.status(None, NOW) == {"open": None, "text": None}
    assert hours.status({"weekly": [None] * 7, "overrides": []}, NOW) == {"open": False, "text": "Closed"}


def test_hours_harvest_override_wins_inside_its_dates():
    harvest = {"label": "Harvest", "date_from": "2026-09-15", "date_to": "2026-11-30",
               "weekly": [{"open": "06:00", "close": "22:30"}] * 7}
    h = {"weekly": WEEK, "overrides": [harvest]}
    assert hours.status(h, at("2026-10-11T20:00")) == {"open": True, "text": "Open until 10:30 PM"}
    assert hours.status(h, at("2026-12-06T20:00"))["open"] is False      # Sunday after harvest


# --------------------------------------------------------------------------- #
# Publisher
# --------------------------------------------------------------------------- #

def public_site(code, show=True, archived=False, hrs=None, polled=NOW):
    with get_sessionmaker()() as db:
        s = Site(name=code.title() + " Elevator", code=code, address="1 Main St", map_url="https://maps.example/x",
                 host="h", port=1433, database_name="d", username="u", password_encrypted=b"x",
                 encrypt="yes", trust_server_certificate=True, polling_enabled=True,
                 show_on_dashboard=True, show_on_public=show, hours=hrs,
                 archived_at=NOW if archived else None)
        db.add(s)
        db.flush()
        if polled:
            db.add(SiteCollectorState(site_id=s.id, last_success_at=polled))
        db.commit()
        return s.id


def rows():
    with get_sessionmaker()() as db:
        return {r.code: r for r in db.query(PublicSiteStatus).all()}


def test_publish_aggregates_with_minimum_trucks(db_clean):
    busy = public_site("busy", hrs=HOURS)
    light = public_site("light")
    public_site("quiet")
    public_site("hidden", show=False)
    public_site("gone", archived=True)
    load(busy, [t(10, 20), t(20, 30), t(30, 40), t(in_ago_min=15, status="open"), t(in_ago_min=30, status="open")])
    load(light, [t(10, 25), t(in_ago_min=5, status="open")])
    with get_sessionmaker()() as db:
        assert publish(db, NOW) == 3
    r = rows()
    assert set(r) == {"busy", "light", "quiet"}          # hidden and archived sites never published
    assert (r["busy"].traffic, r["busy"].minutes, r["busy"].level, r["busy"].trucks_on_site) == ("ok", 30, "warning", 2)
    assert r["busy"].open_state is True and r["busy"].hours_text == "Open until 5 PM"
    # one truck is below public_min_trucks (3): no number from a single load
    assert (r["light"].traffic, r["light"].minutes, r["light"].trucks_on_site) == ("light", None, 1)
    assert (r["quiet"].traffic, r["quiet"].minutes, r["quiet"].open_state) == ("none", None, None)
    assert r["busy"].data_as_of == NOW and r["busy"].stale_after_min == 15

    # Turning the public switch off removes the site on the next publish.
    with get_sessionmaker()() as db:
        db.get(Site, light).show_on_public = False
        db.commit()
        publish(db, NOW)
    assert set(rows()) == {"busy", "quiet"}


def test_public_table_has_no_ticket_level_columns():
    cols = set(PublicSiteStatus.__table__.columns.keys())
    assert not cols & {"ticket_number", "source_ticket_id", "inbound_at", "outbound_at", "duration_s",
                       "site_id", "host", "username", "commodity"}


# --------------------------------------------------------------------------- #
# The public database role: one table, nothing else
# --------------------------------------------------------------------------- #

PW = "Public-Role-Test-Pw-1"


@pytest.fixture
def public_url(db_clean):
    from graintime.common.public_role import enable
    enable(PW)
    return make_url(TEST_DB).set(username="graintime_public", password=PW)


def test_public_role_reads_only_the_aggregates_table(public_url):
    eng = create_engine(public_url)
    try:
        with eng.connect() as c:
            assert c.execute(text("SELECT count(*) FROM public_site_status")).scalar() == 0
        for table in ("tickets", "sites", "users", "sessions", "app_settings", "audit_log",
                      "collector_jobs", "site_collector_state", "mapping_profiles", "dashboards"):
            with eng.connect() as c, pytest.raises(ProgrammingError, match="permission denied"):
                c.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
        with eng.connect() as c, pytest.raises(ProgrammingError, match="permission denied"):
            c.execute(text("DELETE FROM public_site_status"))
    finally:
        eng.dispose()


# --------------------------------------------------------------------------- #
# The public service
# --------------------------------------------------------------------------- #

@pytest.fixture
def pub(public_url, monkeypatch):
    from fastapi.testclient import TestClient

    from graintime.public import app as public_app
    monkeypatch.setenv("GRAINTIME_PUBLIC_DATABASE_URL", public_url.render_as_string(hide_password=False))
    public_app.snapshot.rows, public_app.snapshot.loaded = None, 0.0
    public_app.limiter.buckets.clear()
    with TestClient(public_app.app) as c:
        yield c, public_app


def fresh(public_app):
    public_app.snapshot.loaded = 0.0


def test_public_page_and_feed(pub):
    c, mod = pub
    now = datetime.now(timezone.utc)
    public_site("busy", hrs=HOURS, polled=now)
    public_site("light", polled=now)
    public_site("old", polled=now - timedelta(hours=2))
    with get_sessionmaker()() as db:
        publish(db, now)
        # pretend busy has a real number (traffic computed from live data is tested above)
        r = db.get(PublicSiteStatus, "busy")
        r.traffic, r.minutes, r.level, r.trucks_on_site = "ok", 27, "warning", 4
        db.get(PublicSiteStatus, "old").trucks_on_site = 9
        db.commit()

    feed = c.get("/feed.json")
    assert feed.status_code == 200 and feed.headers["access-control-allow-origin"] == "*"
    assert "max-age" in feed.headers["cache-control"]
    sites = {s["code"]: s for s in feed.json()["sites"]}
    assert sites["busy"]["status"] == "ok" and sites["busy"]["time_on_site_min"] == 27
    assert sites["busy"]["trucks_on_site"] == 4 and sites["busy"]["level"] == "warning"
    assert sites["light"]["status"] == "no_recent_trucks" and sites["light"]["time_on_site_min"] is None
    # stale data is never shown as current, not even the truck count
    assert sites["old"]["status"] == "stale" and sites["old"]["trucks_on_site"] is None
    assert set(sites["busy"]) == {"code", "name", "address", "map_url", "open", "hours", "status",
                                  "time_on_site_min", "level", "trucks_on_site", "last_updated"}

    page = c.get("/")
    assert page.status_code == 200
    body = page.text
    assert "Busy Elevator" in body and "27" in body and "Moderate" in body
    assert "Data delayed" in body and "No recent trucks" in body
    assert "inbound scale" in body                      # the known limitation is stated
    assert "default-src 'none'" in page.headers["content-security-policy"]


def test_public_page_escapes_names_and_drops_bad_links(pub):
    c, mod = pub
    sid = public_site("xss", polled=datetime.now(timezone.utc))
    with get_sessionmaker()() as db:
        s = db.get(Site, sid)
        s.name, s.map_url = "<script>alert(1)</script>", "javascript:alert(1)"
        db.commit()
        publish(db)
    body = c.get("/").text
    assert "<script>alert" not in body and "&lt;script&gt;" in body and "javascript:" not in body


def test_public_service_rate_limits_and_rejects_writes(pub):
    c, mod = pub
    assert c.post("/feed.json").status_code == 405
    codes = [c.get("/feed.json").status_code for _ in range(mod.RATE_BURST + 5)]
    assert codes[0] == 200 and 429 in codes
    assert c.get("/healthz").status_code == 200        # health checks are not limited
    for path in ("/docs", "/openapi.json", "/api/stats/overview"):
        assert c.get(path, headers={"x-forwarded-for": "203.0.113.9"}).status_code in (404, 429)


def test_public_service_unavailable_without_database(monkeypatch):
    from fastapi.testclient import TestClient

    from graintime.public import app as public_app
    monkeypatch.setenv("GRAINTIME_PUBLIC_DATABASE_URL", "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=1")
    public_app.snapshot.rows, public_app.snapshot.loaded = None, 0.0
    public_app.limiter.buckets.clear()
    with TestClient(public_app.app) as c:
        r = c.get("/")
        assert r.status_code == 503 and "temporarily unavailable" in r.text
        assert c.get("/feed.json").status_code == 503


def test_client_key_trusts_forwarded_for_only_from_private_peers():
    from types import SimpleNamespace

    from graintime.public.app import client_key

    def req(peer, xff=None):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})
    assert client_key(req("172.18.0.5", "1.2.3.4, 198.51.100.7")) == "198.51.100.7"
    assert client_key(req("8.8.4.4", "10.0.0.1")) == "8.8.4.4"     # spoofing from the internet ignored
    assert client_key(req("10.0.0.2", "not-an-ip")) == "10.0.0.2"


# --------------------------------------------------------------------------- #
# Hours through the admin api
# --------------------------------------------------------------------------- #

def test_admin_sets_and_clears_site_hours(admin_client):
    from .conftest import SITE
    sid = admin_client.post("/api/admin/sites", json=SITE).json()["id"]
    harvest = {"label": "Harvest", "date_from": "2026-09-15", "date_to": "2026-11-30",
               "weekly": [{"open": "06:00", "close": "22:00"}] * 7}
    r = admin_client.patch(f"/api/admin/sites/{sid}", json={"hours": {"weekly": WEEK, "overrides": [harvest]}})
    assert r.status_code == 200, r.text
    assert r.json()["hours"]["weekly"][6] is None and r.json()["hours"]["overrides"][0]["label"] == "Harvest"
    bad = admin_client.patch(f"/api/admin/sites/{sid}", json={"hours": {"weekly": [{"open": "18:00", "close": "07:00"}] * 7}})
    assert bad.status_code == 422
    bad = admin_client.patch(f"/api/admin/sites/{sid}", json={"hours": {"weekly": WEEK[:6]}})
    assert bad.status_code == 422
    assert admin_client.patch(f"/api/admin/sites/{sid}", json={"hours": None}).json()["hours"] is None
