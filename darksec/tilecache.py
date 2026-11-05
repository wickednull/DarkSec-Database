"""Offline tile cache — download map tiles while online, use them offline.

Tiles are stored on disk as plain PNG/JPEG files:

    maps/tiles/cache/<source>/<z>/<x>/<y>.<ext>

Design rules that matter in the field:

* A tile is only written to disk after it passes validation. Tile providers
  answer policy violations with HTTP 200 + an error image ("Access blocked"),
  so status code alone is not enough.
* Every request carries a descriptive User-Agent and is rate limited.
* Downloads run in a background thread with a pollable job so the UI can show
  real progress instead of freezing.
* Nothing here invents data. If upstream is unavailable, we say so.

Providers are chosen because they permit caching for personal/offline use:
USGS The National Map (public domain), Esri World Imagery/Street (basemap
attribution), CARTO basemaps (OSM data, attribution), OpenTopoMap (CC-BY-SA).
OpenStreetMap's own tile server blocks datacenter/personal clients aggressively
and is kept only as a fallback.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from . import config

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
# ArcGIS-backed services use {z}/{y}/{x}; standard XYZ uses {z}/{x}/{y}.
TILE_SOURCES: dict[str, dict] = {
    "esri_imagery": {
        "name": "Satellite imagery",
        "url_template": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, Maxar, Earthstar Geographics",
        "max_zoom": 19,
        "ext": "jpg",
        "note": "Best for reading terrain, water, buildings and routes.",
    },
    "esri_street": {
        "name": "Street map",
        "url_template": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, HERE, Garmin, OpenStreetMap contributors",
        "max_zoom": 19,
        "ext": "jpg",
        "note": "Roads, place names and infrastructure.",
    },
    "carto_dark": {
        "name": "Dark tactical",
        "url_template": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors © CARTO",
        "max_zoom": 19,
        "ext": "png",
        "note": "Low-glare dark basemap for night use.",
    },
    "usgs_topo": {
        "name": "USGS topo (US)",
        "url_template": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
        "attribution": "USGS The National Map (public domain)",
        "max_zoom": 16,
        "ext": "jpg",
        "note": "Contours, hydrography and land cover. United States only.",
    },
    "usgs_imagery": {
        "name": "USGS imagery topo (US)",
        "url_template": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
        "attribution": "USGS The National Map (public domain)",
        "max_zoom": 16,
        "ext": "jpg",
        "note": "Satellite imagery with topo overlay. United States only.",
    },
    "opentopomap": {
        "name": "OpenTopoMap",
        "url_template": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenTopoMap (CC-BY-SA), © OpenStreetMap contributors",
        "max_zoom": 17,
        "ext": "png",
        "note": "Terrain and contours worldwide.",
    },
    "osm": {
        "name": "OpenStreetMap",
        "url_template": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors (ODbL)",
        "max_zoom": 19,
        "ext": "png",
        "note": "Often blocks automated clients; kept as a fallback only.",
    },
}

DEFAULT_SOURCE = "esri_imagery"
USER_AGENT = "DarkSec-FieldConsole/2.0 (offline survival map cache; +https://github.com/darksec)"
REQUEST_TIMEOUT = 20
MIN_REQUEST_INTERVAL = 0.12  # seconds between upstream requests

# Error images returned with HTTP 200. Reject by hash and by marker text.
_ERROR_HASHES = {
    "8e8d1ff492c2bdae445a83ccdb8e449f",  # OSM "Access blocked"
}
_BLOCK_MARKERS = (b"Access blocked", b"Access denied", b"blocked by policy")

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Paths & cache
# ---------------------------------------------------------------------------
def cache_root() -> Path:
    p = config.MAP_TILE_DIR / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tile_cache_dir(source: str = DEFAULT_SOURCE) -> Path:
    """Return (and create if needed) the on-disk cache directory for source."""
    base = cache_root() / _safe(source)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe(name: str) -> str:
    return "".join(c for c in str(name) if c.isalnum() or c in "_-")[:40] or "unknown"


def tile_path(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Path:
    ext = TILE_SOURCES.get(source, {}).get("ext", "png")
    return tile_cache_dir(source) / str(z) / str(x) / f"{y}.{ext}"


def is_cached(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> bool:
    p = tile_path(z, x, y, source)
    return p.exists() and p.stat().st_size > 0


def get_cached(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Optional[bytes]:
    p = tile_path(z, x, y, source)
    if p.exists() and p.stat().st_size > 0:
        try:
            return p.read_bytes()
        except OSError:
            return None
    return None


def find_cached(z: int, x: int, y: int, prefer: Optional[str] = None) -> Optional[tuple[str, bytes]]:
    """Look for a tile in any source's cache. Prefer the active source."""
    order = []
    if prefer and prefer in TILE_SOURCES:
        order.append(prefer)
    order += [s for s in TILE_SOURCES if s not in order]
    for src in order:
        data = get_cached(z, x, y, src)
        if data:
            return src, data
    return None


# ---------------------------------------------------------------------------
# Upstream fetch + validation
# ---------------------------------------------------------------------------
_last_request_at = 0.0
_throttle_lock = threading.Lock()


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        now = time.monotonic()
        wait = MIN_REQUEST_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _media_type(data: bytes, fallback: str) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


class TileBlocked(Exception):
    """The provider refused the request (policy block, quota, auth)."""


def fetch_tile(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Optional[bytes]:
    """Fetch a single tile, validate it, cache it. Returns bytes or None."""
    src = TILE_SOURCES.get(source)
    if not src:
        return None
    if z > src["max_zoom"]:
        return None
    url = src["url_template"].format(z=z, x=x, y=y)
    _throttle()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/jpeg,image/*"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # network down, DNS, timeout
        print(f"Tile fetch error {source} z={z} x={x} y={y}: {exc}")
        return None

    if resp.status_code in (401, 403, 418, 429):
        raise TileBlocked(f"HTTP {resp.status_code} from {source}")
    if resp.headers.get("x-blocked"):
        raise TileBlocked(f"{source}: {resp.headers.get('x-blocked')}")
    if resp.status_code != 200:
        return None

    data = resp.content or b""
    # Reject provider error images served as HTTP 200.
    if len(data) < 120:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if not ctype.startswith("image/"):
        return None
    if hashlib.md5(data).hexdigest() in _ERROR_HASHES:
        raise TileBlocked(f"{source} returned a policy error tile")
    if any(m in data[:4096] for m in _BLOCK_MARKERS):
        raise TileBlocked(f"{source} returned a text error tile")

    media = _media_type(data, "image/png")
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[media]
    p = tile_cache_dir(source) / str(z) / str(x) / f"{y}.{ext}"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(p)
    return data


_online_checked_at = 0.0
_online_value = False
_online_lock = threading.Lock()


def is_online() -> bool:
    """Cheap, cached connectivity probe so offline tile requests fail fast."""
    global _online_checked_at, _online_value
    now = time.monotonic()
    with _online_lock:
        if now < _online_checked_at:
            return _online_value
        try:
            requests.head("https://server.arcgisonline.com/", timeout=4,
                          headers={"User-Agent": USER_AGENT})
            _online_value = True
        except Exception:
            _online_value = False
        _online_checked_at = now + 60
        return _online_value


def get_or_fetch(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Optional[bytes]:
    """Serve from cache; fetch from upstream on miss. Never raises."""
    cached = get_cached(z, x, y, source)
    if cached is not None:
        return cached
    if not is_online():
        return None
    try:
        return fetch_tile(z, x, y, source)
    except TileBlocked:
        return None


# ---------------------------------------------------------------------------
# Tile maths
# ---------------------------------------------------------------------------
def lon_to_tile(lon: float, z: int) -> int:
    return int(math.floor((lon + 180.0) / 360.0 * (1 << z)))


def lat_to_tile(lat: float, z: int) -> int:
    lat = max(-85.05112878, min(85.05112878, lat))
    lat_rad = math.radians(lat)
    n = 1 << z
    return int(math.floor(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n))


def tiles_in_bbox(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max, source=DEFAULT_SOURCE):
    tiles = []
    for z in range(zoom_min, zoom_max + 1):
        x0, x1 = lon_to_tile(lon_min, z), lon_to_tile(lon_max, z)
        y0, y1 = lat_to_tile(lat_max, z), lat_to_tile(lat_min, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tiles.append({"z": z, "x": x, "y": y, "cached": is_cached(z, x, y, source)})
    return tiles


def estimate_tiles(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max, source=DEFAULT_SOURCE) -> dict:
    """Cheap count without building the full list — used by the UI before download."""
    missing = 0
    total = 0
    for z in range(zoom_min, zoom_max + 1):
        nx = lon_to_tile(lon_max, z) - lon_to_tile(lon_min, z) + 1
        ny = lat_to_tile(lat_min, z) - lat_to_tile(lat_max, z) + 1
        n = max(0, nx) * max(0, ny)
        total += n
    # Exact cached count only when small enough to enumerate, else approximate.
    if total <= 4000:
        missing = sum(1 for t in tiles_in_bbox(
            lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max, source) if not t["cached"])
    else:
        missing = total
    return {"total": total, "missing": missing, "source": source,
            "zoom_range": [zoom_min, zoom_max]}


# ---------------------------------------------------------------------------
# Background download jobs
# ---------------------------------------------------------------------------
def start_download(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max,
                   source=DEFAULT_SOURCE) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "state": "running",
        "source": source,
        "bbox": [lat_min, lon_min, lat_max, lon_max],
        "zoom_range": [zoom_min, zoom_max],
        "total": 0, "completed": 0, "downloaded": 0,
        "already_cached": 0, "failed": 0, "bytes_added": 0,
        "blocked": None, "error": None,
        "started": time.time(), "finished": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job,), daemon=True)
    t.start()
    return job


def _run_job(job: dict) -> None:
    tiles = tiles_in_bbox(*job["bbox"], *job["zoom_range"], job["source"])
    job["total"] = len(tiles)
    consecutive_failures = 0
    try:
        for i, t in enumerate(tiles):
            if t["cached"]:
                job["already_cached"] += 1
                job["completed"] = i + 1
                continue
            try:
                data = fetch_tile(t["z"], t["x"], t["y"], job["source"])
            except TileBlocked as exc:
                job["blocked"] = str(exc)
                job["state"] = "blocked"
                break
            if data:
                job["downloaded"] += 1
                job["bytes_added"] += len(data)
                consecutive_failures = 0
            else:
                job["failed"] += 1
                consecutive_failures += 1
                # Bail out fast when the network is clearly gone.
                if consecutive_failures >= 10 and job["downloaded"] == 0:
                    job["error"] = "Upstream unreachable — no tiles downloaded. Check the connection."
                    job["state"] = "error"
                    break
            job["completed"] = i + 1
    except Exception as exc:  # never leave a job stuck in "running"
        job["error"] = str(exc)
        job["state"] = "error"
    finally:
        job["finished"] = time.time()
        if job["state"] == "running":
            job["state"] = "done"


def job_status(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def host_reachable(source: str = DEFAULT_SOURCE) -> bool:
    src = TILE_SOURCES.get(source)
    if not src:
        return False
    try:
        requests.head("https://" + src["url_template"].split("/")[2],
                      headers={"User-Agent": USER_AGENT}, timeout=5)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stats & maintenance
# ---------------------------------------------------------------------------
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def cache_stats(source: Optional[str] = None) -> dict:
    """Count cached tiles and total size. source=None aggregates every source."""
    sources = [source] if source else list(TILE_SOURCES)
    out = []
    total_tiles = 0
    total_bytes = 0
    for src in sources:
        if src not in TILE_SOURCES:
            continue
        base = cache_root() / _safe(src)
        tiles = 0
        size = 0
        min_z, max_z = 99, 0
        if base.exists():
            for z_dir in base.iterdir():
                if not z_dir.is_dir() or not z_dir.name.isdigit():
                    continue
                z = int(z_dir.name)
                n = 0
                for f in z_dir.rglob("*"):
                    if f.is_file() and f.suffix.lower() in _IMAGE_SUFFIXES:
                        n += 1
                        size += f.stat().st_size
                if n:
                    min_z = min(min_z, z)
                    max_z = max(max_z, z)
                    tiles += n
        total_tiles += tiles
        total_bytes += size
        out.append({
            "source": src,
            "name": TILE_SOURCES[src]["name"],
            "tile_count": tiles,
            "total_bytes": size,
            "total_mb": round(size / (1024 * 1024), 2),
            "zoom_range": [min_z, max_z] if tiles else [],
            "max_zoom": TILE_SOURCES[src]["max_zoom"],
            "attribution": TILE_SOURCES[src]["attribution"],
        })
    result = {
        "tile_count": total_tiles,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "path": str(cache_root()),
        "sources": out,
    }
    if source:
        match = next((s for s in out if s["source"] == source), None)
        if match:
            result.update(match)
    return result


def clear_cache(source: Optional[str] = None) -> dict:
    """Delete cached tiles. source=None clears every source."""
    removed = 0
    freed = 0
    targets = [source] if source else list(TILE_SOURCES)
    for src in targets:
        base = cache_root() / _safe(src)
        if not base.exists():
            continue
        for f in list(base.rglob("*")):
            if f.is_file():
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        # Drop empty zoom/x directories.
        for d in sorted(base.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
    return {"removed": removed, "freed_bytes": freed,
            "freed_mb": round(freed / (1024 * 1024), 2),
            "source": source or "all"}


def sources_info() -> list[dict]:
    """Provider catalogue plus cached counts, for the source picker."""
    stats = {s["source"]: s for s in cache_stats()["sources"]}
    out = []
    for key, src in TILE_SOURCES.items():
        s = stats.get(key, {})
        out.append({
            "id": key,
            "name": src["name"],
            "attribution": src["attribution"],
            "max_zoom": src["max_zoom"],
            "note": src["note"],
            "tile_count": s.get("tile_count", 0),
            "total_mb": s.get("total_mb", 0),
            "zoom_range": s.get("zoom_range", []),
        })
    return out
