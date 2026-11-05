"""Situational awareness synthesis for the operator.

This is the core "what matters to me right now" engine. It takes the raw
intel feed data and produces a synthesized brief tailored to the operator's
position, readiness state, and resources.

Design principle: under stress, operators need decisions, not data.
"""
from __future__ import annotations

import math
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

# Each rule is (predicate, action_text, urgency).
# Urgency: 0 = immediate, 1 = within hours, 2 = within a day, 3 = advisory.

def _rules_critical_in_radius(events: list[dict], radius_km: float) -> list[dict]:
    actions = []
    for ev in events:
        d = ev.get("distance_km", float("inf"))
        sev = ev.get("severity", "INFO")
        mag = ev.get("mag") or 0
        kind = ev.get("kind", "")

        if kind == "usgs" and mag and d < 100:
            if mag >= 6.5:
                actions.append({"text": f"M{mag} earthquake {d:.0f} km away — expect strong shaking. Drop/cover/hold; check utilities.", "urgency": 0})
            elif mag >= 5.0:
                actions.append({"text": f"M{mag} earthquake {d:.0f} km — secure loose objects, expect aftershocks.", "urgency": 1})
            elif mag >= 4.0:
                actions.append({"text": f"M{mag} earthquake {d:.0f} km — felt locally, monitor for aftershocks.", "urgency": 2})

        if kind == "nws" and sev in ("CRITICAL", "SEVERE"):
            actions.append({"text": f"Severe weather alert: {ev['title']}. Seek shelter.", "urgency": 0 if sev == "CRITICAL" else 1})

        if kind == "swpc_kp":
            kp = ev.get("kp")
            try: kp = float(kp) if kp else 0
            except: kp = 0
            if kp >= 7 and d < 1000:
                actions.append({"text": f"Kp {kp} — HF comms likely degraded above ~30 MHz, GPS accuracy reduced.", "urgency": 1})

        if sev == "CRITICAL" and d < 50:
            actions.append({"text": f"CRITICAL event within 50 km: {ev['title']}. Verify via secondary source.", "urgency": 0})

    return actions


def _rules_readiness(ready: dict) -> list[dict]:
    """Generate readiness-driven actions from dashboard readiness scores."""
    actions = []
    if not ready:
        return actions
    for k, v in ready.items():
        try: score = int(v)
        except: continue
        if score <= 25:
            label = {"power": "Power", "water": "Water", "food": "Food",
                     "comms": "Comms", "security": "Security", "health": "Health",
                     "logistics": "Logistics"}.get(k, k)
            actions.append({"text": f"{label} readiness critically low ({score}%). Address before next event.", "urgency": 1})
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
        if qty <= min_qty:
            actions.append({"text": f"{name} at/below minimum ({qty} {unit}). Resupply.", "urgency": 1})
        if expires:
            try:
                exp = datetime.fromisoformat(str(expires))
                if exp.tzinfo is None: exp = exp.replace(tzinfo=timezone.utc)
                days_left = (exp - now).days
                if 0 <= days_left <= 7:
                    actions.append({"text": f"{name} expires in {days_left} day(s). Use or rotate.", "urgency": 2})
                elif days_left < 0:
                    actions.append({"text": f"{name} expired {abs(days_left)} day(s) ago. Discard.", "urgency": 1})
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
            if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
            hours = max(0, (t - now).total_seconds() / 3600)
            if hours < 2:
                actions.append({"text": f"Sunset in {hours:.1f}h — complete daylight tasks now.", "urgency": 2})
        except Exception:
            pass
    return actions


def synthesize_actions(db: Database, lat: float, lon: float,
                       ready: Optional[dict] = None,
                       inventory: Optional[list[dict]] = None,
                       sun: Optional[dict] = None) -> list[dict]:
    """Produce a priority-sorted list of recommended actions."""
    actions = []
    nearby = events_near(db, lat, lon, radius_km=500.0, limit=50)
    actions.extend(_rules_critical_in_radius(nearby, 500.0))
    if ready:
        actions.extend(_rules_readiness(ready))
    if inventory:
        actions.extend(_rules_supplies(inventory))
    if sun:
        actions.extend(_rules_sun(sun, lat, lon))
    actions.sort(key=lambda a: a.get("urgency", 9))
    return actions[:10]


# ---------------------------------------------------------------------------
# Top-level brief
# ---------------------------------------------------------------------------

def situational_brief(db: Database, lat: float, lon: float,
                      elevation_m: float = 0.0,
                      ready: Optional[dict] = None,
                      inventory: Optional[list[dict]] = None,
                      sun: Optional[dict] = None,
                      radius_km: float = 500.0) -> dict:
    """Produce the full situational brief for the dashboard."""
    nearby = events_near(db, lat, lon, radius_km=radius_km, limit=25)
    actions = synthesize_actions(db, lat, lon, ready=ready, inventory=inventory, sun=sun)

    # Top-level recommendation
    critical_near = [e for e in nearby if e.get("severity") in ("CRITICAL", "SEVERE", "EXTREME")]
    critical_urgent = [a for a in actions if a.get("urgency") == 0]

    if critical_urgent:
        stance = "ACT NOW"
        stance_reason = f"{len(critical_urgent)} immediate action(s) required"
    elif critical_near and any(e.get("distance_km", 999) < 100 for e in critical_near):
        stance = "HEIGHTENED AWARENESS"
        stance_reason = f"{len(critical_near)} serious event(s) within 100 km"
    elif actions:
        stance = "PREPARE"
        stance_reason = f"{len(actions)} advisory action(s)"
    else:
        stance = "STANDBY"
        stance_reason = "No immediate threats in area"

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
                        if t.tzinfo is None: t = t.replace(tzinfo=timezone.utc)
                        if 0 <= (t - now).days <= 14:
                            expiring += 1
                    except: pass
            supply_viability = {"low_stock_items": low_stock, "expiring_soon": expiring}
        except Exception:
            pass

    return {
        "lat": lat, "lon": lon,
        "radius_km": radius_km,
        "stance": stance,
        "stance_reason": stance_reason,
        "critical_near": len(critical_near),
        "events_within_radius": len(nearby),
        "top_events": nearby[:10],
        "actions": actions[:10],
        "supply_viability": supply_viability,
        "updated": utcnow(),
    }
