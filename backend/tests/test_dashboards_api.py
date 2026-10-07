from .conftest import SITE


def test_viewer_can_read_stats_but_not_admin(client, make_user):
    pw = make_user("viewer1", "viewer")
    client.post("/api/auth/login", json={"username": "viewer1", "password": pw})
    assert client.get("/api/stats/options").status_code == 200
    assert client.get("/api/stats/summary").status_code == 200
    assert client.get("/api/stats/trend?from=2026-10-01&to=2026-10-07").status_code == 200
    assert client.get("/api/admin/sites").status_code == 403


def test_stats_require_sign_in(client):
    assert client.get("/api/stats/summary").status_code == 401
    assert client.get("/api/dashboards").status_code == 401


def test_stats_validation_and_csv(admin_client):
    c = admin_client
    c.post("/api/admin/sites", json=SITE)
    assert c.get("/api/stats/summary?from=2026-10-08&to=2026-10-01").status_code == 422
    assert c.get("/api/stats/summary?direction=sideways").status_code == 422
    r = c.get("/api/stats/trend?from=2026-10-01&to=2026-10-03&format=csv")
    assert r.status_code == 200 and r.text.splitlines()[0] == "date,completed,median_min,p90_min"
    assert len(r.text.strip().splitlines()) == 4
    r = c.get("/api/stats/overview?format=csv")
    assert r.text.startswith("site,current_min")
    opts = c.get("/api/stats/options").json()
    assert opts["sites"][0]["code"] == "CELINA" and opts["thresholds"]["green_max_min"] == 20


def test_hidden_and_archived_sites_not_in_stats(admin_client):
    c = admin_client
    sid = c.post("/api/admin/sites", json=SITE).json()["id"]
    c.patch(f"/api/admin/sites/{sid}", json={"show_on_dashboard": False})
    assert c.get("/api/stats/options").json()["sites"] == []
    c.patch(f"/api/admin/sites/{sid}", json={"show_on_dashboard": True})
    c.post(f"/api/admin/sites/{sid}/archive")
    assert c.get("/api/stats/options").json()["sites"] == []


def test_dashboards_default_save_reset_delete_and_privacy(client, make_user):
    a_pw = make_user("alice", "viewer")
    make_user("bob", "viewer", "bob-password-123")
    client.post("/api/auth/login", json={"username": "alice", "password": a_pw})
    ds = client.get("/api/dashboards").json()
    assert len(ds) == 1 and ds[0]["name"] == "Overview" and len(ds[0]["widgets"]) >= 8
    did = ds[0]["id"]
    body = {"name": "Harvest", "filters": {"range": "custom", "date_from": "2026-09-01",
                                           "date_to": "2026-10-07", "direction": "received"},
            "widgets": [{"type": "trend", "size": "L", "settings": {"show_p90": False}},
                        {"type": "kpi", "size": "S", "settings": {"metric": "p90"}}]}
    saved = client.put(f"/api/dashboards/{did}", json=body).json()
    assert saved["name"] == "Harvest" and [w["type"] for w in saved["widgets"]] == ["trend", "kpi"]
    assert saved["widgets"][0]["id"]
    bad = {**body, "widgets": [{"type": "pie", "size": "S"}]}
    assert client.put(f"/api/dashboards/{did}", json=bad).status_code == 422
    new = client.post("/api/dashboards", json={"name": "Second", "widgets": []}).json()
    assert len(new["widgets"]) >= 8
    assert len(client.post(f"/api/dashboards/{did}/reset").json()["widgets"]) >= 8
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "bob", "password": "bob-password-123"})
    assert client.put(f"/api/dashboards/{did}", json=body).status_code == 404       # not Bob's
    assert client.delete(f"/api/dashboards/{did}").status_code == 404
    assert [d["name"] for d in client.get("/api/dashboards").json()] == ["Overview"]
