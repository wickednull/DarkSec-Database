"""Global ALPR camera locations from the public DeFlock dataset.

DeFlock publishes OSM-derived ALPR locations as GeoJSON. This module keeps a
local copy under ``fielddata/maps/alpr`` so the map works offline after one
refresh, and serves viewport-limited points or low-zoom clusters.

Source: https://deflock.org (dataset by DeFlock, map data © OpenStreetMap
contributors, ODbL). Locations/tags only; no plate data or live tracking.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from . import config

SOURCES = {
    "us": "https://data.dontgetflocked.com/cameras.geojson.gz",
    "ca": "https://data.dontgetflocked.com/cameras-ca.geojson.gz",
}
CACHE_DIR = Path(config.DATA_DIR) / "maps" / "alpr"
META_FILE = CACHE_DIR / "meta.json"
REFRESH_AFTER_HOURS = 24 * 7
TIMEOUT = 180
DEFAULT_LIMIT = 4000
NOTE = (
    "DeFlock/OpenStreetMap ALPR locations (ODbL). Community-mapped, not a "
    "complete or authoritative registry. Locations only; no plate data."
)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "points": [], "error": None}


def _file(name: str) -> Path:
    return CACHE_DIR / f"{name}.geojson"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _meta() -> dict:
    try:
        if META_FILE.is_file():
            return json.loads(META_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _age_hours(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return None


def status() -> dict:
    meta = _meta()
    files = {}
    total_bytes = 0
    for name in SOURCES:
        path = _file(name)
        if path.is_file():
            stat = path.stat()
            total_bytes += stat.st_size
            files[name] = {
                "bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            }
        else:
            files[name] = None
    fetched = meta.get("fetched_at")
    return {
        "available": all(files.get(name) for name in SOURCES),
        "fetched_at": fetched,
        "age_hours": _age_hours(fetched),
        "refresh_after_hours": REFRESH_AFTER_HOURS,
        "stale": bool(fetched and (_age_hours(fetched) or 0) > REFRESH_AFTER_HOURS),
        "counts": meta.get("counts") or {},
        "bytes": total_bytes,
        "files": files,
        "source": "https://deflock.org",
        "note": NOTE,
        "error": _CACHE.get("error"),
    }


def _normalize_brand(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    low = text.lower()
    if low.startswith(("flock", "floc", "flow safety")):
        return "Flock Safety"
    if low.startswith(("motor", "vigilant")):
        return "Motorola Solutions"
    if low.startswith("genetec"):
        return "Genetec"
    if low.startswith("axis"):
        return "Axis Communications"
    if low.startswith("rekor"):
        return "Rekor"
    if low.startswith("leonardo") or low.startswith("elsag"):
        return "Leonardo"
    if low.startswith("neology") or low.startswith("pips"):
        return "Neology"
    if low.startswith("axon"):
        return "Axon"
    return text[:80]


def _parse_file(path: Path, source_name: str) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        return []
    out: list[dict] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        try:
            lon = float(coords[0]); lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        props = feat.get("properties") or {}
        brand = _normalize_brand(props.get("brand") or props.get("manufacturer") or props.get("make"))
        operator = " ".join(str(props.get("operator") or props.get("owner") or "").split())[:120]
        direction = props.get("direction")
        osm_type = str(props.get("osmType") or "node")
        osm_id = props.get("osmId")
        title = f"{brand} ALPR" if brand else "ALPR camera"
        bits = ["ALPR/ANPR location (OSM-derived)"]
        if brand:
            bits.append(f"brand={brand}")
        if operator:
            bits.append(f"operator={operator}")
        if direction not in (None, ""):
            bits.append(f"direction={direction}")
        if props.get("surveillanceZone"):
            bits.append(f"zone={props.get('surveillanceZone')}")
        if props.get("mountType"):
            bits.append(f"mount={props.get('mountType')}")
        if props.get("osmTimestamp"):
            bits.append(f"OSM timestamp={props.get('osmTimestamp')}")
        out.append({
            "alpr": True,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "title": title,
            "brand": brand,
            "operator": operator,
            "operator_name": operator,
            "direction": direction,
            "zone": props.get("surveillanceZone") or "",
            "surveillance_type": "ALPR",
            "camera_type": props.get("mountType") or "",
            "surveillance_zone": props.get("surveillanceZone") or "",
            "status": "surveillance",
            "flock_tagged": brand == "Flock Safety",
            "mount": props.get("mountType") or "",
            "osm_type": osm_type,
            "osm_id": str(osm_id) if osm_id is not None else "",
            "osm_timestamp": props.get("osmTimestamp") or "",
            "camera_id": str(osm_id) if osm_id is not None else "",
            "url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}" if osm_id is not None else "",
            "summary": "; ".join(bits)[:500],
            "source": f"DeFlock / OpenStreetMap ({source_name.upper()}, ODbL)",
            "kind": "alpr",
        })
    return out


def _load_points() -> list[dict]:
    """Parse the cached GeoJSON files, reloading only when they change."""
    parts = []
    for name in SOURCES:
        path = _file(name)
        try:
            stat = path.stat()
            parts.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            parts.append((str(path), 0, 0))
    key = tuple(parts)
    with _LOCK:
        if _CACHE.get("key") == key:
            return _CACHE.get("points") or []
    points: list[dict] = []
    seen: set[str] = set()
    for name in SOURCES:
        path = _file(name)
        if not path.is_file():
            continue
        for item in _parse_file(path, name):
            ident = f"{item.get('osm_type')}:{item.get('osm_id')}"
            if ident in seen:
                continue
            seen.add(ident)
            points.append(item)
    with _LOCK:
        _CACHE.update({"key": key, "points": points, "error": None})
    return points


def refresh(force: bool = False) -> dict:
    """Download the US + Canada DeFlock datasets into the local cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    current = status()
    if not force and current["available"] and not current["stale"]:
        data = dict(current)
        data["ok"] = True
        data["cached"] = True
        return data
    counts: dict[str, int] = {}
    try:
        for name, url in SOURCES.items():
            resp = requests.get(url, timeout=TIMEOUT,
                                headers={"User-Agent": "DarkSec/2.0 (field console; ALPR locations)",
                                         "Accept": "application/geo+json,application/json"})
            resp.raise_for_status()
            raw = resp.content
            if not raw or raw[:1] not in (b"{", b"["):
                raise RuntimeError(f"{name}: provider returned non-JSON data")
            try:
                payload = json.loads(raw)
            except Exception as exc:
                raise RuntimeError(f"{name}: invalid GeoJSON ({str(exc)[:100]})")
            features = payload.get("features") if isinstance(payload, dict) else None
            if not isinstance(features, list):
                raise RuntimeError(f"{name}: response is not a FeatureCollection")
            counts[name] = len(features)
            tmp = _file(name).with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(_file(name))
        meta = {
            "fetched_at": _now(),
            "counts": counts,
            "sources": SOURCES,
            "note": NOTE,
        }
        META_FILE.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        with _LOCK:
            _CACHE.update({"key": None, "points": [], "error": None})
        _load_points()
        data = status()
        data.update({"ok": True, "cached": False, "counts": counts})
        return data
    except Exception as exc:
        with _LOCK:
            _CACHE["error"] = str(exc)[:300]
        data = status()
        data.update({"ok": False, "cached": False, "error": str(exc)[:300]})
        return data


def features(bbox: tuple[float, float, float, float] | None, zoom: int = 0,
             limit: int = DEFAULT_LIMIT) -> dict:
    """Return ALPR points or low-zoom clusters for the requested viewport."""
    points = _load_points()
    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        if minlon <= maxlon:
            view = [p for p in points if minlat <= p["lat"] <= maxlat and minlon <= p["lon"] <= maxlon]
        else:
            view = [p for p in points if minlat <= p["lat"] <= maxlat
                    and (p["lon"] >= minlon or p["lon"] <= maxlon)]
    else:
        view = points
    total = len(view)
    limit = max(100, min(int(limit), 20000))
    if total > limit and zoom <= 10:
        span = 360.0
        if bbox:
            span = max(abs(maxlon - minlon), abs(maxlat - minlat), 0.01)
        cell = max(0.01, span / 24.0)
        buckets: dict[tuple[int, int], dict] = {}
        for p in view:
            key = (math.floor(p["lon"] / cell), math.floor(p["lat"] / cell))
            b = buckets.get(key)
            if b is None:
                b = {"lat": 0.0, "lon": 0.0, "count": 0, "brands": {}}
                buckets[key] = b
            b["lat"] += p["lat"]; b["lon"] += p["lon"]; b["count"] += 1
            brand = p.get("brand") or ""
            if brand:
                b["brands"][brand] = b["brands"].get(brand, 0) + 1
        clusters = []
        for b in buckets.values():
            brands = sorted(b["brands"].items(), key=lambda kv: -kv[1])
            brand = brands[0][0] if len(brands) == 1 else ""
            clusters.append({
                "lat": round(b["lat"] / b["count"], 5),
                "lon": round(b["lon"] / b["count"], 5),
                "count": b["count"],
                "brand": brand,
                "title": f"{brand} ALPR ×{b['count']}" if brand else f"{b['count']} ALPR cameras",
                "alpr": True,
                "source": "DeFlock / OpenStreetMap (ODbL)",
            })
        clusters.sort(key=lambda c: -c["count"])
        return {
            "mode": "clusters",
            "clusters": clusters[:limit],
            "points": [],
            "in_view": total,
            "dataset_count": len(points),
            "fetched_at": _meta().get("fetched_at"),
            "source": "https://deflock.org",
            "note": NOTE,
        }
    return {
        "mode": "points",
        "clusters": [],
        "points": view[:limit],
        "in_view": total,
        "dataset_count": len(points),
        "fetched_at": _meta().get("fetched_at"),
        "source": "https://deflock.org",
        "note": NOTE,
    }
