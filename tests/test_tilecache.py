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
    monkeypatch.setattr(tilecache, "is_online", lambda: False)
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
    monkeypatch.setattr(tilecache, "is_online", lambda: False)
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
