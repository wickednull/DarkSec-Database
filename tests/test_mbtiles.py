"""Offline MBTiles tests: TMS/XYZ rows, gzip blobs, folders, API and scanning."""
from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 48
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x01" * 48


def make_mbtiles(path: Path, *, scheme: str = "tms", fmt: str = "png",
                 tiles: dict[tuple[int, int, int], bytes] | None = None,
                 gz: bool = False, extra_meta: dict | None = None) -> Path:
    """Create a minimal MBTiles archive in XYZ coordinates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE metadata (name TEXT, value TEXT);"
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
            "tile_row INTEGER, tile_data BLOB);"
            "CREATE UNIQUE INDEX tile_index ON tiles (zoom_level, tile_column, tile_row);"
        )
        meta = {"name": path.stem, "format": fmt, "scheme": scheme,
                "minzoom": "0", "maxzoom": "14"}
        meta.update(extra_meta or {})
        conn.executemany("INSERT INTO metadata VALUES (?, ?)", list(meta.items()))
        for (z, x, y), data in (tiles or {}).items():
            row_y = y if scheme == "xyz" else (1 << z) - 1 - y
            blob = gzip.compress(data) if gz else data
            conn.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, row_y, blob))
        conn.commit()
    finally:
        conn.close()
    return path


def test_mbtiles_tms_and_xyz_rows(tmp_path):
    from darksec import mbtiles

    tms = make_mbtiles(tmp_path / "tms.mbtiles", tiles={(10, 3, 5): PNG})
    xyz = make_mbtiles(tmp_path / "xyz.mbtiles", scheme="xyz", tiles={(10, 3, 5): PNG2})

    hit = mbtiles.lookup_tile(10, 3, 5, raw=str(tms))
    assert hit and hit[0] == PNG
    hit = mbtiles.lookup_tile(10, 3, 5, raw=str(xyz))
    assert hit and hit[0] == PNG2
    # A missing tile must not borrow the flipped row from the other scheme.
    assert mbtiles.lookup_tile(10, 3, (1 << 10) - 1 - 5, raw=str(xyz)) is None


def test_mbtiles_gzip_tiles_and_space_in_path(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "USB MAPS" / "region one.mbtiles",
                           tiles={(8, 1, 2): PNG}, gz=True)
    hit = mbtiles.lookup_tile(8, 1, 2, raw=str(archive))
    assert hit and hit[0] == PNG
    info = mbtiles.describe_path(str(archive))
    assert info["ok"] and info["format"] == "png" and info["maxzoom"] == 14


def test_mbtiles_directory_of_archives(tmp_path):
    from darksec import mbtiles

    folder = tmp_path / "maps"
    make_mbtiles(folder / "north.mbtiles", tiles={(6, 1, 1): PNG})
    make_mbtiles(folder / "south.mbtiles", tiles={(6, 4, 4): PNG2})

    assert mbtiles.valid_source_path(str(folder))
    assert len(mbtiles.resolve_sources(str(folder))) == 2
    hit = mbtiles.lookup_tile(6, 4, 4, raw=str(folder))
    assert hit and hit[0] == PNG2 and hit[1] == "south.mbtiles"
    assert mbtiles.lookup_tile(6, 9, 9, raw=str(folder)) is None

    info = mbtiles.describe_path(str(folder))
    assert info["ok"] and info["count"] == 2 and info["directory"] is True


def test_mbtiles_vector_archive_is_read_for_the_gl_map(tmp_path):
    from darksec import mbtiles

    payload = b"\x1a\x2bvector-tile"
    archive = make_mbtiles(
        tmp_path / "vector.mbtiles", fmt="pbf", gz=True,
        tiles={(5, 1, 1): payload},
        extra_meta={"json": json.dumps({"vector_layers": [{"id": "water"}, {"id": "transportation"}]})},
    )
    info = mbtiles.describe_path(str(archive))
    assert info["vector"] is True
    assert info["raster"] is False
    assert info["ok"] is True
    assert info["usable"] is True
    assert info["vector_layers"] == ["water", "transportation"]
    assert info["indexed"] is True
    # The 2D raster path must not return PBF bytes as an image.
    assert mbtiles.lookup_tile(5, 1, 1, raw=str(archive)) is None
    hit = mbtiles.lookup_vector_tile(5, 1, 1, raw=str(archive))
    assert hit and hit[0] == payload


def test_mbtiles_api_tiles_layers_and_settings_validation(client, tmp_path, monkeypatch):
    from darksec import config, mbtiles

    archive = make_mbtiles(tmp_path / "api.mbtiles", tiles={(13, 4321, 2876): PNG})
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])

    # A plain file is rejected; an archive or a folder of archives is accepted.
    not_a_map = tmp_path / "notes.txt"
    not_a_map.write_text("nope")
    assert client.put("/api/settings", json={"mbtiles_path": str(not_a_map)}).status_code == 400

    try:
        saved = client.put("/api/settings", json={"mbtiles_path": str(archive)})
        assert saved.status_code == 200
        assert saved.json()["mbtiles_path"] == str(archive)

        tile = client.get("/api/map/tiles/13/4321/2876.png")
        assert tile.status_code == 200
        assert tile.headers.get("x-tile-source") == "mbtiles"
        assert tile.content == PNG

        layers = client.get("/api/map/layers").json()
        assert layers["mbtiles"]["configured"] is True
        assert layers["mbtiles"]["usable"] is True
        assert layers["mbtiles"]["count"] == 1
        assert layers["mbtiles"]["revision"]

        scan = client.get("/api/map/mbtiles/scan?refresh=true").json()
        assert any(item["path"] == str(archive) and item["usable"] for item in scan["items"])

        info = client.get("/api/map/mbtiles/info", params={"path": str(archive)}).json()
        assert info["ok"] is True and info["file"] == "api.mbtiles"
    finally:
        client.put("/api/settings", json={"mbtiles_path": ""})

    # The bundled overview may overzoom this coordinate, but it must no longer
    # come from the removed archive.
    fallback = client.get("/api/map/tiles/13/4321/2876.png")
    assert fallback.headers.get("x-tile-source") != "mbtiles"


def test_vector_mbtiles_style_and_tiles(client, tmp_path, monkeypatch):
    from darksec import mbtiles

    payload = b"\x1a\x2bvector-tile"
    archive = make_mbtiles(
        tmp_path / "planet-latest.planet.mbtiles", fmt="pbf", gz=True,
        tiles={(7, 69, 43): payload},
        extra_meta={"json": json.dumps({"vector_layers": [{"id": "water"}, {"id": "transportation"}]}),
                    "attribution": "OpenMapTiles test data"},
    )
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    try:
        assert client.put("/api/settings", json={"mbtiles_path": str(archive)}).status_code == 200

        layers = client.get("/api/map/layers").json()["mbtiles"]
        assert layers["vector"] is True and layers["raster"] is False
        assert layers["usable"] is True

        style = client.get("/api/map/vector/style").json()
        assert style["version"] == 8
        assert style["sources"]["omt"]["type"] == "vector"
        assert style["sources"]["omt"]["maxzoom"] == 14
        assert style["sources"]["omt"]["attribution"] == "OpenMapTiles test data"
        assert any(layer.get("source-layer") == "water" for layer in style["layers"])
        assert style["glyphs"] == "/ui/fonts/{fontstack}/{range}.pbf"
        assert any(layer.get("id") == "street-labels" and layer.get("type") == "symbol"
                   for layer in style["layers"])
        assert any(layer.get("id") == "place-labels" and layer.get("type") == "symbol"
                   for layer in style["layers"])

        tile = client.get("/api/map/vector/tiles/7/69/43.pbf")
        assert tile.status_code == 200
        assert tile.headers["content-type"].startswith("application/vnd.mapbox-vector-tile")
        assert tile.content == payload

        # Cache-busting revision lives in the path so MapLibre's {z}/{x}/{y}
        # template keeps working.
        revised = client.get("/api/map/vector/tiles/abc123/7/69/43.pbf")
        assert revised.status_code == 200 and revised.content == payload

        # Raster endpoint must not present the PBF archive as imagery.
        raster = client.get("/api/map/tiles/7/69/43.png")
        assert raster.headers.get("x-tile-source") != "mbtiles"

        scan = client.get("/api/map/mbtiles/scan", params={"refresh": "true",
                                                          "path": str(archive)}).json()
        assert any(item["path"] == str(archive) and item["vector"] for item in scan["items"])
    finally:
        client.put("/api/settings", json={"mbtiles_path": ""})


def test_mbtiles_missing_tiles_table_is_unusable(tmp_path):
    import sqlite3

    from darksec import mbtiles

    broken = tmp_path / "broken.mbtiles"
    conn = sqlite3.connect(broken)
    conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
    conn.execute("INSERT INTO metadata VALUES ('format', 'png')")
    conn.commit()
    conn.close()

    info = mbtiles.describe_path(str(broken))
    assert info["ok"] is False
    assert "tiles" in (info.get("error") or "")
    assert mbtiles.lookup_tile(1, 0, 0, raw=str(broken)) is None


def test_mbtiles_revision_changes_with_archive(tmp_path):
    import os

    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "rev.mbtiles", tiles={(4, 1, 1): PNG})
    first = mbtiles.revision(str(archive))
    assert first
    os.utime(archive, ns=(1, 2_000_000_000))
    assert mbtiles.revision(str(archive)) != first
    assert mbtiles.revision(str(tmp_path / "missing.mbtiles")) == ""


def test_mbtiles_path_error_explains_missing_and_permission(tmp_path):
    import os

    from darksec import mbtiles

    missing = tmp_path / "PNY512_4" / "planet-latest.planet.mbtiles"
    info = mbtiles.describe_path(str(missing))
    assert info["ok"] is False
    assert "does not exist" in info["error"]
    assert "planet-latest.planet.mbtiles" in info["error"]

    if os.geteuid() != 0:
        blocked = tmp_path / "blocked.mbtiles"
        blocked.write_bytes(b"")
        os.chmod(blocked, 0)
        try:
            info = mbtiles.describe_path(str(blocked))
            assert info["ok"] is False
            assert "Permission denied" in info["error"]
        finally:
            os.chmod(blocked, 0o644)


def test_scan_prefers_mounted_media_and_accepts_extra_root(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "usb" / "region.mbtiles", tiles={(6, 1, 1): PNG})
    items = mbtiles.scan(extra_roots=[tmp_path], budget=3.0)
    assert any(item["path"] == str(archive) for item in items)


def test_vector_style_light_and_dark():
    from darksec import mapstyle

    dark = mapstyle.vector_style(theme="dark", minzoom=2, maxzoom=12)
    light = mapstyle.vector_style(theme="light", minzoom=2, maxzoom=12)
    assert dark["sources"]["omt"]["minzoom"] == 2
    assert dark["sources"]["omt"]["maxzoom"] == 12
    assert dark["layers"][0]["paint"]["background-color"] != light["layers"][0]["paint"]["background-color"]
    assert any(layer.get("source-layer") == "water" for layer in dark["layers"])
    assert any(layer.get("id") == "street-labels" for layer in dark["layers"])
    assert any(layer.get("id") == "place-labels" for layer in dark["layers"])
    assert dark["metadata"]["darksec:labels"] is True


def test_generic_vector_schema_is_rendered_not_black():
    from darksec import mapstyle

    style = mapstyle.vector_style(theme="dark", layers=["osm"])
    assert style["metadata"]["darksec:schema"] == "generic"
    ids = {(layer.get("source-layer"), layer["type"]) for layer in style["layers"] if layer["id"].startswith("gen-")}
    assert ("osm", "fill") in ids
    assert ("osm", "line") in ids
    assert ("osm", "circle") in ids
    assert any(layer.get("id") == "gen-osm-label" and layer.get("type") == "symbol"
               for layer in style["layers"])


def test_mvt_layer_name_probe():
    from darksec import mbtiles

    def varint(n):
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            if n:
                out.append(b | 0x80)
            else:
                out.append(b)
                return bytes(out)

    def field(num, wire, payload):
        return varint((num << 3) | wire) + payload

    def ld(num, data):
        return field(num, 2, varint(len(data)) + data)

    layer = ld(1, b"osm") + field(15, 0, varint(2))
    tile = ld(3, layer)
    assert mbtiles._mvt_layer_names(tile) == ["osm"]


def test_style_endpoint_handles_non_openmaptiles_layers(client, tmp_path, monkeypatch):
    from darksec import mbtiles

    archive = make_mbtiles(
        tmp_path / "osm-latest.mbtiles", fmt="pbf", gz=True,
        tiles={(5, 1, 1): b"\\x1a\\x2bvector"},
        extra_meta={"json": json.dumps({"vector_layers": [{"id": "osm"}]})},
    )
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    try:
        assert client.put("/api/settings", json={"mbtiles_path": str(archive)}).status_code == 200
        style = client.get("/api/map/vector/style").json()
        assert style["metadata"]["darksec:schema"] == "generic"
        assert any(layer.get("source-layer") == "osm" for layer in style["layers"])
        layers = client.get("/api/map/layers").json()["mbtiles"]
        assert layers["layers"] == ["osm"]
    finally:
        client.put("/api/settings", json={"mbtiles_path": ""})


def test_dark_vector_style_has_visible_land_water_and_roads():
    from darksec import mapstyle

    style = mapstyle.vector_style(theme="dark")
    paint = {layer["id"]: layer.get("paint", {}) for layer in style["layers"]}
    bg = paint["background"]["background-color"].lstrip("#")
    water = paint["water"]["fill-color"].lstrip("#")
    road = paint["transportation"]["line-color"].lstrip("#")
    brightness = lambda h: sum(int(h[i:i + 2], 16) for i in (0, 2, 4))
    assert brightness(bg) > 24, "dark background must not collapse into pure black"
    assert brightness(water) > brightness(bg) + 40
    assert brightness(road) > brightness(bg) + 80


def test_bundled_vector_label_glyphs_are_available(client):
    from pathlib import Path

    glyph = Path("ui/fonts/Open Sans Regular/0-255.pbf")
    assert glyph.is_file() and glyph.stat().st_size > 1000
    r = client.get("/ui/fonts/Open%20Sans%20Regular/0-255.pbf")
    assert r.status_code == 200
    assert r.content[:1] == b"\n"


def test_mbtiles_without_format_metadata_detects_vector(tmp_path):
    import sqlite3

    from darksec import mbtiles

    archive = tmp_path / "no-format.mbtiles"
    conn = sqlite3.connect(archive)
    conn.executescript(
        "CREATE TABLE metadata (name TEXT, value TEXT);"
        "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_data BLOB);"
    )
    conn.execute("INSERT INTO metadata VALUES ('name', 'osm-latest')")
    # Minimal valid MVT: a layer named 'osm' with no features.
    layer = bytes([0x1a, 0x05, 0x0a, 0x03]) + b"osm"
    conn.execute("INSERT INTO tiles VALUES (5, 1, 30, ?)", (layer,))
    conn.commit()
    conn.close()

    info = mbtiles.describe_path(str(archive))
    assert info["ok"] is True
    assert info["vector"] is True
    assert info["raster"] is False
    assert mbtiles.lookup_tile(5, 1, 1, raw=str(archive)) is None
    assert mbtiles.lookup_vector_tile(5, 1, 1, raw=str(archive))


def test_mbtiles_bounds_are_derived_from_tiles(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "regional.mbtiles", tiles={(8, 57, 97): PNG})
    info = mbtiles.describe_path(str(archive))
    west, south, east, north = info["bounds"]
    assert -101 < west < -98
    assert 38 < south < 40
    assert west < east and south < north


def test_archives_are_found_in_deep_usb_subfolders(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "maps" / "osm" / "north" / "region.mbtiles",
                           tiles={(6, 1, 1): PNG})
    mbtiles.invalidate()
    found = mbtiles.resolve_sources(str(tmp_path))
    assert archive in found
    assert mbtiles.lookup_tile(6, 1, 1, raw=str(tmp_path))


def test_missing_usb_path_is_resolved_by_archive_name(tmp_path, monkeypatch):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "PNY512" / "region.mbtiles",
                           tiles={(7, 3, 5): PNG})
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    mbtiles.invalidate()
    configured = "/media/admin/PNY512_9/region.mbtiles"

    found = mbtiles.resolve_sources(configured)
    assert found == [archive]
    source = mbtiles.describe_source(configured)
    assert source["usable"] is True
    assert source["resolved"] is True
    assert source["resolved_path"] == str(archive)


def test_empty_archive_reports_a_clear_error(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "empty.mbtiles", tiles={})
    info = mbtiles.describe_path(str(archive))
    assert info["ok"] is False
    assert info["usable"] is False
    assert "no tile rows" in (info["error"] or "")


def test_mislabelled_png_archive_with_vector_bytes_is_detected(tmp_path):
    from darksec import mbtiles

    vector = bytes([0x1a, 0x05, 0x0a, 0x03]) + b"osm"
    archive = make_mbtiles(tmp_path / "mislabelled.mbtiles", fmt="png",
                           tiles={(8, 2, 3): vector})
    info = mbtiles.describe_path(str(archive))
    assert info["ok"] is True
    assert info["vector"] is True
    assert info["format"] == "pbf"


def test_diagnose_reports_mounts_and_sample_tile(tmp_path, monkeypatch, client):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "usb" / "region.mbtiles",
                           tiles={(8, 57, 97): PNG})
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    monkeypatch.setattr(mbtiles, "mount_entries", lambda: [{
        "path": str(tmp_path), "device": "/dev/sdb1", "fstype": "exfat",
        "exists": True, "readable": True,
    }])
    mbtiles.invalidate()
    r = client.get(f"/api/map/mbtiles/diagnose?path={archive}")
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["samples"] and out["samples"][0]["ok"] is True
    assert "mount points visible" in out["report"]
    assert "vector labels:" in out["report"]
    assert "READY" in out["report"]


VECTOR_TILE = bytes([0x1a, 0x05, 0x0a, 0x03]) + b"osm"


def make_loose_tree(root: Path, *, ext: str, data: bytes, z: int, x: int, y: int,
                    nested: str = "") -> Path:
    folder = root / nested / str(z) / str(x) if nested else root / str(z) / str(x)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{y}{ext}").write_bytes(data)
    return root


def test_loose_pbf_tile_tree_is_a_vector_source(tmp_path, client):
    from darksec import mbtiles

    root = make_loose_tree(tmp_path / "USBVEC", ext=".pbf", data=VECTOR_TILE,
                           z=8, x=57, y=97, nested="tiles")
    mbtiles.invalidate()

    info = mbtiles.describe_path(str(root))
    assert info["ok"] is True
    assert info["vector"] is True
    assert info["raster"] is False
    assert info["format"] == "pbf"
    assert info["minzoom"] == 8 and info["maxzoom"] == 8
    assert info["vector_layers"] == ["osm"]
    assert info["bounds"][0] < -99 < info["bounds"][2]
    assert mbtiles.lookup_vector_tile(8, 57, 97, raw=str(root))[0] == VECTOR_TILE
    assert mbtiles.lookup_tile(8, 57, 97, raw=str(root)) is None

    assert client.put("/api/settings", json={"mbtiles_path": str(root)}).status_code == 200
    layers = client.get("/api/map/layers").json()["mbtiles"]
    assert layers["vector"] is True and layers["usable"] is True
    style = client.get("/api/map/vector/style")
    assert style.status_code == 200
    tile = client.get("/api/map/vector/tiles/8/57/97.pbf")
    assert tile.status_code == 200
    assert tile.content == VECTOR_TILE
    assert tile.headers["content-type"].startswith("application/vnd.mapbox-vector-tile")

    client.put("/api/settings", json={"mbtiles_path": ""})
    mbtiles.invalidate()


def test_loose_raster_tile_tree_is_a_raster_source(tmp_path, client):
    from darksec import mbtiles

    root = make_loose_tree(tmp_path / "USBRAS", ext=".png", data=PNG, z=8, x=57, y=97)
    mbtiles.invalidate()
    info = mbtiles.describe_path(str(root))
    assert info["ok"] is True and info["raster"] is True and info["format"] == "png"
    hit = mbtiles.lookup_tile(8, 57, 97, raw=str(root))
    assert hit and hit[0] == PNG
    assert mbtiles.lookup_tile_ancestor(10, 0, 0, raw=str(root)) is None

    assert client.put("/api/settings", json={"mbtiles_path": str(root)}).status_code == 200
    r = client.get("/api/map/tiles/8/57/97.png")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers.get("x-tile-source")

    client.put("/api/settings", json={"mbtiles_path": ""})
    mbtiles.invalidate()


def test_gzipped_loose_pbf_tile_is_decompressed(tmp_path):
    from darksec import mbtiles

    root = make_loose_tree(tmp_path / "USBZIP", ext=".pbf",
                           data=gzip.compress(VECTOR_TILE), z=5, x=3, y=4)
    mbtiles.invalidate()
    hit = mbtiles.lookup_vector_tile(5, 3, 4, raw=str(root))
    assert hit and hit[0] == VECTOR_TILE


def test_pbf_named_sqlite_archive_is_accepted(tmp_path, client):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "vector-pack.pbf", fmt="pbf",
                           tiles={(5, 3, 4): VECTOR_TILE})
    mbtiles.invalidate()
    info = mbtiles.describe_path(str(archive))
    assert info["ok"] is True
    assert info["vector"] is True
    assert mbtiles.lookup_vector_tile(5, 3, 4, raw=str(archive))[0] == VECTOR_TILE
    assert client.put("/api/settings", json={"mbtiles_path": str(archive)}).status_code == 200
    assert client.get("/api/map/layers").json()["mbtiles"]["vector"] is True
    client.put("/api/settings", json={"mbtiles_path": ""})
    mbtiles.invalidate()


def test_scan_finds_loose_tile_folders(tmp_path, monkeypatch):
    from darksec import mbtiles

    root = make_loose_tree(tmp_path / "USBSCAN", ext=".pbf", data=VECTOR_TILE,
                           z=6, x=1, y=2, nested="tiles")
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [root.parent])
    mbtiles.invalidate()
    items = mbtiles.scan(extra_roots=[root.parent], budget=5.0)
    assert any(item.get("path") in (str(root), str(root / "tiles"))
               and item.get("vector") for item in items)
    mbtiles.invalidate()


def test_legacy_mbtiles_1_map_images_schema(tmp_path):
    from darksec import mbtiles

    archive = tmp_path / "legacy.mbtiles"
    conn = sqlite3.connect(archive)
    conn.executescript(
        "CREATE TABLE metadata (name TEXT, value TEXT);"
        "CREATE TABLE map (zoom_level INTEGER, tile_column INTEGER, "
        "tile_row INTEGER, tile_id TEXT);"
        "CREATE TABLE images (tile_id TEXT, tile_data BLOB);"
    )
    conn.executemany("INSERT INTO metadata VALUES (?,?)", [
        ("name", "Legacy pack"), ("format", "png"), ("minzoom", "8"), ("maxzoom", "8"),
    ])
    conn.execute("INSERT INTO map VALUES (8, 57, 158, 't1')")
    conn.execute("INSERT INTO images VALUES ('t1', ?)", (PNG,))
    conn.commit()
    conn.close()

    mbtiles.invalidate()
    info = mbtiles.describe_path(str(archive))
    assert info["ok"] is True
    assert info["legacy"] is True
    assert info["raster"] is True
    assert info["format"] == "png"
    # TMS row 158 is XYZ row 97 at z8.
    assert mbtiles.lookup_tile(8, 57, 97, raw=str(archive))[0] == PNG


def test_world_metadata_bounds_are_replaced_by_real_tile_bounds(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(
        tmp_path / "regional-bad-bounds.mbtiles",
        tiles={(8, 57, 97): PNG},
        extra_meta={"bounds": "-180,-85.0511,180,85.0511"},
    )
    mbtiles.invalidate()
    info = mbtiles.describe_path(str(archive))
    west, south, east, north = info["bounds"]
    assert -101 < west < -98
    assert 38 < south < 41
    assert east < -97 and north < 42


def test_loose_tree_under_a_mount_root_is_not_mistaken_for_an_archive(tmp_path, monkeypatch):
    from darksec import mbtiles

    root = make_loose_tree(tmp_path / "media" / "USB", ext=".pbf",
                           data=VECTOR_TILE, z=8, x=57, y=97)
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    mbtiles.invalidate()
    assert mbtiles.resolve_sources(str(root)) == []
    source = mbtiles.describe_source(str(root))
    assert source["usable"] is True and source["vector"] is True
    assert source["count"] == 1
    assert source["archives"][0]["loose"] is True


def test_existing_empty_folder_does_not_resolve_to_itself(tmp_path, monkeypatch):
    from darksec import mbtiles

    empty = tmp_path / "not-maps-yet"
    empty.mkdir()
    monkeypatch.setattr(mbtiles, "search_roots", lambda: [tmp_path])
    mbtiles.invalidate()
    assert mbtiles.resolve_sources(str(empty)) == []
    assert mbtiles.describe_path(str(empty))["ok"] is False


def test_archive_connection_is_safe_across_worker_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "threaded.mbtiles", tiles={(8, 57, 97): PNG})
    mbtiles.invalidate()
    # First access opens and caches the SQLite connection in this thread, just
    # like a Settings CHECK before the map requests tiles on other workers.
    assert mbtiles.lookup_tile(8, 57, 97, raw=str(archive))[0] == PNG

    def read(_):
        hit = mbtiles.lookup_tile(8, 57, 97, raw=str(archive))
        return bool(hit and hit[0] == PNG)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(read, range(12)))

    with ThreadPoolExecutor(max_workers=1) as pool:
        sample = pool.submit(
            lambda: mbtiles._archive_for(archive).sample_tile()
        ).result()
    assert sample["ok"] is True


def test_vector_style_can_render_geometry_without_glyphs():
    from darksec import mapstyle

    style = mapstyle.vector_style(layers=["water", "place"], glyphs=None)
    assert "glyphs" not in style
    assert style["metadata"]["darksec:labels"] is False
    assert not any(layer.get("type") == "symbol" for layer in style["layers"])
    assert any(layer.get("source-layer") == "water" for layer in style["layers"])

    generic = mapstyle.vector_style(layers=["osm"], glyphs=None)
    assert not any(layer.get("type") == "symbol" for layer in generic["layers"])
    assert any(layer.get("id") == "gen-osm-fill" for layer in generic["layers"])


def test_style_endpoint_omits_glyphs_when_pack_is_missing(tmp_path, monkeypatch, client):
    from darksec import config, mbtiles

    archive = make_mbtiles(tmp_path / "vector.mbtiles", fmt="pbf",
                           tiles={(5, 3, 4): VECTOR_TILE},
                           extra_meta={"json": '{"vector_layers":[{"id":"water"}]}'})
    mbtiles.invalidate()
    assert client.put("/api/settings", json={"mbtiles_path": str(archive)}).status_code == 200

    empty_ui = tmp_path / "ui-without-fonts"
    empty_ui.mkdir()
    monkeypatch.setattr(config, "UI_DIR", empty_ui)
    style = client.get("/api/map/vector/style").json()
    assert "glyphs" not in style
    assert style["metadata"]["darksec:labels"] is False
    assert not any(layer.get("type") == "symbol" for layer in style["layers"])
    assert any(layer.get("source-layer") == "water" for layer in style["layers"])

    client.put("/api/settings", json={"mbtiles_path": ""})
    mbtiles.invalidate()
