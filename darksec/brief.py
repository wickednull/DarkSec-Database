"""Plain-text SITREP brief generation."""
from __future__ import annotations

from datetime import datetime, timezone

from . import astro, feeds, geo, intel, library, sitrep
from .db import Database
from .status import readiness
from .trips import trip_items



def build_brief(db: Database) -> str:
    s = db.all_settings()
    r = readiness(db)
    now = datetime.now(timezone.utc)
    try:
        lat_f = float(s["lat"])
        lon_f = float(s["lon"])
        coords = geo.format_coord(lat_f, lon_f)
        grid = coords["mgrs"]
    except Exception:
        lat_f = lon_f = 0.0
        grid = "UNKNOWN"
    elevation = float(s.get("elevation_m") or 0)
    tasks = db.query("SELECT * FROM tasks WHERE done=0 ORDER BY priority LIMIT 8")
    trips = [t for t in trip_items(db) if t["status"] == "ACTIVE"]
    logs = db.query("SELECT * FROM log_entries ORDER BY ts DESC LIMIT 6")
    incidents = db.query("SELECT * FROM incidents WHERE resolved=0 ORDER BY ts DESC LIMIT 8")
    low = db.query("SELECT * FROM inventory WHERE min_qty > 0 AND qty <= min_qty ORDER BY (qty-min_qty) LIMIT 10")
    feed_summary = feeds.summary(db)
    lib = library.stats(db)
    almanac = None
    try:
        almanac = astro.celestial(lat_f, lon_f, elevation)
    except Exception:
        almanac = None
    try:
        local_threat = intel.threat_index(db, lat=lat_f, lon=lon_f)
    except Exception:
        local_threat = {}
    try:
        sit = sitrep.situational_brief(db, lat_f, lon_f, elevation_m=elevation, ready=r,
                                    inventory=[dict(x) for x in db.query("SELECT * FROM inventory ORDER BY name LIMIT 24")],
                                    sun=almanac, threat=local_threat)
    except Exception:
        sit = {"stance": "UNKNOWN", "stance_reason": "Synthesis unavailable", "critical_near": 0, "events_within_radius": 0, "top_events": [], "actions": []}
    actions = sit.get("actions") or []

    source = str(s.get("position_source") or "unverified").lower()
    fix_at = s.get("position_fix_at") or s.get("position_updated_at")
    age_text = "unknown age"
    age_seconds = None
    try:
        fix_dt = datetime.fromisoformat(str(fix_at).replace("Z", "+00:00"))
        if fix_dt.tzinfo is None:
            fix_dt = fix_dt.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - fix_dt).total_seconds()))
        age_text = (f"{age_seconds // 3600}h {(age_seconds % 3600) // 60:02d}m"
                    if age_seconds >= 3600 else f"{age_seconds // 60}m {age_seconds % 60:02d}s")
    except (TypeError, ValueError):
        pass
    accuracy = s.get("position_accuracy_m")
    try:
        accuracy_text = f" ±{float(accuracy):.1f}m" if accuracy is not None else ""
    except (TypeError, ValueError):
        accuracy_text = ""
    provenance = f"SOURCE: {source.upper()} · FIX AGE: {age_text}{accuracy_text}"
    position_warning = None
    if source == "unverified":
        position_warning = "Position source is unverified; confirm the coordinate before navigating."
    elif source in ("gpsd", "phone") and (
        (source == "gpsd" and (age_seconds is None or age_seconds > 6 * 3600)) or
        (source == "phone" and (age_seconds is None or age_seconds > 24 * 3600))
    ):
        position_warning = "GPS fix is stale or has no usable age; refresh before navigating."
    elif source in ("manual", "geocoded"):
        position_warning = f"{source.capitalize()} reference only; no receiver accuracy guarantee."

    tband = (local_threat or {}).get("band") or "?"
    tscore = (local_threat or {}).get("score")
    tscoped = "location-weighted" if (local_threat or {}).get("events_scoped") else "global/cached"
    lines = [
        "DARKSEC // FIELD SITUATION REPORT",
        "UNCLASSIFIED // FIELD OPERATIONS",
        "=" * 58,
        f"TIME (UTC): {now.isoformat(timespec='seconds')}",
        f"OPERATOR: {s.get('operator') or '-'}    CALLSIGN: {s.get('callsign') or '-'}    TEAM: {s.get('team') or '-'}",
        f"POSITION: {float(s['lat']):.6f}, {float(s['lon']):.6f}   {grid}",
        f"{provenance}",
        *([f"WARNING: {position_warning}"] if position_warning else []),
        f"READINESS: {r['band']} ({r['score'] if r['score'] is not None else 'N/A'}/100)   SOURCE: AUTOMATED OPERATIONAL RISK",
        f"LOCAL THREAT: {tband} ({tscore if tscore is not None else 'N/A'}/100)   MODE: {tscoped}",
        f"STANCE: {sit.get('stance') or 'UNKNOWN'}   {sit.get('stance_reason') or ''}",
        "",
        "1. READINESS SCORECARD",
    ]
    for c in sorted(r.get("categories") or [], key=lambda x: x.get("score") if x.get("score") is not None else 999, reverse=True):
        lines.append(f"   - {c.get('category')}: {float(c.get('score') or 0):.0f}   risk {c.get('risk')}   source {c.get('source')}")
    lines += ["", "2. OPERATIONAL SIGNALS"]
    signals = r.get("signals") or []
    if signals:
        for sig in signals[:12]:
            lines.append(f"   - [{sig.get('category')}] {sig.get('text')}")
    else:
        lines.append("   - No operational risk signals detected.")
    for inc in incidents[:5]:
        lines.append(f"   - [INCIDENT/{inc['severity']}] {inc['title']}"
                     + (f" ({inc['category']})" if inc.get("category") else ""))
    if feed_summary.get("critical_count"):
        lines.append(f"   - [INTEL] {feed_summary['critical_count']} critical cached feed item(s); "
                     "verify with official sources before acting.")
    lines += ["", "3. DECISIONS / ACTIONS"]
    if actions:
        for a in actions:
            lines.append(f"   - [URG {a.get('urgency')}] {a.get('text')}")
    else:
        lines.append("   - No synthesized actions required.")
    lines += ["", "4. MISSION / TASKS"]
    if tasks:
        for t in tasks:
            lines.append(f"   - [{t['priority']}] {t['title']}" + (f" (due {t['due']})" if t["due"] else ""))
    else:
        lines.append("   - No open tasks.")
    lines += ["", "5. FIELD TRIPS / CHECK-INS"]
    if trips:
        for trip in trips:
            lines.append(f"   - [{trip['alert']}] {trip['name']} - party {trip['party_size']} - "
                         f"return {trip['return_at']} - contact {trip['emergency_contact'] or 'NOT SET'}")
            if trip.get("next_checkin_at"):
                lines.append(f"     next check-in due {trip['next_checkin_at']}; share this plan with the contact.")
    else:
        lines.append("   - No active trips.")
    lines.append("   - Check-ins are local records only; no contact is automatically notified.")
    lines += ["", "6. SUSTAINMENT"]
    if low:
        for item in low:
            lines.append(f"   - LOW STOCK: {item['name']} {item['qty']} {item['unit']} (min {item['min_qty']})")
    else:
        lines.append("   - No inventory items below minimum.")
    lines.append(f"   - Library: {lib['documents']} documents, {lib['text_indexed']} full-text indexed.")
    if almanac:
        lines.append(f"   - Sun: rise {almanac['sun']['next_rise']} set {almanac['sun']['next_set']}; "
                     f"Moon {almanac['moon']['illumination_pct']}% {almanac['moon']['phase_name']}.")
    lines += ["", "7. COMMAND / SIGNAL"]
    lines.append(f"   - Vault: {'UNLOCKED' if False else 'SEE CONSOLE'}    OPSEC: {'ON' if s.get('opsec_mode') else 'OFF'}")
    lines.append("   - Comms plan: use the SOI in COMMS. Maintain listening watch where lawful.")
    lines += ["", "8. OPERATIONS LOG"]
    if logs:
        for item in logs:
            lines.append(f"   - {item['ts']} [{item['category']}] {item['title']}")
    else:
        lines.append("   - No entries.")
    lines += ["", "NOTES",
              "   Estimates and online feeds are not substitutes for instruments, official warnings,",
              "   current maps, or qualified medical/legal advice. Verify before acting.",
              "", "END OF REPORT // DARKSEC NIGHTWATCH"]
    return "\n".join(lines)
