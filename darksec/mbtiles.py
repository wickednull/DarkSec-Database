"""Read-only MBTiles support for offline basemaps.

An MBTiles archive is a SQLite database that is usually carried on removable
media, so everything here opens it read-only and never writes to it. The
module supports:

  * a single ``.mbtiles`` file or a directory containing several archives;
  * a folder of loose ``{z}/{x}/{y}.pbf`` (or ``.png``/``.jpg``/``.webp``)
    tiles, for packs exported as plain tile trees instead of SQLite;
  * MBTiles 1.0 ``map`` + ``images`` schemas as well as the modern ``tiles``
    table;
  * the MBTiles TMS row order (the spec default) and non-standard XYZ archives
    that advertise ``scheme=xyz`` in their metadata;
  * gzip-compressed raster and vector tile blobs;
  * raster archives and vector ``pbf``/``mvt`` archives.

Paths are cached briefly so a settings change takes effect immediately, while
tile lookups avoid reopening the database (or the main DarkSec database) for
every tile request.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import string
import threading
import time
from pathlib import Path
from typing import Any

CONFIG_TTL_SECONDS = 20.0
ARCHIVE_RECHECK_SECONDS = 10.0
SCAN_DEPTH = 5
SCAN_BUDGET_SECONDS = 6.0
SCAN_MAX_RESULTS = 80
FOLDER_DEPTH = 3
VECTOR_FORMATS = {"pbf", "mvt", "vector", "protobuf"}
_RASTER_FORMATS = {"png", "jpg", "webp"}

_lock = threading.RLock()
_config_cache: dict[str, Any] = {"at": 0.0, "raw": None}
_sources_cache: dict[str, tuple[float, list[Path]]] = {}
_archive_cache: dict[str, dict[str, Any]] = {}
_scan_cache: dict[str, Any] = {"at": 0.0, "items": []}
_resolve_cache: dict[str, tuple[float, list[Path]]] = {}
# A USB drive usually comes back at the same mount point, but labels and users
# change (PNY512_4 vs PNY512, /media/admin vs /media/operator). Remembering the
# archive basename lets DarkSec find it again without a restart.
_AUTO_RESOLVE_SECONDS = 45.0


def _looks_like_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _is_archive(path: Path) -> bool:
    """An MBTiles SQLite file, including ``.pbf``/``.mvt``-named archives."""
    try:
        if not path.is_file():
            return False
        suffix = path.suffix.lower()
        if suffix == ".mbtiles":
            return True
        if suffix in (".pbf", ".mvt", ".gpkg"):
            return _looks_like_sqlite(path)
        return False
    except OSError:
        return False


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise ValueError("bad varint")


def _mvt_layer_names(data: bytes) -> list[str]:
    """Read layer names from a Mapbox Vector Tile blob without dependencies."""
    names: list[str] = []
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, offset = _varint(data, offset)
            payload = data[offset:offset + length]
            offset += length
            if field != 3:  # Tile.layers
                continue
            name = ""
            inner = 0
            while inner < len(payload):
                lkey, inner = _varint(payload, inner)
                lfield, lwire = lkey >> 3, lkey & 7
                if lwire == 2:
                    llen, inner = _varint(payload, inner)
                    value = payload[inner:inner + llen]
                    inner += llen
                    if lfield == 1:
                        name = value.decode("utf-8", "replace")
                        break
                elif lwire == 0:
                    _value, inner = _varint(payload, inner)
                elif lwire == 5:
                    inner += 4
                elif lwire == 1:
                    inner += 8
                else:
                    break
            if name:
                names.append(name)
        elif wire == 0:
            _value, offset = _varint(data, offset)
        elif wire == 5:
            offset += 4
        elif wire == 1:
            offset += 8
        else:
            break
    return names


def _walk_archives(folder: Path, depth: int = FOLDER_DEPTH,
                   deadline: float | None = None) -> list[Path]:
    """Archives at the top level and up to ``depth`` subfolders below it.

    A tester's USB stick often keeps packs in ``maps/osm/`` or a vendor folder,
    not in the drive root, so a single fixed level is not enough. Hidden and
    system folders are skipped and work stops at ``deadline`` (monotonic) so a
    bad path can never stall a settings save or a tile request.
    """
    found: list[Path] = []
    if deadline is None:
        deadline = time.monotonic() + 3.0
    stack: list[tuple[Path, int]] = [(folder, 0)]
    while stack and time.monotonic() <= deadline:
        current, level = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if time.monotonic() > deadline:
                break
            try:
                if entry.is_file():
                    name = entry.name.lower()
                    if name.endswith(".mbtiles") or (
                            name.endswith((".pbf", ".mvt"))
                            and _is_archive(Path(entry.path))):
                        found.append(Path(entry.path))
                elif entry.is_dir() and level < depth:
                    name = entry.name.lower()
                    if name.startswith((".", "$")) or name in _SKIP_DIRS:
                        continue
                    stack.append((Path(entry.path), level + 1))
            except OSError:
                continue
    return sorted(found, key=lambda p: (p.name.lower(), str(p).lower()))


def _is_tile_tree_root(folder: Path) -> bool:
    """Cheap check for ``{z}/{x}/{y}.tile`` layout before crawling archives."""
    try:
        entries = list(os.scandir(folder))[:128]
    except OSError:
        return False
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            path = Path(entry.path)
            if entry.name.isdigit() and _is_zoom_dir(path):
                return True
            if entry.name.lower() in ("tiles", "mvt", "vector", "pbf"):
                try:
                    for child in list(os.scandir(path))[:32]:
                        if child.is_dir() and child.name.isdigit() and _is_zoom_dir(Path(child.path)):
                            return True
                except OSError:
                    continue
        except OSError:
            continue
    return False


def _iter_archives(folder: Path) -> list[Path]:
    """Archives in a configured folder (top level plus a few subfolders)."""
    return _walk_archives(folder, depth=FOLDER_DEPTH)


# --------------------------------------------------------------------------
# Loose tile trees ({z}/{x}/{y}.pbf or .png) on USB
# --------------------------------------------------------------------------
_LOOSE_ALL = {".pbf", ".mvt", ".png", ".jpg", ".jpeg", ".webp"}
_LOOSE_VECTOR = {".pbf", ".mvt"}
_LOOSE_RASTER = {".png", ".jpg", ".jpeg", ".webp"}
_loose_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}


def _tile_suffix(name: str) -> str:
    return Path(name).suffix.lower()


def _is_zoom_dir(directory: Path) -> bool:
    """A ``{z}/{x}/{y}.pbf`` tree's zoom folder, cheaply identified."""
    if not directory.name.isdigit():
        return False
    try:
        xs = list(os.scandir(directory))[:8]
    except OSError:
        return False
    for x in xs:
        try:
            if not x.is_dir() or not x.name.isdigit():
                continue
            files = list(os.scandir(x.path))[:8]
            if any(f.is_file() and _tile_suffix(f.name) in _LOOSE_ALL for f in files):
                return True
        except OSError:
            continue
    return False


def _find_tile_base(root: Path, max_depth: int = 3) -> Path | None:
    """Locate the folder that directly contains the ``{z}`` folders."""
    if not root.is_dir():
        return None
    if _is_zoom_dir(root):
        return root.parent
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        folder, level = queue.pop(0)
        try:
            children = list(os.scandir(folder))
        except OSError:
            continue
        for entry in children:
            try:
                if not entry.is_dir():
                    continue
                child = Path(entry.path)
                if _is_zoom_dir(child):
                    return folder
                if level >= max_depth:
                    continue
                name = entry.name.lower()
                if name.startswith((".", "$")) or name in _SKIP_DIRS:
                    continue
                queue.append((child, level + 1))
            except OSError:
                continue
    return None


def _loose_zooms(base: Path) -> list[int]:
    try:
        return sorted(int(e.name) for e in os.scandir(base)
                      if e.is_dir() and e.name.isdigit())
    except OSError:
        return []


def _loose_bounds(base: Path, zoom: int) -> list[float] | None:
    zdir = base / str(zoom)
    try:
        xs = [int(e.name) for e in os.scandir(zdir) if e.is_dir() and e.name.isdigit()]
    except OSError:
        return None
    if not xs:
        return None
    # Very deep regional trees can hold thousands of x folders. Stop after a
    # short budget and return an approximate box rather than stalling a map.
    deadline = time.monotonic() + 0.75
    ys: list[int] = []
    for x in xs[:2048]:
        if time.monotonic() > deadline:
            break
        try:
            for entry in os.scandir(zdir / str(x)):
                if entry.is_file() and _tile_suffix(entry.name) in _LOOSE_ALL:
                    stem = Path(entry.name).stem
                    if stem.isdigit():
                        ys.append(int(stem))
        except OSError:
            continue
    if not ys:
        return None
    n = 1 << zoom
    west = min(xs) / n * 360.0 - 180.0
    east = (max(xs) + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(
        math.pi * (1.0 - 2.0 * min(ys) / n))))
    south = math.degrees(math.atan(math.sinh(
        math.pi * (1.0 - 2.0 * (max(ys) + 1) / n))))
    return [west, south, east, north]


def _probe_file_layers(path: Path) -> list[str]:
    try:
        with open(path, "rb") as fh:
            data = fh.read(200000)
    except OSError:
        return []
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError:
            return []
    try:
        return _mvt_layer_names(data)
    except (ValueError, IndexError):
        return []


def loose_info(raw: str | Path | None) -> dict[str, Any] | None:
    """Describe a folder of ``{z}/{x}/{y}.pbf`` (or raster) tiles.

    Cache size is small and time-limited because the folder is on removable
    media. Returns ``None`` when the path is not a tile tree.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    try:
        if not path.is_dir():
            return None
    except OSError:
        return None
    key = str(path)
    now = time.monotonic()
    with _lock:
        cached = _loose_cache.get(key)
        if cached and now - cached[0] < 30.0:
            return cached[1]
    base = _find_tile_base(path)
    info: dict[str, Any] | None = None
    if base is not None:
        zooms = _loose_zooms(base)
        if zooms:
            formats: set[str] = set()
            vector_samples: list[Path] = []
            sample_file: Path | None = None
            for zoom in (zooms[-1], zooms[0]):
                zdir = base / str(zoom)
                try:
                    for x in list(os.scandir(zdir))[:12]:
                        if not x.is_dir():
                            continue
                        for entry in list(os.scandir(x.path))[:12]:
                            if not entry.is_file():
                                continue
                            suffix = _tile_suffix(entry.name)
                            if suffix not in _LOOSE_ALL:
                                continue
                            formats.add(suffix.lstrip("."))
                            if sample_file is None:
                                sample_file = Path(entry.path)
                            if suffix in _LOOSE_VECTOR and len(vector_samples) < 6:
                                vector_samples.append(Path(entry.path))
                except OSError:
                    continue
            vector = bool(formats & {f.lstrip(".") for f in _LOOSE_VECTOR})
            raster = bool(formats & {f.lstrip(".") for f in _LOOSE_RASTER})
            if not formats:
                info = None
            else:
                if vector and raster:
                    # Mixed folders are rare; vector wins so the GL renderer is used.
                    raster = False
                layers = []
                if vector:
                    for candidate in vector_samples:
                        layers = _probe_file_layers(candidate)
                        if layers:
                            break
                info = {
                    "path": str(path),
                    "name": path.name or "Offline tiles",
                    "file": path.name,
                    "size_bytes": None,
                    "format": "pbf" if vector else (sorted(formats)[0] if formats else "unknown"),
                    "scheme": "xyz",
                    "minzoom": zooms[0],
                    "maxzoom": zooms[-1],
                    "bounds": _loose_bounds(base, zooms[0]),
                    "vector": vector,
                    "raster": raster,
                    "usable": True,
                    "has_tiles": True,
                    "vector_layers": layers,
                    "attribution": "",
                    "indexed": True,
                    "loose": True,
                    "base": str(base),
                    "sample": str(sample_file) if sample_file else None,
                    "error": None,
                }
    with _lock:
        _loose_cache[key] = (now, info)
    return info


def loose_lookup(info: dict[str, Any], z: int, x: int, y: int) -> bytes | None:
    """Read a loose ``{z}/{x}/{y}.pbf|png`` tile, with TMS fallback."""
    if not info or not info.get("base"):
        return None
    try:
        z = int(z)
        x = int(x)
        y = int(y)
    except (TypeError, ValueError):
        return None
    if z < 0 or z > 24 or x < 0 or y < 0:
        return None
    base = Path(info["base"])
    exts = (".pbf", ".mvt") if info.get("vector") else (".png", ".jpg", ".jpeg", ".webp")
    n = 1 << z
    rows = [y]
    flipped = n - 1 - y
    if flipped != y:
        rows.append(flipped)
    for row in rows:
        for ext in exts:
            candidate = base / str(z) / str(x) / f"{row}{ext}"
            try:
                if candidate.is_file():
                    data = candidate.read_bytes()
                    if not data:
                        continue
                    if data[:2] == b"\x1f\x8b":
                        try:
                            data = gzip.decompress(data)
                        except OSError:
                            continue
                    return data
            except OSError:
                continue
    return None


def _configured_loose(raw: str | None) -> dict[str, Any] | None:
    text = str(raw or "").strip()
    if not text:
        return None
    info = loose_info(text)
    if info is not None:
        return info
    # The drive may have come back under a different mount path. Check the
    # common roots for the same folder name (no deep walk on the tile path).
    path = Path(text).expanduser()
    name = path.name
    if not name:
        return None
    try:
        if path.exists():
            return None
    except OSError:
        return None
    for root in search_roots():
        candidate = root / name
        try:
            if candidate.is_dir():
                info = loose_info(candidate)
                if info is not None:
                    return info
        except OSError:
            continue
    return None




def _auto_resolve(text: str) -> list[Path]:
    """Find a re-mounted archive/folder by its basename.

    A drive that comes back as ``PNY512`` instead of ``PNY512_4``, or a user
    who mounted the stick under a different path, should not require editing
    Settings or restarting. Only a path that no longer exists is re-resolved,
    and only against mount points, so a typo cannot trigger a full-disk walk
    on every map tile.
    """
    if not text or ("/" not in text and "\\" not in text):
        return []
    path = Path(text).expanduser()
    try:
        if path.exists():
            return []
    except OSError:
        pass
    base = Path(text).name
    if not base or base in (".", ".."):
        return []
    now = time.monotonic()
    with _lock:
        cached = _resolve_cache.get(text)
        if cached and now - cached[0] < _AUTO_RESOLVE_SECONDS:
            return list(cached[1])
    wanted = base.lower()
    deadline = now + 3.0
    found: list[Path] = []
    seen: set[str] = set()
    for root in search_roots():
        if time.monotonic() > deadline or found:
            break
        # Cheap exact hit first: the drive may have kept its folder structure.
        for candidate in (root / base,):
            try:
                if candidate.exists():
                    if _is_archive(candidate):
                        found = [candidate]
                    elif candidate.is_dir():
                        archives = _walk_archives(candidate, depth=FOLDER_DEPTH,
                                                  deadline=deadline)
                        if archives:
                            found = archives
                    if found:
                        break
            except OSError:
                continue
        if found:
            break
        # Then a shallow walk for the same filename anywhere on the drive.
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and time.monotonic() <= deadline and not found:
            folder, level = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if time.monotonic() > deadline:
                    break
                try:
                    if entry.name.lower() == wanted:
                        if entry.is_file() and _is_archive(Path(entry.path)):
                            found = [Path(entry.path)]
                            break
                        if entry.is_dir():
                            archives = _walk_archives(Path(entry.path),
                                                      depth=FOLDER_DEPTH,
                                                      deadline=deadline)
                            if archives:
                                found = archives
                                break
                    if entry.is_dir() and level < FOLDER_DEPTH:
                        name = entry.name.lower()
                        if name.startswith((".", "$")) or name in _SKIP_DIRS:
                            continue
                        stack.append((Path(entry.path), level + 1))
                except OSError:
                    continue
    unique: list[Path] = []
    for path in found:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    with _lock:
        _resolve_cache[text] = (now, list(unique))
        _sources_cache.pop(text, None)
    return unique


def resolve_sources(raw: str | None) -> list[Path]:
    """Turn the configured setting into a list of archives.

    A file path yields that file. A directory yields its ``.mbtiles`` archives
    (top level and a few subfolders). Missing paths yield an empty list, unless
    the same archive can be found on a currently mounted drive.
    """
    text = str(raw or "").strip()
    if not text:
        return []
    now = time.monotonic()
    with _lock:
        cached = _sources_cache.get(text)
        if cached and now - cached[0] < CONFIG_TTL_SECONDS:
            return list(cached[1])
    path = Path(text).expanduser()
    found: list[Path] = []
    try:
        if path.is_file():
            found = [path]
        elif path.is_dir():
            found = [] if _is_tile_tree_root(path) else _iter_archives(path)
    except OSError:
        found = []
    if not found:
        try:
            exists = path.exists()
        except OSError:
            exists = False
        if not exists:
            found = _auto_resolve(text)
    with _lock:
        _sources_cache[text] = (now, list(found))
    return found



def valid_source_path(raw: str | None) -> bool:
    """True when the setting points at an archive or a folder of archives."""
    path_text = str(raw or "").strip()
    if not path_text:
        return True
    path = Path(path_text).expanduser()
    if _is_archive(path):
        return True
    if path.is_dir():
        return bool(_iter_archives(path)) or loose_info(path) is not None
    return False


def configured_path() -> str:
    now = time.monotonic()
    with _lock:
        if _config_cache["raw"] is not None and now - _config_cache["at"] < CONFIG_TTL_SECONDS:
            return str(_config_cache["raw"])
    raw = ""
    try:
        from .db import Database

        d = Database()
        try:
            raw = str(d.get_setting("mbtiles_path", "") or "")
        finally:
            d.close()
    except Exception:
        raw = ""
    with _lock:
        _config_cache.update({"at": now, "raw": raw})
    return raw


def invalidate(raw: str | None = None) -> None:
    """Drop cached settings/archives after a settings change or media swap."""
    with _lock:
        _config_cache.update({"at": time.monotonic(), "raw": raw})
        _sources_cache.clear()
        _resolve_cache.clear()
        _loose_cache.clear()
        _archive_cache.clear()


def _connect(path: Path) -> sqlite3.Connection:
    """Open an archive read-only, including from a read-only USB filesystem."""
    uri = path.resolve().as_uri()
    attempts = (uri + "?mode=ro", uri + "?mode=ro&immutable=1")
    last: Exception | None = None
    for target in attempts:
        try:
            # Archive objects are cached and used by Starlette's threadpool, so
            # the connection must be usable from any worker thread. Access is
            # serialised by Archive.lock.
            conn = sqlite3.connect(target, uri=True, timeout=3,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.Error as exc:
            last = exc
    raise RuntimeError(f"cannot open MBTiles archive: {last}")


class Archive:
    """One read-only MBTiles database with cached metadata."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn: sqlite3.Connection | None = None
        self.error: str | None = None
        self._stat: tuple[int, int] | None = None
        self._checked = time.monotonic()
        self._failed_at = 0.0
        self.metadata: dict[str, str] = {}
        self.format = ""
        self.scheme = "tms"
        self.minzoom: int | None = None
        self.maxzoom: int | None = None
        self.bounds: list[float] | None = None
        self.vector_layers: list[str] = []
        self.attribution = ""
        self.indexed = False
        self.has_tiles = False
        self.legacy = False
        self.name = path.stem
        self._open()

    # ---------------------------------------------------------------- setup
    def _signature(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

    def _tile_source(self) -> str:
        """SQL source exposing zoom_level/tile_column/tile_row/tile_data."""
        if not self.legacy:
            return "tiles"
        return ("(SELECT m.zoom_level AS zoom_level, m.tile_column AS tile_column, "
                "m.tile_row AS tile_row, i.tile_data AS tile_data "
                "FROM map m JOIN images i ON i.tile_id = m.tile_id)")

    def _open(self) -> None:
        self._stat = self._signature()
        self.error = None
        self.format = ""
        self.minzoom = None
        self.maxzoom = None
        self.bounds = None
        self.vector_layers = []
        self.attribution = ""
        self.indexed = False
        self.has_tiles = False
        self.legacy = False
        self.metadata = {}
        try:
            self.conn = _connect(self.path)
            present = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='tiles' AND type IN ('table', 'view')"
            ).fetchone()
            if not present:
                # MBTiles 1.0 stored tiles in map + images instead of tiles.
                names = {
                    str(row["name"])
                    for row in self.conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name IN ('map', 'images')"
                    ).fetchall()
                }
                if not {"map", "images"}.issubset(names):
                    raise RuntimeError("archive has no tiles table")
                self.legacy = True
            self.metadata = self._read_metadata()
            self.has_tiles = bool(
                self.conn.execute(f"SELECT 1 FROM {self._tile_source()} LIMIT 1").fetchone()
            )
            if not self.has_tiles:
                raise RuntimeError("archive has a tiles table but no tile rows (empty pack)")
            try:
                index_table = "map" if self.legacy else "tiles"
                self.indexed = bool(
                    self.conn.execute(f"PRAGMA index_list('{index_table}')").fetchall()
                )
            except sqlite3.Error:
                self.indexed = False
            self._apply_metadata()
            self._failed_at = 0.0
        except Exception as exc:
            self.error = str(exc)[:240]
            self._failed_at = time.monotonic()
            self._close()

    def _close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _read_metadata(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            rows = self.conn.execute("SELECT name, value FROM metadata").fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            try:
                key = str(row["name"] or "").strip().lower()
                value = row["value"]
                if isinstance(value, bytes):
                    value = value.decode("utf-8", "replace")
                out[key] = str(value or "").strip()
            except Exception:
                continue
        return out

    def _apply_metadata(self) -> None:
        meta = self.metadata
        self.format = (meta.get("format") or "").lower().strip()
        self.scheme = (meta.get("scheme") or "tms").lower().strip()
        if self.scheme not in ("tms", "xyz"):
            self.scheme = "tms"
        for key in ("name", "description"):
            if meta.get(key):
                self.name = meta[key][:120]
                break
        try:
            self.minzoom = int(float(meta.get("minzoom"))) if meta.get("minzoom") else None
        except (TypeError, ValueError):
            self.minzoom = None
        try:
            self.maxzoom = int(float(meta.get("maxzoom"))) if meta.get("maxzoom") else None
        except (TypeError, ValueError):
            self.maxzoom = None
        if meta.get("bounds"):
            try:
                parts = [float(p) for p in meta["bounds"].split(",")[:4]]
                if len(parts) == 4:
                    self.bounds = parts
            except (TypeError, ValueError):
                self.bounds = None
        # Attribution is inserted into MapLibre's HTML control; keep it plain
        # text so a supplied archive cannot inject markup into the UI.
        raw_attribution = re.sub(r"<[^>]{0,300}>", " ", meta.get("attribution") or "")
        self.attribution = " ".join(raw_attribution.split())[:300]
        try:
            extra = json.loads(meta.get("json") or "{}")
            self.vector_layers = [
                str(layer.get("id")) for layer in (extra.get("vector_layers") or [])
                if layer.get("id")
            ]
        except (TypeError, ValueError):
            self.vector_layers = []
        if self.vector and not self.vector_layers:
            self.vector_layers = self._probe_vector_layers()
        if self.format in ("jpeg",):
            self.format = "jpg"
        known_formats = {"png", "jpg", "webp"} | VECTOR_FORMATS
        if self.format and self.format not in known_formats:
            self.format = ""
        if not self.format:
            self.format = self._sniff_format()
        # Some tilemaker/tippecanoe archives omit metadata.format. A valid MVT
        # layer probe is decisive; treating those archives as raster produced a
        # black map because PBF bytes were served as images.
        if not self.format and self.vector_layers:
            self.format = "pbf"
        if not self.format:
            probed = self._probe_vector_layers()
            if probed:
                self.format = "pbf"
                if not self.vector_layers:
                    self.vector_layers = probed
        self._correct_format()
        if self.minzoom is None or self.maxzoom is None:
            self._zoom_range()
        # Tile-derived bounds guarantee the auto-fit lands on real data. Some
        # regional extracts incorrectly keep the planet's world bounds in
        # metadata, which opened the map at z0 over an empty area (black map).
        derived = self._bounds_from_tiles()
        if derived:
            self.bounds = derived

    def _correct_format(self) -> None:
        """Reconcile metadata.format with the first tile's real bytes.

        Some tilemaker/tippecanoe exports say ``png`` while storing MVT blobs
        (or leave the format empty). Serving those bytes as images is exactly
        what produces a black map, so the bytes win when they are decisive.
        """
        sniffed = self._sniff_format()
        if self.format in _RASTER_FORMATS and not sniffed:
            layers = self._probe_vector_layers()
            if layers:
                self.format = "pbf"
                if not self.vector_layers:
                    self.vector_layers = layers
        elif self.format in VECTOR_FORMATS and sniffed in _RASTER_FORMATS:
            self.format = sniffed

    def _probe_vector_layers(self) -> list[str]:
        """Fallback for archives that omit the metadata json vector_layers."""
        try:
            row = self.conn.execute(
                f"SELECT tile_data FROM {self._tile_source()} ORDER BY zoom_level LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return []
        if not row:
            return []
        data = bytes(row[0] or b"")
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                return []
        try:
            return _mvt_layer_names(data)
        except (ValueError, IndexError):
            return []

    def _sniff_format(self) -> str:
        try:
            row = self.conn.execute(f"SELECT tile_data FROM {self._tile_source()} LIMIT 1").fetchone()
        except sqlite3.Error:
            return ""
        if not row:
            return ""
        data = bytes(row[0] or b"")
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                return ""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return "png"
        if data[:3] == b"\xff\xd8\xff":
            return "jpg"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        return ""

    def _zoom_range(self) -> None:
        # Metadata normally carries minzoom/maxzoom. Only fall back to a table
        # scan for archives small enough that it cannot stall a map request.
        try:
            if self.path.stat().st_size > 1_500_000_000:
                return
            row = self.conn.execute(
                f"SELECT MIN(zoom_level) AS lo, MAX(zoom_level) AS hi "
                f"FROM {self._tile_source()}"
            ).fetchone()
            if row:
                self.minzoom = int(row["lo"]) if row["lo"] is not None else self.minzoom
                self.maxzoom = int(row["hi"]) if row["hi"] is not None else self.maxzoom
        except (sqlite3.Error, TypeError, ValueError):
            pass

    def _bounds_from_tiles(self) -> list[float] | None:
        """Approximate coverage from tile indexes when metadata has no bounds."""
        try:
            if self.path.stat().st_size > 1_500_000_000 and not self.indexed:
                return None
        except OSError:
            pass
        try:
            row = self.conn.execute(
                f"SELECT MIN(zoom_level) AS z FROM {self._tile_source()}"
            ).fetchone()
            if not row or row["z"] is None:
                return None
            zoom = int(row["z"])
            ext = self.conn.execute(
                f"SELECT MIN(tile_column) AS x0, MAX(tile_column) AS x1, "
                f"MIN(tile_row) AS y0, MAX(tile_row) AS y1 FROM {self._tile_source()} "
                f"WHERE zoom_level=?",
                (zoom,),
            ).fetchone()
            if not ext or ext["x0"] is None:
                return None
            n = 1 << zoom
            west = int(ext["x0"]) / n * 360.0 - 180.0
            east = (int(ext["x1"]) + 1) / n * 360.0 - 180.0
            if self.scheme == "xyz":
                north_row = int(ext["y0"])
                south_row = int(ext["y1"]) + 1
            else:
                # TMS rows increase northwards; convert to XYZ rows.
                north_row = n - 1 - int(ext["y1"])
                south_row = n - int(ext["y0"])
            north = math.degrees(math.atan(math.sinh(
                math.pi * (1.0 - 2.0 * north_row / n))))
            south = math.degrees(math.atan(math.sinh(
                math.pi * (1.0 - 2.0 * south_row / n))))
            return [west, south, east, north]
        except (sqlite3.Error, TypeError, ValueError):
            return None

    # ---------------------------------------------------------------- reads
    @property
    def vector(self) -> bool:
        return self.format in VECTOR_FORMATS

    @property
    def usable(self) -> bool:
        """Readable archive, raster or vector."""
        return self.conn is not None

    @property
    def raster(self) -> bool:
        return self.usable and not self.vector

    def _ensure_current(self) -> None:
        if self.conn is None:
            # Avoid hammering a removed/broken USB path once per tile.
            if time.monotonic() - self._failed_at < 5.0:
                return
            self._open()
            return
        now = time.monotonic()
        if now - self._checked < ARCHIVE_RECHECK_SECONDS:
            return
        self._checked = now
        signature = self._signature()
        if signature != self._stat:
            self._close()
            self._open()

    def tile(self, z: int, x: int, y: int, *, vector: bool = False) -> bytes | None:
        if self.vector and not vector:
            return None
        if self.minzoom is not None and z < self.minzoom:
            return None
        if self.maxzoom is not None and z > self.maxzoom:
            return None
        with self.lock:
            try:
                self._ensure_current()
            except Exception as exc:
                self.error = str(exc)[:240]
                return None
            if self.conn is None:
                return None
            row_y = y if self.scheme == "xyz" else ((1 << z) - 1 - y)
            try:
                row = self.conn.execute(
                    f"SELECT tile_data FROM {self._tile_source()} "
                    "WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, row_y),
                ).fetchone()
            except sqlite3.Error:
                # A removed USB stick invalidates the handle; reopen next time.
                self._close()
                return None
            if not row:
                return None
            data = bytes(row[0] or b"")
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                return None
        return data or None

    def describe(self) -> dict[str, Any]:
        with self.lock:
            try:
                size = self.path.stat().st_size
            except OSError:
                size = None
            out: dict[str, Any] = {
                "path": str(self.path),
                "name": self.name or self.path.stem,
                "file": self.path.name,
                "size_bytes": size,
                "format": self.format or "unknown",
                "scheme": self.scheme,
                "minzoom": self.minzoom,
                "maxzoom": self.maxzoom,
                "bounds": self.bounds,
                "vector": self.vector,
                "raster": self.raster,
                "usable": self.usable,
                "has_tiles": self.has_tiles,
                "vector_layers": list(self.vector_layers),
                "attribution": self.attribution,
                "indexed": self.indexed,
                "legacy": self.legacy,
                "error": self.error,
            }
        return out

    def sample_tile(self) -> dict[str, Any] | None:
        """Read one real tile, so diagnostics can prove the pack serves data."""
        with self.lock:
            try:
                self._ensure_current()
            except Exception as exc:
                return {"ok": False, "error": str(exc)[:200]}
            if self.conn is None:
                return {"ok": False, "error": self.error or "archive is not open"}
            try:
                if self.minzoom is not None:
                    row = self.conn.execute(
                        f"SELECT zoom_level, tile_column, tile_row FROM {self._tile_source()} "
                        "WHERE zoom_level=? LIMIT 1",
                        (int(self.minzoom),),
                    ).fetchone()
                else:
                    row = None
                if row is None:
                    row = self.conn.execute(
                        f"SELECT zoom_level, tile_column, tile_row FROM {self._tile_source()} "
                        "ORDER BY zoom_level LIMIT 1"
                    ).fetchone()
            except sqlite3.Error as exc:
                return {"ok": False, "error": str(exc)[:200]}
            if not row:
                return {"ok": False, "error": "no tile rows"}
            z = int(row["zoom_level"])
            x = int(row["tile_column"])
            row_y = int(row["tile_row"])
            y = row_y if self.scheme == "xyz" else ((1 << z) - 1 - row_y)
        data = self.tile(z, x, y, vector=self.vector)
        return {
            "ok": bool(data),
            "z": z, "x": x, "y": y,
            "bytes": len(data or b""),
            "error": None if data else "tile could not be read",
        }



# ``_checked`` is set in Archive.__init__; it only guards the periodic
# re-stat that notices an archive being replaced or its USB being re-plugged.


def _archive_for(path: Path) -> Archive | None:
    key = str(path)
    now = time.monotonic()
    with _lock:
        entry = _archive_cache.get(key)
        if entry:
            archive = entry["archive"]
            # Keep working archives open; retry an unusable one occasionally so
            # a corrected or re-plugged file is picked up without a restart.
            if archive.usable or now - entry["at"] < ARCHIVE_RECHECK_SECONDS:
                return archive
    archive = Archive(path)
    with _lock:
        _archive_cache[key] = {"at": now, "archive": archive}
    return archive


def lookup_tile(z: int, x: int, y: int, raw: str | None = None) -> tuple[bytes, str] | None:
    """Return ``(raster_tile_bytes, archive_name)`` from the configured source."""
    raw = configured_path() if raw is None else raw
    for path in resolve_sources(raw):
        archive = _archive_for(path)
        if archive is None or not archive.raster:
            continue
        data = archive.tile(z, x, y)
        if data:
            return data, archive.path.name
    loose = _configured_loose(raw)
    if loose and loose.get("raster"):
        data = loose_lookup(loose, z, x, y)
        if data:
            return data, f"{loose.get('name') or 'loose-tiles'}"
    return None


def lookup_tile_ancestor(z: int, x: int, y: int, raw: str | None = None,
                         max_up: int = 5) -> tuple[bytes, str] | None:
    """Nearest lower-zoom raster tile in the archive, for offline overzoom."""
    raw = configured_path() if raw is None else raw
    for path in resolve_sources(raw):
        archive = _archive_for(path)
        if archive is None or not archive.raster:
            continue
        for up in range(1, max_up + 1):
            zz = z - up
            if zz < 0:
                break
            data = archive.tile(zz, x >> up, y >> up)
            if data:
                return data, archive.path.name
    loose = _configured_loose(raw)
    if loose and loose.get("raster"):
        for up in range(1, max_up + 1):
            zz = z - up
            if zz < 0:
                break
            data = loose_lookup(loose, zz, x >> up, y >> up)
            if data:
                return data, f"{loose.get('name') or 'loose-tiles'}-overzoom"
    return None


def lookup_vector_tile(z: int, x: int, y: int, raw: str | None = None) -> tuple[bytes, str] | None:
    """Return ``(pbf_tile_bytes, archive_name)`` from a vector archive."""
    raw = configured_path() if raw is None else raw
    for path in resolve_sources(raw):
        archive = _archive_for(path)
        if archive is None or not archive.vector or not archive.usable:
            continue
        data = archive.tile(z, x, y, vector=True)
        if data:
            return data, archive.path.name
    loose = _configured_loose(raw)
    if loose and loose.get("vector"):
        data = loose_lookup(loose, z, x, y)
        if data:
            return data, f"{loose.get('name') or 'loose-tiles'}"
    return None


def revision(raw: str | None = None) -> str:
    """Short fingerprint of the configured archives for cache-busting URLs.

    Browsers cache tiles for a week; including this in the map tile URL makes a
    swapped archive or a re-plugged USB drive render immediately.
    """
    raw = configured_path() if raw is None else raw
    parts: list[str] = []
    for path in resolve_sources(raw):
        try:
            st = path.stat()
            parts.append(f"{path}|{st.st_size}|{st.st_mtime_ns}")
        except OSError:
            parts.append(str(path))
    if not parts:
        loose = _configured_loose(raw)
        if loose and loose.get("base"):
            try:
                st = Path(loose["base"]).stat()
                parts.append(f"{loose['base']}|{loose.get('minzoom')}|{loose.get('maxzoom')}|{st.st_mtime_ns}")
            except OSError:
                parts.append(str(loose.get("base")))
    if not parts:
        return ""
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def describe_source(raw: str | None = None) -> dict[str, Any]:
    """Describe the configured source for the map/settings UI."""
    raw = configured_path() if raw is None else raw
    paths = resolve_sources(raw)
    archives = [a.describe() for a in (_archive_for(p) for p in paths) if a is not None]
    loose: dict[str, Any] | None = None
    if not archives:
        loose = _configured_loose(raw)
        if loose is not None:
            archives = [loose]
    readable = [a for a in archives if a.get("usable")]
    configured_text = str(raw or "")
    configured_path_obj = Path(configured_text).expanduser() if configured_text.strip() else None
    configured_exists = False
    if configured_path_obj is not None:
        try:
            configured_exists = configured_path_obj.exists()
        except OSError:
            configured_exists = False
    resolved: list[str] = []
    if configured_path_obj is not None:
        try:
            exact = configured_path_obj.resolve()
        except OSError:
            exact = configured_path_obj
        for path in paths:
            try:
                candidate = path.resolve()
            except OSError:
                candidate = path
            # A folder selection resolves to its archives, which is expected.
            if candidate != exact and str(candidate) not in resolved:
                resolved.append(str(candidate))
        if loose is not None and not configured_exists:
            loose_path = str(loose.get("path") or "")
            if loose_path and loose_path not in resolved:
                resolved.insert(0, loose_path)
    return {
        "configured": bool(configured_text.strip()),
        "path": configured_text,
        "archives": archives,
        "count": len(archives),
        "usable": bool(readable),
        "raster": any(a.get("raster") for a in archives),
        "vector": any(a.get("vector") for a in archives),
        "resolved_path": (resolved[0] if resolved and configured_text.strip()
                          and not configured_exists else None),
        "resolved": bool(resolved and configured_text.strip()
                         and not configured_exists),
        "suggestions": [] if readable else (_suggest(configured_path_obj)
                                           if configured_path_obj is not None else []),
        "error": None if not archives else next(
            (a["error"] for a in archives if a.get("error")), None
        ),
    }


def _missing_component(path: Path) -> Path | None:
    """First missing ancestor, so an error can say which part is absent."""
    current = path
    while True:
        parent = current.parent
        if parent == current:
            return current
        try:
            if parent.exists():
                return current if not current.exists() else None
        except OSError:
            return None
        current = parent


def _access_error(path: Path) -> str:
    try:
        import getpass
        user = getpass.getuser()
    except Exception:
        user = "the DarkSec process"
    return (f"Permission denied — the DarkSec process (user {user}) cannot read {path}. "
            "Check the mount permissions or run DarkSec as the desktop user who mounted the drive.")


def _suggest(path: Path) -> list[dict[str, Any]]:
    """Describe archives or tile folders with the same name on a mounted drive."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        found = _auto_resolve(str(path))
    except Exception:
        found = []
    for candidate in found[:5]:
        archive = _archive_for(candidate)
        if archive is None:
            continue
        info = archive.describe()
        info["ok"] = bool(info.get("usable"))
        info["path"] = str(candidate)
        seen.add(str(candidate))
        out.append(info)
    name = path.name
    if name and len(out) < 5:
        for root in search_roots():
            candidate = root / name
            key = str(candidate)
            if key in seen:
                continue
            try:
                if not candidate.is_dir():
                    continue
            except OSError:
                continue
            info = loose_info(candidate)
            if info is not None:
                info["ok"] = True
                seen.add(key)
                out.append(info)
                if len(out) >= 5:
                    break
    return out


def describe_path(path_text: str) -> dict[str, Any]:
    """Validate one operator-supplied path (file or folder)."""
    text = str(path_text or "").strip()
    path = Path(text).expanduser()
    if not text:
        return {"ok": False, "error": "No path supplied"}
    try:
        path.stat()
    except PermissionError:
        return {"ok": False, "path": str(path), "error": _access_error(path),
                "suggestions": _suggest(path)}
    except FileNotFoundError:
        missing = _missing_component(path)
        hint = f" Missing component: {missing}" if missing and missing != path else ""
        return {"ok": False, "path": str(path),
                "error": f"Path does not exist: {path}.{hint}",
                "suggestions": _suggest(path)}
    except OSError as exc:
        return {"ok": False, "path": str(path),
                "error": f"Cannot access {path}: {exc.strerror or exc}",
                "suggestions": _suggest(path)}
    if not os.access(path, os.R_OK):
        return {"ok": False, "path": str(path), "error": _access_error(path)}
    if path.is_file():
        if not _is_archive(path):
            return {"ok": False, "error": "Not an .mbtiles archive"}
        archive = _archive_for(path)
        info = archive.describe() if archive else {"error": "Could not open archive"}
        info["ok"] = bool(info.get("usable"))
        return info
    if not path.is_dir():
        return {"ok": False, "path": str(path), "error": "Not a file or folder"}
    if not os.access(path, os.R_OK | os.X_OK):
        return {"ok": False, "path": str(path), "error": _access_error(path)}
    archives = [] if _is_tile_tree_root(path) else [
        a.describe() for a in (_archive_for(p) for p in _iter_archives(path)) if a
    ]
    readable = [a for a in archives if a.get("usable")]
    if not readable:
        loose = loose_info(path)
        if loose is not None:
            return {
                "ok": True,
                "path": str(path),
                "directory": True,
                "archives": [loose],
                "count": 1,
                "usable": True,
                "raster": bool(loose.get("raster")),
                "vector": bool(loose.get("vector")),
                "loose": True,
                "scheme": "xyz",
                "format": loose.get("format"),
                "minzoom": loose.get("minzoom"),
                "maxzoom": loose.get("maxzoom"),
                "bounds": loose.get("bounds"),
                "vector_layers": loose.get("vector_layers", []),
                "suggestions": [],
                "error": None,
            }
    return {
        "ok": bool(readable),
        "path": str(path),
        "directory": True,
        "archives": archives,
        "count": len(archives),
        "usable": bool(readable),
        "raster": any(a.get("raster") for a in archives),
        "vector": any(a.get("vector") for a in archives),
        "suggestions": [] if readable else _suggest(path),
        "error": None if readable else (
            f"No .mbtiles archives or {{z}}/{{x}}/{{y}}.pbf tile folders found in {path}. "
            "Archives are searched at the top level and up to three subfolders down."
        ),
    }


# --------------------------------------------------------------------------
# Discovery of archives on removable media
# --------------------------------------------------------------------------

_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".svn", ".cache", "lost+found",
    "system volume information", "$recycle.bin", "proc", "sys", "dev",
    "windows", "program files", "program files (x86)", "programdata",
}


def _is_mount_root(path: Path) -> bool:
    text = str(path)
    if os.name == "nt":
        return len(text) <= 3 and text[1:3] == ":\\"
    return text.startswith(("/media", "/run/media", "/mnt", "/Volumes", "/run/user", "/run/host"))


_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "pstore",
    "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "mqueue",
    "hugetlbfs", "bpf", "autofs", "binfmt_misc", "efivarfs", "ramfs", "rpc_pipefs",
    "nsfs", "overlay", "squashfs", "nfsd", "selinuxfs", "systemd-1",
}
_SYSTEM_MOUNTS = {
    "/", "/boot", "/boot/efi", "/usr", "/var", "/var/lib", "/etc", "/opt",
    "/srv", "/tmp", "/proc", "/sys", "/dev", "/run", "/snap", "/var/snap",
}
_REMOVABLE_FS = {
    "vfat", "exfat", "msdos", "ntfs", "ntfs3", "fuseblk", "iso9660", "udf",
    "hfsplus", "hfs", "apfs", "cifs", "smbfs", "nfs", "nfs4", "fuse", "fuse.sshfs",
}


def _unescape_mount(value: str) -> str:
    """Decode the octal escapes used by /proc/mounts (``\\040`` = space)."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 3 < len(value) and value[i + 1:i + 4].isdigit():
            try:
                out.append(chr(int(value[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def mount_entries() -> list[dict[str, Any]]:
    """Real mount points from the kernel, including drives mounted anywhere.

    ``/proc/mounts`` is the only reliable way to see a USB drive mounted
    outside /media (for example under the user's home) or through GVFS. The
    scan roots below are derived from this list as well as the usual paths.
    """
    entries: list[dict[str, Any]] = []
    if os.name == "nt":
        return entries
    seen: set[str] = set()
    for source in ("/proc/self/mounts", "/proc/mounts", "/etc/mtab"):
        try:
            lines = Path(source).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue
            device, mountpoint, fstype = parts[0], _unescape_mount(parts[1]), parts[2]
            if mountpoint in seen:
                continue
            if fstype in _PSEUDO_FS:
                continue
            text = str(mountpoint)
            interesting = (
                fstype in _REMOVABLE_FS
                or _is_mount_root(Path(text))
                or text.startswith(str(Path.home()))
            )
            if not interesting or text in _SYSTEM_MOUNTS:
                continue
            seen.add(text)
            try:
                path = Path(text)
                entries.append({
                    "path": text,
                    "device": device,
                    "fstype": fstype,
                    "exists": path.is_dir(),
                    "readable": path.is_dir() and os.access(path, os.R_OK | os.X_OK),
                })
            except OSError:
                continue
        if entries:
            break
    return entries


def _gvfs_roots() -> list[Path]:
    roots: list[Path] = []
    try:
        roots.append(Path.home() / ".gvfs")
    except Exception:
        pass
    try:
        for base in Path("/run/user").iterdir():
            if base.is_dir():
                roots.append(base / "gvfs")
    except OSError:
        pass
    return roots


def search_roots() -> list[Path]:
    # Removable media first: a huge home folder must never consume the scan
    # budget before the USB drive is reached.
    roots: list[Path] = []
    if os.name == "nt":
        # USB sticks normally mount as D:..Z:; never crawl the system drive.
        for letter in string.ascii_uppercase[3:]:
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.append(drive)
            except OSError:
                continue
    else:
        # Kernel mount points first: this catches /media/<user>/<label>,
        # custom mounts and GVFS shares that the fixed paths below miss.
        for entry in mount_entries():
            if entry["readable"]:
                roots.append(Path(entry["path"]))
        for base in ("/media", "/run/media", "/mnt", "/Volumes", "/run/host"):
            p = Path(base)
            try:
                if p.is_dir():
                    roots.append(p)
            except OSError:
                continue
        roots += _gvfs_roots()
    try:
        from . import config

        roots += [config.DATA_DIR, config.RESOURCE_ROOT / "maps"]
    except Exception:
        pass
    roots.append(Path.home())
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        try:
            if root.is_dir():
                out.append(root)
        except OSError:
            continue
    return out



def _under_app_tiles(path: Path) -> bool:
    try:
        from . import config

        return path.resolve().is_relative_to(Path(config.MAP_TILE_DIR).resolve())
    except Exception:
        return False


def scan(extra_roots: list[Path] | None = None, *, budget: float = SCAN_BUDGET_SECONDS,
         depth: int = SCAN_DEPTH, limit: int = SCAN_MAX_RESULTS) -> list[dict[str, Any]]:
    """Find ``.mbtiles`` archives on mounted media within a time budget."""
    started = time.monotonic()
    roots = list(extra_roots or []) + search_roots()
    found: list[Path] = []
    loose_roots: list[Path] = []
    seen_files: set[str] = set()
    seen_loose: set[str] = set()
    for root in roots:
        if time.monotonic() - started > budget or len(found) >= limit:
            break
        # Mount points are usually shallow and quick; cap the home tree so it
        # cannot use the entire budget on unrelated folders.
        remaining = budget - (time.monotonic() - started)
        share = remaining if _is_mount_root(root) else min(remaining, max(1.0, budget * 0.3))
        root_deadline = time.monotonic() + share
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and time.monotonic() <= root_deadline and len(found) < limit:
            folder, level = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if time.monotonic() > root_deadline or len(found) >= limit:
                    break
                try:
                    if entry.is_file(follow_symlinks=False):
                        name = entry.name.lower()
                        if name.endswith(".mbtiles") or (
                                name.endswith((".pbf", ".mvt"))
                                and _is_archive(Path(entry.path))):
                            key = str(Path(entry.path).resolve())
                            if key not in seen_files:
                                seen_files.add(key)
                                found.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False) and level < depth:
                        name = entry.name.lower()
                        if name.startswith((".", "$")) or name in _SKIP_DIRS:
                            continue
                        path = Path(entry.path)
                        # A `{z}/{x}/{y}.pbf` tree is offered as one source
                        # rather than crawled as thousands of tiny folders.
                        if name.isdigit() and _is_zoom_dir(path):
                            parent = path.parent
                            key = str(parent.resolve())
                            if key not in seen_loose and not _under_app_tiles(parent):
                                seen_loose.add(key)
                                loose_roots.append(parent)
                            continue
                        stack.append((path, level + 1))
                except OSError:
                    continue
    items = []
    for path in found:
        archive = _archive_for(path)
        if archive is None:
            continue
        info = archive.describe()
        info["ok"] = bool(info.get("usable"))
        items.append(info)
    for path in loose_roots:
        info = loose_info(path)
        if info is not None:
            info["ok"] = bool(info.get("usable"))
            items.append(info)
    items.sort(key=lambda i: (not i.get("usable"), i.get("name", "").lower()))
    return items


def cached_scan() -> list[dict[str, Any]]:
    now = time.monotonic()
    with _lock:
        if now - _scan_cache["at"] < 15:
            return list(_scan_cache["items"])
    items = scan()
    with _lock:
        _scan_cache.update({"at": now, "items": items})
    return items


def diagnose(raw: str | None = None) -> dict[str, Any]:
    """Full, copyable USB/MBTiles diagnostic for the Settings screen.

    This exists because a beta tester's drive cannot be inspected directly. The
    report answers the questions that matter: is the path visible to *this*
    process, where is the drive actually mounted, can SQLite open the archive,
    what format/bounds/zoom does it have, and does a real tile read succeed.
    """
    import getpass
    import platform

    raw_text = configured_path() if raw is None else str(raw or "")
    text = raw_text.strip()
    path = Path(text).expanduser() if text else None
    info = describe_path(text) if text else {"ok": False, "error": "No path configured"}

    path_state: dict[str, Any] = {"configured": text, "expanded": str(path) if path else ""}
    if path is not None:
        try:
            st = path.stat()
            path_state.update({
                "exists": True,
                "is_file": path.is_file(),
                "is_dir": path.is_dir(),
                "size_bytes": st.st_size,
                "mode": oct(st.st_mode & 0o777),
                "readable": os.access(path, os.R_OK),
                "executable": os.access(path, os.X_OK),
            })
        except FileNotFoundError:
            path_state.update({"exists": False, "error": "path does not exist"})
            missing = _missing_component(path)
            if missing:
                path_state["missing_component"] = str(missing)
        except PermissionError:
            path_state.update({"exists": None, "error": _access_error(path)})
        except OSError as exc:
            path_state.update({"exists": None, "error": str(exc)[:200]})

    mounts = mount_entries()
    for mount in mounts:
        try:
            mount["archive_count"] = len(_walk_archives(
                Path(mount["path"]), depth=2,
                deadline=time.monotonic() + 0.6))
        except Exception:
            mount["archive_count"] = 0

    found: list[dict[str, Any]] = []
    try:
        for item in scan(budget=4.0):
            found.append({
                "path": item.get("path"),
                "format": item.get("format"),
                "vector": item.get("vector"),
                "usable": item.get("usable"),
                "error": item.get("error"),
            })
    except Exception as exc:
        found = [{"path": None, "error": str(exc)[:200]}]

    samples: list[dict[str, Any]] = []
    for archive_path in resolve_sources(text):
        archive = _archive_for(archive_path)
        if archive is None:
            continue
        sample = archive.sample_tile()
        sample["path"] = str(archive_path)
        sample["format"] = archive.format or "unknown"
        samples.append(sample)
    if not samples:
        loose = _configured_loose(text)
        if loose is not None:
            sample_path = loose.get("sample")
            data: bytes | None = None
            z = x = y = 0
            if sample_path:
                try:
                    z = int(Path(sample_path).parent.parent.name)
                    x = int(Path(sample_path).parent.name)
                    y = int(Path(sample_path).stem)
                except (TypeError, ValueError):
                    z = x = y = 0
                try:
                    data = Path(sample_path).read_bytes()
                    if data[:2] == b"\x1f\x8b":
                        data = gzip.decompress(data)
                except Exception:
                    data = None
            if not data:
                zoom = int(loose.get("minzoom") or 0)
                data = loose_lookup(loose, zoom, 0, 0)
            samples.append({
                "path": sample_path or str(loose.get("base") or loose.get("path")),
                "format": loose.get("format") or "unknown",
                "z": z, "x": x, "y": y,
                "bytes": len(data or b""),
                "ok": bool(data),
                "error": None if data else "no readable tile found in the tree",
            })

    try:
        from . import __version__ as app_version
    except Exception:
        app_version = "?"
    env_flags = []
    for name in ("SNAP", "SNAP_NAME", "FLATPAK_ID", "container"):
        if os.environ.get(name):
            env_flags.append(f"{name}={os.environ.get(name)}")
    try:
        user = getpass.getuser()
    except Exception:
        user = "?"
    try:
        uid = os.getuid()
    except AttributeError:
        uid = None

    lines = [
        "DarkSec MBTiles diagnostics",
        f"version: {app_version}",
        f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"process user: {user} (uid {uid})",
        f"platform: {platform.platform()}",
        f"python: {platform.python_version()}",
        f"sandbox: {', '.join(env_flags) if env_flags else 'none detected'}",
        "",
        f"configured path: {text or '(empty)'}",
        f"resolved path:   {str(path) if path else '(none)'}",
    ]
    for key in ("exists", "is_file", "is_dir", "size_bytes", "mode", "readable", "executable",
                "missing_component", "error"):
        if key in path_state:
            lines.append(f"  {key}: {path_state[key]}")
    if info.get("usable"):
        lines.append(f"archive: READY · {str(info.get('format') or '?').upper()} · "
                     f"z{info.get('minzoom')}–{info.get('maxzoom')} · "
                     f"{'vector' if info.get('vector') else 'raster'}")
    else:
        lines.append(f"archive: NOT USABLE · {info.get('error') or 'unknown error'}")
    if info.get("suggestions"):
        lines.append("found with the same name at:")
        for item in info["suggestions"]:
            lines.append(f"  {item.get('path')} ({'usable' if item.get('usable') else item.get('error')})")
    lines.append("")
    lines.append("mount points visible to this process:")
    if mounts:
        for mount in mounts:
            lines.append(f"  {mount['path']} · {mount['fstype']} · "
                         f"{mount['device']} · readable={mount['readable']} · "
                         f"archives={mount['archive_count']}")
    else:
        lines.append("  (none; /proc/mounts was not readable)")
    lines.append("")
    lines.append("scan roots:")
    for root in search_roots():
        lines.append(f"  {root} · readable={os.access(root, os.R_OK | os.X_OK)}")
    lines.append("")
    lines.append(f"archives found by scan ({len(found)}):")
    for item in found[:20]:
        if item.get("path"):
            lines.append(f"  {item['path']} · {str(item.get('format') or '?').upper()} · "
                         f"{'vector' if item.get('vector') else 'raster'} · "
                         f"{'usable' if item.get('usable') else item.get('error') or 'unusable'}")
        else:
            lines.append(f"  scan error: {item.get('error')}")
    lines.append("")
    lines.append("sample tile read:")
    if samples:
        for sample in samples:
            if sample.get("ok"):
                lines.append(f"  {sample['path']} · z{sample['z']} x{sample['x']} y{sample['y']} "
                             f"· {sample['bytes']} bytes · {sample.get('format')}")
            else:
                lines.append(f"  {sample['path']} · FAILED · {sample.get('error')}")
    else:
        lines.append("  (none; the configured source has no readable archives)")

    return {
        "ok": bool(info.get("usable")),
        "configured": text,
        "path": path_state,
        "archive": info,
        "mounts": mounts,
        "roots": [str(p) for p in search_roots()],
        "found": found,
        "samples": samples,
        "report": "\n".join(lines),
    }
