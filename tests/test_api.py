"""API-level regression tests.

These run against a throwaway database (see conftest.py) and never touch the
operator's real fielddata tree. They are intentionally black-box: the goal is
to pin the contract the UI depends on while the backend is refactored.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from darksec import config


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# --------------------------------------------------------------------- auth
def test_api_requires_authentication(anon):
    assert anon.get("/api/health").status_code == 401
    assert anon.get("/api/dashboard").status_code == 401


def test_token_bootstrap_sets_cookie(anon):
    resp = anon.get("/?token=test-token", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/ui/")
    assert anon.cookies.get("darksec_session") == "test-token"


def test_bad_token_is_rejected(anon):
    assert anon.get("/?token=nope").status_code == 403


def test_mutation_without_csrf_is_rejected(app):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.cookies.set("darksec_session", "test-token")
        resp = c.post("/api/records/tasks", json={"title": "csrf should fail"})
        assert resp.status_code == 403


def test_security_headers(client):
    resp = client.get("/api/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in resp.headers["content-security-policy"]


def test_foreign_host_is_rejected(client):
    resp = client.get("/api/health", headers={"Host": "evil.example"})
    assert resp.status_code == 400


# ------------------------------------------------------------------- status
def test_health_and_status(client):
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["app"] == "DarkSec"

    status = client.get("/api/status").json()
    for key in ("settings", "readiness", "library", "hardware", "system", "counts"):
        assert key in status
    assert status["readiness"]["source"] == "automated"


def test_dashboard_shape(client):
    data = client.get("/api/dashboard").json()
    for key in ("readiness", "almanac", "feeds", "tasks", "sitrep"):
        assert key in data
    assert 0 <= data["readiness"]["score"] <= 100


def test_readiness_weights_sum_to_100(client):
    readiness = client.get("/api/status").json()["readiness"]
    assert sum(c["weight"] for c in readiness["categories"]) == 100
    assert readiness["categories"][0]["score"] <= 100


def test_readiness_position_risk_scales_with_source(client):
    original = client.get("/api/settings").json()

    def risk_for(source):
        client.put("/api/settings", json={"position_source": source})
        categories = client.get("/api/status").json()["readiness"]["categories"]
        return next(c["risk"] for c in categories if c["category"] == "POSITION")

    try:
        assert risk_for("unverified") == 4
        assert risk_for("manual") == 2
        assert risk_for("gpsd") == 4  # no fix timestamp -> treated as stale
        assert risk_for("geocoded") == 2
        assert risk_for("gpsd") > risk_for("manual")
    finally:
        client.put("/api/settings", json={
            "position_source": original["position_source"],
            "position_fix_at": original.get("position_fix_at") or "",
        })


# ----------------------------------------------------------------- settings
def test_settings_round_trip_and_validation(client):
    original = client.get("/api/settings").json()
    updated = client.put("/api/settings", json={"callsign": "TESTWATCH"}).json()
    assert updated["callsign"] == "TESTWATCH"

    bad_key = client.put("/api/settings", json={"not_a_setting": 1})
    assert bad_key.status_code == 400

    bad_range = client.put("/api/settings", json={"lat": 999})
    assert bad_range.status_code == 400

    bad_tx = client.put("/api/settings", json={"sdr_tx_enabled": True})
    assert bad_tx.status_code == 400

    client.put("/api/settings", json={"callsign": original["callsign"]})


# ------------------------------------------------------------------ records
def test_record_crud(client):
    title = _unique("task")
    created = client.post("/api/records/tasks", json={"title": title, "priority": "PRIORITY"})
    assert created.status_code == 200
    row = created.json()["item"]
    assert row["title"] == title

    listed = client.get("/api/records/tasks", params={"q": title}).json()
    assert listed["count"] >= 1
    assert any(item["id"] == row["id"] for item in listed["items"])

    fetched = client.get(f"/api/records/tasks/{row['id']}").json()
    assert fetched["priority"] == "PRIORITY"

    patched = client.patch(f"/api/records/tasks/{row['id']}", json={"done": True}).json()
    assert patched["item"]["done"] == 1

    assert client.delete(f"/api/records/tasks/{row['id']}").json()["deleted"] == row["id"]
    assert client.get(f"/api/records/tasks/{row['id']}").status_code == 404


def test_record_validation_and_unknown_table(client):
    assert client.post("/api/records/tasks", json={}).status_code == 400
    assert client.get("/api/records/not_a_table").status_code == 404
    assert client.post("/api/records/waypoints", json={"name": "x"}).status_code == 400


def test_trip_validation(client):
    bad = client.post("/api/records/trip_plans", json={
        "name": "bad trip",
        "departure_at": "2026-01-02T00:00:00Z",
        "return_at": "2026-01-01T00:00:00Z",
    })
    assert bad.status_code == 400


# -------------------------------------------------------------------- calcs
def test_calculator_catalog_and_run(client):
    catalog = client.get("/api/calc").json()["catalog"]
    assert any(item["name"] == "path_heading" for item in catalog)

    result = client.post("/api/calc/path_heading", json={
        "lat1": 39.8283, "lon1": -98.5795, "lat2": 40.7128, "lon2": -74.0060,
    })
    assert result.status_code == 200
    assert result.json()["result"]
    assert client.post("/api/calc/nope", json={}).status_code == 404


# ------------------------------------------------------------------ library
def test_library_stats_and_empty_search(client):
    stats = client.get("/api/library/stats").json()
    assert "documents" in stats
    search = client.get("/api/library/search", params={"q": "zzzznotpresent"}).json()
    assert search.get("items") == []


# -------------------------------------------------------------------- vault
def test_vault_lifecycle(client):
    assert client.get("/api/vault/status").json()["configured"] is False

    assert client.post("/api/vault/setup", json={"passphrase": "short"}).status_code == 400
    setup = client.post("/api/vault/setup", json={"passphrase": "correct horse battery"})
    assert setup.status_code == 200
    assert setup.json()["unlocked"] is True

    client.post("/api/vault/lock")
    assert client.get("/api/vault/status").json()["unlocked"] is False
    assert client.post("/api/vault/unlock", json={"passphrase": "wrong passphrase"}).status_code == 400
    assert client.post("/api/vault/unlock", json={"passphrase": "correct horse battery"}).status_code == 200

    note = client.post("/api/notes", json={"title": "t", "body": "b", "tags": "x"})
    assert note.status_code == 200
    notes = client.get("/api/notes").json()
    assert notes["items"][0]["title"] == "t"

    client.post("/api/vault/lock")


# --------------------------------------------------------------- logistics
def test_export_and_backup(client):
    title = _unique("backup task")
    client.post("/api/records/tasks", json={"title": title})

    export = client.get("/api/logistics/export")
    assert export.status_code == 200
    assert export.headers["content-disposition"].endswith('darksec-export.json"')

    backup = client.post("/api/logistics/backup", json={"name": _unique("test-backup")}).json()
    assert backup["ok"] is True
    assert (config.BACKUP_DIR / backup["file"]).exists()

    listing = client.get("/api/logistics/backups").json()["items"]
    assert any(item["name"] == backup["file"] for item in listing)

    restore = client.post("/api/logistics/restore", json={"confirm": "RESTORE", "mode": "merge",
                                                         "data": {"tasks": [{"title": "restored"}]}})
    assert restore.status_code == 200
    assert restore.json()["records"] >= 1


def test_search_and_manual(client):
    assert client.get("/api/search", params={"q": "water"}).status_code == 200
    assert client.get("/api/search").status_code == 422
    index = client.get("/api/manual").json()
    assert index["docs"]
    doc = client.get(f"/api/manual/{index['docs'][0]['id']}")
    assert doc.status_code == 200 and doc.text


# ------------------------------------------------------------------- pizzint
def test_pizzint_shape(client):
    data = client.get("/api/pizzint").json()
    assert "source" in data
    assert "doughcon" in data
    assert isinstance(data.get("locations", []), list)


# ---------------------------------------------------------------------- lan
def test_lan_status_shape(client):
    data = client.get("/api/lan/status").json()
    assert "enabled" in data
    assert "ip" in data
