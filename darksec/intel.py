"""Online intelligence layer: live feeds, satellite positions, threat index.

Everything here is optional and public-source-only. The console never requires
network access; every snapshot is cached in the local SQLite database through
the generic feed store, so the UI can render offline with clear age labels.

Sources used by this layer:
  - USGS significant earthquakes (geo events)
  - NOAA SWPC planetary K-index / space weather scales
  - NWS active alerts (US)
  - GDACS disaster alerts, WHO DON, ReliefWeb updates (RSS)
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
from datetime import datetime, timezone

from . import feeds as feedstore
from . import tilecache
from .db import Database, utcnow

CRITICAL_SEVERITIES = ("CRITICAL", "SEVERE", "EXTREME")
SEVERITY_WEIGHT = {"CRITICAL": 40.0, "SEVERE": 25.0, "EXTREME": 40.0, "ALERT": 12.0, "INFO": 0.0}

# Positional feeds are rendered as their own map layers, not as point events.
NON_EVENT_KINDS = {"opensky", "celestrak", "air_quality", "weather"}

# How long an event remains useful on the situational map. GDELT is a live
# 15-minute slice; UCDP is a monthly verified release and necessarily lags.
MAP_MAX_AGE_HOURS = {
    "gdelt_conflict": 48,
    "ucdp_candidate": 120 * 24,
    "usgs_quakes": 7 * 24,
    "gdacs": 45 * 24,
    "eonet": 90 * 24,
    "nws_alerts": 3 * 24,
    "safecast": 30 * 24,
}
MAP_DEFAULT_MAX_AGE_HOURS = 120 * 24

CONFLICT_NEWS_FEEDS = {
    "bbc_world", "guardian_world", "france24_world", "dw_news",
    "aljazeera_world", "reliefweb", "crisisgroup",
}
_CONFLICT_TERMS = re.compile(
    r"\b(war|wars|conflict|airstrike|air strike|missile|drone|shelling|"
    r"offensive|ceasefire|truce|troops|military|army|rebel|insurgent|"
    r"militant|jihadist|paramilitary|attack|attacked|killed|casualties|"
    r"frontline|front line|invasion|occupied|occupation|bomb|bombing|"
    r"explosion|hostage|refugee|evacuat|massacre|genocide|siege|coup|"
    r"junta|sanctions|weapons|artillery|combat|clashes|strike)\b",
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
    return {
        "id": f"{feed.get('id')}:{item.get('id') or item.get('title')}",
        "source": item.get("source") or feed.get("name") or "?",
        "feed_id": feed.get("id"),
        "kind": item.get("kind") or feed.get("kind"),
        "title": item.get("title") or "Event",
        "severity": str(item.get("severity") or "INFO").upper(),
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


def live_satellites(db: Database, observer_lat: float, observer_lon: float, limit: int = 40) -> list[dict]:
    """Propagate the latest CelesTrak TLE snapshot and return passes over the observer."""
    out = []
    # list_feeds intentionally omits the raw payload from API responses, so
    # read the cached TLE blob directly here.
    row = db.one("SELECT payload FROM feeds WHERE kind='celestrak' "
                 "ORDER BY fetched_at DESC LIMIT 1")
    if not row or not row["payload"]:
        return out
    try:
        payload = json.loads(row["payload"])
        items = payload.get("items") or []
    except Exception:
        return out
    from datetime import datetime as _dt, timedelta as _td
    from sgp4.api import Satrec
    from sgp4 import propagation
    now = datetime.now(timezone.utc)
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
            obs_lat = math.radians(observer_lat); obs_lon = math.radians(observer_lon)
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
    return out[:limit]


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
                 lat: float | None = None, lon: float | None = None) -> dict:
    """Weighted danger estimate over cached events.

    When a reference position is supplied, events are distance-weighted so a
    bombing 9,000 km away does not drive the operator's local picture the way
    one inside the same region does. Without a position every event counts
    equally (global view). Not a forecast product.
    """
    if events is None:
        events = live_events(db, limit=200)
    weights = {"CRITICAL": 40.0, "SEVERE": 25.0, "ALERT": 12.0, "EXTREME": 40.0, "INFO": 0.0}
    have_position = lat is not None and lon is not None \
        and -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0
    total = 0.0
    by_source: dict[str, float] = {}
    by_sev: dict[str, int] = {}
    near_count = 0
    for ev in events:
        sev = str(ev.get("severity") or "INFO").upper()
        w = weights.get(sev, 0.0)
        if w <= 0:
            continue
        mult = 1.0
        if have_position and ev.get("lat") is not None and ev.get("lon") is not None:
            try:
                dist = _distance_km(float(lat), float(lon), float(ev["lat"]), float(ev["lon"]))
            except (TypeError, ValueError):
                dist = None
            if dist is not None:
                if dist <= 250.0:
                    mult = 1.0
                elif dist >= 2000.0:
                    mult = 0.05
                else:
                    mult = 0.15 + 0.85 * (1.0 - (dist - 250.0) / 1750.0)
                if dist <= 500.0:
                    near_count += 1
        total += w * mult
        source_key = str(ev.get("source") or "?")
        by_source[source_key] = by_source.get(source_key, 0.0) + w * mult
        by_sev[sev] = by_sev.get(sev, 0) + 1
    score = int(round(min(100.0, total)))
    # Higher weighted danger means a higher threat band. The previous order
    # was inverted, which labelled a quiet picture SEVERE and a bad one NOMINAL.
    band = "SEVERE" if score >= 70 else "ELEVATED" if score >= 45 else "GUARDED" if score >= 25 else "NOMINAL"
    top = [e for e in events if SEVERITY_WEIGHT.get(str(e.get("severity") or "INFO").upper(), 0) > 0][:5]
    return {
        "score": score, "band": band,
        "critical_count": sum(1 for e in events if SEVERITY_WEIGHT.get(str(e.get("severity") or "INFO").upper(), 0) > 0),
        "near_count": near_count,
        "events_scoped": bool(have_position),
        "by_source": {k: round(v, 1) for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])},
        "by_severity": by_sev,
        "top_events": top,
        "updated": utcnow(),
    }


def overview(db: Database, lat: float, lon: float, elevation_m: float = 0.0) -> dict:
    events = live_events(db, limit=200)
    threat = threat_index(db, events, lat, lon)
    aircraft = live_aircraft(db)
    sats = live_satellites(db, lat, lon)
    aq = air_quality(db)
    
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
        sun = astro.celestial(float(lat or 0.0), float(lon or 0.0), float(elevation_m or 0.0))
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
    kpf = by_id.get("swpc_kp") or {}
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
