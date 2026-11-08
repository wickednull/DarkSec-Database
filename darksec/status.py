"""Local system status, hardware detection and operational readiness.

These functions are pure read-only views over the local machine, the database
and the cached feed/intel layers. They are kept out of the FastAPI factory so
they can be tested and reused without starting the server.
"""
from __future__ import annotations

import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from . import config, feeds, intel, sdr, sitrep
from .db import Database, utcnow




# --------------------------------------------------------------------------
# Hardware cache
# --------------------------------------------------------------------------

_hw_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def hardware(force: bool = False) -> dict:
    now = time.time()
    if not force and _hw_cache["data"] and now - _hw_cache["ts"] < 60:
        return _hw_cache["data"]
    data = sdr.detect()
    _hw_cache.update(ts=now, data=data)
    return data


def system_info() -> dict:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(config.DATA_DIR))
    try:
        battery = psutil.sensors_battery()
    except Exception:
        battery = None
    try:
        boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc).isoformat()
    except Exception:
        boot = None
    return {
        "hostname": socket.gethostname(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "cpu_count": psutil.cpu_count(logical=True),
        "load_1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
        "memory_total": vm.total,
        "memory_used": vm.used,
        "memory_pct": vm.percent,
        "disk_total": du.total,
        "disk_used": du.used,
        "disk_free": du.free,
        "disk_pct": round(du.used / du.total * 100, 1) if du.total else None,
        "battery_pct": round(battery.percent, 1) if battery else None,
        "battery_plugged": battery.power_plugged if battery else None,
        "boot_time": boot,
        "uptime_s": int(time.time() - psutil.boot_time()) if boot else None,
        "data_dir": str(config.DATA_DIR),
        "db_bytes": config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0,
        "recordings_bytes": sum(p.stat().st_size for p in Path(config.RECORDING_DIR).glob("*") if p.is_file()),
    }


# --------------------------------------------------------------------------
# SITREP
# --------------------------------------------------------------------------

def readiness(db: Database) -> dict:
    """Automated operational readiness derived from local state and cached intel.

    Readiness is scored as 100 minus capped operational risk across:
      THREATS      - open incidents + location-weighted cached threat signals
      SUSTAINMENT - low/expiring supplies
      INTEL        - feed coverage and freshness (not event severity)
      POSITION     - fix validity for location-scoped decisions

    This is a transparent estimate, not a forecast.
    """
    s = db.all_settings()
    now = datetime.now(timezone.utc)
    try:
        lat = float(s.get("lat")) if s.get("lat") is not None else None
        lon = float(s.get("lon")) if s.get("lon") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    have_position = lat is not None and lon is not None and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0

    incidents = [dict(r) for r in db.query("SELECT * FROM incidents WHERE resolved=0")]
    crit_inc = [i for i in incidents if str(i.get("severity") or "").upper() in ("CRITICAL", "SEVERE", "EXTREME")]
    warn_inc = [i for i in incidents if str(i.get("severity") or "").upper() in ("WARNING", "ALERT")]

    local_threat = {}
    crit_events: list[dict] = []
    try:
        if have_position:
            local_threat = intel.threat_index(db, lat=lat, lon=lon)
            crit_events = [e for e in (local_threat.get("top_events") or [])
                           if str(e.get("severity") or "").upper() in ("CRITICAL", "SEVERE", "EXTREME")
                           and (e.get("distance_km") is None or float(e.get("distance_km") or 9999) <= 150)]
        else:
            local_threat = intel.threat_index(db)
    except Exception:
        local_threat = {}
        crit_events = []
        if have_position:
            try:
                crit_events = [e for e in sitrep.events_near(db, lat, lon, radius_km=100.0, limit=50)
                               if str(e.get("severity") or "").upper() in ("CRITICAL", "SEVERE", "EXTREME")]
            except Exception:
                crit_events = []

    try:
        feed_summary = feeds.summary(db)
        critical_intel_count = int(feed_summary.get("critical_count") or 0)
    except Exception:
        feed_summary = {"feeds": []}
        critical_intel_count = 0
    feed_rows = [f for f in (feed_summary.get("feeds") or [])
                 if f.get("kind") not in ("opensky", "celestrak")]
    stale_feeds = sum(1 for f in feed_rows if f.get("status") == "STALE")
    cached_feeds = sum(1 for f in feed_rows if f.get("status") in ("OK", "STALE"))
    feed_coverage = (cached_feeds / len(feed_rows)) if feed_rows else 1.0

    low_stock = [dict(r) for r in db.query("SELECT * FROM inventory WHERE min_qty > 0 AND qty <= min_qty ORDER BY (qty-min_qty) LIMIT 20")]
    expiring: list[dict] = []
    for it in db.query("SELECT * FROM inventory ORDER BY name LIMIT 24"):
        exp = it.get("expires")
        if not exp:
            continue
        try:
            t = datetime.fromisoformat(str(exp))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            days = (t - now).days
            if 0 <= days <= 14:
                expiring.append(dict(it, _days=days))
        except Exception:
            continue

    source = str(s.get("position_source") or "unverified").lower()
    fix_at = s.get("position_fix_at") or s.get("position_updated_at")
    age_seconds = None
    try:
        fix_dt = datetime.fromisoformat(str(fix_at).replace("Z", "+00:00"))
        if fix_dt.tzinfo is None:
            fix_dt = fix_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - fix_dt).total_seconds()))
    except (TypeError, ValueError):
        age_seconds = None
    # A manual or geocoded coordinate is usable but not a verified fix; a live
    # GPS/phone fix is only trusted while it is reasonably fresh. Readiness
    # should reflect decision quality, not pretend the operator has no position.
    gps_stale = (source == "gpsd" and (age_seconds is None or age_seconds > 6 * 3600)) or (source == "phone" and (age_seconds is None or age_seconds > 24 * 3600))
    if not have_position:
        position_risk = 15.0
    elif source == "unverified":
        position_risk = 8.0
    elif gps_stale:
        position_risk = 7.0
    elif source in ("manual", "geocoded"):
        position_risk = 4.0
    else:
        position_risk = 0.0

    # THREATS: combine operator incidents with location-weighted cached intel.
    local_threat_score = float(local_threat.get("score") or 0.0)
    local_crit = int(local_threat.get("local_critical_count") or 0)
    threat_from_intel = min(20.0, local_threat_score * 0.22) if local_threat.get("events_scoped") else min(12.0, local_threat_score * 0.12)
    threat_risk = min(30.0, 18.0 * len(crit_inc) + 14.0 * local_crit + 8.0 * len(crit_events[:3]) + threat_from_intel)
    threat_risk += min(10.0, 5.0 * len(warn_inc))
    # If the weighted index could not scope any events, keep a small fallback
    # signal for cached critical feed items. When the index did scope them they
    # are already counted above, so this avoids double-counting the same event.
    if not local_threat.get("events_scoped"):
        threat_risk += min(6.0, 1.5 * min(4, critical_intel_count))
    threat_risk = min(35.0, threat_risk)

    supply_risk = min(25.0, 7.0 * len(low_stock) + 4.0 * len(expiring))
    # INTEL measures coverage and freshness, not event severity. Critical
    # events are a THREATS concern; counting them here double-penalised the
    # operator for the same cached headline.
    intel_risk = min(15.0, 8.0 * (1.0 - feed_coverage) + 1.0 * min(5, stale_feeds))
    if feed_rows and cached_feeds == 0:
        intel_risk = max(intel_risk, 6.0)
    if local_threat.get("factors", {}).get("position_used") is False and have_position is False:
        intel_risk = min(15.0, intel_risk + 2.0)

    # Normalize the four categories onto a 100-point risk envelope.
    threat_risk = min(40.0, threat_risk * (40.0 / 35.0))
    supply_risk = min(30.0, supply_risk * (30.0 / 25.0))
    intel_risk = min(15.0, intel_risk)
    position_risk = min(15.0, position_risk)
    total_risk = threat_risk + supply_risk + intel_risk + position_risk
    score = round(max(0.0, min(100.0, 100.0 - total_risk)), 0)
    band = "NOMINAL" if score >= 80 else "DEGRADED" if score >= 60 else "ELEVATED" if score >= 40 else "CRITICAL"

    categories = [
        {"category": "THREATS", "weight": 40.0, "risk": round(threat_risk, 1), "score": round(max(0.0, 100.0 - threat_risk * (100.0 / 40.0)), 1), "source": "automated"},
        {"category": "SUSTAINMENT", "weight": 30.0, "risk": round(supply_risk, 1), "score": round(max(0.0, 100.0 - supply_risk * (100.0 / 30.0)), 1), "source": "automated"},
        {"category": "INTEL", "weight": 15.0, "risk": round(intel_risk, 1), "score": round(max(0.0, 100.0 - intel_risk * (100.0 / 15.0)), 1), "source": "automated"},
        {"category": "POSITION", "weight": 15.0, "risk": round(position_risk, 1), "score": round(max(0.0, 100.0 - position_risk * (100.0 / 15.0)), 1), "source": "automated"},
    ]
    categories = sorted(categories, key=lambda c: c["score"])

    signals: list[dict] = []
    for i in crit_inc[:3]:
        signals.append({"category": "THREATS", "text": f"Open critical incident: {i.get('title')} ({i.get('ts')})"})
    if local_threat.get("events_scoped"):
        if local_crit:
            signals.append({"category": "THREATS", "text": f"{local_crit} critical cached event(s) near position (weighted threat {int(local_threat_score)}/100)"})
        elif local_threat_score >= 45:
            signals.append({"category": "THREATS", "text": f"Location-weighted cached threat elevated ({int(local_threat_score)}/100, {local_threat.get('band')})"})
    for e in crit_events[:3]:
        d = e.get("distance_km")
        dtxt = f" - {d} km away" if d is not None else ""
        signals.append({"category": "THREATS", "text": f"Critical nearby intel: {e.get('title')}{dtxt}"})
    for it in low_stock[:5]:
        signals.append({"category": "SUSTAINMENT", "text": f"Low stock: {it.get('name')} {it.get('qty')}/{it.get('min_qty')}"})
    for it in expiring[:3]:
        signals.append({"category": "SUSTAINMENT", "text": f"Supply expiring soon: {it.get('name')} in {it.get('_days')} days"})
    if critical_intel_count and not local_threat.get("events_scoped"):
        signals.append({"category": "THREATS",
                        "text": f"{critical_intel_count} critical cached feed item(s); confirm with official sources"})
    if stale_feeds >= 3:
        signals.append({"category": "INTEL", "text": f"{stale_feeds} intel feeds are stale; refresh when safe/networked"})
    if feed_rows and feed_coverage < 0.5:
        signals.append({"category": "INTEL",
                        "text": f"Only {cached_feeds}/{len(feed_rows)} intel feeds cached; run SYNC NOW when networked"})
    if not have_position:
        signals.append({"category": "POSITION", "text": "No position set; location-scoped threat and map results are unavailable"})
    elif source == "unverified":
        signals.append({"category": "POSITION",
                        "text": "Position source is unverified; location-scoped results may be inaccurate"})
    elif gps_stale:
        signals.append({"category": "POSITION",
                        "text": f"{source} fix is stale; re-acquire before relying on location-scoped results"})
    elif source in ("manual", "geocoded"):
        signals.append({"category": "POSITION",
                        "text": f"Position is {source} (no GPS accuracy guarantee); verify against terrain and a paper map"})

    return {
        "score": int(score),
        "band": band,
        "source": "automated",
        "signals": signals,
        "categories": categories,
        "threat": {"score": int(local_threat_score), "band": local_threat.get("band"), "events_scoped": bool(local_threat.get("events_scoped"))},
        "updated": utcnow(),
    }
