"""Offline astronomy: sun, moon, twilight and planet positions.

Uses PyEphem's VSOP87/ELP2000 implementations. No network, no data files.
Times are returned as ISO-8601 UTC; the interface converts to local time.
"""
from __future__ import annotations

from datetime import datetime, timezone

import ephem

MOON_PHASES = [
    (0.03, "NEW"), (0.22, "WAXING CRESCENT"), (0.28, "FIRST QUARTER"),
    (0.47, "WAXING GIBBOUS"), (0.53, "FULL"), (0.72, "WANING GIBBOUS"),
    (0.78, "LAST QUARTER"), (0.97, "WANING CRESCENT"), (1.01, "NEW"),
]


def _dt(ephem_date: ephem.Date | None) -> str | None:
    if ephem_date is None:
        return None
    try:
        return ephem.Date(ephem_date).datetime().replace(tzinfo=timezone.utc).isoformat()
    except Exception:
        return None


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (ephem.AlwaysUpError, ephem.NeverUpError, ValueError, TypeError):
        return None


def _observer(lat: float, lon: float, elevation_m: float = 0) -> ephem.Observer:
    obs = ephem.Observer()
    obs.lat = str(lat)
    obs.lon = str(lon)
    obs.elevation = float(elevation_m)
    obs.pressure = 1013.25
    obs.temp = 15.0
    obs.horizon = "0"
    return obs


def phase_name(phase: float) -> str:
    phase = phase % 1.0
    for limit, name in MOON_PHASES:
        if phase < limit:
            return name
    return "NEW"


def _body_info(body: ephem.Body, obs: ephem.Observer, rise_body=None) -> dict:
    body.compute(obs)
    info = {
        "altitude_deg": round(float(body.alt) * 57.295779513, 2),
        "azimuth_deg": round(float(body.az) * 57.295779513, 2),
    }
    rise_body = rise_body or body
    info["next_rise"] = _dt(_safe(obs.next_rising, rise_body))
    info["next_set"] = _dt(_safe(obs.next_setting, rise_body))
    info["next_transit"] = _dt(_safe(obs.next_transit, rise_body))
    return info


def celestial(lat: float, lon: float, elevation_m: float = 0, when: str | None = None) -> dict:
    if when:
        try:
            dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            obs = _observer(lat, lon, elevation_m)
            obs.date = ephem.Date(dt.astimezone(timezone.utc))
        except Exception:
            obs = _observer(lat, lon, elevation_m)
            obs.date = ephem.now()
    else:
        obs = _observer(lat, lon, elevation_m)
        obs.date = ephem.now()

    sun = ephem.Sun()
    moon = ephem.Moon()
    sun.compute(obs)
    moon.compute(obs)

    now = obs.date
    day = ephem.Date(now.datetime().replace(hour=0, minute=0, second=0, microsecond=0))

    # Next four rise/set events with horizons.
    def event(horizon: str, rising: bool) -> str | None:
        o = _observer(lat, lon, elevation_m)
        o.date = now
        o.horizon = horizon
        body = ephem.Sun()
        return _dt(_safe(o.next_rising if rising else o.next_setting, body))

    moon_phase = float(moon.moon_phase)
    moon_illum = float(moon.phase)

    # Seven-day planning table.
    week = []
    for i in range(7):
        o = _observer(lat, lon, elevation_m)
        o.date = ephem.Date(day + i)
        s = ephem.Sun()
        m = ephem.Moon()
        sr = _safe(o.next_rising, s)
        o2 = _observer(lat, lon, elevation_m)
        o2.date = ephem.Date(day + i)
        ss = _safe(o2.next_setting, s)
        o3 = _observer(lat, lon, elevation_m)
        o3.date = ephem.Date(day + i)
        mr = _safe(o3.next_rising, m)
        o4 = _observer(lat, lon, elevation_m)
        o4.date = ephem.Date(day + i)
        ms = _safe(o4.next_setting, m)
        length = None
        if sr and ss:
            length = round(((ss - sr) * 24.0) % 24.0, 2)
        m.compute(ephem.Date(day + i))
        week.append({
            "date": ephem.Date(day + i).datetime().date().isoformat(),
            "sunrise": _dt(sr), "sunset": _dt(ss),
            "moonrise": _dt(mr), "moonset": _dt(ms),
            "day_length_h": length,
            "moon_illum_pct": round(float(m.phase), 1),
        })

    planets = []
    for name in ("Mercury", "Venus", "Mars", "Jupiter", "Saturn", "Uranus", "Neptune"):
        try:
            body = getattr(ephem, name)()
            body.compute(obs)
            planets.append({
                "name": name.upper(),
                "altitude_deg": round(float(body.alt) * 57.295779513, 2),
                "azimuth_deg": round(float(body.az) * 57.295779513, 2),
                "magnitude": round(float(body.mag), 1) if hasattr(body, "mag") else None,
            })
        except Exception:
            continue

    return {
        "utc": _dt(now),
        "observer": {"lat": lat, "lon": lon, "elevation_m": elevation_m},
        "sun": {
            "altitude_deg": round(float(sun.alt) * 57.295779513, 2),
            "azimuth_deg": round(float(sun.az) * 57.295779513, 2),
            "distance_au": round(float(sun.earth_distance), 6),
            "next_rise": event("0", True),
            "next_set": event("0", False),
            "civil_dawn": event("-6", True),
            "civil_dusk": event("-6", False),
            "nautical_dawn": event("-12", True),
            "nautical_dusk": event("-12", False),
            "astro_dawn": event("-18", True),
            "astro_dusk": event("-18", False),
        },
        "moon": {
            "altitude_deg": round(float(moon.alt) * 57.295779513, 2),
            "azimuth_deg": round(float(moon.az) * 57.295779513, 2),
            "distance_km": round(float(moon.earth_distance) * 149597870.7, 0),
            "illumination_pct": round(moon_illum, 1),
            "phase": round(moon_phase, 4),
            "phase_name": phase_name(moon_phase),
            "age_days": round(moon_phase * 29.530588, 1),
            "next_rise": _dt(_safe(obs.next_rising, moon)),
            "next_set": _dt(_safe(obs.next_setting, moon)),
        },
        "planets": planets,
        "week": week,
    }


def sun_times(lat: float, lon: float, date: str | None = None) -> dict:
    """Sunrise/sunset for a single civil date at the observer."""
    obs = _observer(lat, lon)
    if date:
        obs.date = ephem.Date(date)
    else:
        obs.date = ephem.now()
    day = ephem.Date(obs.date.datetime().replace(hour=0, minute=0, second=0, microsecond=0))
    obs.date = day
    sun = ephem.Sun()
    rise = _safe(obs.next_rising, sun)
    o2 = _observer(lat, lon)
    o2.date = day
    setting = _safe(o2.next_setting, sun)
    return {
        "date": day.datetime().date().isoformat(),
        "sunrise": _dt(rise),
        "sunset": _dt(setting),
        "day_length_h": round(((setting - rise) * 24.0) % 24.0, 2) if rise and setting else None,
    }


def tide_rule_of_twelfths(hours_since_low: float, range_m: float) -> dict:
    """Rule of twelfths approximation (not a substitute for tide tables)."""
    h = max(0.0, min(6.0, hours_since_low))
    twelfths = [1, 2, 3, 3, 2, 1]
    idx = min(5, int(h))
    frac = (h - idx)
    units = sum(twelfths[:idx]) + twelfths[idx] * frac
    height = range_m * units / 12.0
    return {
        "hours_since_low": round(h, 2),
        "twelfths_elapsed": round(units, 2),
        "height_above_low_m": round(height, 2),
        "approx": True,
    }
