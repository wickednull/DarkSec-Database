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

import copy
import hashlib
import math
import os
import queue
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import requests
from urllib.parse import quote_plus

from . import config

_CARTO_API_KEY = ""


def set_carto_api_key(key: str) -> None:
    global _CARTO_API_KEY
    _CARTO_API_KEY = str(key or "").strip()


def carto_api_key() -> str:
    return _CARTO_API_KEY

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
        "name": "USGS imagery (US)",
        # USGSImageryTopo is retired; USGSImageryOnly remains available.
        "url_template": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
        "attribution": "USGS The National Map (public domain)",
        "max_zoom": 16,
        "ext": "jpg",
        "note": "USGS satellite imagery. United States only.",
    },
    "opentopomap": {
        "name": "OpenTopoMap",
        "url_template": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenTopoMap (CC-BY-SA), © OpenStreetMap contributors",
        "max_zoom": 17,
        "ext": "png",
        "note": "Terrain and contours worldwide.",
    },
    "esri_labels_places": {
        "name": "Place labels overlay (Esri)",
        "url_template": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, HERE, Garmin, OpenStreetMap contributors",
        "max_zoom": 19,
        "ext": "png",
        "overlay": True,
        "note": "Transparent place, boundary and water labels for satellite imagery.",
    },
    "esri_labels_roads": {
        "name": "Road labels overlay (Esri)",
        "url_template": "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}",
        "attribution": "Esri, HERE, Garmin, OpenStreetMap contributors",
        "max_zoom": 19,
        "ext": "png",
        "overlay": True,
        "note": "Transparent road network and street-name labels for satellite imagery.",
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
# Ceiling for one offline pack, counted across every source in the job (a pack
# with labels is three sources). It only exists to stop a mis-click from
# queueing a continent-scale job; 150k tiles is roughly 2.7 GB on disk and a
# few hours at the polite request rate, and is cancellable/resumable.
MAX_DOWNLOAD_TILES = 150_000
# How long a tile request waits for a queued upstream fetch before painting a
# cached parent instead. Small enough to keep the map responsive; most fast
# providers answer inside it.
TILE_FETCH_WAIT = 0.75

# Error images returned with HTTP 200. Reject by hash and by marker text.
_ERROR_HASHES = {
    "8e8d1ff492c2bdae445a83ccdb8e449f",  # OSM "Access blocked"
    "df9307c738f2c6dbc0f8d31837019f98",  # CARTO "API key required" watermark
}
_BLOCK_MARKERS = (
    b"Access blocked", b"Access denied", b"blocked by policy",
    b"API KEY REQUIRED", b"API key required", b"carto.com/basemaps/apikey",
)

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Paths & cache
# ---------------------------------------------------------------------------
_CACHE_ROOT_TTL = 30.0
_cache_root_lock = threading.RLock()
_cache_root_refresh = threading.Lock()
_cache_root_cache: dict = {"at": 0.0, "raw": None}


def configured_cache_path() -> str:
    """Operator-selected tile folder; empty means the bundled cache folder.

    Stale-while-revalidate: a tile request never waits on the settings database.
    One thread refreshes; everyone else keeps using the previous value.
    """
    now = time.monotonic()
    with _cache_root_lock:
        if _cache_root_cache["raw"] is not None and now - _cache_root_cache["at"] < _CACHE_ROOT_TTL:
            return str(_cache_root_cache["raw"])
        previous = _cache_root_cache["raw"]
    if not _cache_root_refresh.acquire(blocking=False):
        return str(previous or "")
    try:
        raw = ""
        try:
            from .db import Database

            d = Database()
            try:
                raw = str(d.get_setting("map_cache_path", "") or "")
            finally:
                d.close()
        except Exception:
            raw = str(previous or "")
        with _cache_root_lock:
            _cache_root_cache.update({"at": now, "raw": raw})
        return raw
    finally:
        _cache_root_refresh.release()


def invalidate_cache_path(raw: str | None = None) -> None:
    with _cache_root_lock:
        _cache_root_cache.update({"at": time.monotonic(), "raw": raw})
    reset_runtime_caches()
    _invalidate_stats()


def reset_runtime_caches() -> None:
    """Forget in-memory tiles, probe state and directories proven to exist."""
    global _online_checked_at, _online_probed, _online_probing, _online_value
    _invalidate_mem()
    with _cache_root_lock:
        _ready_dirs.clear()
    with _online_lock:
        _online_checked_at = 0.0
        _online_probed = False
        _online_probing = False
        _online_value = False


def cache_root() -> Path:
    """Current cache root. May be on removable media, so never create it here."""
    raw = configured_cache_path()
    return Path(raw).expanduser() if raw else (config.MAP_TILE_DIR / "cache")


_ready_dirs: set[str] = set()


def tile_cache_dir(source: str = DEFAULT_SOURCE) -> Path:
    """Return the on-disk cache directory for source, creating it if possible."""
    base = cache_root() / _safe(source)
    key = str(base)
    with _cache_root_lock:
        ready = key in _ready_dirs
    if not ready:
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            # The USB drive may be unplugged or read-only. Reads will miss and
            # writes will fail cleanly; the map falls back to other sources.
            pass
        else:
            with _cache_root_lock:
                _ready_dirs.add(key)
    return base


def validate_cache_path(raw: str | None, create: bool = True) -> Path:
    """Validate/create an operator tile folder and prove it is writable."""
    text = str(raw or "").strip()
    path = Path(text).expanduser() if text else (config.MAP_TILE_DIR / "cache")
    if path.exists() and not path.is_dir():
        raise ValueError("Tile cache path is not a folder")
    # Find the nearest existing ancestor so a fresh USB subfolder such as
    # /media/user/DRIVE/DarkSec-Tiles can be created safely.
    ancestor = path
    while not ancestor.exists() and ancestor.parent != ancestor:
        ancestor = ancestor.parent
    if not ancestor.exists() or not ancestor.is_dir():
        raise ValueError(f"Parent folder does not exist: {ancestor}")
    if not os.access(ancestor, os.W_OK | os.X_OK):
        raise ValueError(f"Folder is not writable: {ancestor}")
    if not create and not path.exists():
        return path.resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".darksec-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"Tile cache folder is not writable: {exc.strerror or exc}")
    return path.resolve()


def disk_info(path: str | Path | None = None) -> dict:
    """Free/total space for the active cache (or a candidate) folder."""
    target = Path(path).expanduser() if path else cache_root()
    try:
        usage = shutil.disk_usage(str(target))
        return {"total_bytes": usage.total, "free_bytes": usage.free,
                "free_gb": round(usage.free / (1024 ** 3), 2)}
    except OSError:
        return {"total_bytes": None, "free_bytes": None, "free_gb": None}


def _safe(name: str) -> str:
    return "".join(c for c in str(name) if c.isalnum() or c in "_-")[:40] or "unknown"


def tile_path(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Path:
    ext = TILE_SOURCES.get(source, {}).get("ext", "png")
    # Deliberately no mkdir here: this is the per-tile read hot path. Writers
    # create the parent directory themselves (see fetch_tile).
    return cache_root() / _safe(source) / str(z) / str(x) / f"{y}.{ext}"


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 24:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def _has_block_markers(data: bytes) -> bool:
    return any(m in data[:4096] for m in _BLOCK_MARKERS)


def _is_error_tile(data: bytes) -> bool:
    if _has_block_markers(data):
        return True
    return hashlib.md5(data).hexdigest() in _ERROR_HASHES


def is_cached(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> bool:
    key = (source, int(z), int(x), int(y))
    if _mem_get(key) is not None:
        return True
    if _neg_get(key):
        return False
    try:
        return tile_path(z, x, y, source).stat().st_size > 0
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Hot-tile memory cache and miss cache
# ---------------------------------------------------------------------------
# A 256x256 tile is ~10 KB, so a viewport plus its parents fits comfortably in
# RAM. This removes repeated SD-card reads while panning back over the same
# ground and makes the multi-source fallback scans cheap.
_MEM_MAX_BYTES = 24 * 1024 * 1024
_MEM_MAX_ENTRY = 2 * 1024 * 1024
_mem: "OrderedDict[tuple, bytes]" = OrderedDict()
_mem_bytes = 0
_mem_lock = threading.Lock()

# Remember recent misses for a short while. Offline overzoom otherwise re-stats
# every source and zoom ancestor for every missing tile on every redraw.
_NEG_TTL = 8.0
_NEG_MAX = 8192
_neg: dict[tuple, float] = {}
_neg_lock = threading.Lock()


def _mem_get(key: tuple):
    global _mem_bytes
    with _mem_lock:
        data = _mem.get(key)
        if data is not None:
            _mem.move_to_end(key)
        return data


def _mem_put(key: tuple, data: Optional[bytes]) -> None:
    global _mem_bytes
    if not data or len(data) > _MEM_MAX_ENTRY:
        return
    with _mem_lock:
        old = _mem.pop(key, None)
        if old is not None:
            _mem_bytes -= len(old)
        _mem[key] = data
        _mem_bytes += len(data)
        while _mem_bytes > _MEM_MAX_BYTES and _mem:
            _, dropped = _mem.popitem(last=False)
            _mem_bytes -= len(dropped)


def _neg_get(key: tuple) -> bool:
    if not _neg:
        return False
    now = time.monotonic()
    with _neg_lock:
        until = _neg.get(key)
        if until is None:
            return False
        if until < now:
            _neg.pop(key, None)
            return False
        return True


def _neg_put(key: tuple) -> None:
    now = time.monotonic()
    with _neg_lock:
        if len(_neg) >= _NEG_MAX:
            # Dict preserves insertion order: drop the oldest quarter.
            for _ in range(_NEG_MAX // 4):
                try:
                    _neg.pop(next(iter(_neg)))
                except (StopIteration, KeyError):
                    break
        _neg[key] = now + _NEG_TTL


def _neg_drop(key: tuple) -> None:
    if _neg:
        with _neg_lock:
            _neg.pop(key, None)


def _invalidate_mem() -> None:
    global _mem_bytes
    with _mem_lock:
        _mem.clear()
        _mem_bytes = 0
    with _neg_lock:
        _neg.clear()


def get_cached(z: int, x: int, y: int, source: str = DEFAULT_SOURCE) -> Optional[bytes]:
    key = (source, int(z), int(x), int(y))
    data = _mem_get(key)
    if data is not None:
        return data
    if _neg_get(key):
        return None
    p = tile_path(z, x, y, source)
    try:
        if p.stat().st_size <= 0:
            _neg_put(key)
            return None
        data = p.read_bytes()
    except OSError:
        _neg_put(key)
        return None
    if _looks_like_image(data) and not _is_error_tile(data):
        _mem_put(key, data)
        return data
    try:
        p.unlink()
    except OSError:
        pass
    _neg_put(key)
    return None


def find_cached_ancestor(z: int, x: int, y: int, source: str = DEFAULT_SOURCE,
                         max_up: int = 6) -> Optional[tuple[str, bytes]]:
    """Nearest cached parent tile from the same source, for offline overzoom."""
    for up in range(1, max_up + 1):
        zz = z - up
        if zz < 0:
            break
        data = get_cached(zz, x >> up, y >> up, source)
        if data is not None:
            return source, data
    return None


def find_cached(z: int, x: int, y: int, prefer: Optional[str] = None) -> Optional[tuple[str, bytes]]:
    """Look for a tile in any source's cache. Prefer the active source."""
    order = []
    if prefer and prefer in TILE_SOURCES:
        order.append(prefer)
    # Label overlays are transparent and must never become a basemap fallback.
    order += [s for s in TILE_SOURCES
              if s not in order and not TILE_SOURCES[s].get("overlay")]
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

# A source that just failed is skipped for a cooldown instead of retrying for
# every tile in the viewport. This keeps an offline map from spamming DNS/TCP
# attempts (and the console) while panning.
SOURCE_FAIL_COOLDOWN = 60.0
_source_offline_until: dict[str, float] = {}
_source_fail_lock = threading.Lock()


def _source_in_cooldown(source: str) -> bool:
    with _source_fail_lock:
        until = _source_offline_until.get(source, 0.0)
        return time.monotonic() < until


def _mark_source_failure(source: str, exc: Exception) -> None:
    now = time.monotonic()
    with _source_fail_lock:
        first = now >= _source_offline_until.get(source, 0.0)
        _source_offline_until[source] = now + SOURCE_FAIL_COOLDOWN
    if first:
        detail = " ".join(str(exc).split())[:240]
        print(f"Tile source {source} unreachable; pausing fetches for "
              f"{int(SOURCE_FAIL_COOLDOWN)}s: {detail}")


def _clear_source_failure(source: str) -> None:
    with _source_fail_lock:
        _source_offline_until.pop(source, None)


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
    if _source_in_cooldown(source):
        return None
    url = src["url_template"].format(z=z, x=x, y=y)
    if _CARTO_API_KEY and "cartocdn.com" in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}key={quote_plus(_CARTO_API_KEY)}"
    _throttle()
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/jpeg,image/*"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # network down, DNS, timeout
        _mark_source_failure(source, exc)
        return None
    _clear_source_failure(source)

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
    if media not in ("image/png", "image/jpeg", "image/webp"):
        return None
    # Always use the source's configured extension so lookups find the file
    # even when a provider returns a different image format for one tile.
    p = tile_path(z, x, y, source)
    tmp = p.with_suffix(p.suffix + ".part")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        tmp.replace(p)
    except OSError:
        # Removable media unplugged/full/read-only: keep the downloaded bytes
        # for this request but do not claim they were cached.
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    key = (source, int(z), int(x), int(y))
    _mem_put(key, data)
    _neg_drop(key)
    return data


_online_checked_at = 0.0
_online_value = False
_online_probed = False
_online_probing = False
_online_lock = threading.Lock()
_online_event = threading.Event()


def _probe_online() -> None:
    """Refresh connectivity in a worker; never called on the request path."""
    global _online_value, _online_checked_at, _online_probing, _online_probed
    try:
        requests.head("https://server.arcgisonline.com/", timeout=4,
                      headers={"User-Agent": USER_AGENT})
        value = True
    except Exception:
        value = False
    with _online_lock:
        _online_value = value
        _online_checked_at = time.monotonic() + 60
        _online_probing = False
        _online_probed = True
        _online_event.set()


def is_online(block: bool = True) -> bool:
    """Connectivity probe that never stalls the tile hot path.

    ``block=False`` (used by tile serving) always returns the last known value
    and refreshes in the background. The first blocking call in a process waits
    briefly for a real answer so callers that gate network work stay correct.
    """
    global _online_probing
    now = time.monotonic()
    with _online_lock:
        if now < _online_checked_at:
            return _online_value
        first = not _online_probed
        started = False
        if not _online_probing:
            _online_probing = True
            started = True
            _online_event.clear()
            threading.Thread(target=_probe_online, daemon=True,
                             name="tile-online-probe").start()
        value = _online_value
    if first and started and block:
        _online_event.wait(2.5)
        with _online_lock:
            return _online_value
    return value


# ---------------------------------------------------------------------------
# Background single-tile fetches
# ---------------------------------------------------------------------------
# The map must never wait on a provider. A missing tile is queued to a small
# worker pool; the request returns a cached parent/overview at once and the
# next request finds the real tile. Two workers keep the provider rate limit
# honoured while still filling a viewport in a few seconds.
_FETCH_WORKERS = 2
_FETCH_QUEUE_MAX = 256
_fetch_queue: "queue.Queue" = queue.Queue(maxsize=_FETCH_QUEUE_MAX)
_fetch_pending: dict[tuple, threading.Event] = {}
_fetch_lock = threading.Lock()
_fetch_started = False


def _ensure_fetch_workers() -> None:
    global _fetch_started
    if _fetch_started:
        return
    for i in range(_FETCH_WORKERS):
        threading.Thread(target=_fetch_worker, daemon=True,
                         name=f"tile-fetch-{i}").start()
    _fetch_started = True


def _fetch_worker() -> None:
    while True:
        key, z, x, y, source, done = _fetch_queue.get()
        try:
            # Connectivity may have flipped to offline (or the source may be in
            # cooldown) while this job sat in the queue. Drop it instead of
            # holding the radio/network busy for a viewport that has moved on.
            if is_online(block=False) and not _source_in_cooldown(source):
                try:
                    data = fetch_tile(z, x, y, source)
                except TileBlocked:
                    data = None
                if data:
                    # fetch_tile already does this, but a custom/older fetch
                    # path must not leave the tile hidden behind a miss entry.
                    _mem_put(key, data)
                    _neg_drop(key)
        except Exception:
            pass
        finally:
            with _fetch_lock:
                _fetch_pending.pop(key, None)
            done.set()
            _fetch_queue.task_done()


def warm_tile(z: int, x: int, y: int, source: str = DEFAULT_SOURCE,
              wait: float = 0.75) -> Optional[bytes]:
    """Return a tile if available, else queue a fetch and wait at most `wait`.

    Returns cached/served bytes when the provider answered inside the wait
    budget, otherwise None (caller paints a parent/overview). The worker keeps
    going and caches the tile for the next request.
    """
    key = (source, int(z), int(x), int(y))
    data = get_cached(z, x, y, source)
    if data is not None:
        return data
    if not is_online(block=False):
        return None
    with _fetch_lock:
        done = _fetch_pending.get(key)
        if done is None:
            done = threading.Event()
            _fetch_pending[key] = done
            try:
                _ensure_fetch_workers()
                _fetch_queue.put_nowait((key, int(z), int(x), int(y), source, done))
            except queue.Full:
                _fetch_pending.pop(key, None)
                return None
    if wait and wait > 0 and done.wait(wait):
        return get_cached(z, x, y, source)
    return None


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


LABEL_SOURCES = ("esri_labels_places", "esri_labels_roads")


def label_sources_for(source: str) -> list[str]:
    """Transparent label overlays the map draws above any raster basemap."""
    if source in LABEL_SOURCES or source not in TILE_SOURCES:
        return []
    return list(LABEL_SOURCES)


def tiles_in_bbox(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max, source=DEFAULT_SOURCE):
    tiles = []
    for z in range(zoom_min, zoom_max + 1):
        x0, x1 = lon_to_tile(lon_min, z), lon_to_tile(lon_max, z)
        y0, y1 = lat_to_tile(lat_max, z), lat_to_tile(lat_min, z)
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                tiles.append({"z": z, "x": x, "y": y, "cached": is_cached(z, x, y, source)})
    return tiles


def estimate_tiles(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max,
                   source=DEFAULT_SOURCE, include_labels: bool = False) -> dict:
    """Cheap count without building the full list — used by the UI before download."""
    sources = [source] + (label_sources_for(source) if include_labels else [])
    missing = 0
    total = 0
    per_source: dict[str, dict] = {}
    for src in sources:
        src_total = 0
        for z in range(zoom_min, zoom_max + 1):
            nx = lon_to_tile(lon_max, z) - lon_to_tile(lon_min, z) + 1
            ny = lat_to_tile(lat_min, z) - lat_to_tile(lat_max, z) + 1
            src_total += max(0, nx) * max(0, ny)
        # Exact cached count only when small enough to enumerate, else approximate.
        if src_total <= 4000:
            src_missing = sum(1 for t in tiles_in_bbox(
                lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max, src)
                if not t["cached"])
        else:
            src_missing = src_total
        per_source[src] = {"total": src_total, "missing": src_missing}
        total += src_total
        missing += src_missing
    return {"total": total, "missing": missing, "source": source,
            "sources": per_source, "include_labels": bool(include_labels),
            "limit": MAX_DOWNLOAD_TILES, "over_limit": total > MAX_DOWNLOAD_TILES,
            "zoom_range": [zoom_min, zoom_max]}


# ---------------------------------------------------------------------------
# Background download jobs
# ---------------------------------------------------------------------------
def _job_log(job: dict, message: str, level: str = "info") -> None:
    job.setdefault("log", []).append({
        "ts": time.time(), "level": level, "message": str(message)[:400],
    })
    if len(job["log"]) > 400:
        del job["log"][:100]


def start_download(lat_min, lon_min, lat_max, lon_max, zoom_min, zoom_max,
                   source=DEFAULT_SOURCE, label: str = "manual",
                   include_labels: bool = False) -> dict:
    job_id = uuid.uuid4().hex[:12]
    labels = label_sources_for(source) if include_labels else []
    sources = [source, *labels]
    job = {
        "id": job_id,
        "state": "running",
        "source": source,
        "sources": sources,
        "include_labels": bool(labels),
        "label": str(label or "manual")[:20],
        "bbox": [lat_min, lon_min, lat_max, lon_max],
        "zoom_range": [zoom_min, zoom_max],
        "total": 0, "completed": 0, "downloaded": 0,
        "already_cached": 0, "failed": 0, "bytes_added": 0,
        "blocked": None, "error": None, "cancel": False,
        "current": None, "log": [],
        "started": time.time(), "finished": None,
    }
    _job_log(job, f"Started {source} z{zoom_min}–{zoom_max} "
                  f"({'auto' if label == 'auto' else 'manual'} pack)"
                  + (f" · labels: {', '.join(labels)}" if labels else ""))
    with _jobs_lock:
        _jobs[job_id] = job
    t = threading.Thread(target=_run_job, args=(job,), daemon=True)
    t.start()
    return dict(job)


def cancel_download(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        if job.get("state") == "running":
            job["cancel"] = True
            _job_log(job, "Cancel requested — stopping after the current tile.", "warn")
        return {**job, "log": list(job.get("log") or [])}


def list_jobs(limit: int = 12) -> list[dict]:
    """Running jobs first, then the most recent finished jobs."""
    now = time.time()
    with _jobs_lock:
        stale = [jid for jid, job in _jobs.items()
                 if job.get("state") != "running"
                 and (job.get("finished") or now) < now - 3600]
        for jid in stale:
            _jobs.pop(jid, None)
        jobs = list(_jobs.values())
    jobs.sort(key=lambda job: (job.get("state") != "running",
                               -(job.get("started") or 0)))
    return [{**job, "log": list(job.get("log") or [])} for job in jobs[:limit]]


def _run_job(job: dict) -> None:
    sources = job.get("sources") or [job["source"]]
    tiles = []
    for src in sources:
        for t in tiles_in_bbox(*job["bbox"], *job["zoom_range"], src):
            t["source"] = src
            tiles.append(t)
    job["total"] = len(tiles)
    step = max(1, min(200, len(tiles) // 40)) if tiles else 1
    _job_log(job, f"{len(tiles):,} tiles in range · sources: {', '.join(sources)}")
    consecutive_failures = 0
    last_progress = time.monotonic()
    try:
        for i, t in enumerate(tiles):
            if job.get("cancel"):
                job["state"] = "cancelled"
                _job_log(job, f"Cancelled at {i}/{len(tiles)} tiles.", "warn")
                break
            job["current"] = {"z": t["z"], "x": t["x"], "y": t["y"]}
            if t["cached"]:
                job["already_cached"] += 1
                job["completed"] = i + 1
                now = time.monotonic()
                if job["completed"] % step == 0 or now - last_progress >= 2.0:
                    last_progress = now
                    _job_log(job, f"{job['completed']:,}/{len(tiles):,} · "
                                  f"{job['downloaded']:,} new · {job['already_cached']:,} cached")
                continue
            try:
                data = fetch_tile(t["z"], t["x"], t["y"], t.get("source") or job["source"])
            except TileBlocked as exc:
                job["blocked"] = str(exc)
                job["state"] = "blocked"
                _job_log(job, f"Provider blocked the download: {exc}", "error")
                break
            if data:
                job["downloaded"] += 1
                job["bytes_added"] += len(data)
                consecutive_failures = 0
            else:
                job["failed"] += 1
                consecutive_failures += 1
                if job["failed"] <= 5 or job["failed"] % 25 == 0:
                    _job_log(job, f"Tile {t['z']}/{t['x']}/{t['y']} failed "
                                  f"({job['failed']} total failures)", "warn")
                # Bail out fast when the network is clearly gone.
                if consecutive_failures >= 10 and job["downloaded"] == 0:
                    job["error"] = "Upstream unreachable — no tiles downloaded. Check the connection."
                    job["state"] = "error"
                    _job_log(job, job["error"], "error")
                    break
            job["completed"] = i + 1
            now = time.monotonic()
            if job["completed"] % step == 0 or now - last_progress >= 2.0:
                last_progress = now
                _job_log(job, f"{job['completed']:,}/{len(tiles):,} · "
                              f"{job['downloaded']:,} new · {job['already_cached']:,} cached · "
                              f"{job['failed']:,} failed")
    except Exception as exc:  # never leave a job stuck in "running"
        job["error"] = str(exc)
        job["state"] = "error"
        _job_log(job, f"Download error: {exc}", "error")
    finally:
        job["finished"] = time.time()
        if job["state"] == "running":
            job["state"] = "done"
        if job["state"] == "done":
            _job_log(job, f"Finished: {job['downloaded']:,} new · "
                          f"{job['already_cached']:,} already cached · "
                          f"{job['failed']:,} failed · "
                          f"{job['bytes_added'] / 1048576:.1f} MB", "success")
        _invalidate_stats()
        job["current"] = None


def job_status(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return {**job, "log": list(job.get("log") or [])} if job else None


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
# Cache relocation (copy existing tiles to a USB folder)
# ---------------------------------------------------------------------------
_relocations: dict[str, dict] = {}
_reloc_lock = threading.Lock()


def _downloads_running() -> bool:
    with _jobs_lock:
        return any(job.get("state") == "running" for job in _jobs.values())


def start_relocate(destination: str, remove_source: bool = False,
                   switch_cache: bool = False) -> dict:
    """Copy current cache files to destination, optionally deleting source.

    ``switch_cache`` updates ``map_cache_path`` after a fully successful
    copy/move, so closing the progress dialog cannot leave the app pointing at
    an empty old folder.
    """
    if _downloads_running():
        raise RuntimeError("Wait for the running tile download to finish first")
    dest = validate_cache_path(destination)
    src = cache_root().resolve()
    if dest == src:
        raise ValueError("Destination is already the active cache folder")
    if src in dest.parents or dest in src.parents:
        raise ValueError("Source and destination must not be nested")
    files = [f for f in src.rglob("*") if f.is_file() and f.suffix != ".part"] if src.exists() else []
    total_bytes = 0
    for f in files:
        try:
            total_bytes += f.stat().st_size
        except OSError:
            pass
    free = disk_info(dest)["free_bytes"]
    if free is not None and total_bytes > free:
        raise RuntimeError(
            f"Not enough free space at {dest}: need {total_bytes / (1024**3):.2f} GB, "
            f"{free / (1024**3):.2f} GB available"
        )
    job = {
        "id": uuid.uuid4().hex[:12],
        "state": "running" if files else "done",
        "source": str(src), "destination": str(dest),
        "total": len(files), "completed": 0,
        "copied": 0, "skipped": 0, "failed": 0,
        "total_bytes": total_bytes, "done_bytes": 0,
        "remove_source": bool(remove_source),
        "switch_cache": bool(switch_cache),
        "error": None, "started": time.time(), "finished": None if files else time.time(),
    }
    with _reloc_lock:
        _relocations[job["id"]] = job
    if files:
        threading.Thread(target=_run_relocate, args=(job, files), daemon=True).start()
    return dict(job)


def _run_relocate(job: dict, files: list[Path]) -> None:
    src = Path(job["source"])
    dest = Path(job["destination"])
    try:
        for i, f in enumerate(files):
            try:
                rel = f.relative_to(src)
                target = dest / rel
                if target.exists() and target.stat().st_size == f.stat().st_size:
                    job["skipped"] += 1
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + ".part")
                    shutil.copy2(f, tmp)
                    tmp.replace(target)
                    job["copied"] += 1
                    try:
                        job["done_bytes"] += f.stat().st_size
                    except OSError:
                        pass
            except OSError:
                job["failed"] += 1
            job["completed"] = i + 1
        if job["remove_source"] and job["failed"] == 0:
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    job["failed"] += 1
            for d in sorted((p for p in src.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        job["state"] = "done" if job["failed"] == 0 else "partial"
        if job["state"] == "done" and job.get("switch_cache"):
            try:
                from .db import Database

                d = Database()
                try:
                    d.set_setting("map_cache_path", str(dest))
                finally:
                    d.close()
                invalidate_cache_path(str(dest))
            except Exception as exc:
                job["state"] = "partial"
                job["error"] = f"tiles moved but cache path switch failed: {exc}"[:300]
    except Exception as exc:
        job["state"] = "error"
        job["error"] = str(exc)[:300]
    finally:
        job["finished"] = time.time()


def relocate_status(job_id: str) -> Optional[dict]:
    with _reloc_lock:
        job = _relocations.get(job_id)
        return dict(job) if job else None


def relocations_running() -> bool:
    with _reloc_lock:
        return any(job.get("state") == "running" for job in _relocations.values())


def candidate_cache_locations() -> list[dict]:
    """Mount points suitable for storing tiles, with free space."""
    import string
    from . import config as cfg
    from .mbtiles import _is_mount_root

    raw_current = str(configured_cache_path() or "")
    candidates: list[Path] = [cfg.MAP_TILE_DIR / "cache"]
    if raw_current:
        candidates.append(Path(raw_current).expanduser())
    if os.name == "nt":
        for letter in string.ascii_uppercase[3:]:
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    candidates.append(drive / "DarkSec-Tiles")
            except OSError:
                continue
    else:
        for mount in ("/media", "/run/media", "/mnt", "/Volumes"):
            base = Path(mount)
            try:
                if not base.is_dir():
                    continue
            except OSError:
                continue
            try:
                entries = [e for e in base.iterdir() if e.is_dir()]
            except OSError:
                continue
            for entry in entries:
                candidates.append(entry / "DarkSec-Tiles")
                try:
                    for drive in entry.iterdir():
                        if drive.is_dir() and drive.name not in ("lost+found",):
                            candidates.append(drive / "DarkSec-Tiles")
                except OSError:
                    continue
    seen: set[str] = set()
    out = []
    for path in candidates:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        info = disk_info(path if path.exists() else path.parent)
        out.append({
            "path": str(path),
            "label": str(path),
            "exists": path.exists(),
            "default": path == cfg.MAP_TILE_DIR / "cache",
            "writable": os.access(path if path.exists() else path.parent, os.W_OK),
            "mount": _is_mount_root(path.parent if path.name == "DarkSec-Tiles" else path),
            **info,
        })
    out.sort(key=lambda item: (not item["writable"], not item["exists"], item["path"]))
    return out


# ---------------------------------------------------------------------------
# Stats & maintenance
# ---------------------------------------------------------------------------
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


_stats_cache: dict[tuple, tuple[float, dict]] = {}
_stats_lock = threading.Lock()
_stats_refreshing: set[tuple] = set()
STATS_TTL = 5.0


def _invalidate_stats() -> None:
    """Mark cached stats stale but keep the last values for instant responses."""
    with _stats_lock:
        for key, (_at, data) in list(_stats_cache.items()):
            _stats_cache[key] = (0.0, data)


def _walk_images(base: Path):
    """Yield (zoom, size, allocated) for cached images without Path/rglob overhead."""
    stack = [(base, None)]
    while stack:
        directory, zoom = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    name = entry.name
                    z = int(name) if name.isdigit() else zoom
                    stack.append((Path(entry.path), z))
                elif (entry.is_file(follow_symlinks=False)
                      and os.path.splitext(entry.name)[1].lower() in _IMAGE_SUFFIXES):
                    st = entry.stat(follow_symlinks=False)
                    allocated = getattr(st, "st_blocks", None)
                    allocated_bytes = (allocated * 512) if allocated is not None else st.st_size
                    yield zoom, st.st_size, allocated_bytes
            except OSError:
                continue


def _compute_stats(source: Optional[str] = None) -> dict:
    if source:
        sources = [source]
    else:
        sources = list(TILE_SOURCES)
        # Include any on-disk cache directories not in TILE_SOURCES so totals
        # match what is actually stored (legacy/custom source dirs).
        try:
            for entry in os.scandir(cache_root()):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                name = entry.name
                if name not in sources:
                    sources.append(name)
        except OSError:
            pass
    out = []
    total_tiles = 0
    total_bytes = 0
    total_disk_bytes = 0
    for src in sources:
        meta = TILE_SOURCES.get(src, {})
        base = cache_root() / _safe(src)
        tiles = 0
        size = 0
        disk_size = 0
        min_z, max_z = 99, 0
        if base.exists():
            for z, n_bytes, n_disk in _walk_images(base):
                tiles += 1
                size += n_bytes
                disk_size += n_disk
                if z is not None:
                    min_z = min(min_z, z)
                    max_z = max(max_z, z)
        total_tiles += tiles
        total_bytes += size
        total_disk_bytes += disk_size
        out.append({
            "source": src,
            "name": meta.get("name", src),
            "tile_count": tiles,
            "total_bytes": size,
            "total_mb": round(size / (1024 * 1024), 2),
            "disk_bytes": disk_size,
            "disk_mb": round(disk_size / (1024 * 1024), 2),
            "zoom_range": [min_z, max_z] if tiles else [],
            "max_zoom": meta.get("max_zoom"),
            "attribution": meta.get("attribution", ""),
        })
    info = disk_info()
    result = {
        "tile_count": total_tiles,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "disk_bytes": total_disk_bytes,
        "disk_mb": round(total_disk_bytes / (1024 * 1024), 2),
        "path": str(cache_root()),
        "free_bytes": info["free_bytes"],
        "free_gb": info["free_gb"],
        "sources": out,
    }
    if source:
        match = next((s for s in out if s["source"] == source), None)
        if match:
            result.update(match)
    return result


def _refresh_stats(key: tuple, source: Optional[str]) -> None:
    try:
        result = _compute_stats(source)
        with _stats_lock:
            _stats_cache[key] = (time.monotonic(), copy.deepcopy(result))
    except Exception:
        pass
    finally:
        with _stats_lock:
            _stats_refreshing.discard(key)


def cache_stats(source: Optional[str] = None) -> dict:
    """Count cached tiles and total size. source=None aggregates every source.

    Stale-while-revalidate: after the first scan the previous numbers are
    returned immediately while one worker rescans, so a cache with tens of
    thousands of files never stalls the Settings or Map panel.
    """
    key = (source or "*", str(cache_root()))
    now = time.monotonic()
    with _stats_lock:
        cached = _stats_cache.get(key)
        if cached and now - cached[0] < STATS_TTL:
            return copy.deepcopy(cached[1])
        refreshing = key in _stats_refreshing
        if cached is not None and not refreshing:
            _stats_refreshing.add(key)
    if cached is not None:
        if not refreshing:
            threading.Thread(target=_refresh_stats, args=(key, source), daemon=True,
                             name="tile-stats").start()
        return copy.deepcopy(cached[1])
    result = _compute_stats(source)
    with _stats_lock:
        _stats_cache[key] = (time.monotonic(), copy.deepcopy(result))
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
    _invalidate_mem()
    _invalidate_stats()
    return {"removed": removed, "freed_bytes": freed,
            "freed_mb": round(freed / (1024 * 1024), 2),
            "source": source or "all"}


def verify_cache(source: Optional[str] = None, fix: bool = True) -> dict:
    """Check every cached tile is a real image and optionally remove bad ones."""
    targets = [source] if source else list(TILE_SOURCES)
    checked = invalid = removed = 0
    freed = 0
    details: list[str] = []
    for src in targets:
        base = cache_root() / _safe(src)
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix == ".part":
                if fix:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
                continue
            if f.suffix.lower() not in _IMAGE_SUFFIXES:
                continue
            checked += 1
            try:
                data = f.read_bytes()
            except OSError:
                continue
            if _looks_like_image(data) and not _is_error_tile(data):
                continue
            invalid += 1
            if len(details) < 8:
                details.append(str(f.relative_to(cache_root())))
            if fix:
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        _invalidate_mem()
        _invalidate_stats()
    return {
        "checked": checked, "invalid": invalid, "removed": removed,
        "freed_bytes": freed, "freed_mb": round(freed / (1024 * 1024), 2),
        "details": details, "source": source or "all",
    }


def sources_info() -> list[dict]:
    """Provider catalogue plus cached counts, for the source picker."""
    stats = {s["source"]: s for s in cache_stats()["sources"]}
    out = []
    for key, src in TILE_SOURCES.items():
        if src.get("overlay"):
            continue
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
