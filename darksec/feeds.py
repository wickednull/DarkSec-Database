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
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from .db import Database, utcnow

UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36 DarkSecNightwatch/2.0",
    "Accept": "text/plain,text/xml,application/xml,application/json,*/*;q=0.8",
}
TIMEOUT = 15
UCDP_TIMEOUT = 90

SAFECAST_URL = "https://api.safecast.org/measurements.json"
WHO_NEWS_RSS = "https://www.who.int/rss-feeds/news-english.xml"
# WHO's public RSS feed has been returning a 2020/2018 archive with only one
# recent item mixed in. The newsroom Sitefinity API behind the site itself is
# current, ordered and much smaller, so use it as the primary WHO source.
WHO_NEWS_API = ("https://www.who.int/api/news/newsitems"
                "?sf_provider=OpenAccessDataProvider&sf_culture=en"
                "&$top=60&$orderby=PublicationDateAndTime%20desc"
                "&$select=Title,ItemDefaultUrl,FormatedDate,"
                "PublicationDateAndTime,NewsType")
# The Smithsonian weekly report switched to a Daily Volcanic Activity Report
# and stopped updating its RSS in October 2026. USGS HANS publishes current
# CAP volcano alerts as RSS, so merge those in to keep the layer current.
USGS_VOLCANO_CAP = "https://volcanoes.usgs.gov/hans-public/rss/cap/"
SMITHSONIAN_WEEKLY_RSS = "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml"

# Overpass mirrors differ in what they allow: overpass.openstreetmap.fr
# rejects browser-like agents as un-whitelisted, while overpass-api.de is
# tolerant. Keep a plain application UA and fall back to the normal one.
OVERPASS_UA = {
    "User-Agent": "DarkSecNightwatch/2.0 (+https://github.com/darksec; OSM CCTV layer)",
    "Accept": "application/json",
}

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
    "smithsonian_volcano": 48 * 3600,
    "who_don": 14 * 86400,
    "safecast": 48 * 3600,
    "eonet": 48 * 3600,
    "thehackernews": 48 * 3600,
    # USGS is polled every 10 minutes; its payload is a rolling 24h window.
    "usgs_quakes": 2 * 3600,
    # CCTV layers are cached map geometry refreshed manually or on a long
    # cadence. Camera locations do not go "stale" like a news headline.
    "public_cctv": 7 * 86400,
    "cctv_catalog": 7 * 86400,
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
    "crisisgroup": 21 * 86400,
    "rsoe_edis": 4 * 3600,
    "ucdp_candidate": 50 * 86400,
    "celestrak_tle": 24 * 3600,
    "war_on_the_rocks": 14 * 86400,
    # Weekly report cadence: items can legitimately be up to a week old.
    "smithsonian_volcano": 10 * 86400,
    "who_don": 14 * 86400,
    "safecast": 48 * 3600,
    "eonet": 48 * 3600,
    "thehackernews": 48 * 3600,
    # 2.5_day is a rolling 24h feed; a quiet stretch is normal and must not
    # be reported as a broken/stale source.
    "usgs_quakes": 24 * 3600,
    "public_cctv": 30 * 86400,
    "cctv_catalog": 90 * 86400,
}
DEFAULT_STALE_AFTER = 6 * 3600

# Kinds whose item timestamps are layer metadata rather than "latest news".
# Their cached geometry is useful even when the source hasn't updated, so
# data-age staleness does not apply (fetch-age limits still do).
DATA_STALE_EXEMPT_KINDS = {"cctv", "cables", "maritime", "ais"}


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
    """Parse CelesTrak TLE text (2-line or name + 2-line sets)."""
    items = []
    lines = [l.strip() for l in (raw or "").splitlines() if l.strip()]
    idx = 0
    while idx < len(lines):
        name = None
        if lines[idx].startswith("1 ") and idx + 1 < len(lines) and lines[idx + 1].startswith("2 "):
            # The active catalogue is served without name lines.
            l1, l2 = lines[idx], lines[idx + 1]
            idx += 2
        elif (idx + 2 < len(lines) and lines[idx + 1].startswith("1 ")
              and lines[idx + 2].startswith("2 ")):
            # Group endpoints (stations, weather, amateur) include a name line.
            name = lines[idx]
            l1, l2 = lines[idx + 1], lines[idx + 2]
            idx += 3
        else:
            idx += 1
            continue
        norad = None
        m = re.match(r"1\s+(\d{5})U", l1)
        if m:
            norad = int(m.group(1))
        label = (name or "").strip()
        if not label:
            # Older payloads also stored the inclination here; prefer NORAD.
            label = f"NORAD {norad}" if norad else "UNKNOWN"
        items.append({
            "title": label,
            "norad": norad,
            "name": label,
            "line1": l1,
            "line2": l2,
            "source": "CelesTrak",
            "severity": "INFO",
            "time": utcnow(),
            "url": f"https://celestrak.org/norad/tle/{norad}" if norad else None,
        })
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
    trend = []
    hourly_time = hourly.get("time") or []
    hourly_aqi = hourly.get("us_aqi") or []
    current_stamp = str(cur.get("time")
                        or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00"))[:13]
    for idx, stamp in enumerate(hourly_time[:120]):
        if str(stamp)[:13] < current_stamp:
            continue
        value = hourly_aqi[idx] if idx < len(hourly_aqi) else None
        if value is None:
            continue
        if len(trend) >= 72:
            break
        try:
            trend.append({"time": stamp, "aqi": int(float(value))})
        except (TypeError, ValueError):
            continue
    items.append({
        "title": f"Air quality US AQI {int(aqi)}",
        "time": cur.get("time") or datetime.now(timezone.utc).isoformat(),
        "lat": payload.get("latitude"), "lon": payload.get("longitude"),
        "aqi": int(level),
        "pm2_5": cur.get("pm2_5"),
        "category": _aqi_category(level),
        "trend": trend,
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


def _daily_forecast(payload: dict) -> list[dict]:
    """Preserve the Open-Meteo 3-day daily arrays the feed already requests."""
    daily = (payload or {}).get("daily") or {}
    dates = daily.get("time") or []
    out = []
    for idx, date in enumerate(dates):
        def at(name):
            values = daily.get(name) or []
            return values[idx] if idx < len(values) else None
        code = at("weather_code")
        out.append({
            "date": date,
            "temp_max_c": at("temperature_2m_max"),
            "temp_min_c": at("temperature_2m_min"),
            "precip_mm": at("precipitation_sum"),
            "weather_code": code,
            "condition": _WMO.get(int(code), "") if code is not None else "",
        })
    return out


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
        "forecast": _daily_forecast(payload),
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
    rows = [m for m in (payload or []) if isinstance(m, dict)]
    # The API returns rows oldest-first when `since` is used and ignores
    # `order=created_at desc`. Sort locally so the newest readings are kept.
    rows.sort(key=lambda m: _parse_ts(m.get("captured_at")), reverse=True)
    items = []
    for m in rows[:60]:
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


def _parse_who_news(payload: dict) -> list[dict]:
    """Map the WHO Sitefinity news API onto the common feed item shape."""
    items = []
    for row in (payload.get("value") or []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        title = _sanitize(row.get("Title"), 300)
        if not title:
            continue
        path = str(row.get("ItemDefaultUrl") or "").strip()
        url = path if path.startswith(("http://", "https://")) else (
            f"https://www.who.int{path}" if path.startswith("/") else "https://www.who.int/news-room")
        published = row.get("PublicationDateAndTime") or row.get("FormatedDate")
        news_type = _sanitize(row.get("NewsType"), 80)
        items.append({
            "title": title,
            "time": _iso_time(published),
            "url": url,
            "summary": news_type,
            "lat": None, "lon": None,
            "categories": [news_type] if news_type else [],
            "severity": "INFO",
            "source": "WHO",
        })
    return items


def _parse_ts(value) -> float:
    parsed = parse_time(value)
    return parsed.timestamp() if parsed else 0.0


def _merge_items(*groups, limit: int = 120) -> list[dict]:
    """Deduplicate and newest-sort items from several sources."""
    seen = set()
    merged = []
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("url") or "").strip(),
                   str(item.get("title") or "").strip())
            if key == ("", ""):
                key = ("", "", str(item.get("time") or "").strip())
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    merged.sort(key=lambda item: _parse_ts(item.get("time")), reverse=True)
    return merged[:limit]


GDELT_LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_LABEL_VERSION = 3
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

# GDELT's CAMEO coding is automated and frequently wrong. Before presenting a
# media-derived conflict item, require evidence in the article's own URL slug
# (a proxy for its headline). This is what stops an article about wildfire
# funding from being displayed as an "Air / missile strike".
_GDELT_CONFLICT_TERMS = re.compile(
    r"\b(?:missiles?|rockets?|air[- ]?strikes?|airstrikes?|drones?|shelling|"
    r"artillery|attacks?|clashes?|bomb(?:ings?|s)?|explosions?|killed|"
    r"killings?|strikes?|combat|gunfire|ambush(?:es)?|ceasefire|offensive|"
    r"invasions?|casualties|wounded|raids?|kidnappings?|hostages?|"
    r"assassinations?|intercepted?|firing)\b",
    re.I,
)
_GDELT_STRONG_CONFLICT = re.compile(
    r"\b(?:attacks?|clashes?|airstrikes?|air[- ]?strikes?|bomb(?:ings?|s)?|"
    r"shelling|gunfire|ambush(?:es)?|assassinations?|"
    r"kidnappings?|hostages?|raids?|missiles?|rockets?)\b",
    re.I,
)
_GDELT_HAZARD_TERMS = re.compile(
    r"\b(?:wildfires?|fires?|floods?|storms?|hurricanes?|typhoons?|tornadoes?|"
    r"earthquakes?|volcanoes?|volcanic|tsunamis?|droughts?|heatwaves?|"
    r"heat waves?|landslides?|avalanches?|smoke|evacuations?|evacuated)\b",
    re.I,
)
_GDELT_NON_CONFLICT_CONTEXT = re.compile(
    r"\b(?:memorial|veterans?|refugees?|museum|anniversar(?:y|ies)|history|"
    r"historical|film|book|exhibition|festival|election|legislature|"
    r"parliament|court|trial|prison|sentence|sentenced|lawsuit|policy|"
    r"budget|funding|labor|workers?|union|pay|sports?|weather)\b",
    re.I,
)
_GDELT_ACCIDENT_CONTEXT = re.compile(
    r"\b(?:lumber|mill|factory|plant|refinery|pipeline|mine|gas leak|"
    r"chemical leak|ems|firefighter|fire station|house fire|wildfire|"
    r"firecracker|fireworks|collapse|tragedy)\b",
    re.I,
)
_GDELT_HARD_NON_CONFLICT = re.compile(
    r"\b(?:memorial|veterans?|museum|anniversar(?:y|ies)|history|historical|"
    r"film|book|exhibition|festival|photo|remains|indicted|charged|charges|"
    r"plotting|plot|conspiracy|sentenced|accused|remember(?:s|ing|ed)?|"
    r"commemorat(?:e|es|ing|ion)|no new|rejects?|denies|denied|rules? out|"
    r"unlikely|peace (?:terms?|talks?)|negotiat(?:e|es|ion|ions)|talks?|"
    r"inside|what it reveals|explainer|analysis|opinion|preview|outlook|"
    r"explained|protect(?:s|ion)?|defensive|push(?:es|ed|ing)? for|"
    r"condemns?|condemn|calls? for|urges?|unveils?|reveals?|"
    r"preparing for|what to know)\b",
    re.I,
)


def _slug_headline(url: str | None) -> str:
    """Recover a readable headline from a news URL slug.

    GDELT does not carry the article title in its event export. The URL slug
    still gives us the source's own wording often enough to catch a mismatch
    between the automated code and the article.
    """
    if not url:
        return ""
    try:
        path = unquote(urlsplit(url).path)
    except Exception:
        return ""
    parts = [part for part in path.split("/") if part]
    for part in reversed(parts[-3:]):
        slug = re.sub(r"\.(?:html?|php|aspx?|amp)$", "", part, flags=re.I)
        words = re.findall(r"[A-Za-z]{2,}", slug.replace("-", " ").replace("_", " "))
        if len(words) >= 3:
            text = " ".join(words)[:180]
            return text[0].upper() + text[1:]
    return ""


def _gdelt_article_evidence(url: str | None, actors: str, place: str) -> tuple[str, bool, bool]:
    """Return (headline, conflict_evidence, hazard_evidence)."""
    headline = _slug_headline(url)
    text = f"{headline} {actors} {place}"
    conflict = bool(_GDELT_CONFLICT_TERMS.search(text))
    strong = bool(_GDELT_STRONG_CONFLICT.search(text))
    if _GDELT_HARD_NON_CONFLICT.search(text):
        conflict = False
    elif conflict and not strong and (
        _GDELT_NON_CONFLICT_CONTEXT.search(text) or _GDELT_ACCIDENT_CONTEXT.search(text)
    ):
        conflict = False
    if conflict and _GDELT_ACCIDENT_CONTEXT.search(text) and not strong:
        conflict = False
    hazard = bool(_GDELT_HAZARD_TERMS.search(text)) and not conflict
    return headline, conflict, hazard


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
        url = str(row[60] or "").strip() or None
        label = GDELT_EVENT_LABELS.get(code) or {
            "18": "Assault", "19": "Armed clash", "20": "Mass violence",
        }.get(root, "Conflict event")
        headline, conflict_evidence, hazard_evidence = _gdelt_article_evidence(
            url, f"{actor1} {actor2}", place,
        )
        # Never turn an automated code into a headline. If the article slug
        # points at a hazard, drop it. If a headline exists but has no conflict
        # wording, the article is about something else and does not belong in
        # the conflict layer. Without a headline, require an armed actor.
        if hazard_evidence:
            continue
        if headline:
            if not conflict_evidence:
                continue
            title = headline
            severity = "ALERT"
            basis = "article headline"
            confidence = "low"
        elif armed:
            title = f"Unverified media report — {place}" if place else "Unverified media report"
            severity = "INFO"
            basis = "armed actor + automated code"
            confidence = "very low"
        else:
            continue
        actors = f"{actor1} vs {actor2}" if actor1 and actor2 and actor1.upper() != actor2.upper() else (actor1 or actor2)
        summary = (
            f"GDELT automated coding: {label}. "
            + (f"Article headline: {headline}. " if headline else "No article headline available. ")
            + (f"Actors: {actors}. " if actors else "")
            + f"{mentions} mention(s). Media-derived and unverified; event coding and geocoding can be wrong. "
              "Verify the linked report before acting."
        )
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
            "event_type": label,
            "article_headline": headline or None,
            "label_basis": basis,
            "confidence": confidence,
            "label_version": GDELT_LABEL_VERSION,
            "mentions": mentions,
            "goldstein": _as_float(row[30]),
            "fatalities": None,
            "url": url,
            "summary": summary,
        })
    # One article can produce many CAMEO rows (different actors/locations).
    # Keep a single, best-supported item per source URL.
    deduped: dict[str, dict] = {}
    for item in out:
        key = _gdelt_dedupe_key(item)
        prev = deduped.get(key)
        if prev is None or (item.get("mentions") or 0) > (prev.get("mentions") or 0):
            deduped[key] = item
    return list(deduped.values())


def _gdelt_dedupe_key(item: dict) -> str:
    title = re.sub(r"[^a-z0-9]+", " ", str(item.get("title") or "").lower()).strip()
    return title or str(item.get("url") or item.get("id") or "")


def _revalidate_cached_gdelt_item(item: dict) -> dict | None:
    """Re-label a pre-upgrade GDELT cache item from its source slug.

    Older items used the automated CAMEO code as the headline. Keep only items
    whose article slug contains conflict wording, and replace the code label
    with the article headline.
    """
    if item.get("label_version") == GDELT_LABEL_VERSION:
        return item
    headline, conflict, hazard = _gdelt_article_evidence(
        item.get("url"), "", str(item.get("place") or ""),
    )
    if hazard or not headline or not conflict:
        return None
    out = dict(item)
    label = out.get("event_type") or GDELT_EVENT_LABELS.get(
        str(out.get("event_code") or ""), "Conflict event")
    out.update({
        "title": headline,
        "severity": "ALERT",
        "event_type": label,
        "article_headline": headline,
        "label_basis": "article headline (revalidated)",
        "confidence": "low",
        "label_version": GDELT_LABEL_VERSION,
        "source": "GDELT (media-derived)",
        "summary": (
            f"GDELT automated coding: {label}. Article headline: {headline}. "
            "Media-derived and unverified; event coding and geocoding can be wrong. "
            "Verify the linked report before acting."
        ),
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
        if parsed is None or parsed < cutoff or not item.get("id"):
            continue
        revalidated = _revalidate_cached_gdelt_item(item)
        if revalidated is not None:
            kept.append(revalidated)
    # Build a useful window on first run; afterwards the 15-minute slices are
    # merged into the existing cache so the map does not blink empty.
    slices = 4 if not kept else 2
    fresh: list[dict] = []
    errors = []
    downloaded = 0
    for url in _gdelt_export_urls(slices):
        try:
            resp = requests.get(url, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
                raw = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
            downloaded += 1
            fresh.extend(_gdelt_conflict_items(list(csv.reader(io.StringIO(raw), delimiter="\t"))))
        except Exception as exc:
            errors.append(str(exc)[:120])
    if not fresh and not downloaded:
        raise RuntimeError("GDELT export unavailable: " + ("; ".join(errors[:2]) or "download failed"))
    merged = {item["id"]: item for item in kept}
    for item in fresh:
        merged[item["id"]] = item
    # Collapse duplicate rows that point at the same source article.
    deduped: dict[str, dict] = {}
    for item in merged.values():
        key = _gdelt_dedupe_key(item)
        prev = deduped.get(key)
        if prev is None or (item.get("mentions") or 0) > (prev.get("mentions") or 0):
            deduped[key] = item
    items = sorted(deduped.values(), key=lambda item: item.get("time") or "", reverse=True)
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
            severity = _RSOE_SEVERITY.get(colour.lower(), "INFO")
            # RSOE colour-codes dense fog as "critical", but it is a weather
            # advisory rather than a threat. Downgrade it so it does not
            # dominate the threat picture or the critical-events list.
            if any(w in (title + " " + category + " " + summary).lower()
                   for w in ("fog", "visibility", "mist")):
                severity = "INFO"
            out.append({
                "id": event_id or title,
                "title": title,
                "time": _iso_time(date) or "",
                "source": source,
                "severity": severity,
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


def _rsoe_enrich(items: list[dict], limit: int = 400) -> None:
    """Fetch detail pages for events that still need coordinates.

    The list page has no coordinates. Detail pages do, so enrich every event
    that is not already cached with a position. Previously enriched items are
    reused by _refresh_rsoe(), which keeps routine refreshes cheap.
    """
    targets = [it for it in items if it.get("lat") is None][:max(0, limit)]

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
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(fetch, targets))


def _refresh_rsoe(db: Database, row) -> list[dict]:
    """Fetch the RSOE EDIS list page and enrich all events with coordinates."""
    url = (row["url"] or "").strip() or "https://rsoe-edis.org/eventList"
    try:
        previous = json.loads(row["payload"] or "{}").get("items") or []
    except Exception:
        previous = []
    prev_by_id = {
        str(it.get("id")): it for it in previous
        if it.get("id") and it.get("lat") is not None and it.get("lon") is not None
    }
    resp = requests.get(url, headers=UA, timeout=TIMEOUT)
    resp.raise_for_status()
    items = _parse_rsoe(resp.text, row["name"])
    if not items:
        raise RuntimeError("RSOE EDIS returned no parsable events")
    for item in items:
        cached = prev_by_id.get(str(item.get("id")))
        if cached:
            item["lat"] = cached.get("lat")
            item["lon"] = cached.get("lon")
            if len(str(cached.get("summary") or "")) > len(str(item.get("summary") or "")):
                item["summary"] = cached.get("summary")
    _rsoe_enrich(items, limit=400)
    return items


def _parse_nasa_firms(raw: str, source: str = "NASA FIRMS") -> list[dict]:
    """Parse NASA FIRMS area CSV (operator-configured BYOK URL) or JSON/GeoJSON.
    FIRMS requires an API key (MAP_KEY) in the request URL; DarkSec never ships keys.
    Typical CSV columns: latitude,longitude,brightness,scan,track,acq_date,acq_time,
    satellite,instrument,confidence,version,bright_t31,frp,daynight
    """
    text = (raw or "").strip()
    if not text:
        return []
    # JSON/GeoJSON fallback
    if text[0] in "[{":
        try:
            payload = json.loads(text)
            items = _parse_geojson_items(payload, source, "fire", default_severity="ALERT")
            for it in items:
                it.setdefault("kind", "event")
                it.setdefault("category", "fire")
                if not it.get("severity"): it["severity"] = "ALERT"
            return items
        except Exception:
            pass
    # CSV
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception:
        return []
    items = []
    for i, row in enumerate(rows[:5000]):
        if not isinstance(row, dict): continue
        lat = _as_float(row.get("latitude") or row.get("lat"))
        lon = _as_float(row.get("longitude") or row.get("lon") or row.get("lng"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        acq_date = str(row.get("acq_date") or "").strip()
        acq_time = str(row.get("acq_time") or "").strip().zfill(4)
        ts = utcnow()
        if acq_date:
            try:
                hh, mm = (acq_time[:2], acq_time[2:4]) if len(acq_time) >= 4 else ("00","00")
                ts = datetime(int(acq_date[:4]), int(acq_date[5:7]), int(acq_date[8:10]), int(hh), int(mm), tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                ts = acq_date
        conf = row.get("confidence")
        frp = _as_float(row.get("frp"))
        sat = row.get("satellite") or row.get("instrument") or ""
        sev = "ALERT"
        try:
            if conf is not None and str(conf).strip().isdigit() and int(conf) >= 80: sev = "SEVERE"
        except Exception:
            if str(conf).lower() in ("h", "high"): sev = "SEVERE"
        title_sat = f" ({sat})" if sat else ""
        items.append({
            "id": f"firms-{acq_date}-{i}-{lat:.4f}-{lon:.4f}",
            "kind": "event",
            "category": "fire",
            "title": f"Thermal anomaly / fire detection{title_sat}",
            "lat": lat, "lon": lon, "time": ts,
            "summary": f"NASA FIRMS (operator-configured, BYOK). Confidence {conf or '?'}; FRP {frp if frp is not None else '?'} MW. Verify locally before action.",
            "severity": sev,
            "source": source,
            "confidence": conf, "frp": frp, "satellite": row.get("satellite"), "instrument": row.get("instrument"),
            "daynight": row.get("daynight"), "brightness": row.get("brightness"),
            "url": "https://firms.modaps.eosdis.nasa.gov/",
            "raw": {k: v for k, v in row.items() if v not in (None, "")},
        })
    return items


def _normalize(kind: str, raw: str, source: str = "RSS") -> list[dict]:
    if kind == "rss":
        return _parse_rss(raw, source)
    if kind == "rsoe":
        return _parse_rsoe(raw, source)
    if kind in ("nasa_firms", "firms", "firms_csv"):
        return _parse_nasa_firms(raw, source)
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
    if kind == "who_news":
        return _parse_who_news(payload)
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
    needs_position = fid in ("open_meteo_air", "open_meteo_weather")
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
        # Ask for a recent time window instead of relying on `order=desc`
        # (the API ignores it and returned 2018 readings) or a geospatial
        # `distance` filter (slow enough to time out on the public API).
        since = (datetime.now(timezone.utc) - timedelta(hours=6))
        return (f"{SAFECAST_URL}?per_page=200"
                f"&since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    if fid == "gdacs":
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=30)
        return ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
                f"?fromDate={start}&toDate={today}&eventlist=EQ,TC,FL,VO,DR")
    return url


def _overpass_fetch_json(query: str, urls: list[str], budget: float = 45.0) -> dict:
    """Race several public Overpass mirrors and return the first usable JSON.

    Public mirrors are frequently overloaded or unreachable; querying them
    sequentially made a single slow mirror exhaust the whole refresh budget.
    Each attempt has short connect/read limits and the first success wins.
    """
    if not urls:
        raise RuntimeError("no Overpass mirrors configured")
    deadline = time.monotonic() + max(10.0, budget)
    failures: list[str] = []

    def attempt(base_url: str) -> dict:
        remaining = max(2.0, deadline - time.monotonic())
        timeout = (min(6.0, remaining), min(28.0, remaining))
        try:
            resp = None
            for headers in (OVERPASS_UA, UA):
                resp = requests.post(base_url, data={"data": query}, headers=headers, timeout=timeout)
                # Some reverse proxies reject form POSTs with 405/406/415 even
                # though the upstream interpreter accepts GET.
                if resp.status_code in (405, 406, 415):
                    resp = requests.get(base_url, params={"data": query}, headers=headers, timeout=timeout)
                # A mirror may reject one UA but accept the other.
                if resp.status_code == 403 and headers is not UA:
                    continue
                break
            resp.raise_for_status()
            text = resp.text.lstrip()
            if not text.startswith(("{", "[")):
                raise RuntimeError(f"non-JSON response ({text[:40]!r})")
            return resp.json()
        except Exception as exc:
            host = base_url.split("/")[2]
            raise RuntimeError(f"{host}: {exc}") from exc

    pool = ThreadPoolExecutor(max_workers=min(4, len(urls)))
    futures = [pool.submit(attempt, u) for u in urls]
    try:
        for future in as_completed(futures, timeout=max(10.0, budget)):
            try:
                return future.result()
            except Exception as exc:
                failures.append(str(exc))
    except TimeoutError:
        failures.append(f"no mirror answered within {int(budget)}s")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(
        "Overpass mirrors unavailable (" + "; ".join(failures[:4])
        + "). Cached camera locations remain readable; try again later or set cctv_radius_km smaller."
    )


def _refresh_overpass_cctv(db: Database, row) -> list[dict]:
    """Operator-triggered OSM surveillance camera locations via Overpass (ODbL). Locations only, no previews/streams.

    The Overpass query is centered on the operator's configured position. To
    avoid showing only a tiny "near me" bubble, the radius is configurable via
    Settings key `cctv_radius_km` (default 15 km, clamped 5..250 km). Larger
    radii are heavier for public Overpass mirrors and may time out; cached
    camera locations remain visible on the map regardless of refresh status.
    """
    s = db.all_settings()
    try:
        lat = float(s.get("lat")); lon = float(s.get("lon"))
    except Exception:
        lat = lon = None
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("operator position is missing or invalid; set a valid latitude/longitude before refreshing CCTV locations")
    try:
        radius_km = float(s.get("cctv_radius_km") or 15)
    except Exception:
        radius_km = 15.0
    radius_km = max(5.0, min(250.0, radius_km))
    radius_m = int(radius_km * 1000)
    # Nodes and ways cover mapped surveillance cameras. Relations are omitted:
    # they are rare, much heavier to resolve, and were a common timeout cause.
    query = f"""[out:json][timeout:25][maxsize:33554432];
(
  node["man_made"="surveillance"](around:{radius_m},{lat:.5f},{lon:.5f});
  way["man_made"="surveillance"](around:{radius_m},{lat:.5f},{lon:.5f});
);
out center tags qt 4000;"""
    # Public Overpass mirrors differ in load and availability. Race a few
    # global instances with bounded per-attempt timeouts so one dead mirror
    # cannot hang the whole refresh. overpass.osm.ch is omitted (Switzerland).
    overpass_urls = [
        "https://overpass.openstreetmap.fr/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass.nchc.org.tw/api/interpreter",
        "https://overpass.osm.jp/api/interpreter",
    ]
    # Optional operator-configured mirror (regional/self-hosted) goes first.
    custom = str(s.get("cctv_overpass_url") or "").strip()
    if custom.startswith(("http://", "https://")):
        overpass_urls.insert(0, custom)
    data = _overpass_fetch_json(query, overpass_urls, budget=45.0)

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


def _cctv_catalog_candidates(db: Database) -> list[Path]:
    """Local CCTV catalog files (operator-controlled, offline-capable).

    Includes files under fielddata/maps/cctv* and, when present on this
    machine, gods-eye-view curated/config camera catalogs. Local copies/imports
    are preferred for deployments; direct reads from sibling repos are a
    convenience for local development.
    """
    from . import config as _config
    out: list[Path] = []
    resource_root = Path(getattr(_config, "RESOURCE_ROOT", Path(".")))
    data_dir = Path(getattr(_config, "DATA_DIR", resource_root / "fielddata"))
    roots = [
        data_dir / "maps" / "cctv_catalogs",
        data_dir / "maps" / "cctv",
        data_dir / "cctv_catalogs",
        resource_root / "maps" / "cctv_catalogs",
        resource_root / "maps" / "cctv",
    ]
    try:
        settings = db.all_settings() if hasattr(db, "all_settings") else {}
        extra = str((settings or {}).get("cctv_catalog_dir") or (settings or {}).get("cctv_catalog_path") or "").strip()
        if extra:
            ep = Path(extra).expanduser()
            if ep.is_file():
                out.append(ep)
            elif ep.is_dir():
                out.extend(sorted(ep.glob("*.json")))
    except Exception:
        pass
    for root in roots:
        try:
            if root.is_dir():
                out.extend(sorted(root.glob("*.json")))
        except Exception:
            pass
    # Optional local sibling: gods-eye-view curated CCTV packs/configs.
    gev = Path("/home/null/gods-eye-view")
    try:
        if gev.is_dir():
            out.extend(sorted((gev / "config").glob("cctv_sources*.json")))
            q = gev / "public" / "quebec-cameras.json"
            if q.is_file():
                out.append(q)
    except Exception:
        pass
    seen = set()
    uniq = []
    for p in out:
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        uniq.append(p)
    return uniq


def _extract_cctv_catalog_records(data) -> list[dict]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    for k in ("cameras", "sources", "items", "data", "records", "features"):
        v = data.get(k)
        if isinstance(v, list):
            if k == "features" or (v and isinstance(v[0], dict) and v[0].get("type") == "Feature"):
                recs = []
                for f in v:
                    if not isinstance(f, dict):
                        continue
                    props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
                    geom = f.get("geometry") if isinstance(f.get("geometry"), dict) else {}
                    coords = geom.get("coordinates") if isinstance(geom.get("coordinates"), (list, tuple)) else []
                    rec = dict(props)
                    if len(coords) >= 2 and rec.get("lon") is None and rec.get("lng") is None:
                        try:
                            rec["lon"] = float(coords[0]); rec["lat"] = float(coords[1])
                        except Exception:
                            pass
                    if f.get("id") and not rec.get("id"):
                        rec["id"] = f.get("id")
                    recs.append(rec)
                return recs
            return [r for r in v if isinstance(r, dict)]
    if "lat" in data and ("lon" in data or "lng" in data):
        return [data]
    return []


def _normalize_cctv_catalog_item(rec: dict, source_file: str) -> dict | None:
    try:
        lat = rec.get("lat", rec.get("latitude"))
        lon = rec.get("lon", rec.get("lng", rec.get("longitude")))
        if lat is None or lon is None:
            return None
        lat = float(lat); lon = float(lon)
    except Exception:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    name = (rec.get("name") or rec.get("nameEn") or rec.get("title") or rec.get("label")
            or rec.get("camera_id") or rec.get("id") or "Camera")
    provider = (rec.get("provider") or rec.get("operator") or rec.get("operator_name")
                or rec.get("source") or rec.get("sourceKind") or "catalog")
    cam_id = (rec.get("camera_id") or rec.get("id") or rec.get("key")
              or f"{Path(source_file).stem}:{round(lat,5)}:{round(lon,5)}:{str(name)[:40]}")
    snapshot = rec.get("snapshotUrl") or rec.get("snapshot_url") or rec.get("imageUrl") or rec.get("image_url")
    raw_url = rec.get("url") or rec.get("feed_url")
    view_url = rec.get("viewerUrl") or rec.get("viewer_url") or raw_url or snapshot
    heading = rec.get("headingDeg", rec.get("heading_deg", rec.get("heading", rec.get("direction"))))
    try:
        heading = float(heading) if heading not in (None, "") else None
    except Exception:
        heading = None
    credit = rec.get("credit") or rec.get("attribution") or rec.get("operator")
    license_ = rec.get("license") or rec.get("licence")
    feed_type_raw = str(rec.get("feedType") or rec.get("feed_type") or "").lower().strip()
    raw_url_s = str(raw_url or "")
    is_stream = feed_type_raw in ("hls", "m3u8", "mp4", "video", "stream", "mpegts") or bool(re.search(r'\.(m3u8|mp4|m4v|webm|ts)(\?|$)', raw_url_s, re.I))
    if feed_type_raw:
        feed_type = feed_type_raw
    elif is_stream:
        feed_type = "hls" if ".m3u8" in raw_url_s.lower() else "mp4"
    elif snapshot or (isinstance(view_url, str) and any(x in str(view_url).lower() for x in (".jpg", ".jpeg", ".png", "snapshot", "image"))):
        feed_type = "image"
    else:
        feed_type = ""
    stream_url = raw_url_s if is_stream and raw_url_s.startswith(("http://", "https://")) else ""
    item = {
        "id": str(cam_id),
        "camera_id": str(cam_id),
        "kind": "cctv",
        "title": str(name),
        "lat": lat,
        "lon": lon,
        "url": str(rec.get("viewerUrl") or rec.get("viewer_url") or snapshot or raw_url or ""),
        "summary": rec.get("notes") or rec.get("description") or rec.get("category") or "",
        "source": str(provider),
        "provider": str(provider),
        "status": str(rec.get("status") or "catalog"),
        "surveillance_type": str(rec.get("surveillance_type") or rec.get("kind") or "camera"),
        "import_source": Path(source_file).name,
        "source_kind": str(rec.get("sourceKind") or rec.get("source_kind") or "imported_catalog"),
        "time": rec.get("updated_at") or rec.get("time") or utcnow(),
    }
    if snapshot:
        item["snapshot_url"] = str(snapshot)
    if feed_type:
        item["feed_type"] = str(feed_type)
    if stream_url:
        item["stream_url"] = stream_url
    if raw_url_s and not stream_url and raw_url_s.startswith(("http://", "https://")) and not item.get("url"):
        item["url"] = raw_url_s
    if credit:
        item["credit"] = str(credit)
    if license_:
        item["license"] = str(license_)
    if heading is not None and -360 <= heading <= 360:
        item["heading_deg"] = heading
        item["direction"] = heading
    for src, dst in (("headingConfidence", "heading_confidence"), ("pitchDeg", "pitch_deg"),
                     ("fovDeg", "fov_deg"), ("rangeM", "range_m"),
                     ("mountHeightM", "mount_height_m"), ("groundElevationM", "ground_elevation_m"),
                     ("region", "region"), ("city", "city")):
        v = rec.get(src, rec.get(dst))
        if v is not None and v != "":
            item[dst] = v
    return item


def _cctv_catalog_disabled(db: Database) -> set[str]:
    try:
        settings = db.all_settings() if hasattr(db, "all_settings") else {}
        raw = (settings or {}).get("cctv_catalog_disabled_sources") or "[]"
        if isinstance(raw, list):
            vals = raw
        else:
            vals = json.loads(str(raw) or "[]")
        return {str(v) for v in vals if v}
    except Exception:
        return set()


def _refresh_cctv_catalog(db: Database, row) -> tuple[list[dict], dict]:
    """Import local/curated CCTV catalogs into a cached map layer.

    Reads JSON catalogs from fielddata/maps/cctv_catalogs and, when present,
    from a local gods-eye-view checkout (config cctv_sources + quebec). This
    provides broad offline-capable camera coverage; each item preserves source
    credit/license metadata when present. Operators should verify source terms
    before redistributing imported data.
    """
    files = _cctv_catalog_candidates(db)
    disabled = _cctv_catalog_disabled(db)
    pack_stats = []
    if not files:
        return [], {"pack_stats": [], "disabled_sources": sorted(disabled)}
    items = []
    seen_ids = set()
    errors = []
    for path in files:
        name = path.name
        enabled = name not in disabled and str(path) not in disabled
        stat = {"source": name, "path": str(path), "enabled": enabled, "count": 0, "error": None}
        if not enabled:
            pack_stats.append(stat)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            stat["error"] = str(exc)
            errors.append(f"{name}: {exc}")
            pack_stats.append(stat)
            continue
        added = 0
        for rec in _extract_cctv_catalog_records(data):
            item = _normalize_cctv_catalog_item(rec, str(path))
            if not item:
                continue
            iid = str(item.get("id") or item.get("camera_id") or "")
            if iid in seen_ids:
                iid = f"{iid}:{Path(str(path)).stem}"
                item["id"] = iid
                item["camera_id"] = iid
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            items.append(item)
            added += 1
        stat["count"] = added
        pack_stats.append(stat)
    if not items and errors and not any(s.get("enabled") and s.get("count", 0) > 0 for s in pack_stats):
        # All enabled sources failed and produced nothing.
        if not any(s.get("enabled") for s in pack_stats):
            return [], {"pack_stats": pack_stats, "disabled_sources": sorted(disabled)}
        raise RuntimeError("CCTV catalog import found no valid cameras (" + "; ".join(errors[:3]) + ")")
    return items, {"pack_stats": pack_stats, "disabled_sources": sorted(disabled), "imported_at": utcnow()}


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


def _refresh_safecast(db: Database, row) -> list[dict]:
    """Recent Safecast measurements.

    The public API ignores `order=created_at desc` and returns old readings;
    geospatial `distance` queries are slow and frequently time out. Request a
    bounded recent window instead and sort the response locally.
    """
    now = datetime.now(timezone.utc)
    last_exc: Exception | None = None
    for hours in (12, 24 * 7):
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{SAFECAST_URL}?per_page=200&since={since}"
        try:
            raw = _get_text_with_fallbacks(url, [UA], timeout=max(TIMEOUT, 20))
            items = _parse_safecast(json.loads(raw))
            if items:
                return items
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    return []


def _refresh_who(db: Database, row) -> list[dict]:
    """WHO news via the current newsroom API, with the legacy RSS as backup."""
    errors = []
    for url, kind, source in (
        (WHO_NEWS_API, "who_news", row["name"]),
        (WHO_NEWS_RSS, "rss", row["name"]),
    ):
        try:
            raw = _get_text_with_fallbacks(url, [UA], timeout=max(TIMEOUT, 25))
            items = _normalize(kind, raw, source)
            if items:
                return items
            errors.append(f"{url.split('/')[2]}: no items")
        except Exception as exc:
            errors.append(f"{url.split('/')[2]}: {exc}")
    raise RuntimeError("WHO news sources unavailable (" + "; ".join(errors[:2]) + ")")


def _refresh_volcano(db: Database, row) -> list[dict]:
    """Merge the Smithsonian weekly report with current USGS HANS CAP alerts.

    The Smithsonian weekly RSS stopped publishing in October 2026 while the
    program moved to a daily report without an RSS feed. USGS CAP alerts are
    current and official, so they keep the volcano layer from going stale.
    """
    errors = []
    groups = []
    weekly = (row["url"] or "").strip() or SMITHSONIAN_WEEKLY_RSS
    try:
        raw = _get_text_with_fallbacks(weekly, [UA])
        groups.append(_parse_rss(raw, row["name"]))
    except Exception as exc:
        errors.append(f"weekly report: {exc}")
    try:
        raw = _get_text_with_fallbacks(USGS_VOLCANO_CAP, [UA])
        groups.append(_parse_rss(raw, "USGS Volcano (HANS)"))
    except Exception as exc:
        errors.append(f"USGS CAP: {exc}")
    items = _merge_items(*groups, limit=100)
    if not items:
        raise RuntimeError("no volcano activity available (" + "; ".join(errors[:2]) + ")")
    return items


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
            if not url and fid == "cctv_catalog":
                items, meta = _refresh_cctv_catalog(db, row)
                note = "Local/imported CCTV catalogs (verify source terms/attribution before redistribution)"
                payload = {"items": items, "fetched_at": utcnow(), "source_note": note}
                if isinstance(meta, dict):
                    payload.update(meta)
                db.execute(
                    "UPDATE feeds SET fetched_at=?, status='OK', payload=?, error=NULL WHERE id=?",
                    (utcnow(), json.dumps(payload), row["id"]),
                )
                results.append({"id": row["id"], "status": "OK", "count": len(items), "packs": len((meta or {}).get("pack_stats") or [])})
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
            elif fid == "safecast":
                items = _refresh_safecast(db, row)
            elif row["kind"] == "who_news" or (
                fid == "who_don" and (row["url"] or "").strip() in ("", WHO_NEWS_RSS, WHO_NEWS_API)
            ):
                items = _refresh_who(db, row)
            elif fid == "smithsonian_volcano" and (
                (row["url"] or "").strip() in ("", SMITHSONIAN_WEEKLY_RSS)
            ):
                items = _refresh_volcano(db, row)
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
                    if fid == "gdacs":
                        # GDACS's WAF rejects browser-like User-Agent strings
                        # (including the app's standard UA) with an HTML
                        # "Request Rejected" page. A plain client UA gets JSON.
                        resp = requests.get(
                            url,
                            headers={"User-Agent": "python-requests/2.32.3",
                                     "Accept": "application/json"},
                            timeout=TIMEOUT,
                        )
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
        if status == "OK" and data_age is not None and row["kind"] not in DATA_STALE_EXEMPT_KINDS \
                and data_age > DATA_STALE_AFTER.get(row["id"], DEFAULT_STALE_AFTER):
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
