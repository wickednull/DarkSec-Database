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
                 gz: bool = False) -> Path:
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


def test_mbtiles_vector_archive_is_reported_not_drawn(tmp_path):
    from darksec import mbtiles

    archive = make_mbtiles(tmp_path / "vector.mbtiles", fmt="pbf", tiles={(5, 1, 1): b"pbf"})
    info = mbtiles.describe_path(str(archive))
    assert info["vector"] is True
    assert info["ok"] is False
    assert mbtiles.lookup_tile(5, 1, 1, raw=str(archive)) is None


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
