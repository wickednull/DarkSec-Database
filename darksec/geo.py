"""Geodesy, UTM / MGRS grid conversion and magnetic declination.

All formulas are WGS-84. MGRS output uses the standard 100 km square lettering
scheme (AA scheme). Declination uses the NOAA WMM2025 coefficients bundled
with the application.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone

from .vendor.geomag.geomag import GeoMag

A = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)
EP2 = E2 / (1 - E2)
K0 = 0.9996

LAT_BANDS = "CDEFGHJKLMNPQRSTUVWXX"
BAND_RANGES = {
    "C": (-80, -72), "D": (-72, -64), "E": (-64, -56), "F": (-56, -48),
    "G": (-48, -40), "H": (-40, -32), "J": (-32, -24), "K": (-24, -16),
    "L": (-16, -8), "M": (-8, 0), "N": (0, 8), "P": (8, 16), "Q": (16, 24),
    "R": (24, 32), "S": (32, 40), "T": (40, 48), "U": (48, 56),
    "V": (56, 64), "W": (64, 72), "X": (72, 84),
}
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # I and O omitted
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"  # 20 letters A-V


def _zone_for(lat: float, lon: float) -> int:
    zone = int((lon + 180) / 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        zone = 32
    elif 72 <= lat < 84:
        if 0 <= lon < 9:
            zone = 31
        elif 9 <= lon < 21:
            zone = 33
        elif 21 <= lon < 33:
            zone = 35
        elif 33 <= lon < 42:
            zone = 37
    return max(1, min(60, zone))


def _band_for(lat: float) -> str:
    if lat < -80 or lat > 84:
        raise ValueError("MGRS/UTM is only defined between 80S and 84N")
    if lat >= 72:
        return "X"
    idx = int((lat + 80) / 8)
    return "CDEFGHJKLMNPQRSTUVWX"[idx]


def utm_forward(lat: float, lon: float, force_zone: int | None = None) -> dict:
    if not -80 <= lat <= 84:
        raise ValueError("UTM latitude must be between 80S and 84N")
    zone = force_zone or _zone_for(lat, lon)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    e4 = E2 * E2
    e6 = e4 * E2
    n = A / math.sqrt(1 - E2 * math.sin(lat_r) ** 2)
    t = math.tan(lat_r) ** 2
    c = EP2 * math.cos(lat_r) ** 2
    a = math.cos(lat_r) * (lon_r - lon0)
    m = A * (
        (1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat_r
        - (3 * E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat_r)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat_r)
        - (35 * e6 / 3072) * math.sin(6 * lat_r)
    )
    easting = K0 * n * (
        a + (1 - t + c) * a ** 3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * EP2) * a ** 5 / 120
    ) + 500000.0
    northing = K0 * (
        m + n * math.tan(lat_r) * (
            a ** 2 / 2
            + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * EP2) * a ** 6 / 720
        )
    )
    southern = lat < 0
    if southern:
        northing += 10000000.0
    return {
        "zone": zone,
        "band": _band_for(lat),
        "hemisphere": "S" if southern else "N",
        "easting": round(easting, 3),
        "northing": round(northing, 3),
        "lat": lat,
        "lon": lon,
    }


def utm_inverse(zone: int, hemisphere: str, easting: float, northing: float) -> dict:
    x = easting - 500000.0
    y = northing - (10000000.0 if hemisphere.upper() == "S" else 0.0)
    e4 = E2 * E2
    e6 = e4 * E2
    m = y / K0
    mu = m / (A * (1 - E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
        + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
    )
    c1 = EP2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = A / math.sqrt(1 - E2 * math.sin(phi1) ** 2)
    r1 = A * (1 - E2) / (1 - E2 * math.sin(phi1) ** 2) ** 1.5
    d = x / (n1 * K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * EP2 - 3 * c1 * c1) * d ** 6 / 720
    )
    lon = (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * EP2 + 24 * t1 * t1) * d ** 5 / 120
    ) / math.cos(phi1)
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    return {
        "lat": round(math.degrees(lat), 7),
        "lon": round(math.degrees(lon + lon0), 7),
        "zone": zone,
        "hemisphere": hemisphere.upper(),
    }


def mgrs_forward(lat: float, lon: float, precision: int = 5) -> str:
    if not 1 <= precision <= 5:
        raise ValueError("MGRS precision must be 1..5")
    u = utm_forward(lat, lon)
    zone, band = u["zone"], u["band"]
    e100k = int(u["easting"] // 100000)
    n100k = int(u["northing"] // 100000)
    set_idx = zone % 6 or 6
    col_starts = {1: 0, 2: 8, 3: 16, 4: 0, 5: 8, 6: 16}
    col_letter = LETTERS[col_starts[set_idx] + e100k - 1]
    row_offset = 0 if zone % 2 == 1 else 5
    row_letter = ROW_LETTERS[(n100k + row_offset) % 20]
    e_digits = str(int(round(u["easting"])) % 100000).zfill(5)[:precision]
    n_digits = str(int(round(u["northing"])) % 100000).zfill(5)[:precision]
    return f"{zone}{band}{col_letter}{row_letter}{e_digits}{n_digits}"


def mgrs_inverse(value: str) -> dict:
    mgrs = re.sub(r"[\s\-]", "", value).upper()
    m = re.match(r"^(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$", mgrs)
    if not m:
        raise ValueError("Invalid MGRS string")
    zone = int(m.group(1))
    band, col, row, digits = m.group(2), m.group(3), m.group(4), m.group(5)
    if zone < 1 or zone > 60:
        raise ValueError("Invalid UTM zone")
    if len(digits) not in (0, 2, 4, 6, 8, 10):
        raise ValueError("MGRS digits must be even (0,2,4,6,8,10)")
    precision = len(digits) // 2
    if precision:
        e_digits = digits[:precision] + "0" * (5 - precision)
        n_digits = digits[precision:] + "0" * (5 - precision)
    else:
        e_digits = n_digits = "00000"
    set_idx = zone % 6 or 6
    col_starts = {1: 0, 2: 8, 3: 16, 4: 0, 5: 8, 6: 16}
    col_idx = LETTERS.find(col) - col_starts[set_idx] + 1
    if col_idx < 1 or col_idx > 8:
        raise ValueError("MGRS column letter does not match zone set")
    row_offset = 0 if zone % 2 == 1 else 5
    row_idx = (ROW_LETTERS.find(row) - row_offset) % 20
    if ROW_LETTERS.find(row) < 0:
        raise ValueError("Invalid MGRS row letter")
    easting = col_idx * 100000 + int(e_digits)
    base_n = row_idx * 100000 + int(n_digits)
    hemisphere = "N" if band >= "N" else "S"
    lo, hi = BAND_RANGES[band]
    candidates = []
    for k in range(0, 6):
        north = base_n + k * 2000000
        if north > 10000000:
            break
        ll = utm_inverse(zone, hemisphere, easting, north)
        # Reject wrapped/nonsense solutions: the inverse must map back to the
        # same grid cell and to a real longitude.
        if not -180.001 <= ll["lon"] <= 180.001:
            continue
        chk = None
        try:
            chk = utm_forward(ll["lat"], ll["lon"], zone)
        except ValueError:
            continue
        if abs(chk["northing"] - north) > 2 or abs(chk["easting"] - easting) > 2:
            continue
        if lo - 0.2 <= ll["lat"] <= hi + 0.2:
            candidates.append((north, ll))
    if not candidates:
        raise ValueError("MGRS northing does not resolve inside its latitude band")
    north, ll = candidates[0]
    return {**ll, "easting": easting, "northing": north, "band": band, "precision": precision}


def dms(value: float, is_lat: bool | None = None) -> str:
    hemi = ""
    if is_lat is True:
        hemi = "N" if value >= 0 else "S"
    elif is_lat is False:
        hemi = "E" if value >= 0 else "W"
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d}\u00b0{m:02d}'{s:04.1f}\"{hemi}"


def parse_angle(text: str, is_lat: bool | None = None) -> float:
    t = text.strip().upper().replace(",", ".")
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)\s*([NSEW])$", t)
    if m:
        val = float(m.group(1))
        if m.group(2) in "SW":
            val = -abs(val)
        return val
    m = re.match(r"^([+-]?\d{1,3})[^\d]+(\d{1,2})(?:[^\d]+(\d{1,2}(?:\.\d+)?))?[^\w]*([NSEW])?$", t)
    if m:
        d = float(m.group(1))
        mm = float(m.group(2))
        ss = float(m.group(3) or 0)
        val = math.copysign(abs(d) + mm / 60 + ss / 3600, d)
        if m.group(4) in ("S", "W"):
            val = -abs(val)
        return val
    return float(t)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    r = 6371008.8
    d = distance_m / r
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), ((math.degrees(l2) + 540) % 360) - 180


def midpoint(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    bx = math.cos(p2) * math.cos(l2 - l1)
    by = math.cos(p2) * math.sin(l2 - l1)
    p = math.atan2(math.sin(p1) + math.sin(p2), math.sqrt((math.cos(p1) + bx) ** 2 + by ** 2))
    l = l1 + math.atan2(by, math.cos(p1) + bx)
    return math.degrees(p), math.degrees(l)


def cross_track(lat1: float, lon1: float, lat2: float, lon2: float, lat3: float, lon3: float) -> float:
    """Signed distance (m, right positive) of point 3 from the great-circle 1->2."""
    r = 6371008.8
    d13 = haversine(lat1, lon1, lat3, lon3) / r
    b13 = math.radians(initial_bearing(lat1, lon1, lat3, lon3))
    b12 = math.radians(initial_bearing(lat1, lon1, lat2, lon2))
    return math.asin(math.sin(d13) * math.sin(b13 - b12)) * r


def resection(p1: tuple[float, float], b1: float, p2: tuple[float, float], b2: float) -> dict:
    """Two-point resection by iterative closest-approach intersection."""
    best = None
    for dist in range(100, 200000, 50):
        a = destination(p1[0], p1[1], b1, dist)
        b = destination(p2[0], p2[1], b2, dist)
        gap = haversine(a[0], a[1], b[0], b[1])
        if best is None or gap < best[0]:
            best = (gap, a, b, dist)
            if gap < 10:
                break
    gap, a, b, dist = best
    lat = (a[0] + b[0]) / 2
    lon = (a[1] + b[1]) / 2
    return {"lat": lat, "lon": lon, "error_m": gap, "range_from_1_m": dist}


_GEOMAG = GeoMag()


def declination(lat: float, lon: float, altitude_m: float = 0, when: date | None = None) -> dict:
    when = when or datetime.now(timezone.utc).date()
    mag = _GEOMAG.GeoMag(lat, lon, altitude_m * 3.280839895, when)
    return {
        "declination_deg": round(mag.dec, 2),
        "inclination_deg": round(mag.dip, 2),
        "horizontal_intensity_nt": round(mag.bh, 1),
        "total_intensity_nt": round(mag.ti, 1),
        "model": "WMM2025",
        "year": when.year,
    }


def format_coord(lat: float, lon: float) -> dict:
    u = utm_forward(lat, lon)
    return {
        "lat": lat,
        "lon": lon,
        "dd": f"{lat:.6f}, {lon:.6f}",
        "dms": f"{dms(lat, True)} {dms(lon, False)}",
        "utm": f"{u['zone']}{u['hemisphere']} {u['easting']:.0f}E {u['northing']:.0f}N",
        "mgrs": mgrs_forward(lat, lon, 5),
        "band": u["band"],
    }
