"""Optional online intelligence feeds with offline cache.

The console never requires a network. When a connection is available the
operator can pull public feeds (USGS, NOAA SWPC, NWS, GDACS, WHO, ReliefWeb);
every response is cached in SQLite and remains readable offline with a clear
age indicator.
"""
from __future__ import annotations

import csv
import heapq
import html
import io
import json
import re
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from .db import Database, utcnow

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36 DarkSecNightwatch/2.0",
    "Accept": "text/plain,text/xml,application/xml,application/json,*/*;q=0.8",
}
TIMEOUT = 15
UCDP_TIMEOUT = 90

CELESTRAK_HEADERS = [
    {
        "User-Agent": "DarkSecNightwatch/2.0 (+https://github.com/darksec; CelesTrak TLE cache)",
        "Accept": "text/plain,*/*;q=0.8",
    },
    {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
        "Accept": "text/plain,*/*;q=0.8",
    },
]

# Fetch-age limits and data-age limits are different things. A feed can fetch
# successfully while every item in it is months old (Safecast, WHO archives).
# Both are surfaced so the UI never presents an old snapshot as current.
STALE_AFTER = {
    "gdacs": 3 * 3600,
    "gdelt_conflict": 2 * 3600,
    "rsoe_edis": 3 * 3600,
    "ucdp_candidate": 50 * 86400,
    "celestrak_tle": 24 * 3600,
    "crisisgroup": 24 * 3600,
    "war_on_the_rocks": 24 * 3600,
    "smithsonian_volcano": 14 * 86400,
    "who_don": 14 * 86400,
    "safecast": 30 * 86400,
}
DATA_STALE_AFTER = {
    "gdacs": 48 * 3600,
    "gdelt_conflict": 3 * 3600,
    "bbc_world": 24 * 3600,
    "bbc_middle_east": 24 * 3600,
    "guardian_world": 24 * 3600,
    "france24_world": 24 * 3600,
    "dw_news": 24 * 3600,
    "aljazeera_world": 24 * 3600,
    "twz_defense": 7 * 86400,
    "breaking_defense": 7 * 86400,
    "defense_one": 7 * 86400,
    "long_war_journal": 14 * 86400,
    "bbc_world": 24 * 3600,
    "bbc_middle_east": 24 * 3600,
    "guardian_world": 24 * 3600,
    "france24_world": 24 * 3600,
    "dw_news": 24 * 3600,
    "aljazeera_world": 24 * 3600,
    "twz_defense": 7 * 86400,
    "breaking_defense": 7 * 86400,
    "defense_one": 7 * 86400,
    "long_war_journal": 14 * 86400,
    "crisisgroup": 21 * 86400,
    "rsoe_edis": 4 * 3600,
    "ucdp_candidate": 50 * 86400,
    "celestrak_tle": 24 * 3600,
    "crisisgroup": 21 * 86400,
    "war_on_the_rocks": 14 * 86400,
    "smithsonian_volcano": 14 * 86400,
    "who_don": 14 * 86400,
    "safecast": 30 * 86400,
}
DEFAULT_STALE_AFTER = 6 * 3600

# Positional/telemetry feeds are useful as map layers but their timestamps are
# not meaningful "latest intelligence" headlines; exclude them from summaries.
SUMMARY_SKIP_KINDS = {"celestrak", "opensky", "air_quality", "weather",
                      "maritime", "ais", "cables", "cctv", "geojson"}

# Feed payloads can be several megabytes (CelesTrak carries ~16k TLEs,
# OpenSky thousands of positions). Parsing them and walking every item's
# timestamp on each API request made the dashboard and intel views CPU-bound.
# Cache the decoded payload plus its newest timestamp, keyed by the feed row's
# fetch metadata and payload length, so a refresh invalidates the entry.
_PAYLOAD_CACHE: dict[tuple, tuple[dict | None, list[dict], datetime | None]] = {}
_PAYLOAD_CACHE_LOCK = threading.Lock()


def _decode_payload(db: Database, row) -> tuple[dict | None, list[dict], datetime | None]:
    # Avoid pulling the full (sometimes multi-megabyte) payload blob on every
    # request: list_feeds selects length(payload) as a cheap version marker and
    # this helper only reads the blob on a cache miss.
    key = (row["id"], row["fetched_at"], row["status"], row["url"],
           row["payload_len"])
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
    if cached is not None:
        return cached
    blob = db.one("SELECT payload FROM feeds WHERE id=?", (row["id"],))
    raw = (blob["payload"] if blob else "") or ""
    payload = None
    items: list[dict] = []
    if raw:
        try:
            payload = json.loads(raw)
            items = payload.get("items") or []
        except Exception:
            payload = None
            items = []
    latest_time = None
    for item in items:
        parsed = parse_time(item.get("time"))
        if parsed is not None and (latest_time is None or parsed > latest_time):
            latest_time = parsed
    entry = (payload, items, latest_time)
    with _PAYLOAD_CACHE_LOCK:
        # Keep only the current blob for this feed; older refreshes are dead.
        for old in [k for k in _PAYLOAD_CACHE if k[0] == row["id"] and k != key]:
            _PAYLOAD_CACHE.pop(old, None)
        _PAYLOAD_CACHE[key] = entry
    return entry


def parse_time(value) -> datetime | None:
    """Return an aware UTC datetime for RSS, ISO and GDELT/UCDP timestamps."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    # GDELT DATEADDED: YYYYMMDDHHMMSS
    if len(text) >= 14 and text[:14].isdigit():
        try:
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # GDELT SQLDATE: YYYYMMDD
    if len(text) == 8 and text.isdigit():
        try:
            return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    # RFC 822 / RFC 2822, the common RSS pubDate format.
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    # ISO 8601, including the trailing Z used by EONET and Safecast.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass
    # UCDP candidate CSV dates and a few legacy formats.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d",
                "%A, %B %d, %Y - %H:%M", "%A, %d %B %Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _iso_time(value) -> str | None:
    parsed = parse_time(value)
    return parsed.isoformat() if parsed else (str(value).strip() if value else None)


def _age_seconds(value) -> int | None:
    parsed = parse_time(value)
    if parsed is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _sanitize(text, limit: int = 500) -> str:
    """Strip HTML/entities and collapse whitespace for display and storage."""
    clean = re.sub(r"(?s)<[^>]+>", " ", str(text or ""))
    clean = html.unescape(clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:limit]


def _parse_usgs(payload: dict) -> list[dict]:
    items = []
    for f in (payload.get("features") or [])[:50]:
        p = f.get("properties", {})
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        items.append({
            "title": p.get("title") or p.get("place") or "Earthquake",
            "time": datetime.fromtimestamp((p.get("time") or 0) / 1000, tz=timezone.utc).isoformat()
            if p.get("time") else None,
            "magnitude": p.get("mag"), "place": p.get("place"), "url": p.get("url"),
            "lat": coords[1], "lon": coords[0], "depth_km": coords[2],
            "severity": "CRITICAL" if (p.get("mag") or 0) >= 6.5 else "ALERT" if (p.get("mag") or 0) >= 5 else "INFO",
            "source": "USGS",
        })
    return items


def _parse_swpc_kp(payload: list) -> list[dict]:
    items = []
    for row in payload[-48:]:
        items.append({
            "title": f"Kp {row.get('kp_index')}",
            "time": row.get("time_tag"),
            "kp": row.get("kp_index"),
            "estimated_kp": row.get("estimated_kp"),
            "severity": "CRITICAL" if (row.get("kp_index") or 0) >= 7 else "ALERT" if (row.get("kp_index") or 0) >= 5 else "INFO",
            "source": "NOAA SWPC",
        })
    return items


def _parse_swpc_scales(payload: dict) -> list[dict]:
    out = []
    # The product returns {"0": {"R": {...}, "S": {...}, "G": {...}}, ...};
    # the "0" entry is the current day.
    root = payload.get("0") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        root = payload if isinstance(payload, dict) else {}
    for key, label in (("R", "RADIO BLACKOUT"), ("S", "SOLAR RADIATION"), ("G", "GEOMAGNETIC STORM")):
        scale = root.get(key) or root.get(key.lower()) or {}
        if not isinstance(scale, dict) or not scale:
            continue
        current = scale.get("Scale") or scale.get("scale") or "0"
        text = scale.get("Text") or scale.get("text") or ""
        try:
            level = int(current)
        except (TypeError, ValueError):
            level = 0
        out.append({
            "title": f"{label} scale {current}" + (f" ({text})" if text else ""),
            "level": current,
            "text": text,
            "severity": "CRITICAL" if level >= 4 else "ALERT" if level >= 2 else "INFO",
            "source": "NOAA SWPC",
        })
    return out


def _geometry_centroid(geometry: dict | None) -> tuple[float | None, float | None, str]:
    """Return a representative point for a GeoJSON alert polygon."""
    if not geometry:
        return None, None, ""
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "Point" and isinstance(coords, list) and len(coords) >= 2:
        try:
            return float(coords[1]), float(coords[0]), "alert point"
        except (TypeError, ValueError):
            return None, None, ""
    rings = []
    if kind == "Polygon" and isinstance(coords, list) and coords:
        rings = [coords[0]]
    elif kind == "MultiPolygon" and isinstance(coords, list):
        rings = [poly[0] for poly in coords if isinstance(poly, list) and poly]
    if not rings:
        return None, None, ""
    # Use the largest ring so a multipart alert is anchored on its main area.
    ring = max(rings, key=lambda r: len(r) if isinstance(r, list) else 0)
    points = []
    for pair in ring or []:
        try:
            points.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError, IndexError):
            continue
    if not points:
        return None, None, ""
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return lat, lon, "alert polygon centroid"


def _parse_nws(payload: dict) -> list[dict]:
    items = []
    severity_map = {
        "EXTREME": "CRITICAL", "SEVERE": "SEVERE", "MODERATE": "ALERT",
        "MINOR": "INFO", "UNKNOWN": "INFO",
    }
    for f in (payload.get("features") or [])[:100]:
        p = f.get("properties", {})
        lat, lon, precision = _geometry_centroid(f.get("geometry"))
        time = p.get("onset") or p.get("effective") or p.get("sent")
        items.append({
            "title": p.get("headline") or p.get("event") or "Weather alert",
            "time": _iso_time(time),
            "severity": severity_map.get(str(p.get("severity") or "").upper(),
                                         str(p.get("severity") or "INFO").upper()),
            "area": p.get("areaDesc"),
            "onset": p.get("onset"), "expires": p.get("expires"),
            "lat": lat, "lon": lon, "precision": precision,
            "url": p.get("uri"), "source": "NWS",
            "summary": (p.get("description") or "")[:400],
        })
    return items


def _parse_rss(text: str, source: str = "RSS") -> list[dict]:
    """Parse RSS 2.0, RDF/RSS 1.0 and Atom.

    RDF feeds (Deutsche Welle) put items at the root, not under <channel>.
    Guardian, BBC and other RSS 2.0 feeds use RFC 822 pubDates; those are
    normalised to ISO here so every consumer can sort them safely.
    """
    out = []
    try:
        # Strip any XML declaration: requests already decoded the bytes, and a
        # stale ISO-8859-1 declaration would make ElementTree fail on UTF-8.
        clean = re.sub(r"^\s*<\?xml[^>]*\?>", "", text or "", count=1)
        root = ET.fromstring(clean)
    except Exception:
        return out

    atom = "{http://www.w3.org/2005/Atom}"
    rss1 = "{http://purl.org/rss/1.0/}"
    channel = root.find("channel")
    items = list(channel.findall("item")) if channel is not None else []
    if not items:
        items = root.findall("item") or root.findall(f"{rss1}item")
    if not items:
        items = root.findall(f"{atom}entry") or root.findall(f".//{atom}entry")

    def child_text(node, *tags):
        for tag in tags:
            found = node.find(tag)
            if found is not None:
                value = " ".join(found.itertext()).strip()
                if value:
                    return value
        # Last resort for prefixes ElementTree does not know.
        wanted = {t.split("}", 1)[-1].split(":")[-1].lower() for t in tags}
        for child in list(node):
            local = child.tag.split("}", 1)[-1].split(":")[-1].lower()
            if local in wanted:
                value = " ".join(child.itertext()).strip()
                if value:
                    return value
        return ""

    for item in items[:80]:
        title = child_text(item, "title", f"{atom}title")
        link = child_text(item, "link", f"{rss1}link", f"{atom}link")
        if not link:
            for link_node in item.findall(f"{atom}link"):
                if link_node.get("href"):
                    link = link_node.get("href")
                    break
        if not link:
            link = child_text(item, "guid", f"{rss1}guid")
        desc = child_text(
            item, "description", f"{rss1}description", f"{atom}summary",
            f"{atom}content", "{http://purl.org/rss/1.0/modules/content/}encoded",
            "{http://search.yahoo.com/mrss/}description",
        )
        desc = re.sub(r"(?s)<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        raw_time = child_text(
            item, "pubDate", "date", f"{rss1}date",
            "{http://purl.org/dc/elements/1.1/}date",
            f"{atom}published", f"{atom}updated",
        )
        categories = [
            (node.text or "").strip()
            for node in item.findall("category") + item.findall(f"{rss1}category")
            if (node.text or "").strip()
        ]
        lat = lon = None
        point = item.find("{http://www.georss.org/georss}point")
        if point is not None and point.text:
            bits = point.text.split()
            if len(bits) >= 2:
                try:
                    lat, lon = float(bits[0]), float(bits[1])
                except ValueError:
                    lat = lon = None
        if lat is None or lon is None:
            try:
                lat_node = item.find("{http://www.w3.org/2003/01/geo/wgs84_pos}lat")
                lon_node = item.find("{http://www.w3.org/2003/01/geo/wgs84_pos}long")
                if lat_node is not None and lon_node is not None and lat_node.text and lon_node.text:
                    lat, lon = float(lat_node.text), float(lon_node.text)
            except (TypeError, ValueError):
                lat = lon = None
        out.append({
            "title": title,
            "time": _iso_time(raw_time),
            "url": link,
            "summary": desc[:400],
            "lat": lat, "lon": lon,
            "categories": categories[:6],
            "severity": "INFO",
            "source": source,
        })
    return out


def _parse_opensky(payload: dict) -> list[dict]:
    """Live aircraft positions from the public OpenSky state stream."""
    items = []
    states = (payload or {}).get("states") or []
    for st in states:
        if not isinstance(st, (list, tuple)) or len(st) < 8:
            continue
        try:
            lat = float(st[5]); lon = float(st[6]); alt = float(st[7])
        except (TypeError, ValueError):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        callsign = str(st[1] or "").strip()
        country = str(st[2] or "").strip()
        items.append({
            "title": callsign or country or "AIRCRAFT",
            "time": datetime.fromtimestamp(int(st[3] or 0), tz=timezone.utc).isoformat() if st[3] else utcnow(),
            "lat": lat, "lon": lon, "alt_m": alt,
            "speed_kmh": float(st[10]) if isinstance(st[10], (int, float)) else None,
            "heading_deg": float(st[9]) if isinstance(st[9], (int, float)) else None,
            "source": "OpenSky",
            "severity": "INFO",
            "url": f"https://opensky-network.org/flight/?icao24={str(st[0]).lower()}" if str(st[0]).isalnum() else None,
            "summary": f"Public live position. Callsign {callsign or '—'} · {country or 'unknown registry'}.",
        })
    return items


def _parse_celestrak(raw: str) -> list[dict]:
    """Parse CelesTrak TLE text (one or more concatenated TLE sets)."""
    items = []
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    idx = 0
    while idx + 1 < len(lines):
        l1, l2 = lines[idx], lines[idx + 1]
        if not l1.startswith("1 "):
            idx += 1
            continue
        norad = None
        m = re.match(r"1\s+(\d{5})U", l1)
        if m:
            norad = int(m.group(1))
        name = l2[8:16].strip() if len(l2) > 8 else ""
        items.append({
            "title": f"SAT {name or ('NORAD ' + str(norad))}",
            "norad": norad,
            "name": name or (f"NORAD {norad}" if norad else "UNKNOWN"),
            "line1": l1,
            "line2": l2,
            "source": "CelesTrak",
            "severity": "INFO",
            "time": utcnow(),
            "url": f"https://celestrak.org/norad/tle/{norad}" if norad else None,
        })
        idx += 2
    return items


def _aqi_category(level: float) -> str:
    if level <= 50:
        return "GOOD"
    if level <= 100:
        return "MODERATE"
    if level <= 150:
        return "UNHEALTHY FOR SENSITIVE GROUPS"
    if level <= 200:
        return "UNHEALTHY"
    if level <= 300:
        return "VERY UNHEALTHY"
    return "HAZARDOUS"


def _parse_air_quality(payload: dict) -> list[dict]:
    """Air quality index snapshot from Open-Meteo (no API key required)."""
    items = []
    cur = (payload or {}).get("current") or {}
    hourly = (payload or {}).get("hourly") or {}
    aqi = cur.get("us_aqi")
    if aqi is None and isinstance(cur, dict) and cur.get("pm25") is not None:
        aqi = cur.get("pm25")
    if aqi is None and hourly.get("us_aqi"):
        aqi = hourly["us_aqi"][0]
    if aqi is None:
        return items
    level = float(aqi)
    if level >= 301:
        sev = "CRITICAL"
    elif level >= 201:
        sev = "SEVERE"
    elif level >= 151:
        sev = "ALERT"
    else:
        sev = "INFO"
    items.append({
        "title": f"Air quality US AQI {int(aqi)}",
        "time": cur.get("time") or datetime.now(timezone.utc).isoformat(),
        "lat": payload.get("latitude"), "lon": payload.get("longitude"),
        "aqi": int(level),
        "pm2_5": cur.get("pm2_5"),
        "category": _aqi_category(level),
        "source": "Open-Meteo",
        "severity": sev,
        "url": "https://air-quality-api.open-meteo.com/",
        "summary": "Open-Meteo air quality index (US AQI) for the operator position. "
                   "Apply local weather derating; do not use for evacuation decisions alone.",
    })
    return items


_WMO = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "light freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "light snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def _parse_weather(payload: dict) -> list[dict]:
    """Current local conditions from Open-Meteo for the operator position."""
    cur = (payload or {}).get("current") or {}
    if not cur:
        return []
    code = cur.get("weather_code")
    parts = []
    if cur.get("temperature_2m") is not None:
        parts.append(f"{cur['temperature_2m']} °C")
    if cur.get("apparent_temperature") is not None:
        parts.append(f"feels {cur['apparent_temperature']} °C")
    if cur.get("wind_speed_10m") is not None:
        parts.append(f"wind {cur['wind_speed_10m']} km/h")
    if cur.get("wind_gusts_10m") is not None:
        parts.append(f"gusts {cur['wind_gusts_10m']} km/h")
    if cur.get("precipitation") is not None:
        parts.append(f"precip {cur['precipitation']} mm")
    if cur.get("pressure_msl") is not None:
        parts.append(f"{cur['pressure_msl']} hPa")
    cond = _WMO.get(int(code), "") if code is not None else ""
    return [{
        "title": "Local weather · " + (", ".join(parts) or "no values"),
        "time": cur.get("time") or datetime.now(timezone.utc).isoformat(),
        "lat": payload.get("latitude"), "lon": payload.get("longitude"),
        "temperature_c": cur.get("temperature_2m"),
        "apparent_c": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "wind_kmh": cur.get("wind_speed_10m"),
        "gust_kmh": cur.get("wind_gusts_10m"),
        "precip_mm": cur.get("precipitation"),
        "pressure_hpa": cur.get("pressure_msl"),
        "condition": cond,
        "source": "Open-Meteo",
        "severity": "INFO",
        "url": "https://open-meteo.com/",
        "summary": f"Model forecast for the operator position, not an observation. {cond.capitalize()}.",
    }]


def _parse_gdacs(payload: dict) -> list[dict]:
    """GDACS disaster alerts (JSON API; the old RSS endpoint is WAF-blocked)."""
    items = []
    for f in (payload.get("features") or [])[:80]:
        p = f.get("properties") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
        level = str(p.get("alertlevel") or "Green")
        sev = {"Red": "CRITICAL", "Orange": "ALERT", "Green": "INFO"}.get(level, "INFO")
        urls = p.get("url") or {}
        link = urls.get("report") if isinstance(urls, dict) else None
        items.append({
            "title": p.get("name") or p.get("description") or "Disaster alert",
            # GDACS events can persist for long periods; datemodified reflects the
            # latest alert update and is the right freshness signal for cache status.
            "time": p.get("datemodified") or p.get("todate") or p.get("fromdate"),
            "from_date": p.get("fromdate"),
            "to_date": p.get("todate"),
            "date_modified": p.get("datemodified"),
            "lat": coords[1] if len(coords) > 1 else None,
            "lon": coords[0] if coords else None,
            "severity": sev,
            "alertlevel": level,
            "eventtype": p.get("eventtype"),
            "country": p.get("country"),
            "url": link,
            "summary": (p.get("htmldescription") or p.get("description") or "")[:400],
            "source": "GDACS",
        })
    return items


def _parse_eonet(payload: dict) -> list[dict]:
    """NASA EONET open natural events (wildfires, volcanoes, storms, dust)."""
    items = []
    for ev in (payload.get("events") or [])[:80]:
        geometry = ev.get("geometry") or []
        last = geometry[-1] if geometry else {}
        coords = last.get("coordinates") or [None, None]
        cats = ev.get("categories") or [{}]
        category = (cats[0] or {}).get("title") or "Natural event"
        sev = "ALERT" if category in ("Volcanoes", "Wildfires") else "INFO"
        sources = ev.get("sources") or [{}]
        items.append({
            "title": ev.get("title") or category,
            "time": last.get("date"),
            "lat": coords[1] if len(coords) > 1 else None,
            "lon": coords[0] if coords else None,
            "severity": sev,
            "category": category,
            "magnitude": last.get("magnitudeValue"),
            "magnitude_unit": last.get("magnitudeUnit"),
            "url": (sources[0] or {}).get("url") or ev.get("link"),
            "summary": f"EONET {category} event, status open.",
            "source": "NASA EONET",
        })
    return items


def _parse_safecast(payload: list) -> list[dict]:
    """Volunteer radiation measurements. Not a calibrated official monitor."""
    items = []
    for m in (payload or [])[:60]:
        if not isinstance(m, dict):
            continue
        value = m.get("value")
        unit = m.get("unit") or ""
        mid = m.get("id")
        items.append({
            "title": f"{value} {unit} reported".strip(),
            "time": m.get("captured_at"),
            "lat": m.get("latitude"), "lon": m.get("longitude"),
            "value": value, "unit": unit,
            "place": m.get("location_name"),
            "severity": "INFO",
            "url": f"https://api.safecast.org/measurements/{mid}" if mid else "https://safecast.org/",
            "summary": "Volunteer-submitted measurement, not a calibrated official monitor. "
                       "Check the capture date, device and local background before drawing conclusions.",
            "source": "Safecast",
        })
    return items


GDELT_LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_EVENT_LABELS = {
    "180": "Armed clash", "181": "Abduction", "182": "Assault",
    "183": "Bombing / explosion", "184": "Human shield", "185": "Assassination attempt",
    "186": "Assassination", "190": "Military force", "191": "Blockade",
    "192": "Territory occupied", "193": "Small-arms clash", "194": "Heavy weapons",
    "195": "Air / missile strike", "196": "Ceasefire violation", "197": "No-fly violation",
    "200": "Mass killing", "201": "Ethnic cleansing", "202": "Sexual violence",
}
_ARMED_ACTOR = re.compile(
    r"MILIT|ARMY|TROOP|REBEL|INSURG|GUERRILL|MILITANT|JIHAD|PARAMILIT|"
    r"FIGHTER|SOLDIER|NAVY|AIR ?FORCE|WARSHIP|ARTILLERY|DRONE|BATTALION|"
    r"BRIGADE|REGIMENT|SECURITY FORCE|ARMED|HOUTHI|TALIBAN|ISLAMIC STATE|"
    r"WAGNER|HEZBOLLAH|HAMAS|JANJAWEED|RSF|M23|BOKO HARAM",
    re.I,
)


def _as_float(value) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _gdelt_conflict_items(rows: list[list[str]]) -> list[dict]:
    """Filter the GDELT 2.0 15-minute export to armed-conflict events.

    GDELT's CAMEO coding is automated and noisy: a lawsuit can be coded as
    "use conventional military force". We require an armed actor or a
    high-specificity event code, and keep the location precision so the map
    can distinguish a city report from a country-level mention.
    """
    precision_labels = {"1": "country", "2": "state/province", "3": "city",
                        "4": "city", "5": "region"}
    out = []
    now = datetime.now(timezone.utc)
    for row in rows:
        if len(row) < 61:
            continue
        root = str(row[28] or "")
        code = str(row[26] or "")
        if root not in ("18", "19", "20"):
            continue
        lat, lon = _as_float(row[56]), _as_float(row[57])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if abs(lat) < 0.01 and abs(lon) < 0.01:
            continue
        geo_type = str(row[51] or "")
        if geo_type not in ("3", "4", "5"):
            continue
        actor1, actor2 = str(row[6] or "").strip(), str(row[16] or "").strip()
        mentions = _as_int(row[31]) or 0
        armed = bool(_ARMED_ACTOR.search(actor1) or _ARMED_ACTOR.search(actor2))
        high_specificity = code in ("181", "183", "186", "194", "195", "200", "201", "202")
        if not armed and not high_specificity:
            continue
        if not high_specificity and mentions < 3:
            continue
        if high_specificity and mentions < 2:
            continue
        reported = parse_time(row[59]) or parse_time(row[1]) or now
        event_date = parse_time(row[1])
        place = str(row[52] or "").strip()
        label = GDELT_EVENT_LABELS.get(code) or {
            "18": "Assault", "19": "Armed clash", "20": "Mass violence",
        }.get(root, "Conflict event")
        if actor1 and actor2 and actor1.upper() != actor2.upper():
            title = f"{label}: {actor1} vs {actor2}"
        else:
            title = label
        if place:
            title = f"{title} — {place}"
        severity = "CRITICAL" if root == "20" or code in ("183", "194", "195", "200", "201", "202") else "ALERT"
        out.append({
            "id": f"gdelt:{row[0]}",
            "title": title,
            "time": reported.isoformat(),
            "event_date": event_date.isoformat() if event_date else None,
            "lat": round(lat, 5), "lon": round(lon, 5),
            "place": place, "country": str(row[53] or "").strip(),
            "precision": precision_labels.get(geo_type, "reported location"),
            "severity": severity,
            "kind": "conflict",
            "verified": False,
            "source": "GDELT (media-derived)",
            "event_code": code, "event_root": root,
            "mentions": mentions,
            "goldstein": _as_float(row[30]),
            "fatalities": None,
            "url": str(row[60] or "").strip() or None,
            "summary": (f"Automated GDELT coding of a media report ({label}). "
                        f"{mentions} mention(s). Actor and event coding can be wrong; "
                        "verify against the linked report before acting."),
        })
    return out


def _gdelt_export_urls(count: int) -> list[str]:
    resp = requests.get(GDELT_LASTUPDATE, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    export_url = None
    for line in resp.text.splitlines():
        bits = line.split()
        if bits and bits[-1].endswith(".export.CSV.zip"):
            export_url = bits[-1]
            break
    if not export_url:
        raise RuntimeError("GDELT lastupdate did not list an export file")
    stamp = re.search(r"(\d{14})\.export\.CSV\.zip$", export_url)
    if not stamp:
        raise RuntimeError("GDELT export filename was not parseable")
    latest = datetime.strptime(stamp.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    prefix = export_url[:export_url.rfind("/") + 1]
    urls = []
    for i in range(max(1, count)):
        ts = (latest - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
        urls.append(f"{prefix}{ts}.export.CSV.zip")
    return urls


def _refresh_gdelt(db: Database, row) -> list[dict]:
    """Merge the latest GDELT slices with the cached 36-hour window."""
    try:
        previous = json.loads(row["payload"] or "{}").get("items") or []
    except Exception:
        previous = []
    kept = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    for item in previous:
        parsed = parse_time(item.get("time"))
        if parsed is not None and parsed >= cutoff and item.get("id"):
            kept.append(item)
    # Build a useful window on first run; afterwards the 15-minute slices are
    # merged into the existing cache so the map does not blink empty.
    slices = 4 if not kept else 2
    fresh: list[dict] = []
    errors = []
    for url in _gdelt_export_urls(slices):
        try:
            resp = requests.get(url, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
                raw = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
            fresh.extend(_gdelt_conflict_items(list(csv.reader(io.StringIO(raw), delimiter="\t"))))
        except Exception as exc:
            errors.append(str(exc)[:120])
    if not fresh:
        raise RuntimeError("GDELT export unavailable: " + ("; ".join(errors[:2]) or "no matching conflict rows"))
    merged = {item["id"]: item for item in kept}
    for item in fresh:
        merged[item["id"]] = item
    items = sorted(merged.values(), key=lambda item: item.get("time") or "", reverse=True)
    return items[:2500]


def _ucdp_latest_url() -> str:
    """Discover the newest monthly UCDP candidate CSV."""
    fallback = "https://ucdp.uu.se/downloads/candidateged/GEDEvent_v26_0_9.csv"
    try:
        resp = requests.get("https://ucdp.uu.se/downloads/", headers=UA, timeout=30)
        resp.raise_for_status()
    except Exception:
        return fallback
    found = []
    for a, b, c in re.findall(r"GEDEvent_v(\d+)_(\d+)_(\d+)\.csv", resp.text):
        found.append((int(a), int(b), int(c), f"GEDEvent_v{a}_{b}_{c}.csv"))
    if not found:
        return fallback
    _, _, _, name = max(found)
    return f"https://ucdp.uu.se/downloads/candidateged/{name}"


def _parse_ucdp(text: str) -> list[dict]:
    out = []
    try:
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    except Exception:
        return out
    dated = []
    for row in rows:
        parsed = parse_time(row.get("date_start"))
        if parsed is not None:
            dated.append((parsed, row))
    if not dated:
        return out
    cutoff = max(parsed for parsed, _ in dated) - timedelta(days=120)
    precision_labels = {"1": "exact location", "2": "near location",
                        "3": "region", "4": "country"}
    for parsed, row in dated:
        if parsed < cutoff:
            continue
        lat, lon = _as_float(row.get("latitude")), _as_float(row.get("longitude"))
        if lat is None or lon is None:
            continue
        best = _as_int(row.get("best")) or 0
        civilians = _as_int(row.get("deaths_civilians")) or 0
        type_id = str(row.get("type_of_violence") or "")
        type_label = {"1": "state-based conflict", "2": "non-state conflict",
                      "3": "one-sided violence"}.get(type_id, "armed violence")
        side_a = str(row.get("side_a") or "").strip()
        side_b = str(row.get("side_b") or "").strip()
        where = (str(row.get("where_coordinates") or "").strip()
                 or str(row.get("where_description") or "").strip()
                 or str(row.get("country") or "").strip())
        if side_b and type_id != "3":
            title = f"{side_a} vs {side_b}"
        elif side_b:
            title = f"{side_a} against {side_b}"
        else:
            title = side_a or "Armed violence"
        if where:
            title = f"{title} — {where}"
        if best >= 25:
            severity = "CRITICAL"
        elif best >= 5:
            severity = "SEVERE"
        elif best >= 1:
            severity = "ALERT"
        else:
            severity = "INFO"
        headline = re.sub(r"\s+", " ", str(row.get("source_headline") or "")).strip()
        out.append({
            "id": f"ucdp:{row.get('id')}",
            "title": title,
            "time": parsed.isoformat(),
            "lat": round(lat, 5), "lon": round(lon, 5),
            "place": where,
            "country": str(row.get("country") or "").strip(),
            "region": str(row.get("region") or "").strip(),
            "precision": precision_labels.get(str(row.get("where_prec") or ""), "reported location"),
            "severity": severity,
            "kind": "conflict",
            "verified": True,
            "source": "UCDP (verified candidate)",
            "event_type": type_label,
            "fatalities": best,
            "deaths_civilians": civilians,
            "deaths_a": _as_int(row.get("deaths_a")) or 0,
            "deaths_b": _as_int(row.get("deaths_b")) or 0,
            "source_headline": headline[:240],
            "url": "https://ucdp.uu.se/downloads/",
            "summary": (f"UCDP candidate event ({type_label}). Best estimate {best} death(s), "
                        f"including {civilians} civilian(s). Monthly release; verify against "
                        "the source report and current official information."),
        })
    out.sort(key=lambda item: item.get("time") or "", reverse=True)
    return out[:1800]


def _refresh_ucdp(db: Database, row) -> list[dict]:
    url = _ucdp_latest_url()
    resp = requests.get(url, headers=UA, timeout=UCDP_TIMEOUT)
    resp.raise_for_status()
    if "csv" not in (resp.headers.get("content-type") or "").lower() and \
            not resp.text[:80].lower().startswith(("id,", "\ufeffid,")):
        raise RuntimeError("UCDP returned a non-CSV response")
    items = _parse_ucdp(resp.text)
    if not items:
        raise RuntimeError("UCDP candidate CSV contained no geocoded events")
    return items


def _geom_representative_point(geom):
    """Best-effort [lon, lat] for non-Point geometries so map layers can place a marker."""
    if not isinstance(geom, dict):
        return None
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    try:
        if gtype == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            return [float(coords[0]), float(coords[1])]
        pts = []
        def walk(v, depth=0):
            if depth > 5 or len(pts) > 500:
                return
            if isinstance(v, (list, tuple)):
                if len(v) >= 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v[:2]):
                    pts.append((float(v[0]), float(v[1])))
                else:
                    for x in v:
                        walk(x, depth + 1)
        walk(coords)
        if not pts:
            return None
        # Use a middle sample to stay on-feature for lines; average as fallback.
        mid = pts[len(pts) // 2]
        return [mid[0], mid[1]]
    except Exception:
        return None

def _parse_geojson_items(payload, source: str, kind: str, default_severity: str = "INFO") -> list[dict]:
    """Scaffold parser for GeoJSON FeatureCollection or {items:[...]} feeds.
    Expected item fields (best-effort): title/name, lat/lon or geometry Point,
    time/timestamp, url, summary/description, severity, plus pass-through extras."""
    items = []
    raw_items = []
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        raw_items = payload.get("features") or []
        for feat in raw_items:
            if not isinstance(feat, dict):
                continue
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") if geom.get("type") == "Point" else None
            lat = lon = None
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                try:
                    lon, lat = float(coords[0]), float(coords[1])
                except (TypeError, ValueError):
                    lat = lon = None
            if lat is None:
                rp = _geom_representative_point(geom)
                if rp and len(rp) >= 2:
                    lon, lat = float(rp[0]), float(rp[1])
            if lat is None:
                for lk in ("lat", "latitude"):
                    for ok in ("lon", "lng", "longitude"):
                        if props.get(lk) is not None and props.get(ok) is not None:
                            try:
                                lat, lon = float(props[lk]), float(props[ok])
                            except (TypeError, ValueError):
                                pass
            title = props.get("title") or props.get("name") or props.get("label") or props.get("id") or f"{kind} feature"
            t = _iso_time(props.get("time") or props.get("timestamp") or props.get("updated") or props.get("date")) or ""
            it = {
                "title": str(title), "time": t, "source": source,
                "severity": str(props.get("severity") or default_severity).upper(),
                "url": props.get("url") or props.get("link") or "",
                "summary": _sanitize(props.get("summary") or props.get("description") or "", 500),
                "kind": kind,
            }
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                it["lat"], it["lon"] = lat, lon
            for k in ("mmsi", "imo", "callsign", "vessel", "speed_kn", "course_deg", "heading_deg",
                      "alt_m", "cable_id", "capacity", "operator", "camera_id", "status", "feed_url", "id"):
                if props.get(k) is not None:
                    it[k] = props.get(k)
            items.append(it)
        return items
    if isinstance(payload, dict):
        raw_items = payload.get("items") or payload.get("data") or payload.get("results") or []
    elif isinstance(payload, list):
        raw_items = payload
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        lat = lon = None
        for lk in ("lat", "latitude"):
            for ok in ("lon", "lng", "longitude"):
                if it.get(lk) is not None and it.get(ok) is not None:
                    try:
                        lat, lon = float(it[lk]), float(it[ok])
                        break
                    except (TypeError, ValueError):
                        pass
            if lat is not None:
                break
        geom = it.get("geometry") or {}
        coords = geom.get("coordinates") if isinstance(geom, dict) and geom.get("type") == "Point" else None
        if lat is None and isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                lon, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                pass
        if lat is None and isinstance(geom, dict):
            rp = _geom_representative_point(geom)
            if rp and len(rp) >= 2:
                lon, lat = float(rp[0]), float(rp[1])
        title = it.get("title") or it.get("name") or it.get("label") or it.get("mmsi") or it.get("id") or f"{kind} item"
        row = {
            "title": str(title),
            "time": _iso_time(it.get("time") or it.get("timestamp") or it.get("updated") or it.get("date")) or "",
            "source": source,
            "severity": str(it.get("severity") or default_severity).upper(),
            "url": it.get("url") or it.get("link") or "",
            "summary": _sanitize(it.get("summary") or it.get("description") or "", 500),
            "kind": kind,
        }
        if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            row["lat"], row["lon"] = lat, lon
        for k in ("mmsi", "imo", "callsign", "vessel", "speed_kn", "course_deg", "heading_deg",
                  "alt_m", "cable_id", "capacity", "operator", "camera_id", "status", "feed_url", "id"):
            if it.get(k) is not None:
                row[k] = it.get(k)
        items.append(row)
    return items


_RSOE_CARD_RE = re.compile(
    r'<div class="event-card\s+(\w+)"[^>]*>.*?'
    r'<h5 class="title">\s*(.*?)\s*</h5>.*?'
    r'<td class="eventDate">\s*(.*?)\s*</td>.*?'
    r'<td class="location">\s*(.*?)\s*</td>.*?'
    r'<td class="details">\s*<a href="([^"]+)"',
    re.S,
)
_RSOE_SEVERITY = {"red": "CRITICAL", "orange": "SEVERE", "yellow": "ALERT", "blue": "INFO"}
_RSOE_SEVERITY_RANK = {"CRITICAL": 0, "SEVERE": 1, "ALERT": 2, "INFO": 3}


def _parse_rsoe(text: str, source: str = "RSOE EDIS") -> list[dict]:
    """Parse the public RSOE EDIS event-list HTML.

    The list page embeds category headings followed by event cards containing
    title, UTC date, location, colour-coded severity and a details URL. The
    list itself has no coordinates, so _refresh_rsoe() enriches the highest
    priority cards from their detail pages.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for chunk in str(text or "").split('<h4 class="categoryNameMobil">')[1:]:
        raw_category, _, rest = chunk.partition("</h4>")
        category = _sanitize(raw_category, 80)
        for match in _RSOE_CARD_RE.finditer(rest):
            colour, title, date, location, url = match.groups()
            title = _sanitize(title, 300)
            url = url.strip()
            event_id = ""
            id_match = re.search(r"/details/(\d+)", url)
            if id_match:
                event_id = id_match.group(1)
            if not title or (event_id and event_id in seen):
                continue
            if event_id:
                seen.add(event_id)
            place = _sanitize(location, 160)
            summary = f"{category} · {place}" if category else place
            out.append({
                "id": event_id or title,
                "title": title,
                "time": _iso_time(date) or "",
                "source": source,
                "severity": _RSOE_SEVERITY.get(colour.lower(), "INFO"),
                "url": url,
                "summary": summary[:500],
                "kind": "rsoe",
                "category": category,
                "location": place,
            })

    def _ts(value):
        parsed = parse_time(value)
        return parsed.timestamp() if parsed else 0.0

    out.sort(key=lambda item: (_RSOE_SEVERITY_RANK.get(item.get("severity"), 9), -_ts(item.get("time"))))
    return out[:400]


def _get_text_with_fallbacks(url: str, header_sets: list[dict], timeout: int = TIMEOUT) -> str:
    last_exc: Exception | None = None
    for headers in header_sets:
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.HTTPError as exc:
            last_exc = exc
            status = getattr(exc.response, "status_code", None)
            if status in (403, 429) and headers is not header_sets[-1]:
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if headers is not header_sets[-1]:
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError("request failed")


def _rsoe_enrich(items: list[dict], limit: int = 40) -> None:
    """Fetch detail pages for the most significant events to add coordinates.

    One request per event is expensive, so only the top few by severity/time
    are enriched. Unenriched events still appear in the cached intel list.
    """
    targets = items[:max(0, limit)]

    def fetch(item: dict) -> None:
        try:
            resp = requests.get(item["url"], headers=UA, timeout=10)
            resp.raise_for_status()
            text = resp.text
            point = re.search(
                r'id="location"[^>]*value="POINT\(\s*([-0-9.]+)\s+([-0-9.]+)\s*\)"',
                text,
            )
            if point:
                lon, lat = float(point.group(1)), float(point.group(2))
                if -90 <= lat <= 90 and -180 <= lon <= 180:
                    item["lat"], item["lon"] = lat, lon
            desc = re.search(r'<div class="event-description">\s*<p>(.*?)</p>', text, re.S)
            if desc:
                detail = _sanitize(desc.group(1), 400)
                if detail and detail != "-":
                    base = item.get("summary") or ""
                    item["summary"] = (detail + (f" · {base}" if base else ""))[:500]
        except Exception:
            return

    if targets:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(fetch, targets))


def _refresh_rsoe(db: Database, row) -> list[dict]:
    """Fetch the RSOE EDIS list page and enrich a subset with coordinates."""
    url = (row["url"] or "").strip() or "https://rsoe-edis.org/eventList"
    resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    items = _parse_rsoe(resp.text, row["name"])
    if not items:
        raise RuntimeError("RSOE EDIS returned no parsable events")
    _rsoe_enrich(items, limit=40)
    return items


def _normalize(kind: str, raw: str, source: str = "RSS") -> list[dict]:
    if kind == "rss":
        return _parse_rss(raw, source)
    if kind == "rsoe":
        return _parse_rsoe(raw, source)
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        if raw.lstrip().lower().startswith(("<!doctype", "<html")):
            raise RuntimeError("provider returned an HTML page instead of a feed")
        raise RuntimeError(f"provider returned non-JSON data ({raw[:80].strip()!r})")
    if kind == "usgs":
        return _parse_usgs(payload)
    if kind == "swpc_kp":
        return _parse_swpc_kp(payload)
    if kind == "swpc_scales":
        return _parse_swpc_scales(payload)
    if kind == "nws":
        return _parse_nws(payload)
    if kind == "opensky":
        return _parse_opensky(payload)
    if kind == "celestrak":
        return _parse_celestrak(raw)
    if kind == "air_quality":
        return _parse_air_quality(payload)
    if kind == "weather":
        return _parse_weather(payload)
    if kind == "gdacs":
        return _parse_gdacs(payload)
    if kind == "eonet":
        return _parse_eonet(payload)
    if kind == "safecast":
        return _parse_safecast(payload)
    if kind in ("maritime", "ais", "cables", "cctv", "geojson"):
        return _parse_geojson_items(payload, source, kind)
    # Unknown structured kind: best-effort scaffold parse.
    if isinstance(payload, (dict, list)):
        try:
            return _parse_geojson_items(payload, source, kind)
        except Exception:
            return []
    return []


def _feed_url(db: Database, row) -> str:
    """Resolve position-dependent and date-dependent feed URLs."""
    fid = row["id"]
    url = row["url"]
    s = db.all_settings()
    lat_raw = s.get("lat")
    lon_raw = s.get("lon")
    try:
        lat = float(lat_raw) if lat_raw is not None and str(lat_raw).strip() != "" else None
        lon = float(lon_raw) if lon_raw is not None and str(lon_raw).strip() != "" else None
    except (TypeError, ValueError):
        lat = lon = None
    needs_position = fid in ("open_meteo_air", "open_meteo_weather", "safecast")
    if needs_position and (lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)):
        raise ValueError("operator position is missing or invalid; set a valid latitude/longitude before refreshing this feed")
    if fid == "open_meteo_air":
        return ("https://air-quality-api.open-meteo.com/v1/air-quality"
                f"?latitude={lat:.4f}&longitude={lon:.4f}"
                "&current=us_aqi,pm2_5,pm10&hourly=us_aqi&timezone=UTC")
    if fid == "open_meteo_weather":
        return ("https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat:.4f}&longitude={lon:.4f}"
                "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
                "precipitation,weather_code,wind_speed_10m,wind_gusts_10m,pressure_msl"
                "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code"
                "&timezone=UTC&forecast_days=3")
    if fid == "safecast":
        return ("https://api.safecast.org/measurements.json"
                "?per_page=50&order=created_at+desc"
                f"&latitude={lat:.4f}&longitude={lon:.4f}&distance=200000")
    if fid == "gdacs":
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=30)
        return ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
                f"?fromDate={start}&toDate={today}&eventlist=EQ,TC,FL,VO,DR")
    return url


def _refresh_overpass_cctv(db: Database, row) -> list[dict]:
    """Operator-triggered OSM surveillance camera locations via Overpass (ODbL). Locations only, no previews/streams."""
    s = db.all_settings()
    try:
        lat = float(s.get("lat")); lon = float(s.get("lon"))
    except Exception:
        lat = lon = None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("operator position is missing or invalid; set a valid latitude/longitude before refreshing CCTV locations")
    radius_m = 25000  # 25km default; keep Overpass load reasonable
    query = f"""[out:json][timeout:25];
(
  node["man_made"="surveillance"](around:{radius_m},{lat:.5f},{lon:.5f});
  way["man_made"="surveillance"](around:{radius_m},{lat:.5f},{lon:.5f});
  relation["man_made"="surveillance"](around:{radius_m},{lat:.5f},{lon:.5f});
);
out center tags;"""
    overpass_urls = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ]
    last_exc = None
    resp = None
    for ou in overpass_urls:
        try:
            resp = requests.post(ou, data={"data": query}, headers=UA, timeout=40)
            resp.raise_for_status()
            break
        except Exception as exc:
            last_exc = exc
            resp = None
            continue
    if resp is None:
        raise last_exc or RuntimeError("Overpass unavailable")
    data = resp.json()
    items = []
    for el in (data.get("elements") or []):
        tags = el.get("tags") or {}
        ela = el.get("lat") if el.get("lat") is not None else (el.get("center") or {}).get("lat")
        elo = el.get("lon") if el.get("lon") is not None else (el.get("center") or {}).get("lon")
        try:
            ela = float(ela); elo = float(elo)
        except Exception:
            continue
        if not (-90 <= ela <= 90 and -180 <= elo <= 180):
            continue
        surveillance_type = str(tags.get("surveillance:type") or tags.get("camera:type") or tags.get("surveillance") or "surveillance")
        camera_type = str(tags.get("camera:type") or "")
        brand = str(tags.get("brand") or tags.get("manufacturer") or "")
        operator_name = str(tags.get("operator") or tags.get("owner") or "")
        direction = str(tags.get("camera:direction") or tags.get("direction") or "")
        surveillance_zone = str(tags.get("surveillance:zone") or tags.get("surveillance:kind") or "")
        tag_blob = " ".join(f"{k}={v}" for k, v in tags.items()).lower()
        st_low = surveillance_type.lower()
        is_alpr = (
            st_low in {"alpr", "anpr", "lpr", "license_plate", "number_plate", "plate_recognition"}
            or "alpr" in st_low or "anpr" in st_low
            or "license_plate" in tag_blob or "number_plate" in tag_blob
            or any(x in tag_blob for x in ("flock safety", "flock=", "brand=flock", "manufacturer=flock"))
        )
        is_flock_tagged = any(x in tag_blob for x in ("flock safety", "flock=", "brand=flock", "manufacturer=flock", "operator=flock"))
        kind_label = "ALPR/surveillance camera" if is_alpr else "CCTV/surveillance camera"
        title = tags.get("name") or tags.get("ref") or kind_label
        if is_alpr and "alpr" not in title.lower() and "anpr" not in title.lower():
            title = f"{title} [ALPR]"
        summary_bits = [f"surveillance:type={surveillance_type}"] if surveillance_type else []
        if camera_type:
            summary_bits.append(f"camera:type={camera_type}")
        if brand:
            summary_bits.append(f"brand/manufacturer={brand}")
        if operator_name:
            summary_bits.append(f"operator={operator_name}")
        if direction:
            summary_bits.append(f"direction={direction}")
        if surveillance_zone:
            summary_bits.append(f"zone={surveillance_zone}")
        if is_flock_tagged:
            summary_bits.append("OSM tags mention Flock (community-mapped; verify on OSM)")
        if not summary_bits:
            summary_bits = [f"{k}={v}" for k, v in list(tags.items())[:8]]
        items.append({
            "title": title,
            "time": _iso_time(tags.get("survey:date") or tags.get("check_date") or tags.get("source:date")) or utcnow(),
            "source": "OpenStreetMap (Overpass, ODbL)",
            "severity": "INFO",
            "url": f"https://www.openstreetmap.org/{el.get('type','node')}/{el.get('id')}",
            "summary": _sanitize("; ".join(summary_bits), 700),
            "kind": "cctv",
            "lat": ela, "lon": elo,
            "camera_id": str(el.get("id")),
            "status": tags.get("surveillance") or tags.get("camera:mount") or tags.get("man_made") or "unknown",
            "alpr": bool(is_alpr),
            "flock_tagged": bool(is_flock_tagged),
            "surveillance_type": _sanitize(surveillance_type, 80),
            "camera_type": _sanitize(camera_type, 80),
            "brand": _sanitize(brand, 120),
            "operator_name": _sanitize(operator_name, 120),
            "direction": _sanitize(direction, 40),
            "surveillance_zone": _sanitize(surveillance_zone, 80),
        })
    return items[:2000]


def _refresh_local_geojson_scaffold(db: Database, row, kind: str, names: tuple[str, ...]) -> list[dict]:
    """Load a user-provided local GeoJSON file for scaffold layers (cables, etc.)."""
    from . import config as _config
    candidates = []
    for base in (_config.DATA_DIR / "maps", _config.DATA_DIR / "overlays", _config.MAP_TILE_DIR.parent / "data", _config.RESOURCE_ROOT / "maps" / "data"):
        for name in names:
            candidates.append(base / name)
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size <= 15_000_000:
                raw = p.read_text(encoding="utf-8", errors="ignore")
                payload = json.loads(raw)
                items = _parse_geojson_items(payload, row["name"], kind)
                if items:
                    return items
        except Exception:
            continue
    raise RuntimeError("not configured: set feed URL to a GeoJSON/JSON endpoint, or place a local file at fielddata/maps/submarine-cables.geojson (or maps/data/*).")




CABLE_GEOJSON_URLS = [
    "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json",
    "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json",
    "https://raw.githubusercontent.com/telegeography/www.submarinecablemap.com/master/public/api/v3/landing-point/landing-point-geo.json",
    "https://raw.githubusercontent.com/telegeography/www.submarinecablemap.com/master/web/public/api/v3/landing-point/landing-point-geo.json",
]
CABLE_GEOJSON_LICENSE = "TeleGeography Submarine Cable Map data (CC BY-NC-SA 3.0). Attribution required; review terms before commercial/redistributed use."

def _ensure_submarine_cables_geojson() -> "Path | None":
    """Download and cache public submarine cable/landing-point GeoJSON when missing.
    Prefers landing points (Point features) for the current map layer; cached locally
    for offline use after the first successful fetch."""
    from . import config as _config
    target = _config.DATA_DIR / "maps" / "submarine-cables.geojson"
    try:
        if target.is_file() and target.stat().st_size > 1024:
            return target
    except Exception:
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    for url in CABLE_GEOJSON_URLS:
        try:
            resp = requests.get(url, headers=UA, timeout=45)
            resp.raise_for_status()
            raw = resp.text
            payload = json.loads(raw)
            if isinstance(payload, dict) and payload.get("type") == "FeatureCollection" and isinstance(payload.get("features"), list):
                target.write_text(raw, encoding="utf-8")
                return target
            if isinstance(payload, dict) and isinstance(payload.get("features"), list):
                payload.setdefault("type", "FeatureCollection")
                target.write_text(json.dumps(payload), encoding="utf-8")
                return target
        except Exception:
            continue
    return target if target.is_file() else None


def _refresh_maritime_scaffold(db: Database, row) -> list[dict]:
    """Load operator-provided maritime/AIS data from local GeoJSON/JSON files.
    Global real-time AIS usually requires a keyed/provider source; this makes the
    layer work with cached/exported data or a configured GeoJSON/JSON URL."""
    from . import config as _config
    names = ("ais.geojson", "ais.json", "maritime.geojson", "maritime.json", "vessels.geojson", "vessels.json")
    candidates = []
    for base in (_config.DATA_DIR / "maps", _config.DATA_DIR / "overlays", _config.MAP_TILE_DIR.parent / "data", _config.RESOURCE_ROOT / "maps" / "data"):
        for name in names:
            candidates.append(base / name)
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size <= 15_000_000:
                raw = p.read_text(encoding="utf-8", errors="ignore")
                payload = json.loads(raw)
                items = _parse_geojson_items(payload, row["name"], "maritime")
                if items:
                    return items
        except Exception:
            continue
    raise RuntimeError(
        "Maritime/AIS is not configured. Provide an operator/provider GeoJSON or JSON feed URL "
        "(Settings -> Intelligence feeds -> maritime_ais) or place a local file at "
        "fielddata/maps/ais.geojson (or ais.json, maritime.geojson, vessels.geojson, maps/data/*). "
        "Real-time global AIS typically requires an AIS provider/API key; exported snapshots work offline."
    )


def refresh(db: Database, feed_id: str | None = None) -> list[dict]:
    rows = db.query(
        "SELECT * FROM feeds" + (" WHERE id=?" if feed_id else ""),
        (feed_id,) if feed_id else (),
    )
    results = []
    for row in rows:
        try:
            # The UCDP candidate file changes monthly; do not re-download 1.5 MB
            # every time the operator presses refresh.
            if row["kind"] == "ucdp" and row["fetched_at"] and row["payload"]:
                try:
                    fetched = datetime.fromisoformat(row["fetched_at"])
                    if fetched.tzinfo is None:
                        fetched = fetched.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - fetched).total_seconds() < 20 * 3600:
                        cached = json.loads(row["payload"]).get("items") or []
                        results.append({"id": row["id"], "status": "OK", "count": len(cached),
                                        "cached": True})
                        continue
                except Exception:
                    pass
            url = (row["url"] or "").strip()
            fid = row["id"]
            # Built-in public-source fallbacks for scaffold layers (operator-triggered, cached).
            if not url and fid == "public_cctv":
                items = _refresh_overpass_cctv(db, row)
                db.execute(
                    "UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                    (utcnow(), json.dumps({"items": items, "fetched_at": utcnow(), "source_note": "OpenStreetMap via Overpass API (locations only; no live previews)"}), row["id"]),
                )
                results.append({"id": row["id"], "status": "OK", "count": len(items)})
                continue
            if not url and fid == "infra_cables":
                try:
                    items = _refresh_local_geojson_scaffold(db, row, kind="cables", names=("submarine-cables.geojson", "cables.geojson", "subsea-cables.geojson"))
                    note = "Local GeoJSON file"
                except Exception:
                    cached = _ensure_submarine_cables_geojson()
                    if not cached or not cached.is_file():
                        raise RuntimeError("submarine cables not available: could not find a local GeoJSON file and could not download the public dataset (check connectivity). You can also place fielddata/maps/submarine-cables.geojson")
                    items = _parse_geojson_items(json.loads(cached.read_text(encoding="utf-8", errors="ignore")), row["name"], "cables")
                    note = CABLE_GEOJSON_LICENSE
                db.execute(
                    "UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                    (utcnow(), json.dumps({"items": items, "fetched_at": utcnow(), "source_note": note}), row["id"]),
                )
                results.append({"id": row["id"], "status": "OK", "count": len(items)})
                continue
            if not url and fid == "maritime_ais":
                items = _refresh_maritime_scaffold(db, row)
                db.execute(
                    "UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                    (utcnow(), json.dumps({"items": items, "fetched_at": utcnow(), "source_note": "Local GeoJSON/JSON file"}), row["id"]),
                )
                results.append({"id": row["id"], "status": "OK", "count": len(items)})
                continue
            if not url and row["kind"] not in ("gdelt", "ucdp", "rsoe"):
                raise RuntimeError("feed not configured (set a source URL in feed settings/integration)")
            if row["kind"] == "gdelt":
                items = _refresh_gdelt(db, row)
            elif row["kind"] == "ucdp":
                items = _refresh_ucdp(db, row)
            elif row["kind"] == "rsoe":
                items = _refresh_rsoe(db, row)
            else:
                url = _feed_url(db, row)
                if not str(url or "").strip():
                    # Allow local scaffold files even when resolved URL is empty
                    if fid == "infra_cables":
                        try:
                            items = _refresh_local_geojson_scaffold(db, row, kind="cables", names=("submarine-cables.geojson", "cables.geojson", "subsea-cables.geojson"))
                            note = "Local GeoJSON file"
                        except Exception:
                            cached = _ensure_submarine_cables_geojson()
                            if not cached or not cached.is_file():
                                raise RuntimeError("submarine cables not available")
                            items = _parse_geojson_items(json.loads(cached.read_text(encoding="utf-8", errors="ignore")), row["name"], "cables")
                            note = CABLE_GEOJSON_LICENSE
                        db.execute("UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                                   (utcnow(), json.dumps({"items": items, "fetched_at": utcnow(), "source_note": note}), row["id"]))
                        results.append({"id": row["id"], "status": "OK", "count": len(items)})
                        continue
                    if fid == "maritime_ais":
                        items = _refresh_maritime_scaffold(db, row)
                        db.execute("UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                                   (utcnow(), json.dumps({"items": items, "fetched_at": utcnow(), "source_note": "Local GeoJSON/JSON file"}), row["id"]))
                        results.append({"id": row["id"], "status": "OK", "count": len(items)})
                        continue
                    raise RuntimeError("feed not configured (set a source URL in feed settings/integration)")
                if row["kind"] == "celestrak":
                    try:
                        raw = _get_text_with_fallbacks(url, CELESTRAK_HEADERS + [UA], timeout=max(TIMEOUT, 45))
                        items = _parse_celestrak(raw)
                    except requests.HTTPError as exc:
                        # CelesTrak returns 403 with this message when the TLE set
                        # has not changed since the last successful download. That
                        # is not a failure: the cached set is still current.
                        if (getattr(exc.response, "status_code", None) == 403
                                and "has not updated" in getattr(exc.response, "text", "").lower()):
                            cached = db.one("SELECT payload FROM feeds WHERE id=?", (row["id"],))
                            payload = {}
                            if cached and cached["payload"]:
                                try:
                                    payload = json.loads(cached["payload"])
                                except Exception:
                                    payload = {}
                            items = payload.get("items") or []
                            db.execute(
                                "UPDATE feeds SET fetched_at=?, status='OK', error=NULL WHERE id=?",
                                (utcnow(), row["id"]),
                            )
                            results.append({"id": row["id"], "status": "OK", "count": len(items),
                                            "note": "provider reports no update since last download"})
                            continue
                        raise
                else:
                    resp = requests.get(url, headers=UA, timeout=TIMEOUT)
                    resp.raise_for_status()
                    raw = resp.text
                    items = _normalize(row["kind"], raw, row["name"])
                if not items and raw.lstrip().lower().startswith(("<!doctype", "<html")):
                    raise RuntimeError("provider returned an HTML page instead of a feed")
                if not items and row["kind"] in ("weather", "air_quality"):
                    raise RuntimeError("provider response contained no usable current conditions")
            db.execute(
                "UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                (utcnow(), json.dumps({"items": items, "fetched_at": utcnow()}), row["id"]),
            )
            results.append({"id": row["id"], "status": "OK", "count": len(items)})
        except Exception as exc:
            db.execute(
                "UPDATE feeds SET status='ERROR', error=? WHERE id=?",
                (str(exc)[:300], row["id"]),
            )
            results.append({"id": row["id"], "status": "ERROR", "error": str(exc)[:300]})
    return results


def list_feeds(db: Database, item_limit: int | None = 30) -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for row in db.query("SELECT id,name,url,kind,fetched_at,status,error,"
                        "length(payload) AS payload_len FROM feeds ORDER BY name"):
        payload, items, latest_time = _decode_payload(db, row)
        age = None
        if row["fetched_at"]:
            try:
                age = int((now - datetime.fromisoformat(row["fetched_at"])).total_seconds())
            except Exception:
                age = None
        # The newest item's age is distinct from the last successful fetch.
        # Safecast still answers HTTP 200 but its newest public measurement is
        # years old; WHO's RSS also carries a 2020 archive.
        data_age = None
        if latest_time is not None:
            data_age = max(0, int((now - latest_time).total_seconds()))
        status = row["status"] or "NEVER"
        stale_reason = None
        if status == "OK" and age is not None and age > STALE_AFTER.get(row["id"], DEFAULT_STALE_AFTER):
            status = "STALE"
            stale_reason = "fetch"
        if status == "OK" and data_age is not None and data_age > DATA_STALE_AFTER.get(row["id"], DEFAULT_STALE_AFTER):
            status = "STALE"
            stale_reason = "data"
        out.append({
            "id": row["id"], "name": row["name"], "url": row["url"], "kind": row["kind"],
            "fetched_at": row["fetched_at"], "age_seconds": age, "status": status,
            "data_age_seconds": data_age,
            "latest_item_time": latest_time.isoformat() if latest_time else None,
            "stale_reason": stale_reason,
            "error": row["error"], "count": len(items),
            "items": items if item_limit is None else items[:item_limit],
        })
    return out


def summary(db: Database, feeds: list[dict] | None = None) -> dict:
    # The caller usually supplies a display-limited feed list. Critical counts
    # and the latest-headline strip should still inspect every cached item, so
    # read the full set separately (payload decoding is cached).
    display = feeds if feeds is not None else list_feeds(db, item_limit=0)
    full = list_feeds(db, item_limit=None)
    # Count critical items per feed with a cap. UCDP alone can carry hundreds
    # of monthly conflict events; showing that raw total on the dashboard is
    # noisy and not actionable. The cap keeps the indicator meaningful while
    # readiness scoring remains saturated for a genuinely critical picture.
    critical_count = 0
    for f in full:
        n = sum(1 for i in (f.get("items") or [])
                if str(i.get("severity") or "INFO").upper() in ("CRITICAL", "SEVERE", "EXTREME"))
        critical_count += min(10, n)
    headline_items = [i for f in full if f.get("kind") not in SUMMARY_SKIP_KINDS
                      for i in (f.get("items") or [])]
    latest = heapq.nlargest(
        12, headline_items,
        key=lambda x: parse_time(x.get("time")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    return {
        "feeds": display,
        "critical_count": critical_count,
        "latest": latest,
    }
