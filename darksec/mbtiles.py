"""Read-only MBTiles support for offline basemaps.

An MBTiles archive is a SQLite database that is usually carried on removable
media, so everything here opens it read-only and never writes to it. The
module supports:

  * a single ``.mbtiles`` file or a directory containing several archives;
  * the MBTiles TMS row order (the spec default) and non-standard XYZ archives
    that advertise ``scheme=xyz`` in their metadata;
  * gzip-compressed raster tile blobs;
  * raster archives only (vector ``pbf``/``mvt`` archives cannot be drawn by
    the raster basemap and are reported as unsupported instead of failing
    silently).

Paths are cached briefly so a settings change takes effect immediately, while
tile lookups avoid reopening the database (or the main DarkSec database) for
every tile request.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import string
import threading
import time
from pathlib import Path
from typing import Any

CONFIG_TTL_SECONDS = 5.0
ARCHIVE_RECHECK_SECONDS = 10.0
SCAN_DEPTH = 5
SCAN_BUDGET_SECONDS = 4.0
SCAN_MAX_RESULTS = 80
VECTOR_FORMATS = {"pbf", "mvt", "vector", "protobuf"}

_lock = threading.RLock()
_config_cache: dict[str, Any] = {"at": 0.0, "raw": None}
_sources_cache: dict[str, tuple[float, list[Path]]] = {}
_archive_cache: dict[str, dict[str, Any]] = {}
_scan_cache: dict[str, Any] = {"at": 0.0, "items": []}


def _is_archive(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() == ".mbtiles"
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


def _iter_archives(folder: Path) -> list[Path]:
    """Top-level archives plus one level of subfolders, without following links."""
    found: list[Path] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.is_file() and entry.name.lower().endswith(".mbtiles"):
                found.append(Path(entry.path))
            elif entry.is_dir() and not entry.name.startswith((".", "$")):
                try:
                    for child in os.scandir(entry.path):
                        if child.is_file() and child.name.lower().endswith(".mbtiles"):
                            found.append(Path(child.path))
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(found, key=lambda p: p.name.lower())


def resolve_sources(raw: str | None) -> list[Path]:
    """Turn the configured setting into a list of archives.

    A file path yields that file. A directory yields its ``.mbtiles`` archives
    (top level and one level down). Missing paths yield an empty list.
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
            found = _iter_archives(path)
    except OSError:
        found = []
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
        return bool(_iter_archives(path))
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
        _archive_cache.clear()


def _connect(path: Path) -> sqlite3.Connection:
    """Open an archive read-only, including from a read-only USB filesystem."""
    uri = path.resolve().as_uri()
    attempts = (uri + "?mode=ro", uri + "?mode=ro&immutable=1")
    last: Exception | None = None
    for target in attempts:
        try:
            conn = sqlite3.connect(target, uri=True, timeout=3)
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
        self.name = path.stem
        self._open()

    # ---------------------------------------------------------------- setup
    def _signature(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return None

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
        self.metadata = {}
        try:
            self.conn = _connect(self.path)
            present = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='tiles' AND type IN ('table', 'view')"
            ).fetchone()
            if not present:
                raise RuntimeError("archive has no tiles table")
            self.metadata = self._read_metadata()
            self._apply_metadata()
            try:
                self.indexed = bool(self.conn.execute("PRAGMA index_list('tiles')").fetchall())
            except sqlite3.Error:
                self.indexed = False
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
        if not self.format:
            self.format = self._sniff_format()
        if self.minzoom is None or self.maxzoom is None:
            self._zoom_range()

    def _probe_vector_layers(self) -> list[str]:
        """Fallback for archives that omit the metadata json vector_layers."""
        try:
            row = self.conn.execute(
                "SELECT tile_data FROM tiles ORDER BY zoom_level LIMIT 1"
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
            row = self.conn.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
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
                "SELECT MIN(zoom_level) AS lo, MAX(zoom_level) AS hi FROM tiles"
            ).fetchone()
            if row:
                self.minzoom = int(row["lo"]) if row["lo"] is not None else self.minzoom
                self.maxzoom = int(row["hi"]) if row["hi"] is not None else self.maxzoom
        except (sqlite3.Error, TypeError, ValueError):
            pass

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
                    "SELECT tile_data FROM tiles "
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
            "vector_layers": self.vector_layers,
            "attribution": self.attribution,
            "indexed": self.indexed,
            "error": self.error,
        }
        return out


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
        return ""
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:12]


def describe_source(raw: str | None = None) -> dict[str, Any]:
    """Describe the configured source for the map/settings UI."""
    raw = configured_path() if raw is None else raw
    paths = resolve_sources(raw)
    archives = [a.describe() for a in (_archive_for(p) for p in paths) if a is not None]
    readable = [a for a in archives if a.get("usable")]
    return {
        "configured": bool(str(raw or "").strip()),
        "path": str(raw or ""),
        "archives": archives,
        "count": len(archives),
        "usable": bool(readable),
        "raster": any(a.get("raster") for a in archives),
        "vector": any(a.get("vector") for a in archives),
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


def describe_path(path_text: str) -> dict[str, Any]:
    """Validate one operator-supplied path (file or folder)."""
    text = str(path_text or "").strip()
    path = Path(text).expanduser()
    if not text:
        return {"ok": False, "error": "No path supplied"}
    try:
        path.stat()
    except PermissionError:
        return {"ok": False, "path": str(path), "error": _access_error(path)}
    except FileNotFoundError:
        missing = _missing_component(path)
        hint = f" Missing component: {missing}" if missing and missing != path else ""
        return {"ok": False, "path": str(path), "error": f"Path does not exist: {path}.{hint}"}
    except OSError as exc:
        return {"ok": False, "path": str(path),
                "error": f"Cannot access {path}: {exc.strerror or exc}"}
    if not os.access(path, os.R_OK):
        return {"ok": False, "path": str(path), "error": _access_error(path)}
    if path.is_file():
        if path.suffix.lower() != ".mbtiles":
            return {"ok": False, "error": "Not an .mbtiles archive"}
        archive = _archive_for(path)
        info = archive.describe() if archive else {"error": "Could not open archive"}
        info["ok"] = bool(info.get("usable"))
        return info
    if not path.is_dir():
        return {"ok": False, "path": str(path), "error": "Not a file or folder"}
    if not os.access(path, os.R_OK | os.X_OK):
        return {"ok": False, "path": str(path), "error": _access_error(path)}
    archives = [a.describe() for a in (_archive_for(p) for p in _iter_archives(path)) if a]
    readable = [a for a in archives if a.get("usable")]
    return {
        "ok": bool(readable),
        "path": str(path),
        "directory": True,
        "archives": archives,
        "count": len(archives),
        "usable": bool(readable),
        "raster": any(a.get("raster") for a in archives),
        "vector": any(a.get("vector") for a in archives),
        "error": None if readable else (
            f"No .mbtiles archives found in this folder ({path}). "
            "Put the archive directly in this folder or in a first-level subfolder."
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
    return text.startswith(("/media", "/run/media", "/mnt", "/Volumes"))


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
        for base in ("/media", "/run/media", "/mnt", "/Volumes"):
            p = Path(base)
            try:
                if p.is_dir():
                    roots.append(p)
            except OSError:
                continue
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


def scan(extra_roots: list[Path] | None = None, *, budget: float = SCAN_BUDGET_SECONDS,
         depth: int = SCAN_DEPTH, limit: int = SCAN_MAX_RESULTS) -> list[dict[str, Any]]:
    """Find ``.mbtiles`` archives on mounted media within a time budget."""
    started = time.monotonic()
    roots = list(extra_roots or []) + search_roots()
    found: list[Path] = []
    seen_files: set[str] = set()
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
                        if entry.name.lower().endswith(".mbtiles"):
                            key = str(Path(entry.path).resolve())
                            if key not in seen_files:
                                seen_files.add(key)
                                found.append(Path(entry.path))
                    elif entry.is_dir(follow_symlinks=False) and level < depth:
                        name = entry.name.lower()
                        if name.startswith((".", "$")) or name in _SKIP_DIRS:
                            continue
                        stack.append((Path(entry.path), level + 1))
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
