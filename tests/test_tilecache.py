"""Offline tile cache: source consistency, ancestor overzoom and repair."""
from __future__ import annotations

from pathlib import Path

from test_mbtiles import PNG, make_mbtiles

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 160


def use_tmp_cache(monkeypatch, tmp_path: Path) -> None:
    from darksec import config

    monkeypatch.setattr(config, "MAP_TILE_DIR", tmp_path)


def test_cached_parent_tile_is_used_for_offline_overzoom(tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    parent = tilecache.tile_path(9, 123 >> 1, 456 >> 1, "esri_imagery")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(JPEG)

    hit = tilecache.find_cached_ancestor(10, 123, 456, "esri_imagery")
    assert hit and hit[0] == "esri_imagery" and hit[1] == JPEG
    # A different source must not leak into the active source's map.
    assert tilecache.find_cached_ancestor(10, 123, 456, "usgs_topo") is None


def test_corrupt_cached_tile_is_deleted_on_read(tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    bad = tilecache.tile_path(12, 10, 20, "esri_imagery")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"<html>Access blocked</html>")

    assert tilecache.get_cached(12, 10, 20, "esri_imagery") is None
    assert not bad.exists()


def test_verify_cache_removes_error_files_and_partials(tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    good = tilecache.tile_path(5, 1, 2, "esri_imagery")
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_bytes(JPEG)
    bad = tilecache.tile_path(5, 3, 4, "esri_imagery")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not an image")
    part = tilecache.cache_root() / "esri_imagery" / "5" / "1" / "2.jpg.part"
    part.write_bytes(b"partial")

    result = tilecache.verify_cache("esri_imagery", fix=True)
    assert result["checked"] == 2
    assert result["invalid"] == 1
    assert result["removed"] == 2
    assert good.exists()
    assert not bad.exists()
    assert not part.exists()


def test_verify_endpoint(client, tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    bad = tilecache.tile_path(6, 1, 1, "esri_imagery")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"<html>blocked</html>")

    out = client.post("/api/map/cache/verify", json={"source": "esri_imagery"}).json()
    assert out["removed"] == 1
    assert out["invalid"] == 1


def test_mbtiles_parent_tile_is_used_for_overzoom(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "raster.mbtiles", tiles={(8, 3, 5): PNG})
    hit = mbtiles.lookup_tile_ancestor(10, 3 << 2, 5 << 2, raw=str(archive))
    assert hit and hit[0] == PNG
    assert mbtiles.lookup_tile_ancestor(10, 1 << 2, 1 << 2, raw=str(archive)) is None


def test_basemap_prefers_active_source_over_bundled(tmp_path, monkeypatch, client):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    # Bundled overview tile at the same coordinates, as a different provider.
    bundled = tmp_path / "overview" / "10" / "1" / "1.jpg"
    bundled.parent.mkdir(parents=True, exist_ok=True)
    bundled.write_bytes(JPEG)
    cached = tilecache.tile_path(10, 1, 1, "usgs_topo")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(PNG)
    monkeypatch.setattr(tilecache, "is_online", lambda **_: False)
    try:
        assert client.put("/api/settings", json={"tile_source": "usgs_topo"}).status_code == 200
        r = client.get("/api/map/tiles/10/1/1.png")
        assert r.status_code == 200
        assert r.headers.get("x-tile-source") == "usgs_topo"

        # No exact active-source tile: overzoom the active source's parent,
        # not the unrelated bundled overview.
        r = client.get("/api/map/tiles/11/3/3.png")
        assert r.headers.get("x-tile-source") == "usgs_topo-overzoom"
    finally:
        client.put("/api/settings", json={"tile_source": "esri_imagery"})


def test_basemap_overzooms_active_source_parent(tmp_path, monkeypatch, client):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    parent = tilecache.tile_path(9, 1, 1, "usgs_topo")
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_bytes(JPEG)
    monkeypatch.setattr(tilecache, "is_online", lambda **_: False)
    try:
        assert client.put("/api/settings", json={"tile_source": "usgs_topo"}).status_code == 200
        r = client.get("/api/map/tiles/10/2/2.png")
        assert r.status_code == 200
        assert r.headers.get("x-tile-source") == "usgs_topo-overzoom"
    finally:
        client.put("/api/settings", json={"tile_source": "esri_imagery"})


def test_cache_clear_bumps_browser_generation(client, tmp_path, monkeypatch):
    use_tmp_cache(monkeypatch, tmp_path)
    first = client.get("/api/map/layers").json()["cacheGeneration"]
    client.post("/api/map/cache/clear", json={})
    second = client.get("/api/map/layers").json()["cacheGeneration"]
    assert second == first + 1


def test_fetched_tile_is_stored_under_lookup_extension(tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(tilecache, "_source_offline_until", {})
    monkeypatch.setattr(tilecache, "MIN_REQUEST_INTERVAL", 0)
    monkeypatch.setattr(tilecache, "_last_request_at", 0.0)

    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300

    class Resp:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = payload

    monkeypatch.setattr(tilecache.requests, "get", lambda *a, **k: Resp())
    data = tilecache.fetch_tile(11, 5, 6, "esri_imagery")
    assert data == payload
    # esri_imagery is configured as .jpg, but the actual tile was a PNG; the
    # cache lookup must still find it.
    assert tilecache.get_cached(11, 5, 6, "esri_imagery") == payload
    assert tilecache.tile_path(11, 5, 6, "esri_imagery").exists()


def test_cache_path_setting_and_check(client, tmp_path, monkeypatch):
    from darksec import tilecache

    usb = tmp_path / "USB" / "DarkSec-Tiles"
    try:
        r = client.put("/api/settings", json={"map_cache_path": str(usb)})
        assert r.status_code == 200
        assert r.json()["map_cache_path"] == str(usb.resolve())
        assert usb.is_dir()
        tilecache.invalidate_cache_path(str(usb.resolve()))
        assert tilecache.cache_root() == usb.resolve()

        check = client.get("/api/map/cache/check", params={"path": str(usb)}).json()
        assert check["ok"] is True and check["writable"] is True
    finally:
        client.put("/api/settings", json={"map_cache_path": ""})
        tilecache.invalidate_cache_path("")


def test_relocate_copies_existing_tiles(tmp_path, monkeypatch):
    from darksec import tilecache

    src = tmp_path / "old-cache"
    old = src / "esri_imagery" / "9" / "1" / "2.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(JPEG)
    (src / "esri_imagery" / "9" / "1" / "2.jpg.part").write_bytes(b"partial")
    dest = tmp_path / "usb" / "DarkSec-Tiles"

    monkeypatch.setattr(tilecache, "cache_root", lambda: src)
    monkeypatch.setattr(tilecache, "_downloads_running", lambda: False)
    job = tilecache.start_relocate(str(dest))
    for _ in range(100):
        state = tilecache.relocate_status(job["id"])
        if state["state"] != "running":
            break
        import time
        time.sleep(0.02)
    assert state["state"] == "done"
    assert (dest / "esri_imagery" / "9" / "1" / "2.jpg").read_bytes() == JPEG
    assert not (dest / "esri_imagery" / "9" / "1" / "2.jpg.part").exists()


def test_usgs_imagery_uses_the_live_service():
    from darksec import tilecache

    url = tilecache.TILE_SOURCES["usgs_imagery"]["url_template"]
    assert "USGSImageryOnly" in url
    assert "USGSImageryTopo" not in url


def test_download_job_logs_progress_and_can_be_cancelled(tmp_path, monkeypatch):
    import time

    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    def slow_fetch(*args, **kwargs):
        time.sleep(0.01)
        return b"\x00" * 200

    monkeypatch.setattr(tilecache, "fetch_tile", slow_fetch)
    monkeypatch.setattr(tilecache, "tiles_in_bbox", lambda *a, **k: [
        {"z": 10, "x": 100 + i, "y": 200, "cached": False} for i in range(200)
    ])
    job = tilecache.start_download(39.0, -99.0, 40.0, -98.0, 10, 10,
                                   "esri_imagery", label="manual")
    assert job["label"] == "manual"
    deadline = time.time() + 3
    while time.time() < deadline and tilecache.job_status(job["id"])["state"] == "running":
        time.sleep(0.01)
    state = tilecache.job_status(job["id"])
    assert state["state"] == "done"
    assert state["log"]
    assert any("Finished" in line["message"] for line in state["log"])
    assert any(item["id"] == job["id"] for item in tilecache.list_jobs())

    # A fresh job can be cancelled mid-flight.
    job2 = tilecache.start_download(39.0, -99.0, 40.0, -98.0, 10, 10, "esri_imagery")
    tilecache.cancel_download(job2["id"])
    deadline = time.time() + 3
    while time.time() < deadline and tilecache.job_status(job2["id"])["state"] == "running":
        time.sleep(0.01)
    cancelled = tilecache.job_status(job2["id"])
    assert cancelled["state"] == "cancelled"
    assert any("Cancel" in line["message"] for line in cancelled["log"])


def test_relocate_to_usb_moves_tiles_and_switches_cache(tmp_path, monkeypatch, client):
    from darksec import config, tilecache
    from darksec.db import Database

    src = tmp_path / "internal"
    old = src / "esri_imagery" / "9" / "1" / "2.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(JPEG)
    dest = tmp_path / "usb" / "DarkSec-Tiles"
    monkeypatch.setattr(tilecache, "cache_root", lambda: src)
    monkeypatch.setattr(tilecache, "_downloads_running", lambda: False)
    try:
        job = tilecache.start_relocate(str(dest), remove_source=True, switch_cache=True)
        for _ in range(100):
            state = tilecache.relocate_status(job["id"])
            if state["state"] != "running":
                break
            import time
            time.sleep(0.02)
        assert state["state"] == "done", state
        assert (dest / "esri_imagery" / "9" / "1" / "2.jpg").read_bytes() == JPEG
        assert not old.exists()
        assert Database(config.DB_PATH).get_setting("map_cache_path") == str(dest.resolve())
    finally:
        client.put("/api/settings", json={"map_cache_path": ""})
        tilecache.invalidate_cache_path("")


def test_offline_pack_from_another_source_is_used_as_fallback(tmp_path, monkeypatch, client):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    tile = tilecache.tile_path(8, 57, 97, "esri_street")
    tile.parent.mkdir(parents=True, exist_ok=True)
    tile.write_bytes(JPEG)
    monkeypatch.setattr(tilecache, "is_online", lambda **_: False)
    try:
        client.put("/api/settings", json={"tile_source": "usgs_topo"})
        r = client.get("/api/map/tiles/8/57/97.png")
        assert r.status_code == 200
        assert r.headers.get("x-tile-source") == "esri_street"
    finally:
        client.put("/api/settings", json={"tile_source": "esri_imagery"})


def test_label_overlay_is_never_a_basemap_fallback(tmp_path, monkeypatch):
    from darksec import tilecache

    use_tmp_cache(monkeypatch, tmp_path)
    label = tilecache.tile_path(8, 57, 97, "esri_labels_roads")
    label.parent.mkdir(parents=True, exist_ok=True)
    label.write_bytes(JPEG)
    assert tilecache.find_cached(8, 57, 97) is None
    assert tilecache.get_cached(8, 57, 97, "esri_labels_roads") == JPEG


def test_tile_bytes_are_memoised_in_ram(tmp_path, monkeypatch):
    from darksec import config, tilecache

    monkeypatch.setattr(config, "MAP_TILE_DIR", tmp_path)
    path = tilecache.tile_path(9, 7, 8, "esri_imagery")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    assert tilecache.get_cached(9, 7, 8, "esri_imagery") == JPEG
    path.unlink()  # the second read must come from RAM
    assert tilecache.get_cached(9, 7, 8, "esri_imagery") == JPEG


def test_warm_tile_queues_in_the_background(tmp_path, monkeypatch):
    import time
    from darksec import config, tilecache

    monkeypatch.setattr(config, "MAP_TILE_DIR", tmp_path)
    monkeypatch.setattr(tilecache, "is_online", lambda block=True: True)
    calls = []

    def slow_fetch(z, x, y, source="esri_imagery"):
        calls.append((z, x, y))
        time.sleep(0.05)
        path = tilecache.tile_path(z, x, y, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(JPEG)
        return JPEG

    monkeypatch.setattr(tilecache, "fetch_tile", slow_fetch)
    started = time.perf_counter()
    assert tilecache.warm_tile(12, 40, 50, "esri_imagery", wait=0.0) is None
    assert time.perf_counter() - started < 0.1

    deadline = time.time() + 3
    while time.time() < deadline and not tilecache.is_cached(12, 40, 50, "esri_imagery"):
        time.sleep(0.02)
    assert calls == [(12, 40, 50)]
    assert tilecache.get_cached(12, 40, 50, "esri_imagery") == JPEG


def test_offline_probe_never_blocks_tile_requests(monkeypatch):
    import time
    from darksec import tilecache

    tilecache.reset_runtime_caches()

    def slow_head(*_a, **_k):
        time.sleep(0.4)
        raise OSError("offline")

    monkeypatch.setattr(tilecache.requests, "head", slow_head)
    started = time.perf_counter()
    assert tilecache.is_online(block=False) is False
    assert time.perf_counter() - started < 0.2
    time.sleep(0.6)
    assert tilecache.is_online(block=False) is False


def test_tile_route_does_not_wait_for_a_stalled_provider(tmp_path, monkeypatch, client):
    import time
    from darksec import config, tilecache

    monkeypatch.setattr(config, "MAP_TILE_DIR", tmp_path)
    monkeypatch.setattr(tilecache, "TILE_FETCH_WAIT", 0.05)
    monkeypatch.setattr(tilecache, "is_online", lambda block=True: True)

    def stalled(*_a, **_k):
        time.sleep(3)
        return None

    monkeypatch.setattr(tilecache, "fetch_tile", stalled)
    started = time.perf_counter()
    response = client.get("/api/map/tiles/11/1801/901.png")
    assert time.perf_counter() - started < 1.0
    assert response.status_code in (200, 404)


def test_estimate_can_include_label_overlays():
    from darksec import tilecache

    base = tilecache.estimate_tiles(39.70, -105.01, 39.71, -105.00, 12, 12, "esri_imagery")
    with_labels = tilecache.estimate_tiles(39.70, -105.01, 39.71, -105.00, 12, 12,
                                           "esri_imagery", include_labels=True)
    assert with_labels["total"] == base["total"] * 3
    assert set(with_labels["sources"]) == {
        "esri_imagery", "esri_labels_places", "esri_labels_roads"}
    assert with_labels["include_labels"] is True


def test_download_job_fetches_label_overlays(monkeypatch, tmp_path):
    from darksec import config, tilecache

    monkeypatch.setattr(config, "MAP_TILE_DIR", tmp_path)
    monkeypatch.setattr(tilecache, "is_online", lambda **_: True)
    seen = []

    def fake_fetch(z, x, y, source="esri_imagery"):
        seen.append((z, x, y, source))
        return b""

    monkeypatch.setattr(tilecache, "fetch_tile", fake_fetch)
    job = {
        "id": "test", "state": "running", "source": "esri_imagery",
        "sources": ["esri_imagery", "esri_labels_places", "esri_labels_roads"],
        "bbox": [39.70, -105.00, 39.705, -104.995], "zoom_range": [12, 12],
        "total": 0, "completed": 0, "downloaded": 0, "already_cached": 0,
        "failed": 0, "bytes_added": 0, "blocked": None, "error": None,
        "cancel": False, "current": None, "log": [], "started": 0,
        "finished": None, "label": "test",
    }
    tilecache._run_job(job)
    assert {src for _, _, _, src in seen} == {
        "esri_imagery", "esri_labels_places", "esri_labels_roads"}
    assert job["state"] == "done"
    assert job["total"] == len(seen)
