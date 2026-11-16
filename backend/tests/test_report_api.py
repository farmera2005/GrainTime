"""Reports over the api, email settings, and scheduled reports."""
from datetime import date, datetime, timezone

import pytest

from graintime.common import report_schedule as rs
from graintime.common.db import get_sessionmaker
from graintime.common.models import ReportRun, ReportSchedule

from .test_mailer import smtp_server  # noqa: F401  (fixture)
from .test_reports import data  # noqa: F401  (fixture)


# --------------------------------------------------------------------------- #
# Reports for every signed-in user
# --------------------------------------------------------------------------- #

def test_viewer_gets_reports_but_not_settings(client, make_user, data):  # noqa: F811
    assert client.get("/api/reports/period").status_code == 401
    pw = make_user("viewer1", "viewer")
    client.post("/api/auth/login", json={"username": "viewer1", "password": pw})
    r = client.get("/api/reports/period?from=2026-10-07&to=2026-10-07&compare=previous")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "Aaa" in r.text
    j = client.get("/api/reports/period?from=2026-10-07&to=2026-10-07&format=json").json()
    assert {s["code"] for s in j["per_site"]} == {"AAA", "BBB"}
    x = client.get("/api/reports/trucks?from=2026-10-07&to=2026-10-07")
    assert x.status_code == 200 and x.content[:2] == b"PK" and "attachment" in x.headers["content-disposition"]
    c = client.get("/api/reports/trucks?from=2026-10-07&to=2026-10-07&format=csv")
    assert "voided" in c.content.decode("utf-8-sig")
    assert client.get("/api/reports/period?from=2026-10-08&to=2026-10-07").status_code == 422
    assert client.get("/api/settings/email").status_code == 403
    assert client.get("/api/admin/report-schedules").status_code == 403


# --------------------------------------------------------------------------- #
# Email settings
# --------------------------------------------------------------------------- #

GRAPH = {"method": "graph", "from_address": "graintime@mercerlandmark.com",
         "tenant_id": "11111111-2222-3333-4444-555555555555", "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
         "client_secret": "Very-Secret-Value", "link_base_url": "http://graintime:8080/"}


def test_email_settings_are_write_only_and_audited(admin_client, caplog):
    r = admin_client.put("/api/settings/email", json=GRAPH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_client_secret"] and body["configured"] and "Very-Secret-Value" not in r.text
    assert body["link_base_url"] == "http://graintime:8080"
    again = admin_client.put("/api/settings/email", json={**GRAPH, "client_secret": ""}).json()
    assert again["has_client_secret"] is True                         # blank keeps the saved one
    from graintime.common.models import AppSetting, AuditLog
    with get_sessionmaker()() as db:
        assert "Very-Secret-Value" not in str(db.get(AppSetting, "email").value)
        a = [x for x in db.query(AuditLog).all() if x.action == "settings.email_updated"][0]
        assert a.new_value["client_secret"] == "(changed)"
    assert "Very-Secret-Value" not in caplog.text


@pytest.mark.parametrize("patch,word", [({"from_address": "nope"}, "sending mailbox"),
                                        ({"tenant_id": "x y"}, "GUID"),
                                        ({"link_base_url": "graintime"}, "address"),
                                        ({"ca_cert_pem": "hello"}, "PEM")])
def test_email_settings_validation(admin_client, patch, word):
    r = admin_client.put("/api/settings/email", json={**GRAPH, **patch})
    assert r.status_code == 422 and word in r.text


def test_test_email_uses_the_form_without_saving(admin_client, smtp_server):  # noqa: F811
    port, h = smtp_server
    form = {"method": "smtp", "from_address": "graintime@mercerlandmark.com", "smtp_host": "127.0.0.1",
            "smtp_port": port, "smtp_security": "none"}
    r = admin_client.post("/api/settings/email/test", json={"settings": form, "to": "it@mercerlandmark.com"}).json()
    assert r["ok"] is True and h.messages[0].rcpt_tos == ["it@mercerlandmark.com"]
    assert admin_client.get("/api/settings/email").json()["configured"] is False      # not saved
    bad = admin_client.post("/api/settings/email/test", json={"settings": {**form, "smtp_port": 1},
                                                               "to": "it@mercerlandmark.com"}).json()
    assert bad["ok"] is False and bad["code"] == "unreachable"


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #

def sched(**kw):
    s = ReportSchedule(name="Daily truck times", frequency="daily", send_time="06:30", period="yesterday",
                       compare="previous", site_ids=[], direction="received", recipients=["gm@mercerlandmark.com"],
                       enabled=True, include_trucks=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest.mark.parametrize("period,run_day,expected", [
    ("yesterday", date(2026, 11, 16), (date(2026, 11, 15), date(2026, 11, 15))),
    ("last_7_days", date(2026, 11, 16), (date(2026, 11, 9), date(2026, 11, 15))),
    ("last_week", date(2026, 11, 18), (date(2026, 11, 9), date(2026, 11, 15))),       # Wed -> previous Mon-Sun
    ("month_to_date", date(2026, 11, 16), (date(2026, 11, 1), date(2026, 11, 15))),
    ("month_to_date", date(2026, 12, 1), (date(2026, 11, 1), date(2026, 11, 30))),
    ("last_month", date(2026, 1, 5), (date(2025, 12, 1), date(2025, 12, 31))),
    ("season_to_date", date(2026, 11, 16), (date(2026, 9, 1), date(2026, 11, 15))),
    ("season_to_date", date(2026, 3, 2), (date(2025, 9, 1), date(2026, 3, 1))),
])
def test_periods(period, run_day, expected):
    assert rs.period_range(period, run_day) == expected


def test_next_run_follows_eastern_time_across_the_clock_change():
    s = sched()
    # Oct 31 (EDT, UTC-4) then Nov 1 after fall-back (EST, UTC-5): 06:30 local both days
    first = rs.next_run(s, datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc))
    assert first == datetime(2026, 10, 31, 10, 30, tzinfo=timezone.utc)
    assert rs.next_run(s, first) == datetime(2026, 11, 1, 11, 30, tzinfo=timezone.utc)
    weekly = sched(frequency="weekly", weekday=0)                       # Mondays
    assert rs.next_run(weekly, first).astimezone(rs.SITE_TZ).weekday() == 0
    monthly = sched(frequency="monthly", month_day=1, send_time="07:00")
    assert rs.next_run(monthly, first) == datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_schedule_api_crud_and_validation(admin_client):
    body = {"name": "Weekly sites", "frequency": "weekly", "weekday": 0, "send_time": "07:15", "period": "last_week",
            "recipients": ["gm@mercerlandmark.com; ops@mercerlandmark.com", "GM@mercerlandmark.com"],
            "include_trucks": True}
    r = admin_client.post("/api/admin/report-schedules", json=body)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["recipients"] == ["gm@mercerlandmark.com", "ops@mercerlandmark.com"] and s["next_run_at"]
    assert s["period_label"] == "Last week (Mon–Sun)" and s["next_period_label"]
    assert admin_client.post("/api/admin/report-schedules", json={**body, "recipients": ["x"]}).status_code == 422
    assert admin_client.post("/api/admin/report-schedules", json={**body, "send_time": "25:00"}).status_code == 422
    upd = admin_client.put(f"/api/admin/report-schedules/{s['id']}", json={**body, "send_time": "05:00"}).json()
    assert upd["send_time"] == "05:00"
    assert admin_client.post(f"/api/admin/report-schedules/{s['id']}/send-now").status_code == 409   # no email yet
    admin_client.put("/api/settings/email", json=GRAPH)
    assert admin_client.post(f"/api/admin/report-schedules/{s['id']}/send-now").json()["run_requested"] is True
    lst = admin_client.get("/api/admin/report-schedules").json()
    assert lst["email_configured"] and len(lst["schedules"]) == 1
    assert admin_client.delete(f"/api/admin/report-schedules/{s['id']}").json() == {"deleted": True}


def test_run_due_sends_due_and_requested_reports_and_records_failures(data):  # noqa: F811
    sent = []

    def fake_send(cfg, to, subject, html, text, attachments=None, opener=None):
        sent.append({"to": to, "subject": subject, "html": html, "attachments": attachments or []})
    Session = get_sessionmaker()
    with Session() as db:
        due = sched(next_run_at=datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc), include_trucks=True)
        later = sched(name="Later", next_run_at=datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc))
        db.add_all([due, later])
        db.commit()
        due_id, later_id = due.id, later.id
    now = datetime(2026, 10, 8, 10, 31, tzinfo=timezone.utc)
    assert rs.run_due(Session, send=fake_send, now=now) == 1
    msg = sent[0]
    assert msg["subject"] == "Daily truck times: Wed Oct 7, 2026" and msg["to"] == ["gm@mercerlandmark.com"]
    assert "<svg" not in msg["html"] and msg["attachments"][0].filename.endswith(".xlsx")
    with Session() as db:
        s = db.get(ReportSchedule, due_id)
        assert s.last_status == "sent" and s.next_run_at == datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)
        run = db.query(ReportRun).filter_by(schedule_id=due_id).one()
        assert (run.status, run.trigger, run.period_from) == ("sent", "schedule", date(2026, 10, 7))
        db.get(ReportSchedule, later_id).run_requested = True              # "Send now" on the other one
        db.commit()

    def failing(*a, **k):
        from graintime.common.mailer import MailError
        raise MailError("graph_forbidden", "Microsoft 365 refused to send")
    assert rs.run_due(Session, send=failing, now=now) == 1
    with Session() as db:
        s = db.get(ReportSchedule, later_id)
        assert s.last_status == "failed" and s.run_requested is False
        assert s.next_run_at == datetime(2026, 10, 9, 10, 30, tzinfo=timezone.utc)   # manual runs keep the schedule
        run = db.query(ReportRun).filter_by(schedule_id=later_id).one()
        assert run.trigger == "manual" and "refused" in run.error
    assert rs.run_due(Session, send=fake_send, now=now) == 0                         # nothing else due
