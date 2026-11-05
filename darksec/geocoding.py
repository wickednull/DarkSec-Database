"""One-shot, user-initiated OpenStreetMap Nominatim lookup.

Queries are cached only in memory for the life of the process; the address is
never written to the operator database or application logs. Keep this low-rate
and explicitly invoked to comply with the public service's usage policy.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

import requests

ENDPOINT = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "DarkSec-NIGHTWATCH/2.0 (offline-first local field console)"
MIN_INTERVAL_SECONDS = 1.1
CACHE_TTL_SECONDS = 24 * 3600
MAX_CACHE_ENTRIES = 32

_lock = threading.Lock()
_last_request = 0.0
_cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()


class GeocodingError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def search(address: str) -> dict:
    """Search one address; returns a few sanitized candidates and attribution."""
    query = " ".join(str(address or "").split())
    if len(query) < 3:
        raise GeocodingError("Enter at least 3 characters of an address.", 400)
    if len(query) > 200:
        raise GeocodingError("Address query is too long (maximum 200 characters).", 400)
    if any(ord(ch) < 32 for ch in query):
        raise GeocodingError("Address contains unsupported control characters.", 400)

    key = query.casefold()
    global _last_request
    with _lock:
        now = time.monotonic()
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL_SECONDS:
            _cache.move_to_end(key)
            return {"results": cached[1], "provider": "OpenStreetMap Nominatim", "cached": True}
        wait = MIN_INTERVAL_SECONDS - (now - _last_request)
        if wait > 0:
            raise GeocodingError(f"Please wait {wait:.1f} seconds before another address lookup.", 429)
        _last_request = now

    try:
        response = requests.get(
            ENDPOINT,
            params={"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=12,
        )
        if response.status_code == 429:
            raise GeocodingError("The geocoder is rate-limiting requests. Try again later.", 429)
        response.raise_for_status()
        payload = response.json()
    except GeocodingError:
        raise
    except requests.RequestException as exc:
        # Requests exceptions can include the full URL (and thus the address).
        # Keep the private query out of user-facing diagnostics and logs.
        raise GeocodingError(f"Address lookup failed ({type(exc).__name__}); check connectivity and retry.") from exc
    except ValueError as exc:
        raise GeocodingError("The geocoder returned an unreadable response.") from exc

    results = []
    if isinstance(payload, list):
        for item in payload[:5]:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            box = item.get("boundingbox")
            results.append({
                "lat": lat,
                "lon": lon,
                "label": str(item.get("display_name") or "Address result")[:500],
                "category": str(item.get("category") or "")[:80],
                "type": str(item.get("type") or "")[:80],
                "importance": item.get("importance"),
                "boundingbox": box if isinstance(box, list) and len(box) == 4 else None,
            })

    with _lock:
        _cache[key] = (time.monotonic(), results)
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return {
        "results": results,
        "provider": "OpenStreetMap Nominatim",
        "attribution": "© OpenStreetMap contributors",
        "cached": False,
    }
