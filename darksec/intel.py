"""Online intelligence layer: live feeds, satellite positions, threat index.

Everything here is optional and public-source-only. The console never requires
network access; every snapshot is cached in the local SQLite database through
the generic feed store, so the UI can render offline with clear age labels.

Sources used by this layer:
  - USGS significant earthquakes (geo events)
  - NOAA SWPC planetary K-index / space weather scales
  - NWS active alerts (US)
  - GDACS and RSOE EDIS disaster alerts, WHO DON, ReliefWeb updates (RSS)
  - OpenSky live aircraft public positions
  - CelesTrak active satellite TLE (SGP4 propagation)
  - Open-Meteo air quality index
  - BBC World News RSS (live news)

The threat index is our own transparent aggregation over cached events. It is
an operational risk estimate, not a forecast product.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import feeds as feedstore
from . import tilecache
from .db import Database, utcnow

CRITICAL_SEVERITIES = ("CRITICAL", "SEVERE", "EXTREME")
SEVERITY_WEIGHT = {"CRITICAL": 40.0, "SEVERE": 25.0, "EXTREME": 40.0, "ALERT": 12.0, "INFO": 0.0}

# Positional/infrastructure feeds rendered as their own map layers, not as point events.
NON_EVENT_KINDS = {"opensky", "celestrak", "air_quality", "weather", "maritime", "ais", "cables", "cctv", "geojson"}

# How long an event remains useful on the situational map. GDELT is a live
# 15-minute slice; UCDP is a monthly verified release and necessarily lags.
MAP_MAX_AGE_HOURS = {
    "gdelt_conflict": 48,
    "ucdp_candidate": 120 * 24,
    "usgs_quakes": 7 * 24,
    "gdacs": 45 * 24,
    "eonet": 90 * 24,
    "rsoe_edis": 10 * 24,
    "nws_alerts": 3 * 24,
    "safecast": 30 * 24,
    "maritime_ais": 6,
    "infra_cables": 365 * 24,
    "public_cctv": 365 * 24,
}
MAP_DEFAULT_MAX_AGE_HOURS = 120 * 24

# Threat-model tuning. These are transparent heuristics, not forecasts.
THREAT_HALF_LIFE_HOURS = {
    "nws_alerts": 18,
    "gdacs": 72,
    "usgs_quakes": 48,
    "eonet": 96,
    "rsoe_edis": 72,
    "gdelt_conflict": 24,
    "ucdp_candidate": 45 * 24,
    "safecast": 24 * 24,
}
THREAT_MAX_AGE_HOURS = {
    "gdelt_conflict": 72,
    "ucdp_candidate": 180 * 24,
    "nws_alerts": 96,
    "usgs_quakes": 14 * 24,
    "gdacs": 60 * 24,
    "eonet": 120 * 24,
    "rsoe_edis": 21 * 24,
    "safecast": 45 * 24,
}
FEED_CREDIBILITY = {
    "usgs_quakes": 1.0,
    "nws_alerts": 1.0,
    "gdacs": 0.95,
    "eonet": 0.9,
    "ucdp_candidate": 0.95,
    "rsoe_edis": 0.8,
    "safecast": 0.75,
    "gdelt_conflict": 0.55,  # automated media coding; location/labels can be noisy
}
KIND_THREAT_MULT = {
    "conflict": 1.1,
    "usgs": 1.0,
    "nws": 1.0,
    "gdacs": 1.0,
    "eonet": 0.95,
    "radiation": 1.1,
    "rsoe": 0.25,
}
AQI_BANDS = [(50, 0, "GOOD"), (100, 4, "MODERATE"), (150, 10, "USG"),
             (200, 18, "UNHEALTHY"), (300, 28, "VERY UNHEALTHY"), (10**9, 38, "HAZARDOUS")]

CONFLICT_NEWS_FEEDS = {
    "bbc_world", "bbc_middle_east", "guardian_world", "france24_world", "dw_news",
    "aljazeera_world", "reliefweb", "crisisgroup",
    "twz_defense", "breaking_defense", "defense_one", "long_war_journal", "war_on_the_rocks",
}
_CONFLICT_TERMS = re.compile(
    r"\b(war|wars|conflict|airstrike|air strike|missile|drone|drones|shelling|"
    r"offensive|ceasefire|truce|troops|military|army|rebel|insurgent|"
    r"militant|jihadist|paramilitary|attack|attacked|killed|casualties|"
    r"frontline|front line|invasion|occupied|occupation|bomb|bombing|"
    r"explosion|hostage|hostages|refugee|evacuat|massacre|genocide|siege|coup|"
    r"junta|sanctions|weapons|artillery|combat|clashes|strike|strikes|"
    r"rocket|ballistic|cruise missile|air defense|naval|frigate|carrier)\b",
    re.I,
)


def _age_hours(value) -> float | None:
    parsed = feedstore.parse_time(value)
    if parsed is None:
        return None
    return round(max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 3600.0), 1)


def _sort_time(value) -> datetime:
    return feedstore.parse_time(value) or datetime.min.replace(tzinfo=timezone.utc)


def _event_from_item(feed: dict, item: dict) -> dict | None:
    if item.get("lat") is None or item.get("lon") is None:
        return None
    try:
        lat, lon = float(item["lat"]), float(item["lon"])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    _sev = str(item.get("severity") or "INFO").upper()
    if _sev in ("CRITICAL", "SEVERE", "EXTREME", "ALERT") and any(
            w in f"{item.get('title') or ''} {item.get('summary') or ''} {item.get('category') or ''}".lower()
            for w in ("fog", "visibility", "mist")):
        _sev = "INFO"
    return {
        "id": f"{feed.get('id')}:{item.get('id') or item.get('title')}",
        "source": item.get("source") or feed.get("name") or "?",
        "feed_id": feed.get("id"),
        "kind": item.get("kind") or feed.get("kind"),
        "title": item.get("title") or "Event",
        "severity": _sev,
        "time": item.get("time"),
        "age_hours": _age_hours(item.get("time")),
        "lat": lat, "lon": lon,
        "alt_m": item.get("alt_m"),
        "mag": item.get("magnitude") or item.get("mag"),
        "aqi": item.get("aqi"),
        "url": item.get("url"),
        "summary": item.get("summary") or "",
        "place": item.get("place") or "",
        "depth_km": item.get("depth_km"),
        "precision": item.get("precision") or "",
        "verified": bool(item.get("verified")),
        "fatalities": item.get("fatalities"),
        "deaths_civilians": item.get("deaths_civilians"),
        "event_type": item.get("event_type"),
        "country": item.get("country") or "",
        "mentions": item.get("mentions"),
        "event_code": item.get("event_code"),
        "event_date": item.get("event_date"),
        "source_headline": item.get("source_headline") or "",
    }


def live_events(db: Database, limit: int = 60) -> list[dict]:
    """Cached point events, newest first. Aircraft and weather are excluded."""
    items: list[dict] = []
    for f in feedstore.list_feeds(db, item_limit=None):
        if f.get("kind") in NON_EVENT_KINDS:
            continue
        for it in f.get("items") or []:
            event = _event_from_item(f, it)
            if event:
                items.append(event)
    items.sort(key=lambda x: _sort_time(x.get("time")), reverse=True)
    return items[:limit]


def map_events(db: Database) -> list[dict]:
    """Every fresh geolocated event suitable for the world map.

    Old snapshots are excluded by feed-specific windows instead of being drawn
    as if they were current. The returned age and precision fields let the map
    distinguish a city report from a country-level mention.
    """
    out: list[dict] = []
    for f in feedstore.list_feeds(db, item_limit=None):
        if f.get("kind") in NON_EVENT_KINDS:
            continue
        fid = f.get("id")
        max_age = MAP_MAX_AGE_HOURS.get(fid, MAP_DEFAULT_MAX_AGE_HOURS)
        for it in f.get("items") or []:
            event = _event_from_item(f, it)
            if not event:
                continue
            age = event.get("age_hours")
            if age is not None and age > max_age:
                continue
            # A Safecast endpoint that still answers but has no recent
            # measurements must not add years-old radiation dots to the map.
            if fid == "safecast" and (age is None or age > 30 * 24):
                continue
            out.append(event)
    out.sort(key=lambda x: _sort_time(x.get("time")), reverse=True)
    return out


def map_summary(events: list[dict]) -> dict:
    conflicts = [e for e in events
                 if e.get("kind") == "conflict"
                 or e.get("feed_id") in ("gdelt_conflict", "ucdp_candidate")]
    conflict_ids = {id(e) for e in conflicts}
    hazards = [e for e in events if id(e) not in conflict_ids]
    ages = [e["age_hours"] for e in events if e.get("age_hours") is not None]
    return {
        "total": len(events),
        "conflict": len(conflicts),
        "hazards": len(hazards),
        "verified_conflict": len([e for e in conflicts if e.get("verified")]),
        "live_conflict": len([e for e in conflicts if not e.get("verified")]),
        "newest_age_hours": min(ages) if ages else None,
        "oldest_age_hours": max(ages) if ages else None,
        "sources": sorted({e.get("source") for e in events if e.get("source")}),
    }


def _conflict_feed_items(feeds: list[dict], feed_id: str, limit: int = 40) -> list[dict]:
    out = []
    for f in feeds:
        if f.get("id") != feed_id:
            continue
        for it in f.get("items") or []:
            event = _event_from_item(f, it)
            if not event:
                continue
            event["feed_id"] = feed_id
            event["kind"] = "conflict"
            out.append(event)
    out.sort(key=lambda x: _sort_time(x.get("time")), reverse=True)
    return out[:limit]


def _war_headlines(feeds: list[dict], max_age_hours: int = 7 * 24, limit: int = 30) -> list[dict]:
    """Conflict-relevant headlines from current wire/humanitarian feeds.

    The old panel concatenated every world-news feed and sorted RFC-822 dates
    as strings, which floated a 2020 WHO archive item above current reporting.
    Here dates are parsed, stale items are dropped and relevance is required.
    """
    out = []
    seen = set()
    for f in feeds:
        if f.get("id") not in CONFLICT_NEWS_FEEDS:
            continue
        for it in f.get("items") or []:
            text = " ".join(str(part or "") for part in (
                it.get("title"), it.get("summary"), " ".join(it.get("categories") or []),
            ))
            if not _CONFLICT_TERMS.search(text):
                continue
            age = _age_hours(it.get("time"))
            if age is None or age > max_age_hours:
                continue
            title = str(it.get("title") or "").strip()
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "id": f"{f.get('id')}:{title}",
                "source": it.get("source") or f.get("name") or "?",
                "feed_id": f.get("id"),
                "kind": "headline",
                "title": title,
                "severity": str(it.get("severity") or "INFO").upper(),
                "time": it.get("time"),
                "age_hours": age,
                "url": it.get("url"),
                "summary": it.get("summary") or "",
                "place": "",
                "lat": it.get("lat"), "lon": it.get("lon"),
                "verified": False,
            })
    out.sort(key=lambda x: _sort_time(x.get("time")), reverse=True)
    return out[:limit]


def _war_summary(headlines: list[dict], live: list[dict], verified: list[dict]) -> dict:
    def newest_age(rows):
        ages = [row["age_hours"] for row in rows if row.get("age_hours") is not None]
        return min(ages) if ages else None

    all_rows = headlines + live + verified
    return {
        "headlines": len(headlines),
        "live": len(live),
        "verified": len(verified),
        "newest_age_hours": newest_age(all_rows),
        "live_newest_age_hours": newest_age(live),
        "verified_newest_age_hours": newest_age(verified),
        "newest_time": max((row.get("time") for row in all_rows if row.get("time")),
                           key=_sort_time, default=None),
        "verified_latest_time": verified[0].get("time") if verified else None,
    }


def live_aircraft(db: Database, limit: int = 400) -> list[dict]:
    out = []
    for f in feedstore.list_feeds(db, item_limit=None):
        if f.get("kind") != "opensky":
            continue
        for it in f.get("items") or []:
            if it.get("lat") is None or it.get("lon") is None:
                continue
            out.append({
                "id": f"{f.get('id')}:{it.get('title')}",
                "callsign": str(it.get("title") or "AIRCRAFT"),
                "lat": float(it["lat"]), "lon": float(it["lon"]), "alt_m": it.get("alt_m") or it.get("alt_km"),
                "speed_kmh": it.get("speed_kmh"), "heading_deg": it.get("heading_deg"),
                "source": "OpenSky", "severity": "INFO", "time": it.get("time"),
                "url": it.get("url"),
            })
    out.sort(key=lambda x: float(x.get("alt_m") or x.get("alt_km") or 0), reverse=True)
    return out[:limit]


# SGP4 propagation over the full CelesTrak catalogue (~16k objects) costs
# roughly a second per call. The map and intel views request it repeatedly,
# so cache the computed pass list for a short window. Positions move slowly
# enough that a one-minute snapshot is still useful on the situational map.
_SAT_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_SAT_CACHE_LOCK = threading.Lock()
_SAT_CACHE_TTL = 60.0


def live_satellites(db: Database, observer_lat: float, observer_lon: float, limit: int = 40) -> list[dict]:
    """Propagate the latest CelesTrak TLE snapshot and return passes over the observer."""
    # list_feeds intentionally omits the raw payload from API responses, so
    # read the cached TLE blob directly here.
    row = db.one("SELECT payload FROM feeds WHERE id='celestrak_tle' "
                 "ORDER BY fetched_at DESC LIMIT 1")
    if not row or not row["payload"]:
        return []
    raw = row["payload"]
    cache_key = (round(float(observer_lat), 2), round(float(observer_lon), 2),
                 len(raw), hash(raw))
    mono = time.monotonic()
    with _SAT_CACHE_LOCK:
        cached = _SAT_CACHE.get(cache_key)
        if cached is not None and mono - cached[0] < _SAT_CACHE_TTL:
            return cached[1][:limit]

    out = []
    try:
        payload = json.loads(raw)
        items = payload.get("items") or []
    except Exception:
        return out
    from datetime import datetime as _dt, timedelta as _td
    from sgp4.api import Satrec
    from sgp4 import propagation
    now = datetime.now(timezone.utc)
    obs_lat = math.radians(observer_lat)
    obs_lon = math.radians(observer_lon)
    for it in items:
        line1 = it.get("line1"); line2 = it.get("line2")
        if not line1 or not line2:
            continue
        try:
            sat = Satrec.twoline2rv(line1, line2)
            epoch = _dt(2000 + int(str(sat.epochyr or "20")), 1, 1, tzinfo=timezone.utc) + _td(days=float(sat.epochdays))
            tsince = (now - epoch).total_seconds() / 60.0
            r, _v = propagation.sgp4(sat, tsince)
            if sat.error or not r:
                continue
            radius = math.sqrt(sum(c * c for c in r))
            alt_km = radius - 6371.0
            if alt_km < 80.0 or alt_km > 40000.0:
                continue
            # geocentric lat/lon
            x, y, z = r
            rr = math.sqrt(x * x + y * y + z * z)
            lon = math.atan2(y, x)
            lat = math.asin(max(-1.0, min(1.0, z / rr))) if rr else 0.0
            # elevation over observer
            cos_e = math.cos(obs_lat) * math.cos(lat - obs_lon) + math.sin(obs_lat) * math.sin(lat)
            sin_e = math.sin(lat) * math.cos(obs_lat) - math.cos(lat) * math.sin(obs_lat) * math.cos(obs_lon)
            elev = math.asin(max(-1.0, min(1.0, sin_e / max(1e-9, math.sqrt(cos_e * cos_e + sin_e * sin_e))))) if cos_e > 0 else -math.pi / 2
            if elev < math.radians(5.0):
                continue
            out.append({
                "norad": int(it.get("norad") or 0),
                "name": str(it.get("name") or "UNKNOWN"),
                "lat": math.degrees(lat), "lon": math.degrees(lon), "alt_km": round(alt_km, 1),
                "elevation_deg": round(math.degrees(elev), 1),
                "source": "CelesTrak", "severity": "INFO", "time": utcnow(),
            })
        except Exception:
            continue
    out.sort(key=lambda x: -x["elevation_deg"])
    with _SAT_CACHE_LOCK:
        for old in [k for k, (ts, _) in _SAT_CACHE.items() if mono - ts > 600]:
            _SAT_CACHE.pop(old, None)
        _SAT_CACHE[cache_key] = (mono, out)
    return out[:limit]


# --------------------------------------------------------- future pass planner
# Scanning the full CelesTrak catalogue for future passes would cost minutes.
# Restrict the default planner to satellites an operator can plausibly receive
# or observe (weather imaging, ISS/CSS, amateur radio). A free-text query
# overrides the allowlist so any named object can be planned for.
_PASS_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_PASS_CACHE_LOCK = threading.Lock()
_PASS_CACHE_TTL = 300.0
_PASS_STEP_SECONDS = 60.0
_PASS_MAX_CANDIDATES = 400
_PASS_NAME_RE = re.compile(
    r"(ISS|ZARYA|CSS|TIANHE|NOAA|METEOR|GOES|HIMAWARI|FENGYUN|ELEKTRO|SUOMI|DMSP|"
    r"AQUA|TERRA|LANDSAT|SENTINEL|S-NET|AISSAT|OSCAR|RADIO|FOX|SAUDISAT|XW-|"
    r"HOPE|CUBESAT|AMSAT|SO-|AO-|VO-|PO-|TO-|UKUBE|FUNCUBE|NAYIF|CAS-|JY1|"
    r"LAPAN|GAOFEN|ZY-|RESURS|OCEANSAT|SCATSAT|SARAL|METOP|METEOSAT|INSAT|"
    r"GSAT|CARTOSAT|RISAT|YAOGAN|ARGOS|ORBCOMM|IRIDIUM|GLOBALSTAR)",
    re.IGNORECASE,
)


def _observer_ecef(lat_deg: float, lon_deg: float, alt_km: float = 0.0) -> tuple[float, float, float]:
    """WGS84 observer position in earth-centred, earth-fixed coordinates."""
    a = 6378.137
    f = 1.0 / 298.257223563
    e2 = f * (2.0 - f)
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    n = a / math.sqrt(1.0 - e2 * math.sin(lat) ** 2)
    return ((n + alt_km) * math.cos(lat) * math.cos(lon),
            (n + alt_km) * math.cos(lat) * math.sin(lon),
            (n * (1.0 - e2) + alt_km) * math.sin(lat))


def _gmst_rad(dt: datetime) -> float:
    """Greenwich mean sidereal time (IAU 1982) for a UTC datetime."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    seconds = (dt - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds()
    days = seconds / 86400.0
    gmst_deg = 280.46061837 + 360.98564736629 * days
    return math.radians(gmst_deg % 360.0)


def _look_angles(r_eci: tuple[float, float, float], dt: datetime,
                 obs: tuple[float, float, float], lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Return (elevation_deg, azimuth_deg) from an ECI position."""
    theta = _gmst_rad(dt)
    x, y, z = r_eci
    # Rotate ECI -> ECEF about the z axis.
    sx = x * math.cos(theta) + y * math.sin(theta)
    sy = -x * math.sin(theta) + y * math.cos(theta)
    sz = z
    dx, dy, dz = sx - obs[0], sy - obs[1], sz - obs[2]
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    east = -math.sin(lon) * dx + math.cos(lon) * dy
    north = (-math.sin(lat) * math.cos(lon) * dx
             - math.sin(lat) * math.sin(lon) * dy
             + math.cos(lat) * dz)
    up = (math.cos(lat) * math.cos(lon) * dx
          + math.cos(lat) * math.sin(lon) * dy
          + math.sin(lat) * dz)
    rng = math.sqrt(east * east + north * north + up * up)
    if rng <= 0:
        return -90.0, 0.0
    elev = math.degrees(math.asin(max(-1.0, min(1.0, up / rng))))
    az = math.degrees(math.atan2(east, north)) % 360.0
    return elev, az


def next_satellite_passes(db: Database, observer_lat: float, observer_lon: float,
                          hours: int = 12, min_elevation_deg: float = 10.0,
                          limit: int = 40, query: str = "") -> list[dict]:
    """Predict upcoming passes from the cached CelesTrak TLE snapshot.

    Returns rise/peak/set times, maximum elevation and peak azimuth. Intended
    for planning a manual observation or SDR capture, not for antenna pointing
    without a fresh TLE and a local time reference.
    """
    # The active catalogue supplies the full object set but no names; the
    # stations/weather/amateur groups supply human-readable labels. Merge both.
    rows = db.query("SELECT id,payload FROM feeds WHERE kind='celestrak' "
                    "AND payload IS NOT NULL AND payload<>'' "
                    "ORDER BY CASE id WHEN 'celestrak_stations' THEN 0 "
                    "WHEN 'celestrak_weather' THEN 0 WHEN 'celestrak_amateur' THEN 0 "
                    "ELSE 1 END, id")
    if not rows:
        return []
    raw = "\n".join(str(r["payload"] or "") for r in rows)
    q = str(query or "").strip().lower()
    cache_key = (round(float(observer_lat), 2), round(float(observer_lon), 2),
                 int(hours), round(float(min_elevation_deg), 1), q,
                 len(raw), hash(raw))
    mono = time.monotonic()
    with _PASS_CACHE_LOCK:
        cached = _PASS_CACHE.get(cache_key)
        if cached is not None and mono - cached[0] < _PASS_CACHE_TTL:
            return cached[1][:limit]

    seen: set[int] = set()
    items: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception:
            continue
        for it in payload.get("items") or []:
            norad = int(it.get("norad") or 0)
            if norad and norad in seen:
                continue
            if norad:
                seen.add(norad)
            items.append(it)

    candidates = []
    for it in items:
        if not it.get("line1") or not it.get("line2"):
            continue
        # Geostationary objects do not "pass"; they sit in view continuously.
        try:
            if float(it["line2"][52:63]) < 2.0:
                continue
        except (ValueError, IndexError):
            pass
        name = str(it.get("name") or "")
        if q:
            if q not in name.lower() and q != str(it.get("norad") or ""):
                continue
        elif not _PASS_NAME_RE.search(name):
            continue
        candidates.append(it)
        if len(candidates) >= _PASS_MAX_CANDIDATES:
            break

    try:
        from sgp4.api import Satrec
        from sgp4 import propagation
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    obs = _observer_ecef(observer_lat, observer_lon)
    horizon = now + timedelta(hours=max(1, int(hours)))
    step = timedelta(seconds=_PASS_STEP_SECONDS)
    passes: list[dict] = []

    for it in candidates:
        name = str(it.get("name") or "UNKNOWN")
        try:
            sat = Satrec.twoline2rv(it["line1"], it["line2"])
            epoch = datetime(2000 + int(str(sat.epochyr or "20")), 1, 1,
                             tzinfo=timezone.utc) + timedelta(days=float(sat.epochdays))
        except Exception:
            continue
        current = None
        stamp = now
        while stamp <= horizon:
            try:
                r, _v = propagation.sgp4(sat, (stamp - epoch).total_seconds() / 60.0)
            except Exception:
                r = None
            if getattr(sat, "error", False):
                break
            if not r:
                stamp += step
                continue
            elev, az = _look_angles((r[0], r[1], r[2]), stamp, obs,
                                    observer_lat, observer_lon)
            if elev >= float(min_elevation_deg):
                if current is None:
                    current = {"rise": stamp, "peak": stamp, "azimuth_deg": az,
                               "max_elevation_deg": elev}
                elif elev > current["max_elevation_deg"]:
                    current.update(peak=stamp, azimuth_deg=az, max_elevation_deg=elev)
            elif current is not None:
                current["set"] = stamp
                current["duration_min"] = round((stamp - current["rise"]).total_seconds() / 60.0, 1)
                current["ongoing"] = False
                passes.append(_pass_row(it, name, current))
                current = None
            stamp += step
        if current is not None:
            current["set"] = None
            current["duration_min"] = round((horizon - current["rise"]).total_seconds() / 60.0, 1)
            current["ongoing"] = True
            passes.append(_pass_row(it, name, current))

    passes.sort(key=lambda p: p["rise"])
    with _PASS_CACHE_LOCK:
        for old in [k for k, (ts, _) in _PASS_CACHE.items() if mono - ts > 1800]:
            _PASS_CACHE.pop(old, None)
        _PASS_CACHE[cache_key] = (mono, passes)
    return passes[:limit]


def _pass_row(item: dict, name: str, current: dict) -> dict:
    rise = current["rise"]
    return {
        "norad": int(item.get("norad") or 0),
        "name": name,
        "rise": rise.isoformat(),
        "peak": current["peak"].isoformat(),
        "set": current["set"].isoformat() if current.get("set") else None,
        "max_elevation_deg": round(current["max_elevation_deg"], 1),
        "azimuth_deg": round(current["azimuth_deg"], 1),
        "duration_min": current["duration_min"],
        "ongoing": bool(current.get("ongoing")),
        "source": "CelesTrak SGP4",
    }


def layer_feed_items(db: Database, kinds: set[str] | tuple[str, ...], feed_ids: set[str] | tuple[str, ...] | None = None,
                      max_age_hours: float | None = None, limit: int = 2000) -> list[dict]:
    """Cached geolocated items from specific feed kinds/ids for dedicated map layers.
    Unlike generic map events, these preserve layer-specific fields (MMSI, cable id, camera status)."""
    kinds = set(kinds)
    feed_ids = set(feed_ids) if feed_ids else None
    out: list[dict] = []
    for f in feedstore.list_feeds(db, item_limit=None):
        fid = f.get("id")
        kind = f.get("kind")
        if feed_ids and fid not in feed_ids and kind not in kinds:
            continue
        if not feed_ids and kind not in kinds and fid not in (feed_ids or set()):
            continue
        if kind not in kinds and (not feed_ids or fid not in feed_ids):
            continue
        age_cap = max_age_hours
        if age_cap is None:
            age_cap = MAP_MAX_AGE_HOURS.get(fid, 365 * 24)
        for it in f.get("items") or []:
            lat = it.get("lat"); lon = it.get("lon")
            try:
                latf = float(lat) if lat is not None else None
                lonf = float(lon) if lon is not None else None
            except (TypeError, ValueError):
                latf = lonf = None
            if latf is None or lonf is None or not (-90 <= latf <= 90 and -180 <= lonf <= 180):
                continue
            age = _age_hours(it.get("time"))
            if age is not None and age > float(age_cap):
                continue
            row = {
                "id": f"{fid}:{it.get('id') or it.get('mmsi') or it.get('camera_id') or it.get('cable_id') or it.get('title')}",
                "feed_id": fid,
                "kind": it.get("kind") or kind,
                "source": it.get("source") or f.get("name") or "?",
                "title": it.get("title") or it.get("name") or it.get("vessel") or kind,
                "time": it.get("time"),
                "age_hours": age,
                "lat": latf, "lon": lonf,
                "url": it.get("url") or it.get("feed_url"),
                "summary": it.get("summary") or "",
                "status": it.get("status"),
                "feed_status": f.get("status"),
                "feed_fetched_at": f.get("fetched_at"),
            }
            for k in ("mmsi", "imo", "callsign", "vessel", "speed_kn", "course_deg", "heading_deg",
                      "cable_id", "capacity", "operator", "camera_id",
                      "alpr", "flock_tagged", "surveillance_type", "camera_type", "brand",
                      "operator_name", "direction", "surveillance_zone", "status",
                      "snapshot_url", "feed_type", "feedType", "frame_refresh_ms", "frameRefreshMs",
                      "credit", "license", "provider", "region",
                      "source_kind", "heading_confidence", "headingConfidence",
                      "pitch_deg", "fov_deg", "range_m", "mount_height_m", "ground_elevation_m",
                      "viewer_url", "city", "import_source", "stream_url"):
                if it.get(k) is not None and it.get(k) != "":
                    row[k] = it.get(k)
            out.append(row)
    out.sort(key=lambda x: _sort_time(x.get("time")), reverse=True)
    return out[:max(0, int(limit))]


def maritime_positions(db: Database, limit: int = 2000) -> list[dict]:
    return layer_feed_items(db, kinds={"maritime", "ais"}, limit=limit, max_age_hours=12)


def cable_features(db: Database, limit: int = 5000) -> list[dict]:
    return layer_feed_items(db, kinds={"cables"}, limit=limit, max_age_hours=365 * 24)


def cctv_features(db: Database, limit: int = 5000) -> list[dict]:
    return layer_feed_items(db, kinds={"cctv"}, limit=limit, max_age_hours=365 * 24)


def air_quality(db: Database) -> dict | None:
    best = None
    for f in feedstore.list_feeds(db):
        if f.get("kind") != "air_quality":
            continue
        for it in f.get("items") or []:
            best = it
            break
        if best:
            break
    if not best:
        return None
    return {
        "aqi": best.get("aqi"), "category": best.get("category"), "source": best.get("source"),
        "severity": best.get("severity"), "time": best.get("time"), "items": [best],
    }


def threat_index(db: Database, events: list[dict] | None = None,
                 lat: float | None = None, lon: float | None = None,
                 aqi: int | float | None = None) -> dict:
    """Weighted danger estimate over cached events.

    When a reference position is supplied, events are distance- and recency-
    weighted so distant or stale reports do not dominate the local picture.
    Weights are transparent heuristics, not a forecast product.
    """
    if events is None:
        events = live_events(db, limit=250)
    have_position = lat is not None and lon is not None \
        and -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0
    if have_position:
        latf, lonf = float(lat), float(lon)
    else:
        latf = lonf = 0.0

    total = 0.0
    by_source: dict[str, float] = {}
    by_sev: dict[str, int] = {}
    weighted_events: list[dict] = []
    near_count = 0
    local_critical = 0
    stale_excluded = 0

    for ev in events:
        sev = str(ev.get("severity") or "INFO").upper()
        base = SEVERITY_WEIGHT.get(sev, 0.0)
        if base <= 0:
            continue
        age = ev.get("age_hours")
        if age is None:
            age = _age_hours(ev.get("time"))
        rec = _recency_factor(ev.get("feed_id"), age)
        if rec <= 0:
            stale_excluded += 1
            continue
        cred = _credibility_factor(ev)
        kind_mult = KIND_THREAT_MULT.get(str(ev.get("kind") or ev.get("feed_id") or "").lower(), 1.0)
        dist = None
        dist_mult = 1.0
        is_near = False
        if have_position and ev.get("lat") is not None and ev.get("lon") is not None:
            try:
                dist = _distance_km(latf, lonf, float(ev["lat"]), float(ev["lon"]))
                dist_mult, is_near = _distance_factor(dist)
            except (TypeError, ValueError):
                dist_mult, is_near = 0.45, False
        contribution = base * rec * cred * kind_mult * dist_mult
        if contribution <= 0.2:
            continue
        total += contribution
        source_key = str(ev.get("source") or "?")
        by_source[source_key] = by_source.get(source_key, 0.0) + contribution
        by_sev[sev] = by_sev.get(sev, 0) + 1
        if is_near:
            near_count += 1
            if sev in CRITICAL_SEVERITIES:
                local_critical += 1
        weighted_events.append({
            **ev,
            "distance_km": round(dist, 1) if dist is not None else ev.get("distance_km"),
            "age_hours": age,
            "threat_contribution": round(contribution, 2),
        })

    aqi_contribution = 0.0
    aqi_label = None
    try:
        if aqi is not None:
            av = float(aqi)
            for limit, score_add, label in AQI_BANDS:
                if av <= limit:
                    aqi_contribution = float(score_add)
                    aqi_label = label
                    break
    except (TypeError, ValueError):
        aqi_contribution = 0.0
    total += aqi_contribution

    score = int(round(min(100.0, total)))
    if score >= 70:
        band = "SEVERE"
    elif score >= 45:
        band = "ELEVATED"
    elif score >= 25:
        band = "GUARDED"
    else:
        band = "NOMINAL"

    weighted_events.sort(key=lambda x: (-(x.get("threat_contribution") or 0), _sort_time(x.get("time")).timestamp() * -1))
    top = weighted_events[:8]

    return {
        "score": score, "band": band,
        "critical_count": sum(
            1 for e in events
            if SEVERITY_WEIGHT.get(str(e.get("severity") or "INFO").upper(), 0) > 0
            and (lat is None or lon is None or e.get("lat") is None or e.get("lon") is None
                 or _distance_km(float(lat), float(lon), float(e["lat"]), float(e["lon"])) <= 2000)
        ),
        "weighted_event_count": len(weighted_events),
        "near_count": near_count,
        "local_critical_count": local_critical,
        "events_scoped": bool(have_position),
        "by_source": {k: round(v, 1) for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])},
        "by_severity": by_sev,
        "aqi_contribution": round(aqi_contribution, 1),
        "aqi_label": aqi_label,
        "stale_excluded": stale_excluded,
        "factors": {
            "method": "distance_recency_credibility_weighted",
            "position_used": bool(have_position),
            "aqi_used": aqi is not None,
        },
        "top_events": top,
        "updated": utcnow(),
    }


def overview(db: Database, lat: float | None, lon: float | None, elevation_m: float = 0.0) -> dict:
    try:
        latf = float(lat) if lat is not None else None
        lonf = float(lon) if lon is not None else None
        if latf is None or lonf is None or not (-90.0 <= latf <= 90.0 and -180.0 <= lonf <= 180.0):
            latf = lonf = None
    except (TypeError, ValueError):
        latf = lonf = None
    events = live_events(db, limit=250)
    aq = air_quality(db)
    aq_val = (aq or {}).get("aqi")
    threat = threat_index(db, events, latf, lonf, aqi=aq_val)
    aircraft = live_aircraft(db)
    sats = live_satellites(db, latf, lonf) if latf is not None and lonf is not None else []
    
    # Network-backed modules are cached for an hour. Only attempt a live fetch
    # when connectivity is confirmed; offline reads use the last cached copy.
    online = tilecache.is_online()
    from . import nasa, newsapi, who_outbreaks
    if online:
        neo_summary = nasa.get_neo_summary()
        conflict_news = newsapi.fetch_conflict_news()
        disaster_news = newsapi.fetch_disaster_news()
        covid_stats = who_outbreaks.fetch_covid_stats()
        disease_summary = who_outbreaks.get_disease_summary()
    else:
        neo_summary = nasa.cached_neo_summary()
        conflict_news = newsapi.cached_conflict_news()
        disaster_news = newsapi.cached_disaster_news()
        covid_stats = who_outbreaks.cached_covid_stats()
        disease_summary = who_outbreaks.cached_disease_summary()
    
    feeds = feedstore.list_feeds(db)
    feeds_full = feedstore.list_feeds(db, item_limit=None)
    critical = [i for f in feeds_full for i in (f.get("items") or [])
                if str(i.get("severity") or "INFO").upper() in CRITICAL_SEVERITIES]
    sun = None
    try:
        from . import astro
        if latf is not None and lonf is not None:
            sun = astro.celestial(latf, lonf, float(elevation_m or 0.0))
    except Exception:
        sun = None
    war_items = _war_headlines(feeds_full)
    conflict_live = _conflict_feed_items(feeds_full, "gdelt_conflict", limit=40)
    conflict_verified = _conflict_feed_items(feeds_full, "ucdp_candidate", limit=40)
    war_stats = _war_summary(war_items, conflict_live, conflict_verified)

    # Get NASA NEO objects
    neo_objects = (nasa.fetch_neo_objects() if online else nasa.cached_neo_objects())[:10]

    return {
        "threat": threat,
        "events": events,
        "aircraft": aircraft,
        "satellites": sats,
        "air_quality": aq,
        "feeds": feeds,
        "critical_count": len(critical),
        "sun": sun,
        "war": war_items,
        "conflict_live": conflict_live,
        "conflict_verified": conflict_verified,
        "war_summary": war_stats,
        "neo_summary": neo_summary,
        "neo_objects": neo_objects,
        "conflict_news": conflict_news[:10],  # Top 10 conflict stories
        "disaster_news": disaster_news[:10],  # Top 10 disaster stories
        "covid_stats": covid_stats,
        "disease_summary": disease_summary,
        "online": online,
        "updated": utcnow(),
    }


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def _recency_factor(feed_id: str | None, age_hours: float | None) -> float:
    if age_hours is None:
        return 0.7  # unknown age: discount but don't discard
    if age_hours < 0:
        age_hours = 0.0
    max_age = THREAT_MAX_AGE_HOURS.get(feed_id or "", 45 * 24)
    if age_hours > max_age:
        return 0.0
    half_life = THREAT_HALF_LIFE_HOURS.get(feed_id or "", 72)
    return max(0.15, math.pow(0.5, age_hours / max(1.0, half_life)))


def _distance_factor(dist_km: float | None) -> tuple[float, bool]:
    """Return (multiplier, is_near). Near means operationally relevant (<100km)."""
    if dist_km is None:
        return 0.35, False
    if dist_km <= 25:
        return 1.0, True
    if dist_km <= 100:
        return 0.85, True
    if dist_km <= 250:
        return 0.5, False
    if dist_km <= 500:
        return 0.2, False
    if dist_km <= 1000:
        return 0.05, False
    if dist_km <= 2000:
        return 0.01, False
    return 0.0, False


def _credibility_factor(ev: dict) -> float:
    fid = ev.get("feed_id")
    base = FEED_CREDIBILITY.get(fid, 0.8)
    if ev.get("verified"):
        base = max(base, 0.95)
    if fid == "gdelt_conflict":
        precision = str(ev.get("precision") or "").lower()
        if "country" in precision:
            base *= 0.7
        elif "admin" in precision or "region" in precision:
            base *= 0.85
    return max(0.35, min(1.0, base))


def cbrn_status(db: Database, lat: float, lon: float) -> dict:
    """CBRN common operating picture from cached public feeds plus the local
    radiological log. Public feeds are advisory and may be old; the local
    instrument log always takes precedence."""
    online = tilecache.is_online()
    feed_list = feedstore.list_feeds(db)
    by_id = {f.get("id"): f for f in feed_list}

    def items(fid: str) -> list[dict]:
        return (by_id.get(fid) or {}).get("items") or []

    sw = by_id.get("swpc_scales") or {}
    kp = None
    for it in reversed(items("swpc_kp")):
        try:
            kp = float(it.get("kp"))
            break
        except (TypeError, ValueError):
            continue
    scales = [
        {"label": it.get("title"), "level": it.get("level"), "severity": it.get("severity")}
        for it in items("swpc_scales")
    ]

    rad_feed = by_id.get("safecast") or {}
    public_rad = []
    for it in items("safecast")[:24]:
        dist = None
        try:
            if it.get("lat") is not None and it.get("lon") is not None:
                dist = _distance_km(lat, lon, float(it["lat"]), float(it["lon"]))
        except (TypeError, ValueError):
            dist = None
        row = dict(it)
        row["distance_km"] = round(dist, 1) if dist is not None else None
        public_rad.append(row)
    public_rad.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0))

    local = db.one("SELECT * FROM rad_log ORDER BY ts DESC LIMIT 1")
    air = by_id.get("open_meteo_air") or {}
    air_item = (items("open_meteo_air") or [None])[0]

    hazards = [
        i for i in items("eonet")
        if str(i.get("category") or "") in ("Volcanoes", "Wildfires", "Dust and Haze")
    ][:12]

    return {
        "online": online,
        "updated": utcnow(),
        "space_weather": {
            "kp": kp,
            "scales": scales,
            "items": items("swpc_scales")[:6],
            "fetched_at": sw.get("fetched_at"),
            "status": sw.get("status"),
        },
        "radiation": {
            "public": public_rad[:8],
            "local": dict(local) if local else None,
            "fetched_at": rad_feed.get("fetched_at"),
            "status": rad_feed.get("status"),
        },
        "volcano": {
            "items": items("smithsonian_volcano")[:8],
            "fetched_at": (by_id.get("smithsonian_volcano") or {}).get("fetched_at"),
        },
        "hazards": {
            "items": hazards,
            "fetched_at": (by_id.get("eonet") or {}).get("fetched_at"),
        },
        "air_quality": {
            "current": air_item,
            "fetched_at": air.get("fetched_at"),
            "status": air.get("status"),
        },
    }
