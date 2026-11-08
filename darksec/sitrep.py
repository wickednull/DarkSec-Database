"""Situational awareness synthesis for the operator.

This is the core "what matters to me right now" engine. It takes the raw
intel feed data and produces a synthesized brief tailored to the operator's
position, readiness state, and resources.

Design principle: under stress, operators need decisions, not data.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

from . import feeds as feedstore
from . import intel as intelstore
from .db import Database, utcnow

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in kilometers."""
    try:
        lat1, lon1, lat2, lon2 = (float(lat1), float(lon1), float(lat2), float(lon2))
    except (TypeError, ValueError):
        return float("inf")
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Proximity-filtered events
# ---------------------------------------------------------------------------

def events_near(db: Database, lat: float, lon: float,
                radius_km: float = 500.0, limit: int = 25) -> list[dict]:
    """Return cached events with coordinates, within radius_km of (lat, lon),
    sorted by distance ascending, then severity descending."""
    out = []
    for f in feedstore.list_feeds(db):
        for it in f.get("items") or []:
            if it.get("lat") is None or it.get("lon") is None:
                continue
            try:
                d = haversine_km(lat, lon, float(it["lat"]), float(it["lon"]))
            except Exception:
                continue
            if d > radius_km:
                continue
            out.append({
                "id": f"{f.get('id')}:{it.get('title')}",
                "source": it.get("source") or f.get("name") or "?",
                "feed_id": f.get("id"),
                "kind": f.get("kind"),
                "title": it.get("title") or "Event",
                "severity": str(it.get("severity") or "INFO").upper(),
                "time": it.get("time"),
                "lat": float(it["lat"]), "lon": float(it["lon"]),
                "mag": it.get("magnitude") or it.get("mag"),
                "distance_km": round(d, 1),
                "url": it.get("url"),
                "summary": it.get("summary") or "",
                "place": it.get("place") or "",
                "depth_km": it.get("depth_km"),
            })
    # Sort: critical first, then nearest
    sev_rank = {"CRITICAL": 0, "SEVERE": 1, "EXTREME": 0, "ALERT": 2, "INFO": 3}
    out.sort(key=lambda x: (sev_rank.get(x["severity"], 9), x["distance_km"]))
    return out[:limit]


# ---------------------------------------------------------------------------
# Action synthesis
# ---------------------------------------------------------------------------

# Urgency: 0 = immediate, 1 = within hours, 2 = within a day, 3 = advisory.

HAZARD_TERMS = {
    "wildfire": ("wildfire", "fire", "smoke"),
    "volcano": ("volcano", "volcanic", "ash"),
    "flood": ("flood", "flash flood", "storm surge"),
    "storm": ("tornado", "hurricane", "cyclone", "typhoon", "severe thunderstorm", "storm"),
    "chemical_air": ("dust", "haze", "smoke plume", "air quality"),
}


def _ev_age_hours(ev: dict) -> float | None:
    age = ev.get("age_hours")
    if age is not None:
        try:
            return float(age)
        except (TypeError, ValueError):
            pass
    t = ev.get("time")
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return None


def _match_hazard(text: str) -> str | None:
    lower = str(text or "").lower()
    for label, terms in HAZARD_TERMS.items():
        if any(term in lower for term in terms):
            return label
    return None


def _rules_nearby_events(events: list[dict]) -> list[dict]:
    actions = []
    seen: set[str] = set()
    for ev in events:
        d = float(ev.get("distance_km") or 1e9)
        sev = str(ev.get("severity") or "INFO").upper()
        age = _ev_age_hours(ev)
        if age is not None and age > 72 and sev not in ("CRITICAL", "SEVERE", "EXTREME"):
            continue
        mag = ev.get("mag")
        try:
            mag = float(mag) if mag is not None else 0.0
        except (TypeError, ValueError):
            mag = 0.0
        kind = str(ev.get("kind") or ev.get("feed_id") or "")
        title = str(ev.get("title") or "Event")
        key = f"{kind}:{title}:{round(d)}:{sev}"
        if key in seen:
            continue

        if kind == "usgs" and mag and d <= 150:
            if mag >= 6.5 and d <= 120:
                actions.append({"text": f"M{mag:.1f} earthquake {d:.0f} km away — drop/cover/hold; check injuries, gas, water and structural damage; expect aftershocks.", "urgency": 0, "_k": key})
            elif mag >= 5.0 and d <= 150:
                actions.append({"text": f"M{mag:.1f} earthquake {d:.0f} km away — secure loose items, inspect utilities, and monitor aftershocks.", "urgency": 1, "_k": key})
            elif mag >= 4.0 and d <= 75:
                actions.append({"text": f"M{mag:.1f} earthquake {d:.0f} km away — likely locally felt; monitor for aftershocks and verify infrastructure.", "urgency": 2, "_k": key})

        if kind == "nws" and sev in ("CRITICAL", "SEVERE", "EXTREME") and d <= 200:
            hazard = _match_hazard(title + " " + str(ev.get("summary") or ""))
            extra = {
                "flood": " Move to higher ground and avoid flooded routes.",
                "storm": " Shelter in a sturdy interior location away from windows.",
                "wildfire": " Prepare for smoke/reduced visibility; follow evacuation guidance.",
            }.get(hazard or "", " Verify the official alert polygon and follow local guidance.")
            actions.append({"text": f"Severe weather alert {d:.0f} km away: {title}.{extra}", "urgency": 0 if sev in ("CRITICAL", "EXTREME") else 1, "_k": key})

        if (kind == "conflict" or ev.get("feed_id") in ("gdelt_conflict", "ucdp_candidate")) and d <= 150:
            if sev in ("CRITICAL", "SEVERE", "EXTREME") and d <= 50 and (age is None or age <= 48):
                actions.append({"text": f"Recent high-severity conflict report {d:.0f} km away: {title}. Avoid the area, verify via multiple sources, and review movement/COMSEC plans.", "urgency": 0, "_k": key})
            elif d <= 100 and (age is None or age <= 72):
                verified = "verified" if ev.get("verified") else "unverified media"
                actions.append({"text": f"Conflict activity reported {d:.0f} km away ({verified}): {title}. Treat location as approximate and reassess routes.", "urgency": 1, "_k": key})

        if ev.get("feed_id") == "safecast" and d <= 50 and (age is None or age <= 30 * 24):
            actions.append({"text": f"Public radiation measurement reported {d:.0f} km away ({title}). Compare with local instrument readings before taking protective action.", "urgency": 2, "_k": key})

        hazard = _match_hazard(title + " " + str(ev.get("summary") or "") + " " + str(ev.get("event_type") or ""))
        matched_specific = False
        if hazard and d <= 120 and sev in ("CRITICAL", "SEVERE", "EXTREME", "ALERT"):
            matched_specific = True
            if hazard == "wildfire":
                actions.append({"text": f"Wildfire/smoke hazard {d:.0f} km away: {title}. Monitor wind, air quality, and evacuation routes.", "urgency": 1 if d <= 40 else 2, "_k": key})
            elif hazard == "volcano":
                actions.append({"text": f"Volcanic/ash hazard {d:.0f} km away: {title}. Prepare for ashfall impacts to air, water, vehicles and breathing.", "urgency": 1 if d <= 60 else 2, "_k": key})
            elif hazard == "flood":
                actions.append({"text": f"Flood-related hazard {d:.0f} km away: {title}. Avoid low crossings and keep egress routes to higher ground.", "urgency": 1 if d <= 60 else 2, "_k": key})

        # Only add generic fallback when no specific nearby rule already explained it.
        if not matched_specific and sev in ("CRITICAL", "SEVERE", "EXTREME") and d <= 25 and (age is None or age <= 48):
            if not any(a.get("_k") == key and a.get("urgency", 9) <= 1 for a in actions):
                actions.append({"text": f"Critical event within 25 km: {title} ({ev.get('source') or 'source unknown'}). Verify immediately and prioritize life safety.", "urgency": 0, "_k": key})

        seen.add(key)

    return actions


def _rules_environmental_intel(db: Database, lat: float, lon: float) -> list[dict]:
    actions = []
    feeds = {f.get("id"): f for f in feedstore.list_feeds(db)}

    # Space weather is global; events_near cannot see it.
    sw = feeds.get("swpc_scales") or {}
    for item in (sw.get("items") or [])[:6]:
        text = str(item.get("title") or "")
        sev = str(item.get("severity") or "INFO").upper()
        lower = text.lower()
        if "geomagnetic" in lower and sev in ("CRITICAL", "SEVERE", "ALERT"):
            actions.append({"text": f"Space weather: {text}. Expect possible HF degradation, navigation/GPS uncertainty, and power-grid sensitivity in severe cases.", "urgency": 1 if sev == "ALERT" else 0})
        if "radio blackout" in lower and sev in ("CRITICAL", "SEVERE"):
            actions.append({"text": f"Space weather: {text}. Plan for HF radio disruption on sunlit paths.", "urgency": 1})

    aq = feeds.get("open_meteo_air") or {}
    aq_item = ((aq.get("items") or [None])[0]) or {}
    try:
        aqi = float(aq_item.get("aqi"))
    except (TypeError, ValueError):
        aqi = None
    if aqi is not None:
        if aqi >= 151:
            actions.append({"text": f"Air quality unhealthy (AQI {aqi:.0f}). Limit exertion, protect breathing, and consider sealing indoor air if practical.", "urgency": 1 if aqi < 301 else 0})
        elif aqi >= 101:
            actions.append({"text": f"Air quality elevated (AQI {aqi:.0f}). Sensitive tasks and prolonged outdoor exertion may need adjustment.", "urgency": 2})

    local_rad = db.one("SELECT * FROM rad_log ORDER BY ts DESC LIMIT 1")
    if local_rad:
        try:
            dose = float(local_rad.get("dose_rate_usv_h") or 0)
            if dose >= 5.0:
                actions.append({"text": f"Local radiation log elevated ({dose:.2f} µSv/h). Confirm instrument calibration, increase distance/shielding, and limit exposure time.", "urgency": 0 if dose >= 20 else 1})
        except (TypeError, ValueError):
            pass
    return actions


def _rules_intel_quality(db: Database) -> list[dict]:
    actions = []
    try:
        summary = feedstore.summary(db)
    except Exception:
        return actions
    stale_critical = 0
    for f in summary.get("feeds") or []:
        if f.get("kind") in ("opensky", "celestrak"):
            continue
        if f.get("status") == "STALE" and (f.get("count") or 0) > 0:
            stale_critical += 1
    if stale_critical >= 4:
        actions.append({"text": f"{stale_critical} cached intel feeds are stale. Refresh when safe/networked; verify before movement or operational decisions.", "urgency": 2})
    return actions


def _rules_readiness(ready: dict) -> list[dict]:
    """Generate readiness-driven actions from automated readiness scores."""
    actions = []
    if not ready:
        return actions
    cats = ready.get("categories") if isinstance(ready, dict) else ready
    entries: list[tuple[str, float]] = []
    for c in (cats or []):
        if isinstance(c, dict):
            k = str(c.get("category") or "").lower()
            v = c.get("score")
        else:
            k, v = str(c), c
        try:
            entries.append((k, float(v)))
        except (TypeError, ValueError):
            continue
    for k, score in sorted(entries, key=lambda x: x[1]):
        if score <= 60:
            label = {"threats": "Threat environment", "sustainment": "Sustainment", "intel": "Intel cache", "position": "Position"}.get(k, k.replace("_", " ").title())
            actions.append({"text": f"{label} readiness low ({score:.0f}/100). Automated risk signal active; review local intel and resources.", "urgency": 1 if score < 40 else 2})
    overall = None
    try:
        overall = float(ready.get("score")) if isinstance(ready, dict) else None
    except (TypeError, ValueError):
        overall = None
    if overall is not None and overall < 60 and not actions:
        actions.append({"text": f"Operational readiness degraded ({overall:.0f}/100). Prioritize highest-risk category before movement.", "urgency": 1})
    return actions


def _rules_supplies(inventory_items: list[dict]) -> list[dict]:
    """Days-of-supply estimate and rationing advice."""
    actions = []
    now = datetime.now(timezone.utc)
    for it in inventory_items:
        try:
            qty = float(it.get("qty") or 0)
            min_qty = float(it.get("min_qty") or 0)
            unit = it.get("unit") or "ea"
            name = it.get("name") or "?"
            expires = it.get("expires")
        except Exception:
            continue
        if qty <= 0 and min_qty > 0:
            actions.append({"text": f"{name} depleted (0 {unit}). Resupply or substitute.", "urgency": 1})
        elif qty <= min_qty:
            actions.append({"text": f"{name} at/below minimum ({qty} {unit}). Resupply.", "urgency": 1})
        if expires:
            try:
                exp = datetime.fromisoformat(str(expires))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                days_left = (exp - now).days
                if 0 <= days_left <= 7:
                    actions.append({"text": f"{name} expires in {days_left} day(s). Use or rotate.", "urgency": 2})
                elif days_left < 0:
                    actions.append({"text": f"{name} expired {abs(days_left)} day(s) ago. Discard or isolate per policy.", "urgency": 1})
            except Exception:
                pass
    return actions


def _rules_sun(sun: Optional[dict], lat: float, lon: float) -> list[dict]:
    """Daylight / nightfall timing advice."""
    actions = []
    if not sun:
        return actions
    sun_obj = (sun.get("sun") or {})
    next_set = sun_obj.get("next_set")
    next_rise = sun_obj.get("next_rise")
    now = datetime.now(timezone.utc)
    if next_set:
        try:
            t = datetime.fromisoformat(str(next_set))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            hours = max(0, (t - now).total_seconds() / 3600)
            if hours < 2:
                actions.append({"text": f"Sunset in {hours:.1f}h — complete daylight-critical tasks, light, and movement planning now.", "urgency": 2})
        except Exception:
            pass
    if next_rise:
        try:
            t = datetime.fromisoformat(str(next_rise))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            hours = max(0, (t - now).total_seconds() / 3600)
            if 0 < hours < 1.5:
                actions.append({"text": f"Sunrise in {hours:.1f}h — prepare early movement, charging, and visual checks.", "urgency": 2})
        except Exception:
            pass
    return actions


def _dedupe_actions(actions: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for a in actions:
        text = str(a.get("text") or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text.lower())[:220]
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "urgency": int(a.get("urgency", 3)), "source": a.get("source")})
    return out


def synthesize_actions(db: Database, lat: float, lon: float,
                       ready: Optional[dict] = None,
                       inventory: Optional[list[dict]] = None,
                       sun: Optional[dict] = None,
                       **kwargs) -> list[dict]:
    """Produce a priority-sorted list of recommended actions.

    Extra keyword args are accepted for compatibility and future context.
    """
    actions = []
    try:
        radius_km = float(kwargs.get("radius_km") or (250.0 if kwargs.get("tight") else 500.0))
    except (TypeError, ValueError):
        radius_km = 500.0
    nearby = events_near(db, lat, lon, radius_km=radius_km, limit=80)
    actions.extend(_rules_nearby_events(nearby))
    actions.extend(_rules_environmental_intel(db, lat, lon))
    if ready:
        actions.extend(_rules_readiness(ready))
    if inventory:
        actions.extend(_rules_supplies(inventory))
    if sun:
        actions.extend(_rules_sun(sun, lat, lon))
    actions.extend(_rules_intel_quality(db))

    threat = kwargs.get("threat") or {}
    try:
        if int(threat.get("local_critical_count") or 0) > 0 and not any(a.get("urgency") == 0 for a in actions):
            actions.append({"text": "Local weighted threat includes critical events near your position. Confirm official guidance before travel.", "urgency": 0})
    except Exception:
        pass

    actions = _dedupe_actions(actions)
    actions.sort(key=lambda a: (a.get("urgency", 9), a.get("text", "")))
    return actions[:12]


# ---------------------------------------------------------------------------
# Top-level brief
# ---------------------------------------------------------------------------

def situational_brief(db: Database, lat: float, lon: float,
                      elevation_m: float = 0.0,
                      ready: Optional[dict] = None,
                      inventory: Optional[list[dict]] = None,
                      sun: Optional[dict] = None,
                      radius_km: float = 500.0,
                      threat: Optional[dict] = None,
                      **kwargs) -> dict:
    """Produce the full situational brief for the dashboard."""
    nearby = events_near(db, lat, lon, radius_km=radius_km, limit=40)
    actions = synthesize_actions(db, lat, lon, ready=ready, inventory=inventory, sun=sun,
                                 threat=threat, radius_km=radius_km)

    # Location-aware threat context (fallback if caller didn't provide it)
    if threat is None:
        try:
            threat = intelstore.threat_index(db, lat=lat, lon=lon)
        except Exception:
            threat = {}

    critical_near = [e for e in nearby if str(e.get("severity") or "").upper() in ("CRITICAL", "SEVERE", "EXTREME")]
    recent_critical_near = [e for e in critical_near
                            if (e.get("distance_km") or 999) <= 100 and (_ev_age_hours(e) is None or _ev_age_hours(e) <= 48)]
    critical_urgent = [a for a in actions if a.get("urgency") == 0]

    readiness_score = None
    if isinstance(ready, dict):
        try:
            readiness_score = float(ready.get("score"))
        except (TypeError, ValueError):
            readiness_score = None

    threat_score = None
    try:
        threat_score = float((threat or {}).get("score"))
    except (TypeError, ValueError):
        threat_score = None
    local_critical = int((threat or {}).get("local_critical_count") or 0)

    if critical_urgent or local_critical > 0:
        stance = "ACT NOW"
        n = len(critical_urgent) or local_critical
        stance_reason = f"{n} immediate local risk signal(s) require verification/action"
    elif recent_critical_near or (threat_score is not None and threat_score >= 70 and (threat or {}).get("events_scoped")):
        stance = "HEIGHTENED AWARENESS"
        stance_reason = f"{len(recent_critical_near) or 'Local'} serious signal(s) near position; verify sources before movement"
    elif threat_score is not None and threat_score >= 45 and (threat or {}).get("events_scoped"):
        stance = "ELEVATED"
        stance_reason = f"Location-weighted threat index {threat_score:.0f}/100 from cached intel"
    elif readiness_score is not None and readiness_score < 60:
        stance = "ADAPT"
        stance_reason = f"Readiness degraded ({readiness_score:.0f}/100) with active operational constraints"
    elif readiness_score is not None and readiness_score < 80:
        stance = "PREPARE"
        stance_reason = f"Readiness {readiness_score:.0f}/100; review automated risk signals and advisories"
    elif actions:
        stance = "PREPARE"
        stance_reason = f"{len(actions)} advisory action(s) based on location and cached intel"
    else:
        stance = "STANDBY"
        stance_reason = "No immediate local threats detected in cached intel"

    # Supply viability summary
    supply_viability = None
    if inventory:
        try:
            low_stock = sum(1 for it in inventory
                           if float(it.get("qty") or 0) <= float(it.get("min_qty") or 0))
            expiring = 0
            now = datetime.now(timezone.utc)
            for it in inventory:
                exp = it.get("expires")
                if exp:
                    try:
                        t = datetime.fromisoformat(str(exp))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        if 0 <= (t - now).days <= 14:
                            expiring += 1
                    except Exception:
                        pass
            supply_viability = {"low_stock_items": low_stock, "expiring_soon": expiring}
        except Exception:
            pass

    top_events = nearby[:10]
    # Prefer weighted local threat events when available
    try:
        ttop = (threat or {}).get("top_events") or []
        if ttop:
            # keep distance-aware top events from threat model
            top_events = ttop[:10]
    except Exception:
        pass

    return {
        "lat": lat, "lon": lon,
        "radius_km": radius_km,
        "stance": stance,
        "stance_reason": stance_reason,
        "critical_near": len(recent_critical_near),
        "events_within_radius": len(nearby),
        "threat_score": int(threat_score) if threat_score is not None else None,
        "threat_band": (threat or {}).get("band"),
        "top_events": top_events,
        "actions": actions[:10],
        "supply_viability": supply_viability,
        "updated": utcnow(),
    }
