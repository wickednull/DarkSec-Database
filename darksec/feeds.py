"""Optional online intelligence feeds with offline cache.

The console never requires a network. When a connection is available the
operator can pull public feeds (USGS, NOAA SWPC, NWS, GDACS, WHO, ReliefWeb);
every response is cached in SQLite and remains readable offline with a clear
age indicator.
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

import requests

from .db import Database, utcnow

UA = {"User-Agent": "FieldConsole/2.0"}
TIMEOUT = 15
UCDP_TIMEOUT = 90

# Fetch-age limits and data-age limits are different things. A feed can fetch
# successfully while every item in it is months old (Safecast, WHO archives).
# Both are surfaced so the UI never presents an old snapshot as current.
STALE_AFTER = {
    "gdelt_conflict": 2 * 3600,
    "ucdp_candidate": 50 * 86400,
    "celestrak_tle": 24 * 3600,
    "smithsonian_volcano": 14 * 86400,
    "who_don": 14 * 86400,
    "safecast": 30 * 86400,
}
DATA_STALE_AFTER = {
    "gdelt_conflict": 3 * 3600,
    "ucdp_candidate": 50 * 86400,
    "celestrak_tle": 24 * 3600,
    "smithsonian_volcano": 14 * 86400,
    "who_don": 14 * 86400,
    "safecast": 30 * 86400,
}
DEFAULT_STALE_AFTER = 6 * 3600


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
        name = callsign or country or "AIRCRAFT"
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
        "summary": f"Open-Meteo air quality index (US AQI) for the operator position. "
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
            "time": p.get("todate") or p.get("fromdate") or p.get("datemodified"),
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


def _normalize(kind: str, raw: str, source: str = "RSS") -> list[dict]:
    if kind == "rss":
        return _parse_rss(raw, source)
    payload = json.loads(raw)
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
    return []


def _feed_url(db: Database, row) -> str:
    """Resolve position-dependent and date-dependent feed URLs."""
    fid = row["id"]
    url = row["url"]
    s = db.all_settings()
    try:
        lat = float(s.get("lat") if s.get("lat") is not None else 0.0)
        lon = float(s.get("lon") if s.get("lon") is not None else 0.0)
    except (TypeError, ValueError):
        lat, lon = 0.0, 0.0
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
            if row["kind"] == "gdelt":
                items = _refresh_gdelt(db, row)
            elif row["kind"] == "ucdp":
                items = _refresh_ucdp(db, row)
            else:
                url = _feed_url(db, row)
                resp = requests.get(url, headers=UA, timeout=TIMEOUT)
                resp.raise_for_status()
                raw = resp.text
                if row["kind"] == "celestrak":
                    items = _parse_celestrak(raw)
                else:
                    items = _normalize(row["kind"], raw, row["name"])
                if not items and raw.lstrip().lower().startswith(("<!doctype", "<html")):
                    raise RuntimeError("provider returned an HTML page instead of a feed")
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
    for row in db.query("SELECT * FROM feeds ORDER BY name"):
        payload = None
        items = []
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
                items = payload.get("items", [])
            except Exception:
                payload = None
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
        latest_time = None
        for item in items:
            parsed = parse_time(item.get("time"))
            if parsed is None:
                continue
            if latest_time is None or parsed > latest_time:
                latest_time = parsed
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


def summary(db: Database) -> dict:
    feeds = list_feeds(db)
    critical = [i for f in feeds for i in f["items"] if i.get("severity") in ("CRITICAL", "SEVERE", "EXTREME")]
    return {
        "feeds": feeds,
        "critical_count": len(critical),
        "latest": sorted(
            [i for f in feeds for i in f["items"]],
            key=lambda x: x.get("time") or "",
            reverse=True,
        )[:12],
    }
