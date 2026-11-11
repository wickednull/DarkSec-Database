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
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
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


def test_recurring_task_rolls_forward(client):
    title = _unique("recurring")
    created = client.post("/api/records/tasks", json={
        "title": title, "due": "2026-01-01 08:00", "recurrence": "WEEKLY",
    }).json()["item"]
    assert created["recurrence"] == "WEEKLY"

    patched = client.patch(f"/api/records/tasks/{created['id']}", json={"done": True}).json()
    assert patched["item"]["done"] == 1
    assert patched["next_task_id"]

    following = client.get(f"/api/records/tasks/{patched['next_task_id']}").json()
    assert following["done"] == 0
    assert following["recurrence"] == "WEEKLY"
    assert following["due"] == "2026-01-08 08:00"
    assert following["title"] == title

    client.delete(f"/api/records/tasks/{created['id']}")
    client.delete(f"/api/records/tasks/{patched['next_task_id']}")


def test_medication_log_and_summary(client):
    callsign = _unique("patient")
    patient = client.post("/api/records/patients", json={"callsign": callsign}).json()["item"]
    med = client.post("/api/records/medication_log", json={
        "patient_id": patient["id"], "medication": "TESTDRUG", "dose": "1 tablet",
        "route": "PO", "administered_by": "operator", "indication": "test",
    }).json()["item"]
    assert med["medication"] == "TESTDRUG"

    listed = client.get(f"/api/medical/patients/{patient['id']}/medications").json()["items"]
    assert any(item["id"] == med["id"] for item in listed)

    summary = client.get("/api/medical/summary").json()
    assert "recent_medications" in summary
    assert any(item["id"] == med["id"] for item in summary["recent_medications"])

    client.delete(f"/api/records/medication_log/{med['id']}")
    client.delete(f"/api/records/patients/{patient['id']}")


def test_feed_update_clears_cache_on_source_change(client):
    feeds = client.get("/api/feeds").json()["feeds"]
    original = next(f for f in feeds if f["id"] == "bbc_world")
    updated = client.put("/api/feeds/bbc_world", json={
        "name": "BBC World (test)", "url": "https://example.invalid/rss.xml", "kind": "rss",
    })
    assert updated.status_code == 200
    assert updated.json()["source_changed"] is True

    after = next(f for f in client.get("/api/feeds").json()["feeds"] if f["id"] == "bbc_world")
    assert after["status"] == "NEVER"
    assert after["count"] == 0

    restored = client.put("/api/feeds/bbc_world", json={
        "name": original["name"], "url": original["url"], "kind": original["kind"],
    })
    assert restored.status_code == 200


def test_maintenance_due(client):
    task = client.post("/api/records/tasks", json={
        "title": _unique("maintenance"), "due": "2000-01-01",
    }).json()["item"]
    item = client.post("/api/records/inventory", json={
        "name": _unique("expiring"), "expires": "2000-01",
    }).json()["item"]

    out = client.get("/api/maintenance/due", params={"days": 30}).json()
    assert any(row["id"] == task["id"] and row["overdue"] for row in out["tasks"])
    assert any(row["id"] == item["id"] and row["expired"] for row in out["inventory"])

    client.delete(f"/api/records/tasks/{task['id']}")
    client.delete(f"/api/records/inventory/{item['id']}")


def test_weather_and_air_parsers_preserve_forecast(client):
    from darksec import feeds

    wx = feeds._parse_weather({
        "latitude": 1.0, "longitude": 2.0,
        "current": {"temperature_2m": 10, "weather_code": 1},
        "daily": {
            "time": ["2026-11-08"],
            "temperature_2m_max": [12.5],
            "temperature_2m_min": [4.0],
            "precipitation_sum": [0.4],
            "weather_code": [3],
        },
    })[0]
    assert wx["forecast"][0]["temp_max_c"] == 12.5
    assert wx["forecast"][0]["condition"] == "overcast"

    aq = feeds._parse_air_quality({
        "latitude": 1.0, "longitude": 2.0,
        "current": {"us_aqi": 42, "time": "2026-11-08T00:00:00Z"},
        "hourly": {"time": ["2026-11-08T00:00", "2026-11-08T01:00"], "us_aqi": [42, 55]},
    })[0]
    assert aq["trend"] == [
        {"time": "2026-11-08T00:00", "aqi": 42},
        {"time": "2026-11-08T01:00", "aqi": 55},
    ]


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


# ----------------------------------------------------------------------- map
def test_map_intel_includes_all_cached_layers(client):
    import json
    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    cctv_payload = {
        "items": [{
            "id": "cam-global-1", "kind": "cctv", "title": "CCTV A",
            "lat": 51.5074, "lon": -0.1278, "time": "2026-01-01T00:00:00Z",
            "source": "OSM", "camera_id": "cam-global-1", "surveillance_type": "camera",
        }],
        "fetched_at": "2026-01-01T00:00:00Z",
    }
    cable_payload = {
        "items": [{
            "id": "cp-global-1", "kind": "cables", "title": "Cable Landing X",
            "lat": -33.8688, "lon": 151.2093, "source": "cables",
        }],
    }
    db.execute("update feeds set payload=?, status='ERROR', fetched_at=? where id='public_cctv'",
               (json.dumps(cctv_payload), "2026-01-01T00:00:00Z"))
    db.execute("update feeds set payload=?, status='OK', fetched_at=? where id='infra_cables'",
               (json.dumps(cable_payload), "2026-01-01T00:00:00Z"))

    data = client.get("/api/map/intel").json()
    assert isinstance(data.get("cctv"), list)
    assert any(c.get("title") == "CCTV A" and c.get("lat") == 51.5074 for c in data["cctv"])
    assert isinstance(data.get("cables"), list)
    assert any(c.get("title") == "Cable Landing X" and c.get("lon") == 151.2093 for c in data["cables"])
    summary = data.get("summary") or {}
    assert summary.get("cctv", 0) >= 1
    assert summary.get("cables", 0) >= 1
    # Cached layers are returned globally (not filtered to operator proximity).
    assert any(abs(float(c.get("lon") or 0) - (-0.1278)) < 0.001 for c in data["cctv"])

# ---------------------------------------------------------------------- cctv
def test_cctv_catalog_endpoints_shape(client):
    status = client.get("/api/cctv/catalog/status")
    assert status.status_code == 200
    data = status.json()
    assert "frame_cache" in data
    assert "candidate_files" in data or "pack_stats" in data

    packs = client.get("/api/cctv/catalog/packs")
    assert packs.status_code == 200
    pdata = packs.json()
    assert "packs" in pdata
    assert "disabled" in pdata


def test_cctv_stream_info_registered_only(client):
    import json
    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    payload = {
        "items": [{
            "id": "stream-cam-1", "kind": "cctv", "title": "Test HLS Cam",
            "lat": 40.0, "lon": -74.0, "feed_type": "hls",
            "stream_url": "https://example.test/live/cam.m3u8",
            "snapshot_url": "https://example.test/snap.jpg",
            "source": "test", "credit": "Test Credit", "license": "Test License",
        }, {
            "id": "img-cam-1", "kind": "cctv", "title": "Test Image Cam",
            "lat": 41.0, "lon": -73.0, "feed_type": "image",
            "snapshot_url": "https://example.test/image.jpg",
            "source": "test",
        }],
    }
    db.execute("update feeds set payload=?, status='OK', fetched_at=? where id='cctv_catalog'",
               (json.dumps(payload), "2026-01-01T00:00:00Z"))

    r = client.get("/api/cctv/stream/info", params={"id": "cctv_catalog:stream-cam-1"})
    assert r.status_code == 200
    info = r.json()
    assert info["ok"] is True
    assert info["feed_type"] == "hls"
    assert "/api/cctv/stream/manifest" in (info.get("proxied_url") or "")
    assert info.get("credit") == "Test Credit"

    r2 = client.get("/api/cctv/stream/info", params={"id": "cctv_catalog:img-cam-1"})
    assert r2.status_code == 200
    assert r2.json().get("proxied_url") is None

    missing = client.get("/api/cctv/stream/info", params={"id": "cctv_catalog:nope"})
    assert missing.status_code == 404


def test_cctv_frame_registered_only(client):
    import json
    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    payload = {"items": [{"id": "nosnap-1", "kind": "cctv", "title": "No Snap", "lat": 1, "lon": 2, "source": "test"}]}
    db.execute("update feeds set payload=?, status='OK' where id='cctv_catalog'", (json.dumps(payload),))
    r = client.get("/api/cctv/frame", params={"id": "cctv_catalog:nosnap-1"})
    assert r.status_code == 404  # no registered snapshot


def test_cctv_stream_manifest_rewrites_registered_hls(client, monkeypatch):
    import json
    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    payload = {"items": [{"id": "hls-1", "kind": "cctv", "title": "HLS", "lat": 1, "lon": 2,
                          "feed_type": "hls", "stream_url": "https://cam.example.test/path/live.m3u8", "source": "test"}]}
    db.execute("update feeds set payload=?, status='OK' where id='cctv_catalog'", (json.dumps(payload),))

    manifest = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\nlow.m3u8\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\"\nseg1.ts\nhttps://evil.example/other.ts\n"

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/vnd.apple.mpegurl"}
        text = manifest
        def raise_for_status(self): return None
        def close(self): return None
    monkeypatch.setattr("darksec.server.requests.get", lambda *a, **k: FakeResp())

    r = client.get("/api/cctv/stream/manifest", params={"id": "cctv_catalog:hls-1"})
    assert r.status_code == 200
    body = r.text
    assert "/api/cctv/stream/resource" in body
    assert "https://cam.example.test" not in body  # rewritten
    # cross-host absolute URI should remain unrewritten/preserved (safety: not proxied as same-host)
    assert "https://evil.example/other.ts" in body


def test_cctv_stream_resource_blocks_cross_host(client, monkeypatch):
    import base64, json
    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    payload = {"items": [{"id": "hls-2", "kind": "cctv", "title": "HLS", "lat": 1, "lon": 2,
                          "feed_type": "hls", "stream_url": "https://cam.example.test/live.m3u8", "source": "test"}]}
    db.execute("update feeds set payload=?, status='OK' where id='cctv_catalog'", (json.dumps(payload),))
    evil = base64.urlsafe_b64encode(b"https://evil.example/seg.ts").decode().rstrip("=")
    r = client.get("/api/cctv/stream/resource", params={"id": "cctv_catalog:hls-2", "u": evil})
    assert r.status_code == 400

def test_cctv_stream_allowlist_rewrites_configured_hosts(client, monkeypatch):
    import json
    from darksec import config
    from darksec.db import Database
    import darksec.server as srv

    db = Database(config.DB_PATH)
    payload = {"items": [{"id": "hls-allow", "kind": "cctv", "title": "HLS", "lat": 1, "lon": 2,
                          "feed_type": "hls", "stream_url": "https://cam.example.test/live.m3u8", "source": "test"}]}
    db.execute("update feeds set payload=?, status='OK' where id='cctv_catalog'", (json.dumps(payload),))
    manifest = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://cdn.allowed.test/key.key"\nhttps://cdn.allowed.test/seg1.ts\nseg2.ts\n'

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/vnd.apple.mpegurl"}
        text = manifest
        def raise_for_status(self): return None
    monkeypatch.setattr(srv.requests, "get", lambda *a, **k: FakeResp())

    # Without allowlist, cross-host CDN URIs remain unrewritten (safe default)
    client.put("/api/settings", json={"cctv_stream_host_allowlist": "[]"})
    r = client.get("/api/cctv/stream/manifest", params={"id": "cctv_catalog:hls-allow"})
    assert r.status_code == 200
    assert "https://cdn.allowed.test/key.key" in r.text
    assert "https://cdn.allowed.test/seg1.ts" in r.text
    assert "/api/cctv/stream/resource" in r.text  # same-host seg2.ts rewritten

    # With allowlist, configured CDN host is rewritten/proxied
    db.set_setting("cctv_stream_host_allowlist", json.dumps(["cdn.allowed.test"]))
    r = client.get("/api/cctv/stream/manifest", params={"id": "cctv_catalog:hls-allow"})
    assert r.status_code == 200
    assert "/api/cctv/stream/resource" in r.text
    assert "https://cdn.allowed.test/key.key" not in r.text
    assert "https://cdn.allowed.test/seg1.ts" not in r.text
    # cleanup
    db.set_setting("cctv_stream_host_allowlist", "[]")


# ------------------------------------------------------------ feed freshness
def test_safecast_parser_keeps_newest_measurements():
    from darksec import feeds

    items = feeds._parse_safecast([
        {"id": 1, "value": 10, "unit": "cpm", "captured_at": "2018-09-01T16:29:55.000Z",
         "latitude": 1, "longitude": 2},
        {"id": 2, "value": 11, "unit": "cpm", "captured_at": "2026-11-10T23:00:00.000Z",
         "latitude": 1, "longitude": 2},
        {"id": 3, "value": 12, "unit": "cpm", "captured_at": "2025-01-01T00:00:00.000Z",
         "latitude": 1, "longitude": 2},
    ])
    assert [item["time"] for item in items] == [
        "2026-11-10T23:00:00.000Z",
        "2025-01-01T00:00:00.000Z",
        "2018-09-01T16:29:55.000Z",
    ]


def test_who_news_api_parser():
    from darksec import feeds

    items = feeds._parse_who_news({"value": [{
        "Title": "WHO update",
        "ItemDefaultUrl": "/10-11-2026-who-update",
        "PublicationDateAndTime": "2026-11-10T21:00:54Z",
        "NewsType": "News",
    }]})
    assert items[0]["title"] == "WHO update"
    assert items[0]["url"] == "https://www.who.int/10-11-2026-who-update"
    assert items[0]["time"].startswith("2026-11-10T21:00:54")
    assert items[0]["source"] == "WHO"


def test_stale_thresholds_fit_source_cadence(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from darksec import feeds
    from darksec.db import Database

    db = Database(str(tmp_path / "freshness.db"))
    now = datetime.now(timezone.utc)
    quake_time = (now - timedelta(hours=8)).isoformat()
    db.execute(
        "UPDATE feeds SET status='OK', fetched_at=?, payload=? WHERE id='usgs_quakes'",
        (now.isoformat(), json.dumps({"items": [{"title": "M 3.0", "time": quake_time}]})),
    )
    # CCTV timestamps are capture/fetch metadata, not headline freshness.
    db.execute(
        "UPDATE feeds SET status='OK', fetched_at=?, payload=? WHERE id='public_cctv'",
        (now.isoformat(), json.dumps({"items": [
            {"title": "Camera", "time": (now - timedelta(days=60)).isoformat(), "kind": "cctv"}
        ]})),
    )
    rows = {f["id"]: f for f in feeds.list_feeds(db)}
    # An 8h-old quake in a rolling 24h feed is normal, not stale.
    assert rows["usgs_quakes"]["status"] == "OK"
    assert rows["public_cctv"]["status"] == "OK"


def test_overpass_get_fallback_on_406(monkeypatch):
    import requests

    from darksec import feeds

    def fake_post(*args, **kwargs):
        class Rejected:
            status_code = 406
            def raise_for_status(self):
                raise requests.HTTPError("406 Not Acceptable", response=self)
        return Rejected()

    def fake_get(*args, **kwargs):
        class Ok:
            status_code = 200
            text = '{"elements": []}'
            def raise_for_status(self): return None
            def json(self): return {"elements": []}
        return Ok()

    monkeypatch.setattr(feeds.requests, "post", fake_post)
    monkeypatch.setattr(feeds.requests, "get", fake_get)
    assert feeds._overpass_fetch_json("q", ["https://mirror.test/api/interpreter"], budget=10) == {"elements": []}


def test_osm_camera_page_is_not_a_snapshot(client):
    import json

    from darksec import config
    from darksec.db import Database

    db = Database(config.DB_PATH)
    payload = {"items": [{
        "id": "osm-page-1", "kind": "cctv", "title": "OSM camera", "lat": 1, "lon": 2,
        "url": "https://www.openstreetmap.org/node/123", "source": "OpenStreetMap (Overpass, ODbL)",
    }]}
    db.execute("update feeds set payload=?, status='OK' where id='cctv_catalog'", (json.dumps(payload),))
    # The OSM node page must not be fetched as an image snapshot.
    r = client.get("/api/cctv/frame", params={"id": "cctv_catalog:osm-page-1"})
    assert r.status_code == 404


def test_assistant_chat_log_saves_to_vault(client):
    from darksec import config
    from darksec.db import Database

    marker = _unique("secret-chat-body")
    chat_body = ("operator: check the water filter\nDelamain: noted.\n" * 400) + marker
    status = client.get("/api/vault/status").json()
    if not status.get("configured"):
        client.post("/api/vault/setup", json={"passphrase": "correct horse battery"})
    else:
        client.post("/api/vault/unlock", json={"passphrase": "correct horse battery"})
    client.post("/api/vault/lock")

    locked = client.post("/api/assistant/vault-note", json={"title": "Chat", "content": chat_body})
    assert locked.status_code == 403

    client.post("/api/vault/unlock", json={"passphrase": "correct horse battery"})
    out = client.post("/api/assistant/vault-note", json={
        "title": "Chat archive", "content": chat_body, "tags": "delamain,chat-log",
    })
    assert out.status_code == 200
    note_id = out.json()["id"]

    notes = client.get("/api/notes").json()["items"]
    item = next(n for n in notes if n["id"] == note_id)
    assert item["title"] == "Chat archive"
    assert item["body"] == chat_body

    db = Database(config.DB_PATH)
    stored = db.one("SELECT cipher FROM notes WHERE id=?", (note_id,))
    assert marker not in stored["cipher"]
    leaked = db.one("SELECT count(*) c FROM log_entries WHERE body LIKE ?", (f"%{marker}%",))
    assert leaked["c"] == 0
    client.post("/api/vault/lock")
