"""NASA Near-Earth Object tracking for doomsday monitoring.

Fetches asteroid close approaches and calculates threat levels based on:
- Size (larger = more dangerous)
- Miss distance (closer = more dangerous)
- Hazardous classification
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

NASA_NEO_FEED_URL = "https://api.nasa.gov/neo/rest/v1/feed/today"

# In-memory TTL cache so we don't hammer NASA's rate-limited DEMO_KEY.
# Cache survives for `TTL_SECONDS` after the first successful fetch.
_cache: dict = {
    "objects": None,
    "summary": None,
    "fetched_at": 0.0,
    "retry_after": 0.0,
}
TTL_SECONDS = 3600  # 1 hour


def _cache_expired() -> bool:
    return time.monotonic() - _cache["fetched_at"] > TTL_SECONDS


def cached_neo_objects() -> list[dict]:
    return _cache["objects"] or []


def cached_neo_summary() -> dict | None:
    return _cache["summary"]


def _get_api_key() -> str:
    """Use DEMO_KEY by default; allow override via config.json."""
    try:
        from . import config
        cfg_path = config.DATA_ROOT / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text("utf-8"))
            key = (cfg or {}).get("nasa_api_key") or (cfg or {}).get("nasa") and ((cfg.get("nasa") or {}).get("api_key"))
            if key:
                return str(key)
    except Exception:
        pass
    return "DEMO_KEY"


def _fetch_live(api_key: str) -> list[dict]:
    """Call the NASA NEO API. Raises on HTTP error."""
    resp = requests.get(NASA_NEO_FEED_URL, params={"api_key": api_key}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    objects = []
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    neo_list = data.get("near_earth_objects", {}).get(today_str, [])
    for neo in neo_list:
        approaches = neo.get("close_approach_data") or []
        if not approaches:
            continue
        closest = approaches[0]
        name = neo.get("name", "Unknown")
        is_hazardous = bool(neo.get("is_potentially_hazardous_asteroid"))
        diameter_meters = float((neo.get("estimated_diameter", {}).get("meters", {}).get("estimated_diameter_max") or 0) or 0)
        miss_km = float((closest.get("miss_distance", {}).get("kilometers") or 0) or 0)
        miss_ld = float((closest.get("miss_distance", {}).get("lunar") or 0) or 0)
        velocity_kmh = float((closest.get("relative_velocity", {}).get("kilometers_per_hour") or 0) or 0)
        threat_score = calculate_threat_score(diameter_meters, miss_km, is_hazardous)
        objects.append({
            "title": f"NEO: {name}",
            "time": closest.get("close_approach_date_full", today_str),
            "name": name,
            "is_hazardous": is_hazardous,
            "diameter_meters": round(diameter_meters, 1),
            "miss_distance_km": round(miss_km, 1),
            "miss_distance_ld": round(miss_ld, 3),
            "velocity_kmh": round(velocity_kmh, 1),
            "threat_score": threat_score,
            "threat_level": get_threat_level(threat_score),
            "source": "NASA",
            "severity": get_severity(threat_score),
            "summary": f"{name} ({diameter_meters:.0f}m) passes at {miss_km:,.0f} km ({miss_ld:.2f} LD) at {velocity_kmh:,.0f} km/h",
            "url": neo.get("nasa_jpl_url", ""),
        })
    objects.sort(key=lambda x: x["threat_score"], reverse=True)
    return objects


def fetch_neo_objects(api_key: Optional[str] = None) -> list[dict]:
    """Fetch today's near-earth objects from NASA API (cached for 1 hour)."""
    now = time.monotonic()
    if _cache["objects"] is not None and not _cache_expired():
        return _cache["objects"]
    if now < _cache["retry_after"]:
        return _cache["objects"] or []
    try:
        key = api_key or _get_api_key()
        objects = _fetch_live(key)
        _cache["objects"] = objects
        _cache["summary"] = None  # invalidate derived summary too
        _cache["fetched_at"] = time.monotonic()
        _cache["retry_after"] = 0.0
        return objects
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        delay = 3600 if status == 429 else 6 * 3600 if status in (401, 403) else 300
        _cache["retry_after"] = time.monotonic() + delay
        code = f"HTTP {status}" if status else type(exc).__name__
        # Do not print the exception URL: it contains the API key.
        print(f"NASA NEO unavailable ({code}); retry deferred for {delay // 60} min")
        return _cache["objects"] or []
    except ValueError:
        _cache["retry_after"] = time.monotonic() + 300
        print("NASA NEO returned invalid data; retry deferred for 5 min")
        return _cache["objects"] or []


def calculate_threat_score(
    diameter_meters: float,
    miss_distance_km: float,
    is_hazardous: bool,
) -> float:
    """
    Calculate threat score 0-100 based on:
    - Size: 0-50 points (bigger = more dangerous)
    - Distance: 0-30 points (closer = more dangerous)
    - Hazardous flag: +20 points
    """
    score = 0.0
    if diameter_meters >= 10000:
        score += 50
    elif diameter_meters >= 1000:
        score += 40
    elif diameter_meters >= 140:
        score += 30
    elif diameter_meters >= 50:
        score += 20
    else:
        score += 10
    if miss_distance_km <= 100000:
        score += 30
    elif miss_distance_km <= 500000:
        score += 25
    elif miss_distance_km <= 1000000:
        score += 20
    elif miss_distance_km <= 5000000:
        score += 15
    elif miss_distance_km <= 20000000:
        score += 10
    else:
        score += 5
    if is_hazardous:
        score += 20
    return min(score, 100)


def get_threat_level(score: float) -> str:
    if score >= 80:
        return "CRITICAL"
    elif score >= 60:
        return "HIGH"
    elif score >= 40:
        return "ELEVATED"
    elif score >= 20:
        return "LOW"
    return "MINIMAL"


def get_severity(score: float) -> str:
    if score >= 80:
        return "CRITICAL"
    elif score >= 60:
        return "SEVERE"
    elif score >= 40:
        return "ALERT"
    return "INFO"


def get_neo_summary(api_key: Optional[str] = None) -> dict:
    """Get summary statistics for NEO dashboard display."""
    if _cache["summary"] is not None and not _cache_expired():
        return _cache["summary"]
    objects = fetch_neo_objects(api_key=api_key)
    if not objects:
        summary = {
            "total": 0, "hazardous": 0, "closest_km": 0,
            "largest_m": 0, "max_threat_score": 0, "threat_level": "UNKNOWN",
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        _cache["summary"] = summary
        return summary
    hazardous_count = sum(1 for o in objects if o["is_hazardous"])
    closest = min(objects, key=lambda x: x["miss_distance_km"])
    largest = max(objects, key=lambda x: x["diameter_meters"])
    max_threat = max(o["threat_score"] for o in objects)
    summary = {
        "total": len(objects),
        "hazardous": hazardous_count,
        "closest_km": closest["miss_distance_km"],
        "closest_name": closest["name"],
        "largest_m": largest["diameter_meters"],
        "largest_name": largest["name"],
        "max_threat_score": max_threat,
        "threat_level": get_threat_level(max_threat),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    _cache["summary"] = summary
    return summary
