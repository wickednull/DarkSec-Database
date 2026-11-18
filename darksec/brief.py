"""Plain-text SITREP brief generation."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from . import astro, feeds, geo, intel, library, sitrep
from .db import Database
from .status import local_weather_snapshot, readiness
from .trips import trip_items



def build_brief(db: Database, vault: Optional[dict] = None) -> str:
    s = db.all_settings()
    opsec = bool(s.get("opsec_mode"))
    try:
        critical_radius_km = float(s.get("critical_radius_km") or 2000)
    except (TypeError, ValueError):
        critical_radius_km = 2000.0
    critical_radius_km = max(100.0, min(10000.0, critical_radius_km))
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

    # Count only critical cached events that are reasonably close to the
    # operator, so distant global headlines do not inflate the field report.
    nearby_critical = 0
    try:
        latf = float(s["lat"]); lonf = float(s["lon"])
        for ev in intel.live_events(db, limit=250):
            sev = str(ev.get("severity") or "INFO").upper()
            if sev not in ("CRITICAL", "SEVERE", "EXTREME"):
                continue
            if ev.get("lat") is None or ev.get("lon") is None:
                continue
            try:
                dist = geo.distance(latf, lonf, float(ev["lat"]), float(ev["lon"])) if hasattr(geo, "distance") else None
            except Exception:
                dist = None
            # fallback haversine
            if dist is None:
                import math
                rlat1, rlon1 = math.radians(latf), math.radians(lonf)
                rlat2, rlon2 = math.radians(float(ev["lat"])), math.radians(float(ev["lon"]))
                dlat = rlat2 - rlat1; dlon = rlon2 - rlon1
                a = math.sin(dlat/2)**2 + math.cos(rlat1)*math.cos(rlat2)*math.sin(dlon/2)**2
                dist = 6371*2*math.asin(math.sqrt(a))
            if dist <= critical_radius_km:
                nearby_critical += 1
    except Exception:
        nearby_critical = int(feed_summary.get("critical_count") or 0)
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
                                    sun=almanac, threat=local_threat, vault=vault)
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
    if opsec:
        position_line = "POSITION: OP//MASKED // OP//MASKED"
        source_line = "SOURCE: MASKED · FIX AGE: MASKED"
    else:
        position_line = f"POSITION: {float(s['lat']):.6f}, {float(s['lon']):.6f}   {grid}"
        source_line = provenance
    lines = [
        "DARKSEC // FIELD SITUATION REPORT",
        "UNCLASSIFIED // FIELD OPERATIONS",
        "=" * 58,
        f"TIME (UTC): {now.isoformat(timespec='seconds')}",
        f"OPERATOR: {s.get('operator') or '-'}    CALLSIGN: {s.get('callsign') or '-'}    TEAM: {s.get('team') or '-'}",
        position_line,
        source_line,
        *([f"WARNING: {position_warning}"] if position_warning else []),
        f"READINESS: {r['band']} ({r['score'] if r['score'] is not None else 'N/A'}/100)   SOURCE: AUTOMATED OPERATIONAL RISK",
        f"LOCAL THREAT: {tband} ({tscore if tscore is not None else 'N/A'}/100)   MODE: {tscoped}",
        f"STANCE: {sit.get('stance') or 'UNKNOWN'}   {sit.get('stance_reason') or ''}",
        "",
    ]

    def _age_text(seconds):
        if seconds is None:
            return "unknown age"
        try:
            sec = int(seconds)
        except (TypeError, ValueError):
            return "unknown age"
        if sec >= 86400:
            return f"{sec // 86400}d {(sec % 86400) // 3600}h old"
        if sec >= 3600:
            return f"{sec // 3600}h {(sec % 3600) // 60:02d}m old"
        return f"{sec // 60}m old"

    wx = local_weather_snapshot(db)
    if wx:
        bits = []
        if wx.get("temp_c") is not None:
            bits.append(f"{wx['temp_c']} °C")
        if wx.get("condition"):
            bits.append(str(wx["condition"]))
        if wx.get("apparent_c") is not None:
            bits.append(f"feels {wx['apparent_c']} °C")
        if wx.get("wind_kmh") is not None:
            bits.append(f"wind {wx['wind_kmh']} km/h")
        if wx.get("gust_kmh") is not None:
            bits.append(f"gusts {wx['gust_kmh']} km/h")
        if wx.get("precip_mm") is not None:
            bits.append(f"precip {wx['precip_mm']} mm")
        if wx.get("humidity_pct") is not None:
            bits.append(f"{wx['humidity_pct']}% RH")
        if wx.get("pressure_hpa") is not None:
            bits.append(f"{wx['pressure_hpa']} hPa")
        lines.append("LOCAL WEATHER")
        lines.append("   - " + (", ".join(bits) or "no values"))
        lines.append(
            f"   - {wx.get('source') or 'Open-Meteo'} model forecast for the configured position · "
            f"{wx.get('status') or 'UNKNOWN'} · {_age_text(wx.get('data_age_seconds') or wx.get('age_seconds'))}"
            " · not an observation"
        )
        for fc in (wx.get("forecast") or [])[:2]:
            parts = [str(fc.get("date") or "")]
            if fc.get("temp_max_c") is not None or fc.get("temp_min_c") is not None:
                parts.append(f"max {fc.get('temp_max_c', '—')} / min {fc.get('temp_min_c', '—')} °C")
            if fc.get("condition"):
                parts.append(str(fc["condition"]))
            if fc.get("precip_mm") is not None:
                parts.append(f"precip {fc['precip_mm']} mm")
            lines.append("   - Forecast " + " · ".join(p for p in parts if p))
        if wx.get("location_mismatch"):
            lines.append("   - WARNING: cached weather coordinates differ from the current position; refresh feeds before relying on it.")
        lines.append("")
    else:
        lines += ["LOCAL WEATHER", "   - unavailable (no cached Open-Meteo weather for the configured position)", ""]
    lines.append("1. READINESS SCORECARD")
    for c in sorted(r.get("categories") or [], key=lambda x: x.get("score") if x.get("score") is not None else 999, reverse=True):
        lines.append(f"   - {c.get('category')}: {float(c.get('score') or 0):.0f}   risk {c.get('risk')}   source {c.get('source')}")
    lines += ["", "2. OPERATIONAL SIGNALS"]
    signals = [s for s in (r.get("signals") or [])
               if not (s.get("category") == "INTEL" and "stale" in str(s.get("text", "")).lower())]
    if signals:
        for sig in signals[:12]:
            lines.append(f"   - [{sig.get('category')}] {sig.get('text')}")
    else:
        lines.append("   - No operational risk signals detected.")
    for inc in incidents[:5]:
        lines.append(f"   - [INCIDENT/{inc['severity']}] {inc['title']}"
                     + (f" ({inc['category']})" if inc.get("category") else ""))
    if nearby_critical:
        lines.append(f"   - [INTEL] {nearby_critical} critical cached feed item(s) within {int(round(critical_radius_km))} km; "
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


def to_markdown(text: str) -> str:
    """Render the plain-text brief as a portable Markdown report."""
    out: list[str] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if set(stripped) == {"="}:
            continue
        if line.startswith("DARKSEC // FIELD SITUATION REPORT"):
            out.append(f"# {stripped}")
        elif line.startswith("UNCLASSIFIED"):
            out.append(f"*{stripped}*")
        elif line.startswith("END OF REPORT"):
            out.extend(["---", "", f"*{stripped}*"])
        elif stripped == "NOTES" or re.match(r"^\d+\.\s+\S", stripped):
            if out and out[-1]:
                out.append("")
            out.append(f"## {stripped}")
        elif line.startswith("   - "):
            out.append(f"- {stripped[2:]}" if stripped.startswith("- ") else f"- {stripped.lstrip('- ')}")
        elif line.startswith("     ") and stripped:
            out.append(f"  {stripped}")
        else:
            out.append(stripped)
    return "\n".join(out).strip() + "\n"
