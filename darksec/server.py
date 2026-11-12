"""DarkSec field console API.

A localhost-only FastAPI application. Every request is authenticated with a
random per-launch token delivered to the Electron shell (or browser) and kept
in an HttpOnly cookie. Mutating requests are additionally origin-checked.

The server exposes only local data and local hardware. Online feeds are pulled
only when the operator explicitly asks for them.
"""
from __future__ import annotations

import asyncio
import base64
import calendar
import csv
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
import zipfile

import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__, assistant, astro, autosync, config, feeds, geo, geocoding, intel, library, phonegps, sdr, sitrep, tilecache
from .config import APP_NAME
from .brief import build_brief, to_markdown
from .calc_catalog import CALC_CATALOG, CALC_FUNCS
from .db import Database, utcnow
from .security import Vault
from .status import hardware, system_info, readiness
from .trips import parse_utc as _parse_utc, trip_items

try:
    from charset_normalizer import from_bytes as _charset_from_bytes
except Exception:
    _charset_from_bytes = None

APP_CODENAME = "NIGHTWATCH"
COOKIE = "darksec_session"
CSRF_COOKIE = "darksec_csrf"
TX_ACK = "I AM RESPONSIBLE"

# Transient CCTV frame cache (registered URLs only, no disk persistence).
_CCTV_FRAME_LOCK = threading.Lock()
_CCTV_FRAME_CACHE: dict[str, dict] = {}
_CCTV_FRAME_INFLIGHT: set[str] = set()
_CCTV_FRAME_STATUS: dict[str, dict] = {}
_CCTV_FRAME_MAX_ENTRIES = 512
_CCTV_FRAME_MAX_BYTES = 48 * 1024 * 1024
_CCTV_FRAME_MAX_ITEM_BYTES = 3 * 1024 * 1024


# --------------------------------------------------------------------------
# Static reference data
# --------------------------------------------------------------------------

def _load_json(name: str) -> Any:
    path = config.PKG_DIR / "data" / name
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


FREQ_DATA = _load_json("frequencies.json")
REFERENCE_DATA = _load_json("reference.json")
CHECKLIST_DATA = _load_json("checklists.json")
MANUAL_INDEX = _load_json("manual/index.json")

_mtime_cache: dict[str, tuple[float, Any]] = {}


def _cached_json(path: Path) -> Any:
    key = str(path)
    mtime = path.stat().st_mtime
    hit = _mtime_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    _mtime_cache[key] = (mtime, data)
    return data


# --------------------------------------------------------------------------
# Generic operator records
# --------------------------------------------------------------------------

RECORD_TABLES: dict[str, dict] = {
    "log_entries": {
        "fields": ["ts", "category", "title", "body", "lat", "lon", "tags"],
        "required": ["title"], "order": "ts DESC, id DESC", "stamp": "ts",
        "search": ["title", "body", "tags", "category"],
    },
    "tasks": {
        "fields": ["ts", "title", "detail", "priority", "due", "done", "category", "recurrence"],
        "required": ["title"], "order": "done ASC, CASE priority WHEN 'IMMEDIATE' THEN 0 "
        "WHEN 'PRIORITY' THEN 1 WHEN 'ROUTINE' THEN 2 ELSE 3 END, ts DESC",
        "stamp": "ts", "search": ["title", "detail", "category"],
    },
    "patients": {
        "fields": ["callsign", "name", "age", "sex", "weight_kg", "blood_type", "allergies",
                   "meds", "history", "triage", "status", "note", "created", "updated"],
        "required": ["callsign"], "order": "created DESC", "stamp": "created",
        "search": ["callsign", "name", "note", "allergies", "meds"],
    },
    "vitals": {
        "fields": ["ts", "patient_id", "hr", "rr", "sys", "dia", "spo2", "temp_c", "glucose",
                   "pain", "avpu", "note"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts",
        "search": ["note", "avpu"],
    },
    "medication_log": {
        "fields": ["ts", "patient_id", "medication", "dose", "route", "administered_by",
                   "indication", "reaction", "note"],
        "required": ["medication"], "order": "ts DESC, id DESC", "stamp": "ts",
        "search": ["medication", "indication", "reaction", "note", "administered_by"],
    },
    "weather_log": {
        "fields": ["ts", "temp_c", "pressure_hpa", "humidity", "wind_dir", "wind_kmh",
                   "gust_kmh", "precip_mm", "cloud_cover", "cloud_type", "visibility_km",
                   "note", "lat", "lon"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts",
        "search": ["note", "cloud_type"],
    },
    "water_log": {
        "fields": ["ts", "source", "volume_l", "method", "chlorine_ppm", "note"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts", "search": ["source", "method", "note"],
    },
    "power_log": {
        "fields": ["ts", "battery_pct", "fuel_l", "generator_hours", "solar_kwh", "load_w", "note"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts", "search": ["note"],
    },
    "rad_log": {
        "fields": ["ts", "dose_rate_usv_h", "cumulative_usv", "location", "note"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts", "search": ["location", "note"],
    },
    "waypoints": {
        "fields": ["name", "lat", "lon", "ele", "type", "note", "created", "updated"],
        "required": ["name", "lat", "lon"], "order": "created DESC", "stamp": "created",
        "search": ["name", "type", "note"],
    },
    "tracks": {
        "fields": ["name", "geojson", "color", "created"],
        "required": ["name", "geojson"], "order": "created DESC", "stamp": "created", "search": ["name"],
    },
    "radio_log": {
        "fields": ["ts", "freq_hz", "mode", "callsign", "summary", "lat", "lon"],
        "required": [], "order": "ts DESC, id DESC", "stamp": "ts",
        "search": ["callsign", "summary", "mode"],
    },
    "incidents": {
        "fields": ["ts", "severity", "category", "title", "detail", "lat", "lon", "resolved"],
        "required": ["title"], "order": "resolved ASC, ts DESC", "stamp": "ts",
        "search": ["title", "detail", "category"],
    },
    "inventory": {
        "fields": ["name", "category", "qty", "unit", "min_qty", "expires", "location",
                   "notes", "updated"],
        "required": ["name"], "order": "category ASC, name ASC", "stamp": "updated",
        "search": ["name", "category", "location", "notes"],
    },
    "caches": {
        "fields": ["name", "lat", "lon", "contents", "status", "note", "updated"],
        "required": ["name"], "order": "name ASC", "stamp": "updated", "search": ["name", "contents", "note"],
    },
    "comms_plans": {
        "fields": ["name", "data", "created"], "required": ["name", "data"],
        "order": "created DESC", "stamp": "created", "search": ["name", "data"],
    },
    "opords": {
        "fields": ["title", "data", "created"], "required": ["title", "data"],
        "order": "created DESC", "stamp": "created", "search": ["title", "data"],
    },
    "trip_plans": {
        "fields": ["name", "start_waypoint_id", "end_waypoint_id", "route", "departure_at",
                   "return_at", "checkin_interval_min", "emergency_contact", "party_size", "notes",
                   "status", "started_at", "last_checkin_at", "created", "updated"],
        "required": ["name", "departure_at", "return_at"],
        "order": "CASE status WHEN 'ACTIVE' THEN 0 WHEN 'PLANNED' THEN 1 ELSE 2 END, departure_at DESC",
        "stamp": "created", "search": ["name", "route", "emergency_contact", "notes"],
    },
}

BOOL_FIELDS = {"done", "resolved", "checked"}
INT_FIELDS = {"patient_id", "hr", "rr", "sys", "dia", "pain", "wind_dir", "cloud_cover", "age",
              "start_waypoint_id", "end_waypoint_id", "checkin_interval_min", "party_size"}
FLOAT_FIELDS = {"lat", "lon", "ele", "volume_l", "chlorine_ppm", "temp_c", "pressure_hpa",
                "humidity", "wind_kmh", "gust_kmh", "precip_mm", "visibility_km",
                "battery_pct", "fuel_l", "generator_hours", "solar_kwh", "load_w",
                "dose_rate_usv_h", "cumulative_usv", "qty", "min_qty", "weight_kg",
                "spo2", "glucose", "freq_hz"}


def _clean_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in BOOL_FIELDS:
        return 1 if value in (True, 1, "1", "true", "on", "yes") else 0
    if field in INT_FIELDS:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    if field in FLOAT_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    text = str(value)
    return text[:200_000]


RECURRENCE_VALUES = {"", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "YEARLY"}


def _parse_due(value: Any) -> datetime | None:
    """Parse the loose due strings the UI accepts (date, date+time, ISO)."""
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_expiry(value: Any) -> datetime | None:
    """Parse inventory expiry text. Accepts YYYY-MM, dates, and ISO stamps.

    A month-only value expires at the end of that month, which matches how
    consumables are normally labelled.
    """
    if value in (None, ""):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12:
            last = calendar.monthrange(year, month)[1]
            return datetime(year, month, last, 23, 59)
    return _parse_due(text)


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _next_due(value: Any, recurrence: str) -> str | None:
    """Return the next occurrence date for a recurring task, or None."""
    rec = str(recurrence or "").strip().upper()
    if rec not in RECURRENCE_VALUES or not rec:
        return None
    base = _parse_due(value) or datetime.now(timezone.utc).replace(tzinfo=None)
    if rec == "DAILY":
        nxt = base + timedelta(days=1)
    elif rec == "WEEKLY":
        nxt = base + timedelta(days=7)
    elif rec == "YEARLY":
        nxt = _add_months(base, 12)
    elif rec == "QUARTERLY":
        nxt = _add_months(base, 3)
    else:
        nxt = _add_months(base, 1)
    if base.hour or base.minute:
        return nxt.strftime("%Y-%m-%d %H:%M")
    return nxt.strftime("%Y-%m-%d")


def _record_row(row: sqlite3.Row) -> dict:
    return dict(row)


# --------------------------------------------------------------- AI write limits
# Delamain may write to the mission log and vault when the operator asks. These
# caps keep a misbehaving local model from filling the disk with junk notes.
AI_LOG_MAX_PER_HOUR = 20
AI_LOG_TOTAL_CAP = 1000
AI_NOTE_MAX_PER_HOUR = 10
AI_NOTE_TOTAL_CAP = 200
AI_LOG_MAX_CHARS = 20_000
AI_NOTE_MAX_CHARS = 10_000
# Explicit operator-initiated chat archives can be much larger than a model
# tool note; vault notes allow up to 2M chars, so keep a generous bound here.
AI_CHAT_MAX_CHARS = 500_000


def _hour_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()


def assistant_live_status(d: Database, vault_obj=None) -> dict:
    """Compact, read-only operational snapshot for Delamain's live-status tool.

    Reads only local caches (no network fetches) and includes ages so the model
    can distinguish fresh data from an offline/stale snapshot.
    """
    s = d.all_settings()
    online = tilecache.is_online()
    opsec = bool(s.get("opsec_mode"))
    try:
        lat = float(s.get("lat")) if s.get("lat") is not None else None
        lon = float(s.get("lon")) if s.get("lon") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    have_pos = (lat is not None and lon is not None
                and -90 <= lat <= 90 and -180 <= lon <= 180)
    try:
        # `summary()` returns display rows with zero items; fetch a bounded
        # item list here so weather/air/space-weather snapshots are available.
        rows = feeds.list_feeds(d, item_limit=30)
        latest = (feeds.summary(d).get("latest") or [])
    except Exception:
        rows = []
        latest = []
    feeds_out = []
    feed_counts = {"total": len(rows), "ok": 0, "stale": 0, "error": 0, "never": 0}
    for f in rows:
        if f.get("kind") in ("opensky", "celestrak"):
            continue
        status = str(f.get("status") or "NEVER")
        key = status.lower() if status.lower() in ("ok", "stale", "error", "never") else "never"
        feed_counts[key] = feed_counts.get(key, 0) + 1
        if status != "OK" or f.get("error"):
            feeds_out.append({
                "id": f.get("id"), "status": status,
                "age_seconds": f.get("age_seconds"),
                "data_age_seconds": f.get("data_age_seconds"),
                "error": (str(f.get("error") or "")[:160] or None),
            })
    alerts = []
    seen_alerts = set()
    for item in latest:
        sev = str(item.get("severity") or "INFO").upper()
        if sev not in ("CRITICAL", "SEVERE", "EXTREME", "ALERT"):
            continue
        title = str(item.get("title") or "Event")
        alert_key = re.sub(r"\s+", " ", title.lower())[:120]
        if alert_key in seen_alerts:
            continue
        seen_alerts.add(alert_key)
        alerts.append({
            "title": title[:160], "severity": sev,
            "time": item.get("time"), "source": item.get("source"),
            "summary": str(item.get("summary") or "")[:180],
        })
        if len(alerts) >= 8:
            break
    try:
        ready = readiness(d, include_pizzint=False)
    except Exception:
        ready = {}
    vault_state = vault_obj.status() if vault_obj is not None else None
    sit = {}
    if have_pos:
        try:
            threat = intel.threat_index(d, lat=lat, lon=lon)
        except Exception:
            threat = {}
        try:
            inventory = [dict(x) for x in d.query(
                "SELECT * FROM inventory ORDER BY name LIMIT 24")]
            sit = sitrep.situational_brief(
                d, lat, lon,
                elevation_m=float(s.get("elevation_m") or 0),
                ready=ready, inventory=inventory, threat=threat,
                vault=vault_state,
            )
        except Exception:
            sit = {}

    by_id = {f.get("id"): f for f in rows}

    def first_item(feed_id: str) -> dict | None:
        items = (by_id.get(feed_id) or {}).get("items") or []
        return items[0] if items else None

    weather = first_item("open_meteo_weather")
    air = first_item("open_meteo_air")
    kp = first_item("swpc_kp")
    scales = [
        {"title": i.get("title"), "text": i.get("text"), "severity": i.get("severity")}
        for i in ((by_id.get("swpc_scales") or {}).get("items") or [])[:4]
    ]

    water = d.one(
        "SELECT ts, source, volume_l, method, chlorine_ppm, note "
        "FROM water_log ORDER BY ts DESC LIMIT 1"
    )
    water_out = {"latest": dict(water) if water else None, "alert": None}
    if water and water["chlorine_ppm"] is not None:
        try:
            ppm = float(water["chlorine_ppm"])
            if ppm < 0.2:
                water_out["alert"] = f"Free chlorine low ({ppm:g} ppm); verify treatment before drinking"
            elif ppm > 4.0:
                water_out["alert"] = f"Free chlorine high ({ppm:g} ppm); verify dosing and local guidance"
        except (TypeError, ValueError):
            pass

    trips = [t for t in trip_items(d) if t.get("status") == "ACTIVE"]
    try:
        sync = autosync.engine.snapshot()
    except Exception:
        sync = {}

    position = {"missing": True}
    if opsec:
        position = {"redacted": True, "reason": "OPSEC mode"}
    elif have_pos:
        fix_at = s.get("position_fix_at") or s.get("position_updated_at")
        fix_age = None
        if fix_at:
            try:
                dt = datetime.fromisoformat(str(fix_at).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                fix_age = max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
            except (TypeError, ValueError):
                fix_age = None
        position = {"lat": lat, "lon": lon,
                    "source": s.get("position_source") or "unverified",
                    "fix_age_seconds": fix_age}

    return {
        "time": utcnow(),
        "online": online,
        "opsec": opsec,
        "vault": vault_state,
        "position": position,
        "sync": {
            "enabled": sync.get("enabled"), "running": sync.get("running"),
            "last_success": sync.get("last_success"), "last_error": sync.get("last_error"),
            "refreshed": sync.get("refreshed"), "failed": sync.get("failed"),
        },
        "readiness": {
            "score": ready.get("score"), "band": ready.get("band"),
            "signals": [str(x.get("text") or "") for x in (ready.get("signals") or [])][:6],
        },
        "feeds": {**feed_counts, "problems": feeds_out[:12]},
        "alerts": alerts[:10],
        "weather": ({
            "feed_age_seconds": (by_id.get("open_meteo_weather") or {}).get("age_seconds"),
            "data_age_seconds": (by_id.get("open_meteo_weather") or {}).get("data_age_seconds"),
            "time": weather.get("time"), "temp_c": weather.get("temperature_c"),
            "apparent_c": weather.get("apparent_c"), "condition": weather.get("condition"),
            "wind_kmh": weather.get("wind_kmh"), "gust_kmh": weather.get("gust_kmh"),
            "precip_mm": weather.get("precip_mm"),
            "forecast": (weather.get("forecast") or [])[:3],
        } if weather else None),
        "air_quality": ({
            "feed_age_seconds": (by_id.get("open_meteo_air") or {}).get("age_seconds"),
            "time": air.get("time"), "aqi": air.get("aqi"),
            "category": air.get("category"), "pm2_5": air.get("pm2_5"),
        } if air else None),
        "space_weather": {
            "feed_age_seconds": (by_id.get("swpc_kp") or {}).get("age_seconds"),
            "kp": kp.get("kp") if kp else None,
            "scales": scales,
        },
        "water": water_out,
        "trip_alerts": [
            {"name": t.get("name"), "alert": t.get("alert"),
             "minutes_overdue": t.get("minutes_overdue"),
             "return_minutes_overdue": t.get("return_minutes_overdue")}
            for t in trips
        ],
        "sitrep": ({
            "stance": sit.get("stance"), "stance_reason": sit.get("stance_reason"),
            "critical_near": sit.get("critical_near"),
            "actions": [
                {"urgency": a.get("urgency"), "text": a.get("text")}
                for a in (sit.get("actions") or [])[:6]
            ],
            "top_events": [
                {"title": e.get("title"), "severity": e.get("severity"),
                 "distance_km": e.get("distance_km"), "time": e.get("time"),
                 "source": e.get("source"), "feed_id": e.get("feed_id")}
                for e in (sit.get("top_events") or [])[:5]
            ],
        } if sit else None),
        "note": ("Local cache only. age_seconds/data_age_seconds show freshness; "
                 "do not present STALE feeds as current. Position is redacted in OPSEC mode."),
    }


def assistant_live_status_compact(d: Database, vault_obj=None) -> dict:
    """Token-efficient live status for the model's tool result.

    The full snapshot is useful for the UI but JSON-heavy (several thousand
    characters ~= 45s of prompt prefill on a CPU-only box). Keep the actionable
    fields and drop long alert summaries and duplicate age fields.
    """
    out = assistant_live_status(d, vault_obj)
    feeds = out.get("feeds") or {}
    weather = out.get("weather") or None
    air = out.get("air_quality") or None
    space = out.get("space_weather") or {}
    ready = out.get("readiness") or {}
    sit = out.get("sitrep") or None
    return {
        "time": out.get("time"),
        "online": out.get("online"),
        "opsec": out.get("opsec"),
        "position": out.get("position"),
        "sync": out.get("sync"),
        "readiness": {"score": ready.get("score"), "band": ready.get("band"),
                      "signals": (ready.get("signals") or [])[:3]},
        "feeds": {
            "total": feeds.get("total"), "ok": feeds.get("ok"),
            "stale": feeds.get("stale"), "error": feeds.get("error"),
            "problems": [
                {"id": p.get("id"), "status": p.get("status"), "age_seconds": p.get("age_seconds")}
                for p in (feeds.get("problems") or [])[:4]
            ],
        },
        "alerts": [
            {"title": str(a.get("title") or "")[:110], "severity": a.get("severity"),
             "source": a.get("source"), "time": a.get("time")}
            for a in (out.get("alerts") or [])[:6]
        ],
        "weather": ({
            "age_seconds": weather.get("feed_age_seconds"),
            "time": weather.get("time"), "temp_c": weather.get("temp_c"),
            "apparent_c": weather.get("apparent_c"), "condition": weather.get("condition"),
            "wind_kmh": weather.get("wind_kmh"), "gust_kmh": weather.get("gust_kmh"),
            "precip_mm": weather.get("precip_mm"),
            "forecast": (weather.get("forecast") or [])[:2],
        } if weather else None),
        "air_quality": ({
            "age_seconds": air.get("feed_age_seconds"),
            "aqi": air.get("aqi"), "category": air.get("category"),
            "pm2_5": air.get("pm2_5"),
        } if air else None),
        "space_weather": {
            "age_seconds": space.get("feed_age_seconds"),
            "kp": space.get("kp"),
            "scales": [
                {"title": s.get("title"), "severity": s.get("severity")}
                for s in (space.get("scales") or [])[:2]
            ],
        },
        "water": out.get("water"),
        "trip_alerts": out.get("trip_alerts"),
        "sitrep": ({
            "stance": sit.get("stance"), "stance_reason": sit.get("stance_reason"),
            "critical_near": sit.get("critical_near"),
            "actions": [a.get("text") for a in (sit.get("actions") or [])[:4]],
        } if sit else None),
        "note": "Cached local data only; report ages and never claim STALE data is current.",
    }


def ai_log_save(d: Database, title: str, body: str, category: str = "GENERAL") -> dict:
    """Insert an AI-authored mission-log entry with hourly and total caps."""
    content = str(body or "").strip()[:AI_LOG_MAX_CHARS]
    if not content:
        raise ValueError("body is required")
    heading = str(title or "Delamain note").strip()[:200] or "Delamain note"
    cat = re.sub(r"[^A-Za-z0-9 _/-]", "", str(category or "GENERAL")).strip().upper()[:40] or "GENERAL"
    recent = d.one("SELECT count(*) c FROM log_entries WHERE category='AI' AND ts >= ?", (_hour_ago(),))
    if recent and int(recent["c"] or 0) >= AI_LOG_MAX_PER_HOUR:
        raise ValueError(f"AI log limit reached ({AI_LOG_MAX_PER_HOUR}/hour). Try again later.")
    total = d.one("SELECT count(*) c FROM log_entries WHERE category='AI'")
    if total and int(total["c"] or 0) >= AI_LOG_TOTAL_CAP:
        overflow = int(total["c"]) - AI_LOG_TOTAL_CAP + 1
        ids = [r["id"] for r in d.query("SELECT id FROM log_entries WHERE category='AI' ORDER BY id ASC LIMIT ?", (overflow,))]
        for rid in ids:
            d.delete("log_entries", rid)
    new_id = d.insert("log_entries", {"ts": utcnow(), "category": "AI", "title": heading, "body": content,
                                      "tags": f"delamain,{cat.lower()}", "lat": None, "lon": None})
    d.audit("ASSISTANT_LOG", f"id={new_id} chars={len(content)} cat={cat}")
    return {"ok": True, "id": new_id, "title": heading,
            "limit_note": f"Saved to mission log (AI category). Max {AI_LOG_MAX_PER_HOUR}/hour, {AI_LOG_TOTAL_CAP} total."}


def ai_sitrep(d: Database, v=None, max_chars: int = 6000) -> dict:
    """Generate the current field SITREP from local data."""
    try:
        cap = max(1000, min(int(max_chars or 6000), 12000))
    except (TypeError, ValueError):
        cap = 6000
    vault_status = v.status() if v is not None else None
    try:
        text = build_brief(d, vault=vault_status)
    except Exception as exc:
        return {"ok": False, "error": f"SITREP generation failed: {type(exc).__name__}: {str(exc)[:200]}"}
    d.audit("ASSISTANT_SITREP", f"chars={len(text)}")
    return {"ok": True, "generated": utcnow(), "brief": text[:cap],
            "truncated": len(text) > cap,
            "note": ("Generated locally from cached feeds and records. Estimates and online "
                     "feeds are not substitutes for instruments, official warnings or "
                     "qualified advice; verify before acting.")}


def ai_sitrep_save(d: Database, v=None, note: str = "") -> dict:
    """Save a server-generated SITREP snapshot to the mission log."""
    made = ai_sitrep(d, v, max_chars=12000)
    if not made.get("ok"):
        return made
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    title = f"SITREP {stamp}"
    body = made["brief"]
    extra = str(note or "").strip()
    if extra:
        body += f"\n\nOPERATOR NOTE: {extra[:1000]}"
    try:
        saved = ai_log_save(d, title, body, "SITREP")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    undo = _register_undo({"kind": "log_remove", "id": int(saved["id"])},
                          f"Saved SITREP: {title}")
    return {"ok": True, "id": saved["id"], "title": title,
            "chars": len(body), "undo": undo,
            "note": saved.get("limit_note")}


def ai_note_save(d: Database, v, title: str, body: str, tags: str = "",
                 max_chars: int = AI_NOTE_MAX_CHARS) -> dict:
    """Insert an AI-authored encrypted note with hourly and total caps."""
    if not v.configured():
        raise ValueError("Vault is not configured. Set it up in SECURITY before saving notes.")
    if not v.unlocked():
        raise ValueError("Vault is locked. Unlock it before asking Delamain to save a note.")
    content = str(body or "").strip()[:max_chars]
    if not content:
        raise ValueError("body is required")
    heading = str(title or "Delamain note").strip()[:300] or "Delamain note"
    recent = d.one("SELECT count(*) c FROM notes WHERE tags='delamain' AND ts >= ?", (_hour_ago(),))
    if recent and int(recent["c"] or 0) >= AI_NOTE_MAX_PER_HOUR:
        raise ValueError(f"AI note limit reached ({AI_NOTE_MAX_PER_HOUR}/hour). Try again later.")
    total = d.one("SELECT count(*) c FROM notes WHERE tags='delamain'")
    if total and int(total["c"] or 0) >= AI_NOTE_TOTAL_CAP:
        overflow = int(total["c"]) - AI_NOTE_TOTAL_CAP + 1
        ids = [r["id"] for r in d.query("SELECT id FROM notes WHERE tags='delamain' ORDER BY id ASC LIMIT ?", (overflow,))]
        for rid in ids:
            d.delete("notes", rid)
    label = str(tags or "").strip()[:500]
    cipher = v.encrypt(json.dumps({"title": heading, "body": content, "tags": label}))
    new_id = d.insert("notes", {"ts": utcnow(), "updated": utcnow(), "title": "ENCRYPTED",
                                "cipher": cipher, "tags": "delamain"})
    d.audit("ASSISTANT_NOTE", f"id={new_id} chars={len(content)}")
    return {"ok": True, "id": new_id, "title": heading,
            "limit_note": f"Saved encrypted vault note. Max {AI_NOTE_MAX_PER_HOUR}/hour, {AI_NOTE_TOTAL_CAP} total."}





# ------------------------------------------------------------------ vault reads
def _norm_note_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _vault_note_records(d: Database, v, limit: int = 500) -> list[dict]:
    if not v.configured():
        raise ValueError("Vault is not configured. Set it up in SECURITY first.")
    if not v.unlocked():
        raise ValueError("Vault is locked. Unlock it to read notes.")
    out = []
    for row in d.query(
        "SELECT id, ts, updated, cipher FROM notes ORDER BY updated DESC, id DESC LIMIT ?",
        (max(1, min(int(limit or 500), 2000)),),
    ):
        try:
            payload = json.loads(v.decrypt(row["cipher"]))
        except Exception:
            continue
        out.append({
            "id": row["id"],
            "title": str(payload.get("title") or "UNTITLED"),
            "body": str(payload.get("body") or ""),
            "tags": str(payload.get("tags") or ""),
            "ts": row["ts"],
            "updated": row["updated"] or row["ts"],
        })
    return out


def ai_vault_notes_list(d: Database, v, limit: int = 30) -> dict:
    notes = _vault_note_records(d, v)
    try:
        limit = max(1, min(int(limit or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    return {
        "ok": True,
        "count": len(notes),
        "notes": [
            {"id": n["id"], "title": n["title"], "tags": n["tags"],
             "updated": n["updated"], "chars": len(n["body"])}
            for n in notes[:limit]
        ],
    }


def _resolve_vault_note(notes: list[dict], note_id=None,
                        title: str | None = None) -> tuple[dict | None, dict | None]:
    if note_id not in (None, ""):
        try:
            wanted = int(note_id)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            for note in notes:
                if int(note["id"]) == wanted:
                    return note, None
    query = _norm_note_text(title or "")
    if not query:
        return None, {"ok": False, "error": "Provide a note title or id.",
                      "available": [n["title"] for n in notes[:12]]}
    q_tokens = set(query.split())
    scored = []
    for note in notes:
        nt = _norm_note_text(note["title"])
        nb = _norm_note_text(note["body"])[:4000]
        n_tokens = set(nt.split())
        if query == nt:
            score = 1.0
        elif query in nt or (len(nt) >= 6 and nt in query):
            score = 0.96
        elif query in nb:
            score = 0.78
        else:
            overlap = len(q_tokens & n_tokens) / max(1, len(q_tokens))
            body_overlap = len(q_tokens & set(nb.split())) / max(1, len(q_tokens))
            ratio = SequenceMatcher(None, query, nt).ratio()
            ratio_score = ratio * 0.82 if ratio >= 0.8 else 0.0
            overlap_score = overlap * 0.9 if (overlap >= 0.6 or len(q_tokens) == 1) else 0.0
            body_score = body_overlap * 0.55 if (body_overlap >= 0.6 or len(q_tokens) == 1) else 0.0
            score = max(ratio_score, overlap_score, body_score)
        if score >= 0.28:
            scored.append((score, note))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, {"ok": False,
                      "error": f"No vault note matched {title!r}.",
                      "available": [n["title"] for n in notes[:12]]}
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.12 and scored[0][0] < 0.9:
        return None, {"ok": False, "ambiguous": True,
                      "error": "Multiple vault notes matched. Specify the exact title or id.",
                      "matches": [
                          {"id": n["id"], "title": n["title"], "updated": n["updated"]}
                          for _, n in scored[:6]
                      ]}
    return scored[0][1], None


def ai_vault_note_get(d: Database, v, note_id=None, title: str | None = None,
                      max_body: int = 4000) -> dict:
    notes = _vault_note_records(d, v)
    if not notes:
        return {"ok": False, "error": "The encrypted vault has no notes."}
    note, error = _resolve_vault_note(notes, note_id, title)
    if error:
        return error
    return _vault_note_payload(note, max_body)


def ai_vault_note_update(d: Database, v, note_id=None, title: str | None = None,
                         new_title: str | None = None, body: str | None = None,
                         append: str | None = None, tags: str | None = None,
                         max_body: int = 4000) -> dict:
    """Edit an existing encrypted vault note. Never creates a new note."""
    notes = _vault_note_records(d, v)
    if not notes:
        return {"ok": False, "error": "The encrypted vault has no notes."}
    note, error = _resolve_vault_note(notes, note_id, title)
    if error:
        return error
    old_title = str(note.get("title") or "UNTITLED")
    old_body = str(note.get("body") or "")
    old_tags = str(note.get("tags") or "")
    heading = str(new_title).strip() if new_title not in (None, "") else old_title
    content = old_body if body is None else str(body)
    addition = str(append or "").strip()
    if addition:
        content = (content.rstrip() + "\n\n" + addition).strip() if content.strip() else addition
    label = old_tags if tags is None else str(tags)
    if heading == old_title and content == old_body and label == old_tags:
        return {"ok": False, "error": "No changes were provided for this note.",
                "note": {"id": note["id"], "title": old_title}}
    cipher = v.encrypt(json.dumps({"title": heading, "body": content, "tags": label}))
    now = utcnow()
    d.execute("UPDATE notes SET cipher=?, updated=? WHERE id=?", (cipher, now, note["id"]))
    d.audit("ASSISTANT_NOTE_EDIT", f"id={note['id']} chars={len(content)}")
    updated = dict(note, title=heading, body=content, tags=label, updated=now)
    return _vault_note_payload(updated, max_body)


def _vault_note_payload(note: dict, max_body: int = 4000) -> dict:
    body = note.get("body") or ""
    truncated = len(body) > max_body
    return {
        "ok": True,
        "note": {
            "id": note["id"], "title": note["title"],
            "body": body[:max_body], "tags": note.get("tags") or "",
            "updated": note.get("updated"),
            "truncated": truncated,
        },
    }


def ai_vault_read_reply(d: Database, v, prompt: str) -> str:
    """Deterministic reply for a read/list vault request (no model needed)."""
    notes = _vault_note_records(d, v)
    if not notes:
        return "Your encrypted vault has no notes yet."
    text = str(prompt or "")
    wants_list = bool(re.search(
        r"\b(?:list|all (?:my |the )?(?:vault )?notes|what notes|which notes|"
        r"how many notes|show (?:me )?(?:all )?(?:my |the )?notes|"
        r"what do i have|what's in (?:my |the )?vault)\b", text, re.I))
    query = re.sub(
        r"\b(?:please|can you|could you|would you|will you|read|retrieve|show|open|list|"
        r"find|load|search|recall|get|pull up|bring up|display|print|recover|access|"
        r"do i have|is there|any|what is|what's|tell me|me|my|the|a|an|note|notes|from|"
        r"in|inside|vault|to|aloud|out|contents?|of|about|for|stored|saved)\b",
        " ", text, flags=re.I,
    )
    query = re.sub(r"[^A-Za-z0-9 _-]", " ", query)
    query = re.sub(r"\s+", " ", query).strip(" .")
    if wants_list or not query:
        lines = [f"- **{n['title']}** — updated {n['updated']} ({len(n['body'])} chars)"
                 for n in notes[:30]]
        more = f"\n\n({len(notes) - 30} more not shown)" if len(notes) > 30 else ""
        return "Encrypted vault notes:\n\n" + "\n".join(lines) + more
    result = ai_vault_note_get(d, v, title=query, max_body=12000)
    if result.get("ok"):
        note = result["note"]
        out = f"**{note['title']}** — updated {note['updated']}\n\n{note['body']}"
        if note.get("truncated"):
            out += "\n\n_(Note truncated for display.)_"
        return out
    if result.get("ambiguous"):
        lines = [f"- **{m['title']}** (#{m['id']}) — updated {m['updated']}"
                 for m in result.get("matches") or []]
        return ("I found multiple vault notes matching that request. Which one?\n\n"
                + "\n".join(lines))
    available = result.get("available") or [n["title"] for n in notes[:12]]
    return ("No vault note matched that request. Available notes:\n\n"
            + "\n".join(f"- {t}" for t in available)
            + "\n\nNo new note was saved.")


# ------------------------------------------------------------------ operator records
def _ai_num(value):
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _ai_row(row) -> dict:
    return dict(row) if row is not None else {}


def ai_mission_log_search(d: Database, query: str = "", category: str = "",
                          limit: int = 10) -> dict:
    try:
        limit = max(1, min(int(limit or 10), 30))
    except (TypeError, ValueError):
        limit = 10
    clauses, params = [], []
    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("(title LIKE ? OR body LIKE ? OR tags LIKE ? OR category LIKE ?)")
        params.extend([like, like, like, like])
    cat = str(category or "").strip().upper()
    if cat:
        clauses.append("category = ?")
        params.append(cat)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = d.query(f"SELECT * FROM log_entries{where} ORDER BY ts DESC, id DESC LIMIT ?",
                   (*params, limit))
    items = []
    for row in rows:
        item = _ai_row(row)
        items.append({
            "id": item.get("id"), "ts": item.get("ts"), "category": item.get("category"),
            "title": item.get("title"), "body": str(item.get("body") or "")[:400],
            "tags": item.get("tags"),
        })
    return {"ok": True, "count": len(items), "items": items}


_TASK_PRIORITIES = ("IMMEDIATE", "PRIORITY", "ROUTINE")


def _find_task(d: Database, task_id=None, title: str | None = None):
    if task_id not in (None, ""):
        try:
            row = d.one("SELECT * FROM tasks WHERE id=?", (int(task_id),))
        except (TypeError, ValueError):
            row = None
        if row:
            return row, None
    q = str(title or "").strip()
    if not q:
        return None, {"ok": False, "error": "Provide a task id or title."}
    rows = d.query("SELECT * FROM tasks WHERE title LIKE ? ORDER BY done ASC, ts DESC LIMIT 8",
                   (f"%{q}%",))
    if not rows:
        return None, {"ok": False, "error": f"No task matched {q!r}.",
                      "open_tasks": [dict(r) for r in d.query(
                          "SELECT id,title,priority,due FROM tasks WHERE done=0 ORDER BY ts DESC LIMIT 12")]}
    if len(rows) > 1:
        exact = [r for r in rows if str(r["title"]).lower() == q.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, {"ok": False, "ambiguous": True,
                      "error": "Multiple tasks matched. Specify the id or exact title.",
                      "matches": [dict(r) for r in rows[:8]]}
    return rows[0], None


def ai_tasks_list(d: Database, status: str = "open", limit: int = 15) -> dict:
    try:
        limit = max(1, min(int(limit or 15), 50))
    except (TypeError, ValueError):
        limit = 15
    state = str(status or "open").lower()
    where = " WHERE done=0" if state in ("open", "active", "pending") else (
        " WHERE done=1" if state in ("done", "complete", "completed") else "")
    rows = d.query(
        "SELECT * FROM tasks" + where + " ORDER BY done ASC, CASE priority WHEN 'IMMEDIATE' "
        "THEN 0 WHEN 'PRIORITY' THEN 1 WHEN 'ROUTINE' THEN 2 ELSE 3 END, ts DESC LIMIT ?",
        (limit,),
    )
    tasks = []
    for row in rows:
        item = _ai_row(row)
        tasks.append({
            "id": item.get("id"), "title": item.get("title"),
            "detail": str(item.get("detail") or "")[:200],
            "priority": item.get("priority"), "due": item.get("due"),
            "done": int(item.get("done") or 0), "category": item.get("category"),
        })
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def ai_task_create(d: Database, title: str, detail: str = "", priority: str = "ROUTINE",
                   due: str | None = None, category: str = "GENERAL",
                   recurrence: str = "") -> dict:
    heading = str(title or "").strip()[:200]
    if not heading:
        return {"ok": False, "error": "A task title is required."}
    prio = str(priority or "ROUTINE").strip().upper()
    if prio not in _TASK_PRIORITIES:
        prio = "ROUTINE"
    task_id = d.insert("tasks", {
        "ts": utcnow(), "title": heading, "detail": str(detail or "")[:4000],
        "priority": prio, "due": str(due or "").strip()[:40] or None,
        "done": 0, "category": str(category or "GENERAL").strip().upper()[:40] or "GENERAL",
        "recurrence": str(recurrence or "").strip()[:120],
    })
    d.audit("ASSISTANT_TASK_CREATE", f"id={task_id} {heading[:100]}")
    task = _ai_row(d.one("SELECT * FROM tasks WHERE id=?", (task_id,)))
    task["detail"] = str(task.get("detail") or "")[:1000]
    return {"ok": True, "task": task,
            "undo": _register_undo({"kind": "task_delete", "id": task_id},
                                    f"Created task: {heading}")}


def ai_task_complete(d: Database, task_id=None, title: str | None = None) -> dict:
    row, error = _find_task(d, task_id, title)
    if error:
        return error
    task_id = int(row["id"])
    previous_done = int(row["done"] or 0)
    if previous_done:
        return {"ok": True, "already_done": True, "task": _ai_row(row)}
    d.execute("UPDATE tasks SET done=1 WHERE id=?", (task_id,))
    d.audit("ASSISTANT_TASK_COMPLETE", f"id={task_id}")
    return {"ok": True, "task": _ai_row(d.one("SELECT * FROM tasks WHERE id=?", (task_id,))),
            "undo": _register_undo({"kind": "task_done", "id": task_id, "done": previous_done},
                                    f"Completed task: {row['title']}")}


def _inventory_status(item: dict) -> str:
    qty = _ai_num(item.get("qty")) or 0.0
    minimum = _ai_num(item.get("min_qty")) or 0.0
    if minimum > 0 and qty <= minimum:
        return "LOW"
    return "OK"


def ai_inventory_query(d: Database, query: str = "", low_only: bool = False,
                       expiring_days=None, limit: int = 25) -> dict:
    try:
        limit = max(1, min(int(limit or 25), 100))
    except (TypeError, ValueError):
        limit = 25
    clauses, params = [], []
    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("(name LIKE ? OR category LIKE ? OR location LIKE ? OR notes LIKE ?)")
        params.extend([like, like, like, like])
    if low_only:
        clauses.append("min_qty > 0 AND qty <= min_qty")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = d.query(f"SELECT * FROM inventory{where} ORDER BY name LIMIT ?", (*params, limit * 2))
    out = []
    now = datetime.now(timezone.utc)
    try:
        days = int(expiring_days) if expiring_days not in (None, "") else None
    except (TypeError, ValueError):
        days = None
    for row in rows:
        item = _ai_row(row)
        item["status"] = _inventory_status(item)
        item = {
            "id": item.get("id"), "name": item.get("name"), "category": item.get("category"),
            "qty": item.get("qty"), "unit": item.get("unit"),
            "min_qty": item.get("min_qty"), "expires": item.get("expires"),
            "location": item.get("location"), "status": item.get("status"),
        }
        if days is not None:
            exp = str(item.get("expires") or "").strip()
            if not exp:
                continue
            try:
                parsed = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                remaining = (parsed - now).days
                if remaining > days:
                    continue
                item["days_until_expiry"] = remaining
            except ValueError:
                continue
        out.append(item)
    return {"ok": True, "count": len(out[:limit]), "items": out[:limit]}


def _find_inventory(d: Database, item_id=None, name: str | None = None):
    if item_id not in (None, ""):
        try:
            row = d.one("SELECT * FROM inventory WHERE id=?", (int(item_id),))
        except (TypeError, ValueError):
            row = None
        if row:
            return row, None
    q = str(name or "").strip()
    if not q:
        return None, {"ok": False, "error": "Provide an inventory item id or name."}
    rows = d.query("SELECT * FROM inventory WHERE name LIKE ? ORDER BY name LIMIT 8", (f"%{q}%",))
    if not rows:
        return None, None
    if len(rows) > 1:
        exact = [r for r in rows if str(r["name"]).lower() == q.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, {"ok": False, "ambiguous": True,
                      "error": "Multiple inventory items matched. Specify the id or exact name.",
                      "matches": [dict(r) for r in rows[:8]]}
    return rows[0], None


def ai_inventory_adjust(d: Database, item_id=None, name: str | None = None,
                        delta=None, qty=None, unit: str | None = None,
                        category: str | None = None, min_qty=None,
                        location: str | None = None, notes: str | None = None,
                        expires: str | None = None) -> dict:
    row, error = _find_inventory(d, item_id, name)
    if error:
        return error
    change = _ai_num(delta)
    set_qty = _ai_num(qty)
    if row is None:
        heading = str(name or "").strip()[:200]
        if not heading or (change is None and set_qty is None):
            return {"ok": False, "error": "No matching inventory item. Provide a name and qty/delta to create one."}
        new_qty = max(0.0, set_qty if set_qty is not None else (change or 0.0))
        new_id = d.insert("inventory", {
            "name": heading, "category": str(category or "GENERAL").strip().upper()[:40] or "GENERAL",
            "qty": new_qty, "unit": str(unit or "ea").strip()[:20] or "ea",
            "min_qty": _ai_num(min_qty) or 0.0, "expires": str(expires or "").strip() or None,
            "location": str(location or "").strip()[:120], "notes": str(notes or "").strip()[:1000],
            "updated": utcnow(),
        })
        d.audit("ASSISTANT_INVENTORY_CREATE", f"id={new_id} {heading[:100]} qty={new_qty:g}")
        item = _ai_row(d.one("SELECT * FROM inventory WHERE id=?", (new_id,)))
        item["status"] = _inventory_status(item)
        return {"ok": True, "created": True, "item": item,
                "undo": _register_undo({"kind": "inventory_delete", "id": new_id},
                                        f"Created inventory item: {heading}")}
    current = _ai_num(row["qty"]) or 0.0
    previous = {key: row[key] for key in ("qty", "unit", "category", "min_qty", "location", "notes", "expires")}
    new_qty = set_qty if set_qty is not None else current + (change or 0.0)
    new_qty = max(0.0, new_qty)
    fields = {"qty": new_qty, "updated": utcnow()}
    if unit is not None:
        fields["unit"] = str(unit).strip()[:20] or "ea"
    if category is not None:
        fields["category"] = str(category).strip().upper()[:40] or "GENERAL"
    if min_qty is not None:
        fields["min_qty"] = _ai_num(min_qty) or 0.0
    if location is not None:
        fields["location"] = str(location).strip()[:120]
    if notes is not None:
        fields["notes"] = str(notes).strip()[:1000]
    if expires is not None:
        fields["expires"] = str(expires).strip() or None
    if new_qty == current and len(fields) == 2:
        return {"ok": False, "error": "No inventory change was requested."}
    sets = ", ".join(f"{k}=?" for k in fields)
    d.execute(f"UPDATE inventory SET {sets} WHERE id=?", (*fields.values(), int(row["id"])))
    d.audit("ASSISTANT_INVENTORY_ADJUST", f"id={row['id']} qty {current:g}->{new_qty:g}")
    item = _ai_row(d.one("SELECT * FROM inventory WHERE id=?", (int(row["id"]),)))
    item["status"] = _inventory_status(item)
    return {"ok": True, "item": item, "previous_qty": current, "new_qty": new_qty,
            "undo": _register_undo(
                {"kind": "inventory_restore", "id": int(row["id"]), "prev": previous},
                f"Inventory {row['name']}: {current:g} → {new_qty:g}")}


def ai_incident_create(d: Database, title: str, detail: str = "", severity: str = "INFO",
                       category: str = "SECURITY", lat=None, lon=None) -> dict:
    heading = str(title or "").strip()[:200]
    if not heading:
        return {"ok": False, "error": "An incident title is required."}
    sev = str(severity or "INFO").strip().upper()
    if sev not in ("CRITICAL", "WARNING", "ALERT", "INFO"):
        sev = "INFO"
    la, lo = _ai_num(lat), _ai_num(lon)
    if la is not None and not (-90 <= la <= 90):
        la = None
    if lo is not None and not (-180 <= lo <= 180):
        lo = None
    inc_id = d.insert("incidents", {
        "ts": utcnow(), "severity": sev, "category": str(category or "SECURITY").strip().upper()[:40] or "SECURITY",
        "title": heading, "detail": str(detail or "")[:4000], "lat": la, "lon": lo, "resolved": 0,
    })
    d.audit("ASSISTANT_INCIDENT_CREATE", f"id={inc_id} severity={sev}")
    incident = _ai_row(d.one("SELECT * FROM incidents WHERE id=?", (inc_id,)))
    incident["detail"] = str(incident.get("detail") or "")[:2000]
    return {"ok": True, "incident": incident,
            "undo": _register_undo({"kind": "incident_delete", "id": inc_id},
                                    f"Created incident: {heading}")}


def ai_incident_resolve(d: Database, incident_id=None, title: str | None = None,
                        resolution: str = "") -> dict:
    row = None
    if incident_id not in (None, ""):
        try:
            row = d.one("SELECT * FROM incidents WHERE id=?", (int(incident_id),))
        except (TypeError, ValueError):
            row = None
    if row is None and str(title or "").strip():
        q = str(title).strip()
        rows = d.query("SELECT * FROM incidents WHERE resolved=0 AND title LIKE ? ORDER BY ts DESC LIMIT 8",
                       (f"%{q}%",))
        if len(rows) == 1:
            row = rows[0]
        elif len(rows) > 1:
            return {"ok": False, "ambiguous": True,
                    "error": "Multiple unresolved incidents matched. Specify the id or exact title.",
                    "matches": [dict(r) for r in rows[:8]]}
    if row is None:
        return {"ok": False, "error": "No matching unresolved incident found.",
                "open_incidents": [dict(r) for r in d.query(
                    "SELECT id,title,severity,ts FROM incidents WHERE resolved=0 ORDER BY ts DESC LIMIT 12")]}
    inc_id = int(row["id"])
    previous_detail = str(row["detail"] or "")
    detail = previous_detail
    note = str(resolution or "").strip()
    if note:
        detail = (detail + "\n\nResolution: " + note).strip()
    d.execute("UPDATE incidents SET resolved=1, detail=? WHERE id=?", (detail[:8000], inc_id))
    d.audit("ASSISTANT_INCIDENT_RESOLVE", f"id={inc_id}")
    incident = _ai_row(d.one("SELECT * FROM incidents WHERE id=?", (inc_id,)))
    incident["detail"] = str(incident.get("detail") or "")[:2000]
    return {"ok": True, "incident": incident,
            "undo": _register_undo({"kind": "incident_restore", "id": inc_id,
                                    "resolved": 0, "detail": previous_detail},
                                    f"Resolved incident: {row['title']}")}


def _checklist_state(d: Database) -> dict:
    return {f"{r['kit_id']}:{r['item_id']}": int(r["checked"] or 0)
            for r in d.query("SELECT * FROM checklist_state")}


def _find_checklist_kit(kit_id=None, kit_name=None):
    kits = CHECKLIST_DATA.get("kits") or []
    wanted = str(kit_id or kit_name or "").strip().lower()
    if not wanted:
        return None
    for kit in kits:
        if str(kit.get("id") or "").lower() == wanted or str(kit.get("name") or "").lower() == wanted:
            return kit
    for kit in kits:
        if wanted in str(kit.get("name") or "").lower() or wanted in str(kit.get("id") or "").lower():
            return kit
    return None


def ai_checklist_read(d: Database, kit_id=None, kit_name=None) -> dict:
    state = _checklist_state(d)
    kits = CHECKLIST_DATA.get("kits") or []
    selected = _find_checklist_kit(kit_id, kit_name)
    if (kit_id or kit_name) and selected is None:
        return {"ok": False, "error": "Checklist kit not found.",
                "kits": [{"id": k.get("id"), "name": k.get("name")} for k in kits]}
    output = []
    for kit in ([selected] if selected else kits):
        items = []
        checked = 0
        for item in kit.get("items") or []:
            is_checked = bool(state.get(f"{kit['id']}:{item['id']}", 0))
            checked += 1 if is_checked else 0
            items.append({"id": item.get("id"), "text": item.get("text"),
                          "critical": bool(item.get("critical")), "checked": is_checked})
        entry = {"id": kit.get("id"), "name": kit.get("name"),
                 "description": kit.get("description"), "checked": checked,
                 "total": len(items)}
        if selected is not None:
            entry["items"] = items
        output.append(entry)
    return {"ok": True, "kits": output, "note": CHECKLIST_DATA.get("note", "")}


def ai_checklist_update(d: Database, kit_id=None, item_id=None, item_text=None,
                        checked: bool = True) -> dict:
    if not isinstance(checked, bool):
        checked = str(checked).strip().lower() in ("1", "true", "yes", "checked", "on")
    kit = _find_checklist_kit(kit_id, kit_id)
    if kit is None:
        return {"ok": False, "error": "Checklist kit not found.",
                "kits": [{"id": k.get("id"), "name": k.get("name")}
                         for k in (CHECKLIST_DATA.get("kits") or [])]}
    item = None
    if item_id not in (None, ""):
        item = next((i for i in (kit.get("items") or [])
                     if str(i.get("id")) == str(item_id)), None)
    if item is None and str(item_text or "").strip():
        q = str(item_text).strip().lower()
        matches = [i for i in (kit.get("items") or [])
                   if q in str(i.get("text") or "").lower()]
        if len(matches) == 1:
            item = matches[0]
        elif len(matches) > 1:
            return {"ok": False, "ambiguous": True,
                    "error": "Multiple checklist items matched. Specify the item id.",
                    "matches": [{"id": i.get("id"), "text": i.get("text")} for i in matches[:8]]}
    if item is None:
        return {"ok": False, "error": "Checklist item not found in that kit.",
                "items": [{"id": i.get("id"), "text": i.get("text")}
                          for i in (kit.get("items") or [])]}
    previous = bool(_checklist_state(d).get(f"{kit['id']}:{item['id']}", 0))
    d.execute("INSERT INTO checklist_state(kit_id,item_id,checked,updated) VALUES(?,?,?,?) "
              "ON CONFLICT(kit_id,item_id) DO UPDATE SET checked=excluded.checked, updated=excluded.updated",
              (kit["id"], item["id"], 1 if checked else 0, utcnow()))
    d.audit("ASSISTANT_CHECKLIST_UPDATE", f"{kit['id']}:{item['id']} checked={bool(checked)}")
    return {"ok": True, "kit": kit.get("name"), "item": item.get("text"),
            "item_id": item.get("id"), "checked": bool(checked),
            "undo": _register_undo(
                {"kind": "checklist_restore", "kit_id": kit["id"], "item_id": item["id"],
                 "checked": previous},
                f"{'Checked' if checked else 'Unchecked'}: {item.get('text')}")}


def _water_flag(row: dict) -> str | None:
    ppm = _ai_num(row.get("chlorine_ppm"))
    if ppm is None:
        return None
    if ppm < 0.2:
        return "LOW"
    if ppm > 4.0:
        return "HIGH"
    return "OK"


def ai_water_log_read(d: Database, limit: int = 10, days=None) -> dict:
    try:
        limit = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        limit = 10
    rows = d.query("SELECT * FROM water_log ORDER BY ts DESC, id DESC LIMIT ?", (limit * 4,))
    out = []
    now = datetime.now(timezone.utc)
    try:
        cutoff = now - timedelta(days=int(days)) if days not in (None, "") else None
    except (TypeError, ValueError):
        cutoff = None
    for row in rows:
        item = _ai_row(row)
        item["chlorine_status"] = _water_flag(item)
        item = {
            "id": item.get("id"), "ts": item.get("ts"), "source": item.get("source"),
            "volume_l": item.get("volume_l"), "method": item.get("method"),
            "chlorine_ppm": item.get("chlorine_ppm"),
            "note": str(item.get("note") or "")[:300],
            "chlorine_status": item.get("chlorine_status"),
        }
        if cutoff is not None:
            try:
                ts = datetime.fromisoformat(str(item.get("ts") or "").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except ValueError:
                continue
        out.append(item)
        if len(out) >= limit:
            break
    return {"ok": True, "count": len(out), "items": out}


def ai_water_test_log(d: Database, source: str = "", volume_l=None, method: str = "",
                      chlorine_ppm=None, note: str = "") -> dict:
    ppm = _ai_num(chlorine_ppm)
    vol = _ai_num(volume_l)
    if not (str(source or "").strip() or str(method or "").strip()
            or ppm is not None or str(note or "").strip()):
        return {"ok": False, "error": "Provide a source, method, chlorine reading or note."}
    new_id = d.insert("water_log", {
        "ts": utcnow(), "source": str(source or "").strip()[:120],
        "volume_l": vol, "method": str(method or "").strip()[:120],
        "chlorine_ppm": ppm, "note": str(note or "").strip()[:2000],
    })
    row = _ai_row(d.one("SELECT * FROM water_log WHERE id=?", (new_id,)))
    row["chlorine_status"] = _water_flag(row)
    d.audit("ASSISTANT_WATER_LOG", f"id={new_id} ppm={ppm if ppm is not None else '—'}")
    return {"ok": True, "item": row,
            "undo": _register_undo({"kind": "water_delete", "id": new_id},
                                    f"Logged water test: {row.get('source') or 'water'}")}


_SYSTEM_DENY_PARTS = {
    ".ssh", ".gnupg", ".aws", ".kube", ".docker", ".password-store",
    "keyrings", "id_rsa", "id_ed25519", "credentials", "tokens", ".env",
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".authinfo",
    "secrets", "secret", "token", "gcloud", "azure", "darksec-nightwatch",
}
_SYSTEM_DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")
_SYSTEM_DENY_PATHS = (
    "/etc/shadow", "/etc/sudoers", "/etc/sudoers.d", "/root",
    "/proc", "/sys", "/dev", "/boot", "/bin", "/sbin", "/usr", "/lib",
    "/lib64", "/var/lib", "/run",
)


_TEXT_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)


def _decode_text_bytes(raw: bytes) -> tuple[str, str]:
    for bom, encoding in _TEXT_BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding, errors="replace"), encoding
            except Exception:
                pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    if _charset_from_bytes is not None:
        try:
            best = _charset_from_bytes(raw).best()
            if best is not None:
                decoded = str(best)
                if "\x00" not in decoded[:4096]:
                    return decoded, str(best.encoding or "detected")
        except Exception:
            pass
    for encoding in ("cp1252", "latin-1"):
        try:
            return raw.decode(encoding, errors="replace"), encoding
        except Exception:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _looks_binary(raw: bytes) -> bool:
    if b"\x00" not in raw[:4096]:
        return False
    # UTF-16/32 text is accepted only with a BOM; arbitrary NUL-bearing data
    # is treated as binary rather than guessed into printable gibberish.
    return not any(raw.startswith(bom) for bom, _ in _TEXT_BOMS)


def _extract_document_text(path: Path) -> tuple[str, str, int | None] | None:
    ext = path.suffix.lower()
    if ext in (".pdf", ".epub", ".cbz", ".html", ".htm"):
        try:
            text, pages = library.extract_text(path, ext)
        except Exception:
            return None
        return (text or ""), ext.lstrip("."), pages
    if ext in (".docx", ".odt"):
        member = "word/document.xml" if ext == ".docx" else "content.xml"
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read(member).decode("utf-8", errors="replace")
        except Exception:
            return None
        xml = re.sub(r"(?i)</(?:w:p|text:p|text:h)>", "\n", xml)
        text = re.sub(r"(?s)<[^>]+>", " ", xml)
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text, ext.lstrip("."), None
    return None


def _system_root(d: Database) -> Path:
    raw = str(d.get_setting("assistant_system_root", "~") or "~").strip() or "~"
    return Path(os.path.expanduser(raw)).resolve()


def _system_path(d: Database, raw: str) -> tuple[Path, Path]:
    root = _system_root(d)
    candidate = Path(os.path.expanduser(str(raw or ".")))
    if not candidate.is_absolute():
        candidate = root / candidate
    target = candidate.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"Path is outside the configured system-tools root ({root}).")
    try:
        rel_parts = [p.lower() for p in target.relative_to(root).parts]
    except ValueError:
        rel_parts = [p.lower() for p in target.parts]
    if any(part in _SYSTEM_DENY_PARTS for part in rel_parts):
        raise ValueError("Path is blocked by the system-tools credential policy.")
    if target.suffix.lower() in _SYSTEM_DENY_SUFFIXES:
        raise ValueError("Credential/key file extensions are blocked.")
    target_text = str(target)
    if any(target_text == p or target_text.startswith(p.rstrip("/") + "/")
           for p in _SYSTEM_DENY_PATHS):
        raise ValueError("System path is blocked by policy.")
    app_data = Path(config.DATA_DIR).resolve()
    if target == app_data or app_data in target.parents:
        raise ValueError("The application data directory is blocked from assistant file tools.")
    return root, target


def ai_system_list(d: Database, path: str = ".", limit: int = 200) -> dict:
    if not d.get_setting("assistant_system_tools", False):
        return {"ok": False, "error": "System file tools are disabled in Settings."}
    try:
        limit = max(1, min(int(limit or 200), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        root, target = _system_path(d, path)
        if not target.exists():
            return {"ok": False, "error": f"Path does not exist: {target}"}
        if not target.is_dir():
            return {"ok": False, "error": f"Not a directory: {target}"}
        entries = []
        for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:limit]:
            if entry.name.lower() in _SYSTEM_DENY_PARTS:
                continue
            try:
                st = entry.stat()
                entries.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": st.st_size if entry.is_file() else None,
                    "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                })
            except OSError:
                entries.append({"name": entry.name, "type": "unknown", "size": None, "modified": None})
        d.audit("ASSISTANT_SYSTEM_LIST", str(target)[:300])
        return {"ok": True, "root": str(root), "path": str(target),
                "count": len(entries), "entries": entries}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def ai_system_read(d: Database, path: str, max_chars: int = 6000) -> dict:
    if not d.get_setting("assistant_system_tools", False):
        return {"ok": False, "error": "System file tools are disabled in Settings."}
    try:
        max_chars = max(200, min(int(max_chars or 6000), 20000))
    except (TypeError, ValueError):
        max_chars = 6000
    try:
        root, target = _system_path(d, path)
        if not target.exists() or not target.is_file():
            return {"ok": False, "error": f"File not found: {target}"}
        size = target.stat().st_size
        is_doc = target.suffix.lower() in (".pdf", ".epub", ".cbz", ".html", ".htm", ".docx", ".odt")
        if size > (20_000_000 if is_doc else 5_000_000):
            return {"ok": False, "error": "File exceeds the read size limit."}
        extracted = _extract_document_text(target)
        if extracted is not None:
            text, fmt, pages = extracted
            encoding = "extracted"
        else:
            raw = target.read_bytes()
            if _looks_binary(raw):
                return {"ok": False, "error": "Binary file; only text and supported document formats can be read."}
            text, encoding = _decode_text_bytes(raw)
            fmt = target.suffix.lstrip(".").lower() or "text"
            pages = None
        if not text.strip():
            return {"ok": False, "error": "No extractable text found (scanned/image-only or empty file)."}
        d.audit("ASSISTANT_SYSTEM_READ", str(target)[:300])
        return {"ok": True, "root": str(root), "path": str(target),
                "format": fmt, "encoding": encoding, "pages": pages,
                "chars": len(text), "truncated": len(text) > max_chars,
                "content": text[:max_chars]}
    except PermissionError:
        return {"ok": False, "error": "Permission denied (non-sudo tools cannot elevate)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def ai_system_write(d: Database, path: str, content: str, overwrite: bool = False,
                    append: bool = False) -> dict:
    if not d.get_setting("assistant_system_tools", False):
        return {"ok": False, "error": "System file tools are disabled in Settings."}
    text = str(content or "")
    if len(text.encode("utf-8", errors="ignore")) > 1_000_000:
        return {"ok": False, "error": "Content exceeds the 1 MB write limit."}
    try:
        root, target = _system_path(d, path)
        if target.exists() and target.is_dir():
            return {"ok": False, "error": f"Target is a directory: {target}"}
        if not target.parent.exists() or not target.parent.is_dir():
            return {"ok": False, "error": f"Parent directory does not exist: {target.parent}"}
        if target.exists() and not overwrite and not append:
            return {"ok": False, "exists": True,
                    "error": "File exists. Set overwrite=true to replace it (a .bak backup is created)."}
        backup = None
        if target.exists() and not append and target.stat().st_size <= 5_000_000:
            backup = target.parent / (target.name + ".bak")
            shutil.copy2(target, backup)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as handle:
            handle.write(text)
        d.audit("ASSISTANT_SYSTEM_WRITE", f"{target} bytes={len(text.encode('utf-8'))} append={bool(append)} overwrite={bool(overwrite)}")
        return {"ok": True, "root": str(root), "path": str(target),
                "bytes": len(text.encode("utf-8")), "append": bool(append),
                "backup": str(backup) if backup else None}
    except PermissionError:
        return {"ok": False, "error": "Permission denied (non-sudo tools cannot elevate)."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


def _save_title_from_text(text: str) -> str:
    for line in str(text or "").splitlines():
        clean = re.sub(r"^[#>*\-\s]+", "", line).strip()
        if clean:
            return clean[:120]
    return "Delamain chat"


def vault_save_fast_events(d: Database, v, prompt: str, history: list[dict] | None):
    """Save the exact previous message when the operator says 'save this'."""
    content = assistant.last_reference_text(history, prompt)
    if not content:
        return None
    title = _save_title_from_text(content)

    def events():
        if v.configured() and not v.unlocked():
            yield {
                "type": "vault_unlock_required",
                "tool": "save_note",
                "title": title,
                "detail": "Delamain needs to save this response to the encrypted vault. Unlock it to continue; the note is saved automatically.",
            }
            deadline = time.time() + 120.0
            while time.time() < deadline and not v.unlocked():
                time.sleep(0.4)
            yield {"type": "vault_unlock_result", "tool": "save_note",
                   "unlocked": bool(v.unlocked())}
            if not v.unlocked():
                yield {"type": "done", "ok": True, "model": "vault", "content":
                       "Vault is locked. Unlock it in SECURITY to save this note.",
                       "thinking": "", "thinking_present": False, "tools": [],
                       "created_at": utcnow()}
                return
        try:
            result = ai_note_save(d, v, title, content, "delamain,chat-log",
                                  max_chars=AI_CHAT_MAX_CHARS)
            content_out = f"Saved to the encrypted vault: **{title}** (#{result.get('id')})."
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:300]}
            content_out = f"Could not save to the vault: {str(exc)[:300]}"
        yield {"type": "done", "ok": True, "model": "vault", "content": content_out,
               "thinking": "", "thinking_present": False,
               "tools": [{"name": "save_note", "args": {"title": title}, "result": result}],
               "created_at": utcnow()}

    return events()


def _calc_catalog_entry(name: str) -> dict | None:
    return next((entry for entry in CALC_CATALOG if entry.get("name") == name), None)


def ai_calc_list(name: str = "", group: str = "") -> dict:
    wanted_name = str(name or "").strip()
    wanted_group = str(group or "").strip().upper()
    items = []
    for entry in CALC_CATALOG:
        if wanted_group and str(entry.get("group") or "").upper() != wanted_group:
            continue
        if wanted_name and wanted_name != entry.get("name"):
            continue
        item = {"name": entry.get("name"), "label": entry.get("label"),
                "group": entry.get("group")}
        if wanted_name:
            item["fields"] = [
                {"name": field.get("name"), "label": field.get("label"),
                 "type": field.get("type"), "unit": field.get("unit")}
                for field in (entry.get("fields") or [])
            ]
        items.append(item)
    return {"ok": True, "count": len(items), "calculators": items[:80]}


def ai_calculate(name: str, params: dict | None = None) -> dict:
    import inspect
    calc_name = str(name or "").strip()
    fn = CALC_FUNCS.get(calc_name)
    entry = _calc_catalog_entry(calc_name)
    if fn is None:
        matches = [{"name": e.get("name"), "label": e.get("label"), "group": e.get("group")}
                   for e in CALC_CATALOG
                   if calc_name.lower() in str(e.get("name") or "").lower()
                   or calc_name.lower() in str(e.get("label") or "").lower()][:12]
        return {"ok": False, "error": f"Unknown calculator {calc_name!r}.",
                "matches": matches}
    payload = params if isinstance(params, dict) else {}
    try:
        signature = inspect.signature(fn)
        kwargs = {}
        for pname, param in signature.parameters.items():
            if pname not in payload or payload[pname] in ("", None):
                continue
            value = payload[pname]
            ann = param.annotation
            if ann in (int, float) or ann in ("int", "float"):
                value = float(value)
                if ann is int or ann == "int":
                    value = int(value)
            kwargs[pname] = value
        result = fn(**kwargs)
        return {"ok": True, "name": calc_name,
                "label": (entry or {}).get("label") or calc_name,
                "result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300],
                "name": calc_name,
                "fields": (entry or {}).get("fields") or []}


def ai_convert_coordinates(lat, lon) -> dict:
    try:
        la = float(lat)
        lo = float(lon)
        if not (-90 <= la <= 90 and -180 <= lo <= 180):
            return {"ok": False, "error": "Coordinates out of range."}
        return {"ok": True, **geo.format_coord(la, lo)}
    except (TypeError, ValueError):
        return {"ok": False, "error": "lat and lon are required numbers."}


def _compass_point(bearing: float) -> str:
    points = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
              "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return points[int((float(bearing) % 360) / 22.5 + 0.5) % 16]


def ai_distance_bearing(d: Database, lat1=None, lon1=None, lat2=None, lon2=None) -> dict:
    s = d.all_settings()
    try:
        if lat1 is None or lon1 is None:
            lat1, lon1 = float(s.get("lat")), float(s.get("lon"))
        la1, lo1, la2, lo2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Valid start and destination coordinates are required."}
    if not (-90 <= la1 <= 90 and -180 <= lo1 <= 180 and
            -90 <= la2 <= 90 and -180 <= lo2 <= 180):
        return {"ok": False, "error": "Coordinates out of range."}
    distance_m = geo.haversine(la1, lo1, la2, lo2)
    distance_km = distance_m / 1000.0
    bearing = geo.initial_bearing(la1, lo1, la2, lo2)
    mid = geo.midpoint(la1, lo1, la2, lo2)
    return {
        "ok": True,
        "start": {"lat": la1, "lon": lo1},
        "destination": {"lat": la2, "lon": lo2},
        "distance_m": round(distance_m, 1),
        "distance_km": round(distance_km, 3),
        "distance_mi": round(distance_m / 1609.344, 3),
        "bearing_deg": round(bearing % 360, 1),
        "bearing_cardinal": _compass_point(bearing),
        "midpoint": {"lat": round(mid[0], 6), "lon": round(mid[1], 6)},
    }


def ai_waypoints_list(d: Database, query: str = "", radius_km=None, limit: int = 25) -> dict:
    try:
        limit = max(1, min(int(limit or 25), 100))
    except (TypeError, ValueError):
        limit = 25
    q = str(query or "").strip()
    rows = d.query("SELECT * FROM waypoints ORDER BY name LIMIT 2000")
    s = d.all_settings()
    center = None
    if radius_km not in (None, ""):
        try:
            center = (float(s.get("lat")), float(s.get("lon")))
            radius = float(radius_km)
        except (TypeError, ValueError):
            return {"ok": False, "error": "A valid operator position and radius_km are required."}
    out = []
    for row in rows:
        if q and q.lower() not in f"{row['name']} {row['type']} {row['note']}".lower():
            continue
        item = {"id": row["id"], "name": row["name"], "lat": row["lat"], "lon": row["lon"],
                "ele": row["ele"], "type": row["type"], "note": str(row["note"] or "")[:300]}
        if center is not None:
            distance_km = geo.haversine(center[0], center[1], row["lat"], row["lon"]) / 1000.0
            if distance_km > radius:
                continue
            item["distance_km"] = round(distance_km, 2)
        out.append(item)
    if center is not None:
        out.sort(key=lambda item: item.get("distance_km", 0))
    return {"ok": True, "count": len(out[:limit]), "waypoints": out[:limit]}


def ai_waypoint_create(d: Database, name: str, lat, lon, ele=None, type: str = "WAYPOINT",
                       note: str = "") -> dict:
    heading = str(name or "").strip()[:200]
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"ok": False, "error": "name, lat and lon are required."}
    if not heading or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return {"ok": False, "error": "Valid name, lat and lon are required."}
    try:
        elevation = float(ele) if ele not in (None, "") else None
    except (TypeError, ValueError):
        elevation = None
    now = utcnow()
    waypoint_id = d.insert("waypoints", {
        "name": heading, "lat": la, "lon": lo, "ele": elevation,
        "type": str(type or "WAYPOINT").strip().upper()[:40] or "WAYPOINT",
        "note": str(note or "").strip()[:1000], "created": now, "updated": now,
    })
    d.audit("ASSISTANT_WAYPOINT_CREATE", f"id={waypoint_id} {heading[:100]}")
    return {"ok": True, "waypoint": _ai_row(d.one("SELECT * FROM waypoints WHERE id=?", (waypoint_id,))),
            "undo": _register_undo({"kind": "waypoint_delete", "id": waypoint_id},
                                    f"Created waypoint: {heading}")}


def _find_patient(d: Database, patient_id=None, callsign: str = "", name: str = ""):
    if patient_id not in (None, ""):
        try:
            row = d.one("SELECT * FROM patients WHERE id=?", (int(patient_id),))
        except (TypeError, ValueError):
            row = None
        if row:
            return row, None
    q = str(callsign or name or "").strip()
    if not q:
        return None, {"ok": False, "error": "Provide a patient id, callsign or name."}
    rows = d.query(
        "SELECT * FROM patients WHERE callsign LIKE ? OR name LIKE ? "
        "ORDER BY updated DESC, created DESC LIMIT 12",
        (f"%{q}%", f"%{q}%"),
    )
    if not rows:
        return None, {"ok": False, "error": f"No patient matched {q!r}."}
    if len(rows) > 1:
        exact = [r for r in rows if str(r["callsign"]).lower() == q.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, {"ok": False, "ambiguous": True,
                      "error": "Multiple patients matched. Specify the callsign or id.",
                      "matches": [{"id": r["id"], "callsign": r["callsign"],
                                   "name": r["name"], "triage": r["triage"],
                                   "status": r["status"]} for r in rows[:8]]}
    return rows[0], None


def ai_patients_list(d: Database, query: str = "", triage: str = "",
                     status: str = "", limit: int = 20) -> dict:
    if not d.get_setting("assistant_medical_tools", False):
        return {"ok": False, "error": "Medical record tools are disabled in Settings."}
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    clauses, params = [], []
    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("(callsign LIKE ? OR name LIKE ? OR allergies LIKE ? OR meds LIKE ? OR note LIKE ?)")
        params.extend([like] * 5)
    if str(triage or "").strip():
        clauses.append("triage = ?"); params.append(str(triage).strip().upper())
    if str(status or "").strip():
        clauses.append("status = ?"); params.append(str(status).strip().upper())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = d.query(f"SELECT * FROM patients{where} ORDER BY updated DESC, created DESC LIMIT ?",
                   (*params, limit))
    items = [{"id": r["id"], "callsign": r["callsign"], "name": r["name"],
              "age": r["age"], "sex": r["sex"], "triage": r["triage"],
              "status": r["status"], "updated": r["updated"]} for r in rows]
    return {"ok": True, "count": len(items), "patients": items,
            "note": "Read-only medical records. Not a diagnosis or prescription."}


def ai_patient_get(d: Database, patient_id=None, callsign: str = "", name: str = "",
                   include_vitals: bool = True, include_meds: bool = True,
                   limit: int = 6) -> dict:
    if not d.get_setting("assistant_medical_tools", False):
        return {"ok": False, "error": "Medical record tools are disabled in Settings."}
    row, error = _find_patient(d, patient_id, callsign, name)
    if error:
        return error
    try:
        limit = max(1, min(int(limit or 6), 20))
    except (TypeError, ValueError):
        limit = 6
    patient = _ai_row(row)
    for key, cap in (("history", 1000), ("note", 800), ("allergies", 400), ("meds", 600)):
        patient[key] = str(patient.get(key) or "")[:cap]
    out = {"ok": True, "patient": patient,
           "note": "Read-only record. Do not diagnose or prescribe; recommend qualified care."}
    if include_vitals:
        out["vitals"] = [
            {"id": r["id"], "ts": r["ts"], "hr": r["hr"], "rr": r["rr"],
             "sys": r["sys"], "dia": r["dia"], "spo2": r["spo2"],
             "temp_c": r["temp_c"], "glucose": r["glucose"], "pain": r["pain"],
             "avpu": r["avpu"], "note": str(r["note"] or "")[:200]}
            for r in d.query(
                "SELECT * FROM vitals WHERE patient_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (int(row["id"]), limit))
        ]
    if include_meds:
        out["medications"] = [
            {"id": r["id"], "ts": r["ts"], "medication": r["medication"],
             "dose": r["dose"], "route": r["route"],
             "administered_by": r["administered_by"], "indication": r["indication"],
             "reaction": r["reaction"], "note": str(r["note"] or "")[:200]}
            for r in d.query(
                "SELECT * FROM medication_log WHERE patient_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                (int(row["id"]), limit))
        ]
    d.audit("ASSISTANT_MEDICAL_READ", f"patient={row['id']}")
    return out


def ai_vitals_list(d: Database, patient_id=None, callsign: str = "", name: str = "",
                   limit: int = 15) -> dict:
    if not d.get_setting("assistant_medical_tools", False):
        return {"ok": False, "error": "Medical record tools are disabled in Settings."}
    row, error = _find_patient(d, patient_id, callsign, name)
    if error:
        return error
    try:
        limit = max(1, min(int(limit or 15), 40))
    except (TypeError, ValueError):
        limit = 15
    rows = d.query("SELECT * FROM vitals WHERE patient_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                   (int(row["id"]), limit))
    d.audit("ASSISTANT_MEDICAL_READ", f"patient={row['id']} vitals")
    vitals = [
        {"id": r["id"], "ts": r["ts"], "hr": r["hr"], "rr": r["rr"],
         "sys": r["sys"], "dia": r["dia"], "spo2": r["spo2"],
         "temp_c": r["temp_c"], "glucose": r["glucose"], "pain": r["pain"],
         "avpu": r["avpu"], "note": str(r["note"] or "")[:200]}
        for r in rows
    ]
    return {"ok": True, "patient": {"id": row["id"], "callsign": row["callsign"]},
            "count": len(vitals), "vitals": vitals,
            "note": "Read-only observations. Not a diagnosis."}


def ai_medications_list(d: Database, patient_id=None, callsign: str = "", name: str = "",
                        limit: int = 10) -> dict:
    if not d.get_setting("assistant_medical_tools", False):
        return {"ok": False, "error": "Medical record tools are disabled in Settings."}
    row, error = _find_patient(d, patient_id, callsign, name)
    if error:
        return error
    try:
        limit = max(1, min(int(limit or 10), 30))
    except (TypeError, ValueError):
        limit = 10
    rows = d.query("SELECT * FROM medication_log WHERE patient_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                   (int(row["id"]), limit))
    d.audit("ASSISTANT_MEDICAL_READ", f"patient={row['id']} medications")
    medications = [
        {"id": r["id"], "ts": r["ts"], "medication": r["medication"],
         "dose": r["dose"], "route": r["route"], "administered_by": r["administered_by"],
         "indication": r["indication"], "reaction": r["reaction"],
         "note": str(r["note"] or "")[:200]}
        for r in rows
    ]
    return {"ok": True, "patient": {"id": row["id"], "callsign": row["callsign"]},
            "count": len(medications), "medications": medications,
            "note": "Read-only medication history. Do not prescribe or change doses."}


def _ai_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "checked", "open", "done")


def ai_task_update(d: Database, task_id=None, title: str | None = None,
                   new_title: str | None = None, detail: str | None = None,
                   priority: str | None = None, due: str | None = None,
                   category: str | None = None, recurrence: str | None = None,
                   done=None) -> dict:
    row, error = _find_task(d, task_id, title)
    if error:
        return error
    before = _ai_row(row)
    fields = {}
    if new_title not in (None, ""):
        fields["title"] = str(new_title).strip()[:200]
    if detail is not None:
        fields["detail"] = str(detail)[:4000]
    if priority is not None:
        prio = str(priority).strip().upper()
        fields["priority"] = prio if prio in _TASK_PRIORITIES else "ROUTINE"
    if due is not None:
        fields["due"] = str(due).strip()[:40] or None
    if category is not None:
        fields["category"] = str(category).strip().upper()[:40] or "GENERAL"
    if recurrence is not None:
        fields["recurrence"] = str(recurrence).strip()[:120]
    if done is not None:
        fields["done"] = 1 if _ai_bool(done) else 0
    if not fields:
        return {"ok": False, "error": "No task changes were provided."}
    sets = ", ".join(f"{key}=?" for key in fields)
    d.execute(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), int(row["id"])))
    d.audit("ASSISTANT_TASK_UPDATE", f"id={row['id']} fields={','.join(fields)}")
    updated = _ai_row(d.one("SELECT * FROM tasks WHERE id=?", (int(row["id"]),)))
    updated["detail"] = str(updated.get("detail") or "")[:1000]
    return {"ok": True, "task": updated,
            "undo": _register_undo({"kind": "task_restore", "id": int(row["id"]),
                                    "prev": {key: before.get(key) for key in fields}},
                                    f"Updated task: {updated.get('title')}")}


def _find_incident(d: Database, incident_id=None, title: str | None = None):
    if incident_id not in (None, ""):
        try:
            row = d.one("SELECT * FROM incidents WHERE id=?", (int(incident_id),))
        except (TypeError, ValueError):
            row = None
        if row:
            return row, None
    q = str(title or "").strip()
    if not q:
        return None, {"ok": False, "error": "Provide an incident id or title."}
    rows = d.query("SELECT * FROM incidents WHERE title LIKE ? ORDER BY ts DESC LIMIT 12",
                   (f"%{q}%",))
    if not rows:
        return None, {"ok": False, "error": f"No incident matched {q!r}."}
    if len(rows) > 1:
        exact = [r for r in rows if str(r["title"]).lower() == q.lower()]
        if len(exact) == 1:
            return exact[0], None
        return None, {"ok": False, "ambiguous": True,
                      "error": "Multiple incidents matched. Specify the id or exact title.",
                      "matches": [dict(r) for r in rows[:8]]}
    return rows[0], None


def ai_incidents_list(d: Database, status: str = "open", severity: str = "",
                      limit: int = 20) -> dict:
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    clauses, params = [], []
    state = str(status or "open").lower()
    if state in ("open", "active", "unresolved"):
        clauses.append("resolved=0")
    elif state in ("closed", "resolved", "done"):
        clauses.append("resolved=1")
    if str(severity or "").strip():
        clauses.append("severity = ?"); params.append(str(severity).strip().upper())
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = d.query(f"SELECT * FROM incidents{where} ORDER BY ts DESC, id DESC LIMIT ?",
                   (*params, limit))
    incidents = [
        {"id": r["id"], "ts": r["ts"], "severity": r["severity"],
         "category": r["category"], "title": r["title"],
         "detail": str(r["detail"] or "")[:300], "resolved": int(r["resolved"] or 0),
         "lat": r["lat"], "lon": r["lon"]}
        for r in rows
    ]
    return {"ok": True, "count": len(incidents), "incidents": incidents}


def ai_incident_update(d: Database, incident_id=None, title: str | None = None,
                       new_title: str | None = None, detail: str | None = None,
                       severity: str | None = None, category: str | None = None,
                       lat=None, lon=None, resolved=None) -> dict:
    row, error = _find_incident(d, incident_id, title)
    if error:
        return error
    before = _ai_row(row)
    fields = {}
    if new_title not in (None, ""):
        fields["title"] = str(new_title).strip()[:200]
    if detail is not None:
        fields["detail"] = str(detail)[:8000]
    if severity is not None:
        sev = str(severity).strip().upper()
        fields["severity"] = sev if sev in ("CRITICAL", "WARNING", "ALERT", "INFO") else "INFO"
    if category is not None:
        fields["category"] = str(category).strip().upper()[:40] or "SECURITY"
    if lat is not None:
        value = _ai_num(lat)
        fields["lat"] = value if value is not None and -90 <= value <= 90 else None
    if lon is not None:
        value = _ai_num(lon)
        fields["lon"] = value if value is not None and -180 <= value <= 180 else None
    if resolved is not None:
        fields["resolved"] = 1 if _ai_bool(resolved) else 0
    if not fields:
        return {"ok": False, "error": "No incident changes were provided."}
    sets = ", ".join(f"{key}=?" for key in fields)
    d.execute(f"UPDATE incidents SET {sets} WHERE id=?", (*fields.values(), int(row["id"])))
    d.audit("ASSISTANT_INCIDENT_UPDATE", f"id={row['id']} fields={','.join(fields)}")
    updated = _ai_row(d.one("SELECT * FROM incidents WHERE id=?", (int(row["id"]),)))
    updated["detail"] = str(updated.get("detail") or "")[:2000]
    return {"ok": True, "incident": updated,
            "undo": _register_undo({"kind": "incident_restore_full", "id": int(row["id"]),
                                    "prev": {key: before.get(key) for key in fields}},
                                    f"Updated incident: {updated.get('title')}")}


def ai_mission_log_update(d: Database, log_id=None, title: str | None = None,
                          body: str | None = None, category: str | None = None,
                          tags: str | None = None) -> dict:
    try:
        row = d.one("SELECT * FROM log_entries WHERE id=?", (int(log_id),))
    except (TypeError, ValueError):
        row = None
    if not row:
        return {"ok": False, "error": "Mission log entry not found. Search it first for the id."}
    before = _ai_row(row)
    fields = {}
    if title not in (None, ""):
        fields["title"] = str(title).strip()[:200]
    if body is not None:
        fields["body"] = str(body)[:20000]
    if category is not None:
        fields["category"] = str(category).strip().upper()[:40] or "GENERAL"
    if tags is not None:
        fields["tags"] = str(tags).strip()[:500]
    if not fields:
        return {"ok": False, "error": "No mission-log changes were provided."}
    sets = ", ".join(f"{key}=?" for key in fields)
    d.execute(f"UPDATE log_entries SET {sets} WHERE id=?", (*fields.values(), int(row["id"])))
    d.audit("ASSISTANT_LOG_UPDATE", f"id={row['id']}")
    updated = _ai_row(d.one("SELECT * FROM log_entries WHERE id=?", (int(row["id"]),)))
    updated["body"] = str(updated.get("body") or "")[:1000]
    return {"ok": True, "item": updated,
            "undo": _register_undo({"kind": "log_restore", "id": int(row["id"]),
                                    "prev": {key: before.get(key) for key in fields}},
                                    f"Edited mission log: {updated.get('title')}")}


def ai_mission_log_delete(d: Database, log_id=None) -> dict:
    try:
        row = d.one("SELECT * FROM log_entries WHERE id=?", (int(log_id),))
    except (TypeError, ValueError):
        row = None
    if not row:
        return {"ok": False, "error": "Mission log entry not found. Search it first for the id."}
    snapshot = _ai_row(row)
    d.execute("DELETE FROM log_entries WHERE id=?", (int(row["id"]),))
    d.audit("ASSISTANT_LOG_DELETE", f"id={row['id']}")
    return {"ok": True, "deleted": int(row["id"]),
            "undo": _register_undo({"kind": "log_restore_row", "row": snapshot},
                                    f"Deleted mission log: {snapshot.get('title')}")}


def ai_inventory_delete(d: Database, item_id=None, name: str | None = None) -> dict:
    row, error = _find_inventory(d, item_id, name)
    if error:
        return error
    if row is None:
        return {"ok": False, "error": "Inventory item not found."}
    snapshot = _ai_row(row)
    d.execute("DELETE FROM inventory WHERE id=?", (int(row["id"]),))
    d.audit("ASSISTANT_INVENTORY_DELETE", f"id={row['id']} {row['name'][:100]}")
    return {"ok": True, "deleted": int(row["id"]),
            "undo": _register_undo({"kind": "inventory_restore_row", "row": snapshot},
                                    f"Deleted inventory: {snapshot.get('name')}")}


def ai_checklist_reset(d: Database, kit_id=None) -> dict:
    kit = _find_checklist_kit(kit_id, kit_id)
    if kit is None:
        return {"ok": False, "error": "Checklist kit not found.",
                "kits": [{"id": k.get("id"), "name": k.get("name")}
                         for k in (CHECKLIST_DATA.get("kits") or [])]}
    rows = [dict(r) for r in d.query("SELECT * FROM checklist_state WHERE kit_id=?", (kit["id"],))]
    d.execute("DELETE FROM checklist_state WHERE kit_id=?", (kit["id"],))
    d.audit("ASSISTANT_CHECKLIST_RESET", str(kit["id"]))
    return {"ok": True, "kit": kit.get("name"), "cleared": len(rows),
            "undo": _register_undo({"kind": "checklist_reset_restore", "rows": rows},
                                    f"Reset checklist: {kit.get('name')}")}


def vault_fast_events(d: Database, v, prompt: str):
    """Deterministic stream for simple vault read/list requests.

    Avoids a model round-trip (and the empty-response/timeout failure mode)
    when the operator just wants a note read back.
    """
    if v.configured() and not v.unlocked():
        yield {
            "type": "vault_unlock_required",
            "tool": "vault_read",
            "detail": "Delamain needs to read an encrypted note. Unlock the vault to continue; the note opens automatically after unlock.",
        }
        deadline = time.time() + 120.0
        while time.time() < deadline and not v.unlocked():
            time.sleep(0.4)
        yield {"type": "vault_unlock_result", "tool": "vault_read",
               "unlocked": bool(v.unlocked())}
        if not v.unlocked():
            yield {"type": "done", "ok": True, "model": "vault", "content":
                   "Vault is locked. Unlock it in SECURITY to read encrypted notes.",
                   "thinking": "", "thinking_present": False, "tools": [],
                   "created_at": utcnow()}
            return
    try:
        content = ai_vault_read_reply(d, v, prompt)
    except Exception as exc:
        content = f"Could not read the encrypted vault: {str(exc)[:300]}"
    yield {"type": "done", "ok": True, "model": "vault", "content": content,
           "thinking": "", "thinking_present": False, "tools": [],
           "created_at": utcnow()}


_CONFIRM_LOCK = threading.Lock()
_CONFIRM_PENDING: dict[str, dict] = {}
_UNDO_LOCK = threading.Lock()
_UNDO_REGISTRY: dict[str, dict] = {}
_CONFIRM_TOOLS = assistant.CONFIRMABLE_TOOLS


def _register_undo(payload: dict, label: str) -> dict:
    token = secrets.token_urlsafe(12)
    now = time.time()
    with _UNDO_LOCK:
        for key, value in list(_UNDO_REGISTRY.items()):
            if now - float(value.get("created") or 0) > 900:
                _UNDO_REGISTRY.pop(key, None)
        _UNDO_REGISTRY[token] = {"payload": payload, "label": label, "created": now}
    return {"token": token, "label": label}


def _confirm_preview(d: Database, name: str, args: dict) -> tuple[str, str]:
    args = args if isinstance(args, dict) else {}
    try:
        if name == "adjust_inventory":
            row, _ = _find_inventory(d, args.get("id"), args.get("name"))
            if row:
                current = _ai_num(row["qty"]) or 0
                delta = _ai_num(args.get("delta"))
                qty = _ai_num(args.get("qty"))
                target = qty if qty is not None else current + (delta or 0)
                return (f"Inventory: {row['name']}",
                        f"Quantity {current:g} → {max(0, target):g} {row.get('unit') or ''}".strip())
            return ("Create inventory item", str(args.get("name") or "unnamed item"))
        if name == "complete_task":
            row, _ = _find_task(d, args.get("id"), args.get("title"))
            return ("Complete task", str(row["title"]) if row else str(args.get("title") or args.get("id") or ""))
        if name == "resolve_incident":
            title = args.get("title") or args.get("id") or ""
            return ("Resolve incident", str(title))
        if name == "update_checklist":
            kit = _find_checklist_kit(args.get("kit_id"), args.get("kit_id"))
            item = None
            kit_id = args.get("kit_id")
            item_id = args.get("item_id")
            if kit and item_id:
                item = next((i for i in (kit.get("items") or []) if str(i.get("id")) == str(item_id)), None)
            state = "check" if args.get("checked", True) else "uncheck"
            return (f"{state.title()} checklist item",
                    f"{kit.get('name') if kit else kit_id}: {item.get('text') if item else (args.get('item_text') or item_id or '')}")
        if name == "create_task":
            return ("Create task", str(args.get("title") or ""))
        if name == "create_incident":
            return (f"Create {str(args.get('severity') or 'INFO').upper()} incident",
                    str(args.get("title") or ""))
        if name == "log_water_test":
            bits = [str(args.get("source") or "water")]
            if args.get("chlorine_ppm") is not None:
                bits.append(f"{args['chlorine_ppm']} ppm chlorine")
            if args.get("method"):
                bits.append(str(args["method"]))
            return ("Log water test", " · ".join(bits))
        if name == "create_waypoint":
            return ("Create waypoint", str(args.get("name") or "")
                    + (f" — {args.get('lat')}, {args.get('lon')}" if args.get("lat") is not None else ""))
        if name == "update_task":
            row, _ = _find_task(d, args.get("id"), args.get("title"))
            return ("Update task", str(row["title"]) if row else str(args.get("new_title") or args.get("id") or ""))
        if name == "update_incident":
            row, _ = _find_incident(d, args.get("id"), args.get("title"))
            return ("Update incident", str(row["title"]) if row else str(args.get("new_title") or args.get("id") or ""))
        if name == "update_mission_log":
            return ("Edit mission log", f"entry #{args.get('id')}")
        if name == "delete_mission_log":
            return ("Delete mission log", f"entry #{args.get('id')}")
        if name == "delete_inventory":
            row, _ = _find_inventory(d, args.get("id"), args.get("name"))
            return ("Delete inventory item", str(row["name"]) if row else str(args.get("name") or args.get("id") or ""))
        if name == "reset_checklist":
            kit = _find_checklist_kit(args.get("kit_id"), args.get("kit_id"))
            return ("Reset checklist", str(kit.get("name") if kit else args.get("kit_id") or ""))
        if name == "save_sitrep":
            return ("Save SITREP to mission log",
                    "Snapshot generated now from local data; entry is tagged SITREP and undo is available.")
        if name == "write_system_file":
            raw = str(args.get("path") or "")
            try:
                _, target = _system_path(d, raw)
                existed = target.exists()
            except Exception:
                target, existed = raw, False
            mode = "append to" if args.get("append") else ("overwrite" if (existed and args.get("overwrite")) else "write")
            return (f"File {mode}: {target}", f"{len(str(args.get('content') or ''))} characters")
    except Exception:
        pass
    return (f"Run {name}", "")


def _register_confirmation() -> str:
    token = secrets.token_urlsafe(12)
    now = time.time()
    with _CONFIRM_LOCK:
        for key, value in list(_CONFIRM_PENDING.items()):
            if now - float(value.get("created") or 0) > 600:
                _CONFIRM_PENDING.pop(key, None)
        _CONFIRM_PENDING[token] = {"approved": None, "created": now}
    return token


def _wait_confirmation(token: str, timeout: float = 180.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _CONFIRM_LOCK:
            entry = _CONFIRM_PENDING.get(token)
            decision = entry.get("approved") if entry else False
        if decision is not None:
            with _CONFIRM_LOCK:
                _CONFIRM_PENDING.pop(token, None)
            return bool(decision)
        time.sleep(0.2)
    with _CONFIRM_LOCK:
        _CONFIRM_PENDING.pop(token, None)
    return None


def assistant_tool_preflight(d: Database, v):
    """Pause vault/record/system writes until unlock/confirmation."""
    def preflight(name: str, args: dict):
        name = str(name or "")
        vault_needed = (name in ("save_note", "list_vault_notes", "get_vault_note", "edit_vault_note")
                        and v.configured() and not v.unlocked())
        confirm_needed = name in _CONFIRM_TOOLS
        if not vault_needed and not confirm_needed:
            return None
        args = args if isinstance(args, dict) else {}
        title = str(args.get("title") or "Delamain note")[:200]
        verb = "save" if name == "save_note" else ("edit" if name == "edit_vault_note" else "read")

        def events():
            if vault_needed:
                yield {
                    "type": "vault_unlock_required",
                    "tool": name,
                    "title": title,
                    "detail": f"Delamain needs to {verb} an encrypted note. Unlock the vault to continue; the request runs automatically after unlock.",
                }
                deadline = time.time() + 120.0
                while time.time() < deadline and not v.unlocked():
                    time.sleep(0.4)
                yield {"type": "vault_unlock_result", "tool": name,
                       "unlocked": bool(v.unlocked())}
                if not v.unlocked():
                    raise assistant.ToolDenied("Vault is locked; action cancelled.")
            if confirm_needed:
                summary, detail = _confirm_preview(d, name, args)
                token = _register_confirmation()
                yield {"type": "tool_confirm_required", "id": token, "tool": name,
                       "summary": summary, "detail": detail}
                approved = _wait_confirmation(token)
                if approved is None:
                    raise assistant.ToolDenied("Confirmation timed out; action cancelled.")
                if not approved:
                    raise assistant.ToolDenied("Operator declined; action cancelled.")

        return events()
    return preflight


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def _server_ready(port: int, use_https: bool = False, timeout: float = 60.0) -> bool:
    import ssl
    import time
    import urllib.error
    import urllib.request
    scheme = "https" if use_https else "http"
    context = ssl._create_unverified_context() if use_https else None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{scheme}://127.0.0.1:{port}/api/health",
                                   timeout=1.5, context=context)
            return True
        except urllib.error.HTTPError:
            # 401/403 still proves the HTTP listener is accepting connections.
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _announce_when_ready(port: int, token: str, handshake: bool, browser_url: str,
                         use_https: bool = False) -> None:
    if not _server_ready(port, use_https=use_https):
        print("DarkSec: server did not become ready before the startup timeout.", flush=True)
        return
    if handshake:
        print(json.dumps({"ready": True, "app": APP_NAME, "version": __version__,
                          "port": port, "token": token}), flush=True)
    if browser_url:
        try:
            import webbrowser
            webbrowser.open(browser_url)
        except Exception as exc:
            print(f"DarkSec: could not open browser: {exc}", flush=True)


def create_app(port: int = 0, token: str | None = None, open_browser: bool = False,
               handshake: bool = False, no_index: bool = False,
               lan: bool = False, lan_ip: str | None = None,
               secure_cookies: bool = False) -> FastAPI:
    token = token or secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(18)

    async def _catalog_auto_refresh_loop(app: FastAPI):
        # Controlled, operator-configurable: only runs when cctv_catalog_refresh_hours > 0.
        while True:
            try:
                await asyncio.sleep(900)  # check every 15 min
                d = getattr(app.state, "db", None)
                if d is None:
                    continue
                try:
                    hours = float(d.all_settings().get("cctv_catalog_refresh_hours") or 0)
                except Exception:
                    hours = 0
                if hours <= 0:
                    continue
                row = d.one("SELECT fetched_at, status FROM feeds WHERE id='cctv_catalog'")
                if not row:
                    continue
                fetched = row["fetched_at"]
                due = True
                if fetched:
                    try:
                        # utcnow() format in this codebase is ISO-like text; be permissive
                        ft = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
                        age_h = (datetime.now(timezone.utc) - (ft if ft.tzinfo else ft.replace(tzinfo=timezone.utc))).total_seconds() / 3600.0
                        due = age_h >= max(1.0, min(168.0, hours))
                    except Exception:
                        due = True
                if due:
                    await run_in_threadpool(feeds.refresh, d, "cctv_catalog")
            except asyncio.CancelledError:
                break
            except Exception:
                # never crash the app due to background refresh
                await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database()
        app.state.db = db
        app.state.vault = Vault(db)
        app.state.started = utcnow()
        from .lan import LanManager
        if getattr(app.state, "lan_manager", None) is None:
            app.state.lan_manager = LanManager()
        lm = app.state.lan_manager
        if db.get_setting("lan_access_enabled") and not db.get_setting("opsec_mode"):
            ip = app.state.lan_ip
            if lm.start(app, ip=ip):
                if lm.ip:
                    app.state.allowed_hosts.add(lm.ip)
                if lm.ip and lm.port:
                    app.state.allowed_origins.add(f"https://{lm.ip}:{lm.port}")
        app.state.sweep_active = False
        db.audit("APP_START", f"port={port} version={__version__}")
        if not os.environ.get("DARKSEC_NO_SYNC"):
            autosync.engine.start(db)
        if not no_index and not os.environ.get("DARKSEC_NO_INDEX"):
            try:
                # Always reconcile metadata (fast) so PDFs added to the library
                # folders show up without a manual reindex; deep text extraction
                # only runs for documents that are not yet indexed.
                library.start_index(deep=bool(db.get_setting("auto_deep_index", True)), force=False)
                db.audit("INDEX_AUTO", "Background library index started")
            except Exception as exc:
                db.audit("INDEX_AUTO_FAIL", str(exc)[:300])
        if handshake or open_browser:
            if lan:
                browser_host = lan_ip or "127.0.0.1"
                browser_url = f"https://{browser_host}:{port}/?token={token}"
            else:
                browser_url = f"http://127.0.0.1:{port}/?token={token}"
            threading.Thread(
                target=_announce_when_ready,
                args=(port, token, handshake, browser_url if open_browser else "",
                      bool(lan)),
                name="darksec-ready-announcer",
                daemon=True,
            ).start()
        catalog_task = None
        if not os.environ.get("DARKSEC_NO_CATALOG_AUTOSYNC"):
            catalog_task = asyncio.create_task(_catalog_auto_refresh_loop(app))
        yield
        try:
            if catalog_task:
                catalog_task.cancel()
                try:
                    await catalog_task
                except Exception:
                    pass
            phonegps.pairing.stop()
            if getattr(app.state, "lan_manager", None):
                app.state.lan_manager.stop()
            autosync.engine.stop()
            db.audit("APP_STOP", "shutdown")
        except Exception:
            pass
        db.close()

    loopback_names = {"127.0.0.1", "localhost", "::1"}
    allowed_hosts = set(loopback_names)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                       f"http://[::1]:{port}"}
    if lan and lan_ip:
        allowed_hosts.add(lan_ip)
        scheme = "https"
        allowed_origins.add(f"{scheme}://{lan_ip}:{port}")
        allowed_origins.add(f"{scheme}://localhost:{port}")

    app = FastAPI(title="DarkSec Field Console", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    app.state.csrf = csrf
    app.state.port = port
    app.state.lan = lan
    app.state.lan_ip = lan_ip
    app.state.secure_cookies = secure_cookies
    app.state.allowed_hosts = allowed_hosts
    app.state.allowed_origins = allowed_origins
    app.state.lan_manager = None

    def host_name(value: str) -> str | None:
        if not value or "@" in value or any(ch in value for ch in " \t\r\n/\\"):
            return None
        try:
            return urlsplit("//" + value).hostname
        except Exception:
            return None

    def origin_allowed(request: Request, value: str) -> bool:
        return value.rstrip("/") in getattr(request.app.state, "allowed_origins", allowed_origins)

    def check_token(request: Request) -> bool:
        supplied = request.cookies.get(COOKIE) or request.headers.get("X-DarkSec-Token", "")
        if supplied and secrets.compare_digest(supplied, token):
            return True
        return False

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        host = request.headers.get("host", "")
        if host and host_name(host) not in getattr(request.app.state, "allowed_hosts", allowed_hosts):
            return JSONResponse({"error": "Invalid host"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and not origin_allowed(request, origin):
            return JSONResponse({"error": "Cross-origin request denied"}, status_code=403)
        # Token bootstrap: /?token=... sets the cookie and redirects.
        if path == "/" and request.query_params.get("token"):
            if secrets.compare_digest(request.query_params["token"], token):
                params = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                dest = "/ui/?" + urlencode(params) if params else "/ui/"
                resp = RedirectResponse(dest, status_code=303)
                resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                                secure=secure_cookies)
                resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/",
                                secure=secure_cookies)
                return resp
            return PlainTextResponse("Invalid launch token", status_code=403)
        if lan and path.startswith("/api/"):
            try:
                if db(request).get_setting("opsec_mode"):
                    return JSONResponse({"error": "LAN access disabled in OPSEC mode"}, status_code=403)
            except Exception:
                pass
        if path.startswith("/api/"):
            if not check_token(request):
                if request.query_params.get("token") and secrets.compare_digest(
                        request.query_params["token"], token):
                    resp = JSONResponse({"error": "Token accepted; retry with cookie"}, status_code=401)
                    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/",
                                    secure=secure_cookies)
                    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="strict", path="/",
                                    secure=secure_cookies)
                    return resp
                return JSONResponse({"error": "Authentication required"}, status_code=401)
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                # Cookie-authenticated mutation: require an exact same-origin
                # Origin, an explicit launch-token header, or the CSRF header.
                supplied_token = request.headers.get("X-DarkSec-Token", "")
                if (supplied_token and secrets.compare_digest(supplied_token, token)) or origin:
                    pass
                else:
                    csrf_header = request.headers.get("X-DarkSec-CSRF", "")
                    if not csrf_header or not secrets.compare_digest(csrf_header, csrf):
                        return JSONResponse({"error": "CSRF check failed"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(self), microphone=(), camera=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        is_html = path == "/" or path == "/ui/" or path.endswith(".html")
        if path.startswith("/api/") or is_html:
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; "                "form-action 'self'",
            )
        if path.startswith("/api/") or path.startswith("/ui/") or is_html:
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ------------------------------------------------------------- helpers
    def db(request: Request) -> Database:
        return request.app.state.db

    def vault(request: Request) -> Vault:
        return request.app.state.vault

    def require(condition: bool, message: str, status: int = 400):
        if not condition:
            raise HTTPException(status_code=status, detail=message)

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def permission_error(_request: Request, exc: PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(FileNotFoundError)
    async def not_found(_request: Request, exc: FileNotFoundError):
        return JSONResponse({"error": str(exc) or "Not found"}, status_code=404)

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health(request: Request):
        return {"ok": True, "app": APP_NAME, "version": __version__, "codename": APP_CODENAME,
                "time": utcnow(), "started": request.app.state.started}

    @app.get("/api/status")
    def status(request: Request, force: bool = False):
        d = db(request)
        s = d.all_settings()
        lib = library.stats(d)
        vault_state = vault(request).status()
        hw = hardware(force=force)
        return {
            "app": APP_NAME, "version": __version__, "codename": APP_CODENAME,
            "time": utcnow(), "started": request.app.state.started,
            "online": tilecache.is_online(),
            "sync": autosync.engine.snapshot(),
            "trip_alerts": [
                {key: trip.get(key) for key in (
                    "id", "name", "alert", "alert_level", "next_checkin_at",
                    "minutes_overdue", "return_minutes_overdue",
                )}
                for trip in trip_items(d) if trip["status"] == "ACTIVE"
            ],
            "settings": s, "vault": vault_state, "readiness": readiness(d),
            "library": lib, "hardware": hw, "system": system_info(),
            "counts": {
                "waypoints": d.one("SELECT count(*) c FROM waypoints")["c"],
                "tracks": d.one("SELECT count(*) c FROM tracks")["c"],
                "patients": d.one("SELECT count(*) c FROM patients")["c"],
                "tasks_open": d.one("SELECT count(*) c FROM tasks WHERE done=0")["c"],
                "logs": d.one("SELECT count(*) c FROM log_entries")["c"],
                "incidents_open": d.one("SELECT count(*) c FROM incidents WHERE resolved=0")["c"],
                "inventory": d.one("SELECT count(*) c FROM inventory")["c"],
                "caches": d.one("SELECT count(*) c FROM caches")["c"],
                "recordings": d.one("SELECT count(*) c FROM sdr_recordings")["c"],
            },
            "feeds": {"critical_count": feeds.summary(d)["critical_count"]},
            "tx_enabled": bool(s.get("sdr_tx_enabled") and s.get("sdr_tx_attested")),
        }

    @app.get("/api/dashboard")
    def dashboard(request: Request):
        d = db(request)
        s = d.all_settings()
        r = readiness(d)
        lat, lon = float(s["lat"]), float(s["lon"])
        elevation = float(s.get("elevation_m") or 0)
        almanac = None
        try:
            almanac = astro.celestial(lat, lon, elevation)
        except Exception:
            almanac = None
        feed_summary = feeds.summary(d)
        try:
            local_threat = intel.threat_index(d, lat=lat, lon=lon)
        except Exception:
            local_threat = {}
        try:
            sit = sitrep.situational_brief(d, lat, lon, elevation_m=elevation,
                                         ready=r,
                                         inventory=[dict(x) for x in d.query("SELECT * FROM inventory ORDER BY name LIMIT 24")],
                                         sun=almanac, threat=local_threat,
                                         vault=vault(request).status())
        except Exception:
            sit = {"stance": "UNKNOWN", "stance_reason": "Synthesis unavailable", "critical_near": 0,
                   "events_within_radius": 0, "top_events": [], "actions": []}
        tasks = [dict(x) for x in d.query(
            "SELECT * FROM tasks WHERE done=0 ORDER BY CASE priority WHEN 'IMMEDIATE' THEN 0 "
            "WHEN 'PRIORITY' THEN 1 WHEN 'ROUTINE' THEN 2 ELSE 3 END, ts DESC LIMIT 8")]
        logs = [dict(x) for x in d.query("SELECT * FROM log_entries ORDER BY ts DESC LIMIT 10")]
        incidents = [dict(x) for x in d.query(
            "SELECT * FROM incidents WHERE resolved=0 ORDER BY ts DESC LIMIT 6")]
        low = [dict(x) for x in d.query(
            "SELECT * FROM inventory WHERE min_qty > 0 AND qty <= min_qty ORDER BY (qty-min_qty) LIMIT 8")]
        try:
            coords = geo.format_coord(lat, lon)
        except Exception:
            coords = None
        return {
            "readiness": r, "tasks": tasks, "logs": logs,
            "incidents": incidents, "low_stock": low, "feeds": feed_summary,
            "sitrep": sit,
            "trips": trip_items(d),
            "almanac": almanac, "coords": coords, "library": library.stats(d),
            "hardware": hardware(), "system": system_info(), "settings": s,
            "vault": vault(request).status(),
            "online": tilecache.is_online(),
            "sync": autosync.engine.snapshot(d),
            "brief_ready": True,
        }

    @app.get("/api/brief", response_class=PlainTextResponse)
    def brief(request: Request, format: str = "txt", download: bool = False):
        text = build_brief(db(request), vault=vault(request).status())
        media = "text/plain; charset=utf-8"
        suffix = "txt"
        if str(format or "").lower() in ("md", "markdown"):
            text = to_markdown(text)
            media = "text/markdown; charset=utf-8"
            suffix = "md"
        headers = {}
        if download:
            headers["Content-Disposition"] = (
                f'attachment; filename="darksec-sitrep-{datetime.now(timezone.utc).date().isoformat()}.{suffix}"'
            )
        return PlainTextResponse(text, media_type=media, headers=headers)

    @app.post("/api/brief/save")
    def brief_save(request: Request, payload: dict = Body(default={})):
        """Save a server-generated SITREP snapshot to the mission log."""
        payload = payload if isinstance(payload, dict) else {}
        return ai_sitrep_save(db(request), vault(request), payload.get("note") or "")

    @app.get("/api/system")
    def system(request: Request):
        return {"system": system_info(), "hardware": hardware(),
                "data_dir": str(config.DATA_DIR), "library_roots": [str(p) for p in library.library_roots()]}

    @app.get("/api/audit")
    def audit(request: Request, limit: int = Query(100, ge=1, le=1000)):
        rows = db(request).query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return {"items": [dict(r) for r in rows]}

    @app.post("/api/audit")
    def audit_add(request: Request, payload: dict = Body(...)):
        action = str(payload.get("action") or "OPERATOR")[:120]
        detail = str(payload.get("detail") or "")[:2000]
        db(request).audit(action, detail, request.client.host if request.client else "")
        return {"ok": True}

    # ------------------------------------------------------------ assistant (local Ollama)
    def assistant_tool_executor(d: Database, v, prompt: str = ""):
        """Execute the narrow set of local tools Delamain is allowed to call."""
        read_only = assistant.vault_read_only_intent(prompt) or assistant.vault_edit_intent(prompt)
        def execute(name: str, args: dict) -> dict:
            args = args if isinstance(args, dict) else {}
            name = str(name or "").strip()
            try:
                if read_only and name in ("save_note", "save_log_entry"):
                    return {"ok": False, "error": "Blocked: the operator asked to read/retrieve a vault note, not save one."}
                if name in ("create_task", "complete_task", "adjust_inventory",
                            "create_incident", "resolve_incident", "update_checklist",
                            "log_water_test", "update_task", "update_incident",
                            "update_mission_log", "delete_mission_log",
                            "delete_inventory", "reset_checklist",
                            "save_sitrep") and not assistant.record_write_intent(prompt):
                    return {"ok": False, "error": "Blocked: the operator did not explicitly ask to modify records."}
                if name == "get_live_status":
                    return assistant_live_status_compact(d, v)
                if name == "get_sitrep":
                    return ai_sitrep(d, v, args.get("max_chars") or 6000)
                if name == "save_sitrep":
                    return ai_sitrep_save(d, v, args.get("note") or "")
                if name == "get_library_document":
                    title = str(args.get("title") or args.get("name") or "").strip()
                    if not title:
                        return {"ok": False, "error": "title is required"}
                    matches = library.find_document(d, title, limit=6)
                    if not matches:
                        return {"ok": False, "error": f"No document matched {title!r}",
                                "matches": []}
                    doc = matches[0]
                    excerpt = library.document_excerpt(
                        d, int(doc["id"]),
                        query=str(args.get("query") or ""),
                        max_chars=args.get("max_chars") or 4000,
                        page=args.get("page"),
                        span=args.get("span") or 1,
                    )
                    return {"ok": True,
                            "document": {key: doc.get(key) for key in
                                         ("id", "name", "category", "rel_path", "pages")},
                            "matches": [{key: m.get(key) for key in
                                         ("id", "name", "category", "score")}
                                        for m in matches[:5]],
                            "excerpt": excerpt.get("text") or "",
                            "chars": excerpt.get("chars"),
                            "truncated": excerpt.get("truncated"),
                            "focus": excerpt.get("focus"),
                            "citation": excerpt.get("citation"),
                            "pages": excerpt.get("pages") or [],
                            "page_count": excerpt.get("page_count"),
                            "sections": excerpt.get("sections") or [],
                            "note": excerpt.get("note")}
                if name == "list_vault_notes":
                    return ai_vault_notes_list(d, v, args.get("limit") or 30)
                if name == "get_vault_note":
                    return ai_vault_note_get(d, v, args.get("id"),
                                             args.get("title") or args.get("query"))
                if name == "edit_vault_note":
                    return ai_vault_note_update(
                        d, v, args.get("id"), args.get("title") or args.get("query"),
                        args.get("new_title"), args.get("body"), args.get("append"),
                        args.get("tags"),
                    )
                if name == "search_mission_log":
                    return ai_mission_log_search(d, args.get("query") or "",
                                                 args.get("category") or "",
                                                 args.get("limit") or 20)
                if name == "list_tasks":
                    return ai_tasks_list(d, args.get("status") or "open",
                                         args.get("limit") or 30)
                if name == "create_task":
                    return ai_task_create(d, args.get("title"), args.get("detail") or "",
                                          args.get("priority") or "ROUTINE",
                                          args.get("due"), args.get("category") or "GENERAL",
                                          args.get("recurrence") or "")
                if name == "complete_task":
                    return ai_task_complete(d, args.get("id"), args.get("title"))
                if name == "query_inventory":
                    return ai_inventory_query(d, args.get("query") or "",
                                              bool(args.get("low_only")),
                                              args.get("expiring_days"),
                                              args.get("limit") or 50)
                if name == "adjust_inventory":
                    return ai_inventory_adjust(
                        d, args.get("id"), args.get("name"), args.get("delta"),
                        args.get("qty"), args.get("unit"), args.get("category"),
                        args.get("min_qty"), args.get("location"), args.get("notes"),
                        args.get("expires"),
                    )
                if name == "create_incident":
                    return ai_incident_create(d, args.get("title"), args.get("detail") or "",
                                              args.get("severity") or "INFO",
                                              args.get("category") or "SECURITY",
                                              args.get("lat"), args.get("lon"))
                if name == "resolve_incident":
                    return ai_incident_resolve(d, args.get("id"), args.get("title"),
                                               args.get("resolution") or "")
                if name == "read_checklist":
                    return ai_checklist_read(d, args.get("kit_id"), args.get("kit_name"))
                if name == "update_checklist":
                    return ai_checklist_update(d, args.get("kit_id"), args.get("item_id"),
                                               args.get("item_text"),
                                               args.get("checked", True))
                if name == "read_water_log":
                    return ai_water_log_read(d, args.get("limit") or 10, args.get("days"))
                if name == "log_water_test":
                    return ai_water_test_log(d, args.get("source") or "",
                                             args.get("volume_l"), args.get("method") or "",
                                             args.get("chlorine_ppm"), args.get("note") or "")
                if name == "list_system_directory":
                    return ai_system_list(d, args.get("path") or ".", args.get("limit") or 200)
                if name == "read_system_file":
                    return ai_system_read(d, args.get("path") or "", args.get("max_chars") or 6000)
                if name == "write_system_file":
                    if not assistant.system_write_intent(prompt):
                        return {"ok": False, "error": "Blocked: the operator did not explicitly ask to write a file."}
                    return ai_system_write(d, args.get("path") or "", args.get("content") or "",
                                           bool(args.get("overwrite")), bool(args.get("append")))
                if name == "list_calculators":
                    return ai_calc_list(args.get("name") or "", args.get("group") or "")
                if name == "calculate":
                    return ai_calculate(args.get("name") or "", args.get("parameters") or {})
                if name == "convert_coordinates":
                    return ai_convert_coordinates(args.get("lat"), args.get("lon"))
                if name == "distance_bearing":
                    return ai_distance_bearing(d, args.get("lat1"), args.get("lon1"),
                                               args.get("lat2"), args.get("lon2"))
                if name == "list_waypoints":
                    return ai_waypoints_list(d, args.get("query") or "",
                                             args.get("radius_km"), args.get("limit") or 25)
                if name == "create_waypoint":
                    return ai_waypoint_create(d, args.get("name"), args.get("lat"),
                                              args.get("lon"), args.get("ele"),
                                              args.get("type") or "WAYPOINT",
                                              args.get("note") or "")
                if name == "list_patients":
                    return ai_patients_list(d, args.get("query") or "", args.get("triage") or "",
                                            args.get("status") or "", args.get("limit") or 20)
                if name == "get_patient":
                    return ai_patient_get(d, args.get("patient_id"), args.get("callsign") or "",
                                          args.get("name") or "", bool(args.get("include_vitals", True)),
                                          bool(args.get("include_meds", True)),
                                          args.get("limit") or 10)
                if name == "list_vitals":
                    return ai_vitals_list(d, args.get("patient_id"), args.get("callsign") or "",
                                          args.get("name") or "", args.get("limit") or 20)
                if name == "list_medications":
                    return ai_medications_list(d, args.get("patient_id"), args.get("callsign") or "",
                                               args.get("name") or "", args.get("limit") or 20)
                if name == "update_task":
                    return ai_task_update(d, args.get("id"), args.get("title"), args.get("new_title"),
                                          args.get("detail"), args.get("priority"), args.get("due"),
                                          args.get("category"), args.get("recurrence"), args.get("done"))
                if name == "list_incidents":
                    return ai_incidents_list(d, args.get("status") or "open",
                                             args.get("severity") or "", args.get("limit") or 20)
                if name == "update_incident":
                    return ai_incident_update(d, args.get("id"), args.get("title"), args.get("new_title"),
                                              args.get("detail"), args.get("severity"), args.get("category"),
                                              args.get("lat"), args.get("lon"), args.get("resolved"))
                if name == "update_mission_log":
                    return ai_mission_log_update(d, args.get("id"), args.get("title"), args.get("body"),
                                                 args.get("category"), args.get("tags"))
                if name == "delete_mission_log":
                    return ai_mission_log_delete(d, args.get("id"))
                if name == "delete_inventory":
                    return ai_inventory_delete(d, args.get("id"), args.get("name"))
                if name == "reset_checklist":
                    return ai_checklist_reset(d, args.get("kit_id"))
                if name == "save_log_entry":
                    return ai_log_save(d, args.get("title"), args.get("body"), args.get("category") or "GENERAL")
                if name == "save_note":
                    return ai_note_save(d, v, args.get("title"), args.get("body"), args.get("tags") or "")
                if name == "search_library":
                    q = str(args.get("query") or "").strip()
                    if not q:
                        return {"ok": False, "error": "query is required"}
                    category = str(args.get("category") or "").strip() or None
                    rows = library.search(d, q, category, limit=4)
                    results = []
                    for r in rows[:4]:
                        text = d.one("SELECT text FROM library_docs WHERE id=?", (r.get("id"),))
                        page = library.page_for_query(str((text or {}).get("text") or ""), q)
                        results.append({
                            "id": r.get("id"), "name": r.get("name"),
                            "category": r.get("category"), "path": r.get("rel_path"),
                            "page": page,
                            "snippet": (r.get("snippet") or "")[:800],
                        })
                    return {"ok": True, "results": results}
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
            return {"ok": False, "error": f"unknown tool: {name}"}
        return execute

    @app.get("/api/assistant/status")
    async def assistant_status(request: Request, fresh: bool = False):
        return await run_in_threadpool(assistant.status, db(request), fresh)

    @app.get("/api/assistant/models")
    async def assistant_models(request: Request, fresh: bool = False):
        return await run_in_threadpool(assistant.list_models, db(request), fresh)

    @app.post("/api/assistant/warm")
    async def assistant_warm(request: Request, payload: dict = Body(default={})):
        """Preload a model so the first chat does not wait for the cold load."""
        model = str(payload.get("model") or "").strip() or None
        return await run_in_threadpool(assistant.warm, db(request), model)

    @app.post("/api/assistant/chat")
    async def assistant_chat(request: Request, payload: dict = Body(...)):
        d = db(request)
        prompt = str(payload.get("prompt") or payload.get("message") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        context = payload.get("context")
        model = str(payload.get("model") or "").strip() or None
        think = payload.get("think") if "think" in payload else None
        conversation_id = payload.get("conversation_id")
        try:
            conversation_id = int(conversation_id) if conversation_id is not None else None
        except Exception:
            conversation_id = None
        images = payload.get("images") if isinstance(payload.get("images"), list) else []
        image_paths = [p for p in (assistant.save_image(x) for x in images) if p]
        # Do not audit full prompt/content by default (may contain sensitive notes).
        d.audit("ASSISTANT_CHAT", json.dumps({"model": model or d.get_setting("ollama_model", ""), "prompt_chars": len(prompt), "has_context": bool(context), "think": think, "images": len(image_paths)}), request.client.host if request.client else "")
        v = vault(request)
        direct = None
        if assistant.referential_save_intent(prompt):
            save_text = assistant.last_reference_text(history, prompt)
            if save_text:
                if v.configured() and not v.unlocked():
                    raise HTTPException(status_code=403, detail="Vault is locked. Unlock it to save this note.")
                save_title = _save_title_from_text(save_text)
                try:
                    saved = ai_note_save(d, v, save_title, save_text, "delamain,chat-log",
                                         max_chars=AI_CHAT_MAX_CHARS)
                    content = f"Saved to the encrypted vault: **{save_title}** (#{saved.get('id')})."
                    tools = [{"name": "save_note", "args": {"title": save_title}, "result": saved}]
                except Exception as exc:
                    content = f"Could not save to the vault: {str(exc)[:300]}"
                    tools = []
                direct = {"ok": True, "model": "vault", "content": content,
                          "thinking": "", "thinking_present": False, "tools": tools,
                          "created_at": utcnow()}
        if direct is None and assistant.vault_direct_read_intent(prompt):
            if v.configured() and not v.unlocked():
                raise HTTPException(status_code=403, detail="Vault is locked. Unlock it to read encrypted notes.")
            try:
                content = ai_vault_read_reply(d, v, prompt)
            except Exception as exc:
                content = f"Could not read the encrypted vault: {str(exc)[:300]}"
            direct = {"ok": True, "model": "vault", "content": content,
                      "thinking": "", "thinking_present": False, "tools": [],
                      "created_at": utcnow()}
        try:
            result = direct or await run_in_threadpool(assistant.chat, d, prompt, history, context, model, think, image_paths, assistant_tool_executor(d, v, prompt))
            # Persist to conversation history when requested.
            if conversation_id is not None:
                try:
                    d.insert("assistant_messages", {"conversation_id": conversation_id, "role": "user", "content": prompt, "images": json.dumps(image_paths), "model": model or d.get_setting("ollama_model", ""), "meta": "", "created_at": utcnow()})
                    d.insert("assistant_messages", {"conversation_id": conversation_id, "role": "assistant", "content": result.get("content") or "", "images": "[]", "model": result.get("model"), "meta": result.get("done_reason") or "", "created_at": utcnow()})
                    d.execute("UPDATE assistant_conversations SET updated_at=? WHERE id=?", (utcnow(), conversation_id))
                except Exception:
                    pass
            d.audit("ASSISTANT_OK", json.dumps({"model": result.get("model"), "chars": len(result.get("content") or "")}), request.client.host if request.client else "")
            result["image_paths"] = image_paths
            return result
        except ValueError as e:
            d.audit("ASSISTANT_ERR", f"400:{str(e)[:200]}")
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            d.audit("ASSISTANT_ERR", f"502:{str(e)[:200]}")
            raise HTTPException(status_code=502, detail=str(e))
        except requests.exceptions.Timeout:
            d.audit("ASSISTANT_ERR", "504:timeout")
            raise HTTPException(status_code=504, detail="Ollama request timed out (model may still be loading or prompt too large for timeout). Increase assistant timeout in Settings.")
        except requests.exceptions.ConnectionError as e:
            d.audit("ASSISTANT_ERR", f"503:{str(e)[:200]}")
            raise HTTPException(status_code=503, detail=f"Ollama connection failed: {e}")
        except requests.exceptions.RequestException as e:
            d.audit("ASSISTANT_ERR", f"502req:{str(e)[:200]}")
            raise HTTPException(status_code=502, detail=f"Ollama request failed: {e}")
        except Exception as e:
            d.audit("ASSISTANT_ERR", f"500:{type(e).__name__}:{str(e)[:200]}")
            raise HTTPException(status_code=500, detail=f"Assistant error: {type(e).__name__}: {e}")

    @app.post("/api/assistant/chat/stream")
    async def assistant_chat_stream(request: Request, payload: dict = Body(...)):
        d = db(request)
        prompt = str(payload.get("prompt") or payload.get("message") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        context = payload.get("context")
        model = str(payload.get("model") or "").strip() or None
        think = payload.get("think") if "think" in payload else None
        conversation_id = payload.get("conversation_id")
        try:
            conversation_id = int(conversation_id) if conversation_id is not None else None
        except Exception:
            conversation_id = None
        images = payload.get("images") if isinstance(payload.get("images"), list) else []
        image_paths = [p for p in (assistant.save_image(x) for x in images) if p]
        use_model = model or d.get_setting("ollama_model", "")
        d.audit("ASSISTANT_CHAT", json.dumps({"model": use_model, "prompt_chars": len(prompt), "has_context": bool(context), "think": think, "stream": True, "images": len(image_paths)}), request.client.host if request.client else "")

        def generate():
            final_content = ""
            final_model = use_model
            v = vault(request)
            try:
                save_fast = (vault_save_fast_events(d, v, prompt, history)
                             if assistant.referential_save_intent(prompt) else None)
                if save_fast is not None:
                    iterator = save_fast
                elif assistant.vault_direct_read_intent(prompt):
                    iterator = vault_fast_events(d, v, prompt)
                else:
                    iterator = assistant.chat_stream(d, prompt, history, context, model, think, image_paths,
                                                     assistant_tool_executor(d, v, prompt),
                                                     assistant_tool_preflight(d, v))
                for chunk in iterator:
                    yield json.dumps(chunk, ensure_ascii=False, default=str) + "\n"
                    if isinstance(chunk, dict) and chunk.get("type") == "done":
                        final_content = chunk.get("content") or final_content
                        final_model = chunk.get("model") or final_model
                        try: d.audit("ASSISTANT_OK", json.dumps({"model": final_model, "chars": len(final_content or ""), "stream": True}), request.client.host if request.client else "")
                        except Exception: pass
                        if conversation_id is not None:
                            try:
                                d.insert("assistant_messages", {"conversation_id": conversation_id, "role": "user", "content": prompt, "images": json.dumps(image_paths), "model": model or d.get_setting("ollama_model", ""), "meta": "", "created_at": utcnow()})
                                d.insert("assistant_messages", {"conversation_id": conversation_id, "role": "assistant", "content": final_content or "", "images": "[]", "model": final_model, "meta": chunk.get("done_reason") or "", "created_at": utcnow()})
                                d.execute("UPDATE assistant_conversations SET updated_at=? WHERE id=?", (utcnow(), conversation_id))
                            except Exception:
                                pass
            except Exception as e:
                try: d.audit("ASSISTANT_ERR", f"stream500:{type(e).__name__}:{str(e)[:200]}")
                except Exception: pass
                yield json.dumps({"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}"}, ensure_ascii=False) + "\n"

        return StreamingResponse(generate(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----------------------------------------------------------- assistant history/images
    @app.get("/api/assistant/conversations")
    def assistant_conversations_list(request: Request):
        d = db(request)
        rows = d.query("SELECT c.id, c.title, c.created_at, c.updated_at, "
                       "(SELECT count(*) FROM assistant_messages m WHERE m.conversation_id=c.id) AS msg_count "
                       "FROM assistant_conversations c ORDER BY c.updated_at DESC")
        return {"items": [dict(r) for r in rows]}

    @app.post("/api/assistant/conversations")
    def assistant_conversations_create(request: Request, payload: dict = Body(default={})):
        d = db(request)
        title = str(payload.get("title") or "New chat").strip()[:120] or "New chat"
        cid = d.insert("assistant_conversations", {"title": title, "created_at": utcnow(), "updated_at": utcnow()})
        return {"id": cid, "title": title, "created_at": utcnow(), "updated_at": utcnow()}

    @app.get("/api/assistant/conversations/{conv_id}/messages")
    def assistant_conversation_messages(request: Request, conv_id: int):
        d = db(request)
        rows = d.query("SELECT id, conversation_id, role, content, images, model, meta, created_at "
                       "FROM assistant_messages WHERE conversation_id=? ORDER BY id ASC", (conv_id,))
        out = []
        for r in rows:
            item = dict(r)
            try:
                item["images"] = json.loads(item.get("images") or "[]")
            except Exception:
                item["images"] = []
            out.append(item)
        return {"items": out}

    @app.delete("/api/assistant/conversations/{conv_id}")
    def assistant_conversation_delete(request: Request, conv_id: int):
        d = db(request)
        d.execute("DELETE FROM assistant_messages WHERE conversation_id=?", (conv_id,))
        d.execute("DELETE FROM assistant_conversations WHERE id=?", (conv_id,))
        d.audit("ASSISTANT_CONV_DELETE", f"id={conv_id}")
        return {"ok": True}

    @app.post("/api/assistant/image")
    async def assistant_image_upload(request: Request, payload: dict = Body(...)):
        d = db(request)
        data = payload.get("data")
        path = assistant.save_image(data)
        require(bool(path), "Invalid image data", 400)
        name = Path(path).name
        d.audit("ASSISTANT_IMAGE", f"{name}")
        return {"path": path, "url": f"/api/assistant/image/{name}"}

    @app.get("/api/assistant/image/{name}")
    def assistant_image_get(name: str):
        safe = Path(name).name
        path = config.UPLOAD_DIR / safe
        require(path.exists() and path.is_file(), "Image not found", 404)
        # basic safety: ensure inside upload dir
        try:
            path.resolve().relative_to(config.UPLOAD_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Forbidden")
        from fastapi.responses import FileResponse
        return FileResponse(path)

    @app.post("/api/assistant/vault-note")
    def assistant_vault_note(request: Request, payload: dict = Body(...)):
        """Save a Delamain chat log as an encrypted vault note."""
        d = db(request)
        v = vault(request)
        if not v.configured():
            raise HTTPException(status_code=403,
                                detail="Vault is not configured. Set up the encrypted vault in SECURITY before saving chat logs.")
        try:
            return ai_note_save(d, v,
                                payload.get("title") or "Delamain note",
                                payload.get("content") or payload.get("body") or "",
                                payload.get("tags") or "delamain,chat-log",
                                max_chars=AI_CHAT_MAX_CHARS)
        except ValueError as e:
            msg = str(e)
            if "Vault is locked" in msg:
                msg = "Vault is locked. Unlock it in SECURITY, then save this chat."
                raise HTTPException(status_code=403, detail=msg)
            raise HTTPException(status_code=400, detail=msg)

    @app.post("/api/assistant/confirm")
    def assistant_confirm(request: Request, payload: dict = Body(...)):
        cid = str(payload.get("id") or "")
        approved = bool(payload.get("approved"))
        with _CONFIRM_LOCK:
            entry = _CONFIRM_PENDING.get(cid)
            if entry is None:
                raise HTTPException(status_code=404, detail="Confirmation request not found or expired")
            entry["approved"] = approved
        return {"ok": True, "id": cid, "approved": approved}

    @app.post("/api/assistant/undo")
    def assistant_undo(request: Request, payload: dict = Body(...)):
        token = str(payload.get("token") or "")
        now = time.time()
        with _UNDO_LOCK:
            entry = _UNDO_REGISTRY.pop(token, None)
        if entry is None or now - float(entry.get("created") or 0) > 900:
            raise HTTPException(status_code=404, detail="Undo expired or not found")
        data = entry.get("payload") or {}
        kind = str(data.get("kind") or "")
        d = db(request)
        if kind == "task_restore":
            previous = data.get("prev") or {}
            fields = {key: previous.get(key) for key in
                      ("title", "detail", "priority", "due", "category", "recurrence", "done")}
            sets = ", ".join(f"{key}=?" for key in fields)
            d.execute(f"UPDATE tasks SET {sets} WHERE id=?",
                      (*fields.values(), int(data.get("id"))))
        elif kind == "incident_restore_full":
            previous = data.get("prev") or {}
            fields = {key: previous.get(key) for key in
                      ("title", "detail", "severity", "category", "lat", "lon", "resolved")}
            sets = ", ".join(f"{key}=?" for key in fields)
            d.execute(f"UPDATE incidents SET {sets} WHERE id=?",
                      (*fields.values(), int(data.get("id"))))
        elif kind == "log_restore":
            previous = data.get("prev") or {}
            fields = {key: previous.get(key) for key in ("title", "body", "category", "tags")}
            sets = ", ".join(f"{key}=?" for key in fields)
            d.execute(f"UPDATE log_entries SET {sets} WHERE id=?",
                      (*fields.values(), int(data.get("id"))))
        elif kind == "log_remove":
            d.execute("DELETE FROM log_entries WHERE id=?", (int(data.get("id")),))
        elif kind in ("log_restore_row", "inventory_restore_row"):
            row = data.get("row") or {}
            table = "log_entries" if kind == "log_restore_row" else "inventory"
            if row:
                columns = list(row.keys())
                d.execute(f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) "
                          f"VALUES({', '.join('?' for _ in columns)})",
                          tuple(row.get(column) for column in columns))
        elif kind == "checklist_reset_restore":
            for entry in data.get("rows") or []:
                d.execute("INSERT INTO checklist_state(kit_id,item_id,checked,updated) VALUES(?,?,?,?) "
                          "ON CONFLICT(kit_id,item_id) DO UPDATE SET checked=excluded.checked, updated=excluded.updated",
                          (entry.get("kit_id"), entry.get("item_id"),
                           int(entry.get("checked") or 0), entry.get("updated") or utcnow()))
        elif kind == "inventory_restore":
            previous = data.get("prev") or {}
            fields = {k: previous.get(k) for k in
                      ("qty", "unit", "category", "min_qty", "location", "notes", "expires")}
            fields["updated"] = utcnow()
            sets = ", ".join(f"{key}=?" for key in fields)
            d.execute(f"UPDATE inventory SET {sets} WHERE id=?",
                      (*fields.values(), int(data.get("id"))))
        elif kind == "inventory_delete":
            d.execute("DELETE FROM inventory WHERE id=?", (int(data.get("id")),))
        elif kind == "task_done":
            d.execute("UPDATE tasks SET done=? WHERE id=?",
                      (int(data.get("done") or 0), int(data.get("id"))))
        elif kind == "task_delete":
            d.execute("DELETE FROM tasks WHERE id=?", (int(data.get("id")),))
        elif kind == "incident_delete":
            d.execute("DELETE FROM incidents WHERE id=?", (int(data.get("id")),))
        elif kind == "incident_restore":
            if data.get("detail") is not None:
                d.execute("UPDATE incidents SET resolved=?, detail=? WHERE id=?",
                          (int(data.get("resolved") or 0), str(data.get("detail")),
                           int(data.get("id"))))
            else:
                d.execute("UPDATE incidents SET resolved=? WHERE id=?",
                          (int(data.get("resolved") or 0), int(data.get("id"))))
        elif kind == "checklist_restore":
            d.execute("INSERT INTO checklist_state(kit_id,item_id,checked,updated) VALUES(?,?,?,?) "
                      "ON CONFLICT(kit_id,item_id) DO UPDATE SET checked=excluded.checked, updated=excluded.updated",
                      (str(data.get("kit_id")), str(data.get("item_id")),
                       1 if data.get("checked") else 0, utcnow()))
        elif kind == "water_delete":
            d.execute("DELETE FROM water_log WHERE id=?", (int(data.get("id")),))
        elif kind == "waypoint_delete":
            d.execute("DELETE FROM waypoints WHERE id=?", (int(data.get("id")),))
        else:
            raise HTTPException(status_code=400, detail="Unknown undo action")
        d.audit("ASSISTANT_UNDO", str(entry.get("label") or kind)[:300])
        return {"ok": True, "label": entry.get("label") or "Undone"}

    @app.post("/api/assistant/log")
    def assistant_log(request: Request, payload: dict = Body(...)):
        # Chat-log saves are encrypted at rest: keep the legacy path as an
        # alias so older clients cannot accidentally write plaintext logs.
        return assistant_vault_note(request, payload)

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    def get_settings(request: Request):
        return db(request).all_settings()

    @app.put("/api/settings")
    async def put_settings(request: Request, payload: dict = Body(...)):
        d = db(request)
        current = d.all_settings()
        payload = dict(payload)
        if payload.get("opsec_mode") and not current.get("opsec_mode"):
            phonegps.pairing.stop()
        tx_ack = payload.pop("tx_ack", None)
        allowed = set(config.default_settings().keys())
        unknown = set(payload) - allowed
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown settings: {', '.join(sorted(unknown))}")
        if payload.get("sdr_tx_enabled") and not (
                payload.get("sdr_tx_attested", current.get("sdr_tx_attested"))):
            raise HTTPException(status_code=400, detail="Legal attestation must be recorded before TX can be enabled")
        if payload.get("sdr_tx_enabled") or payload.get("sdr_tx_attested"):
            if str(tx_ack or "") != TX_ACK:
                raise HTTPException(status_code=400, detail=f'Transmit acknowledgement required: send tx_ack="{TX_ACK}"')
        numeric = {"lat": (-90, 90), "lon": (-180, 180), "elevation_m": (-500, 9000),
                   "declination": (-180, 180), "sdr_rx_gain": (0, 62), "sdr_lna_gain": (0, 40),
                   "position_accuracy_m": (0, 100000),
                   "position_mode": (0, 3), "font_scale": (0.8, 1.5)}
        for key, value in payload.items():
            if key in numeric:
                if key == "position_accuracy_m" and value is None:
                    d.set_setting(key, None)
                    continue
                try:
                    f = float(value)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{key} must be numeric")
                lo, hi = numeric[key]
                if not lo <= f <= hi:
                    raise HTTPException(status_code=400, detail=f"{key} out of range")
                value = f
            if key in ("sdr_tx_enabled", "sdr_tx_attested", "sdr_amp", "scanlines", "sound",
                       "map_vector", "auto_deep_index", "feed_auto_refresh", "opsec_mode", "dem_enabled", "assistant_enabled", "assistant_think", "assistant_tools", "assistant_tools_auto", "assistant_library_context", "assistant_history_filter"):
                value = bool(value)
            if key in ("callsign", "team", "operator", "timezone", "units", "date_format",
                       "grid_format", "accent", "tile_source", "mbtiles_path", "dem_mbtiles_path",
                       "dem_encoding", "epoch", "position_source", "position_updated_at", "position_fix_at",
                       "ollama_base_url", "ollama_model", "assistant_keep_alive"):
                value = str(value)[:500]
            if key == "assistant_system_prompt":
                value = str(value or "")[:8000]
            if key in ("assistant_timeout_sec", "assistant_num_ctx", "assistant_max_tokens"):
                try:
                    iv = int(float(value))
                except Exception:
                    raise HTTPException(status_code=400, detail=f"{key} must be a number")
                if key == "assistant_timeout_sec":
                    iv = max(5, min(1800, iv))
                if key == "assistant_num_ctx":
                    iv = max(512, min(32768, iv))
                if key == "assistant_max_tokens":
                    iv = max(128, min(8192, iv))
                value = iv
            if key == "assistant_temperature":
                try:
                    fv = float(value)
                except Exception:
                    raise HTTPException(status_code=400, detail="assistant_temperature must be a number")
                value = max(0.0, min(2.0, fv))
            if key == "ollama_base_url" and value:
                v = str(value).strip()
                if not v.startswith(("http://", "https://")):
                    raise HTTPException(status_code=400, detail="ollama_base_url must start with http:// or https://")
                value = v.rstrip("/")
            if key in ("mbtiles_path", "dem_mbtiles_path") and value:
                p = Path(str(value)).expanduser()
                if not p.exists() or not p.is_file():
                    raise HTTPException(status_code=400, detail=f"{key} file does not exist")
            if key == "dem_encoding" and value and str(value) not in ("terrarium", "mapbox", "custom"):
                raise HTTPException(status_code=400, detail="dem_encoding must be terrarium, mapbox, or custom")
            d.set_setting(key, value)
        if "lan_access_enabled" in payload:
            lm = getattr(request.app.state, "lan_manager", None)
            if payload.get("lan_access_enabled"):
                if lm is None:
                    from .lan import LanManager
                    lm = LanManager()
                    request.app.state.lan_manager = lm
                if not (d.get_setting("opsec_mode") if False else False):
                    pass
                if d.get_setting("opsec_mode"):
                    d.set_setting("lan_access_enabled", False)
                else:
                    if lm.start(request.app, ip=request.app.state.lan_ip):
                        if lm.ip:
                            request.app.state.allowed_hosts.add(lm.ip)
                        if lm.ip and lm.port:
                            request.app.state.allowed_origins.add(f"https://{lm.ip}:{lm.port}")
                    else:
                        d.set_setting("lan_access_enabled", False)
            elif lm is not None:
                lm.stop()
        if "opsec_mode" in payload and payload.get("opsec_mode"):
            lm = getattr(request.app.state, "lan_manager", None)
            if lm is not None:
                lm.stop()
            d.set_setting("lan_access_enabled", False)
        d.audit("SETTINGS", f"Updated: {', '.join(sorted(payload))}",
                request.client.host if request.client else "")
        updated = d.all_settings()
        try:
            old_sig = (round(float(current.get("lat")), 4), round(float(current.get("lon")), 4))
            new_sig = (round(float(updated.get("lat")), 4), round(float(updated.get("lon")), 4))
            if old_sig != new_sig:
                autosync.engine.position_changed()
        except (TypeError, ValueError):
            pass
        return updated

    # ------------------------------------------------------------- records
    def _table(name: str) -> dict:
        spec = RECORD_TABLES.get(name)
        require(spec is not None, "Unknown record table", 404)
        return spec

    def _validate_trip(d: Database, values: dict, existing: dict | None = None) -> None:
        trip = dict(existing or {})
        trip.update(values)
        try:
            interval_value = trip.get("checkin_interval_min")
            party_value = trip.get("party_size")
            interval = int(60 if interval_value in (None, "") else interval_value)
            party = int(1 if party_value in (None, "") else party_value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Check-in interval and party size must be whole numbers")
        require(5 <= interval <= 1440, "Check-in interval must be 5–1440 minutes")
        require(1 <= party <= 100, "Party size must be 1–100")
        for key in ("start_waypoint_id", "end_waypoint_id"):
            waypoint_id = trip.get(key)
            if waypoint_id is not None:
                require(bool(d.one("SELECT 1 FROM waypoints WHERE id=?", (waypoint_id,))),
                        f"{key.replace('_', ' ').title()} does not exist")
        departure = _parse_utc(trip.get("departure_at"))
        expected_return = _parse_utc(trip.get("return_at"))
        require(departure is not None and expected_return is not None,
                "Departure and expected return must be valid date/time values")
        require(expected_return > departure, "Expected return must be after departure")

    @app.get("/api/records/{table}")
    def records_list(request: Request, table: str, q: str = "", limit: int = Query(200, ge=1, le=2000),
                     offset: int = Query(0, ge=0)):
        spec = _table(table)
        d = db(request)
        sql = f"SELECT * FROM {table}"
        params: list = []
        if q.strip():
            like = f"%{q.strip()[:200]}%"
            clauses = [f"{f} LIKE ?" for f in spec["search"]]
            sql += " WHERE (" + " OR ".join(clauses) + ")"
            params += [like] * len(clauses)
        sql += f" ORDER BY {spec['order']} LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = [dict(r) for r in d.query(sql, params)]
        count = d.one(f"SELECT count(*) c FROM {table}")["c"]
        return {"items": rows, "count": count}

    @app.get("/api/records/{table}/{row_id}")
    def records_get(request: Request, table: str, row_id: int):
        _table(table)
        row = db(request).one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        require(bool(row), "Record not found", 404)
        return dict(row)

    @app.post("/api/records/{table}")
    def records_create(request: Request, table: str, payload: dict = Body(...)):
        spec = _table(table)
        clean = {}
        for field in spec["fields"]:
            if table == "trip_plans" and field in ("status", "started_at", "last_checkin_at"):
                continue  # lifecycle fields are written only by trip actions / restore
            if field in payload:
                clean[field] = _clean_value(field, payload[field])
        for req in spec.get("required", []):
            require(clean.get(req) not in (None, ""), f"{req} is required")
        stamp = spec.get("stamp")
        if stamp and not clean.get(stamp):
            clean[stamp] = utcnow()
        if table == "patients":
            clean.setdefault("created", utcnow())
            clean["updated"] = utcnow()
        if table == "inventory":
            clean["updated"] = utcnow()
        if table == "caches":
            clean["updated"] = utcnow()
        if table == "waypoints":
            clean.setdefault("created", utcnow())
            clean["updated"] = utcnow()
            if not clean.get("type"):
                clean["type"] = "WAYPOINT"
        if table == "trip_plans":
            clean.setdefault("checkin_interval_min", 60)
            clean.setdefault("party_size", 1)
            clean["updated"] = utcnow()
            _validate_trip(db(request), clean)
        if table == "tasks":
            rec = str(clean.get("recurrence") or "").strip().upper()
            clean["recurrence"] = rec if rec in RECURRENCE_VALUES else ""
        new_id = db(request).insert(table, clean)
        db(request).audit("RECORD_NEW", f"{table} #{new_id}")
        return {"id": new_id, "item": dict(db(request).one(f"SELECT * FROM {table} WHERE id=?", (new_id,)))}

    @app.patch("/api/records/{table}/{row_id}")
    def records_update(request: Request, table: str, row_id: int, payload: dict = Body(...)):
        spec = _table(table)
        d = db(request)
        current = d.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        require(bool(current), "Record not found", 404)
        clean = {
            field: _clean_value(field, payload[field])
            for field in spec["fields"]
            if field in payload and not (table == "trip_plans" and field in ("status", "started_at", "last_checkin_at"))
        }
        if table in ("patients", "inventory", "caches", "waypoints", "trip_plans"):
            clean["updated"] = utcnow()
        if table == "trip_plans":
            _validate_trip(d, clean, dict(current))
        if table == "tasks":
            rec = str(clean.get("recurrence", current["recurrence"]) or "").strip().upper()
            rec = rec if rec in RECURRENCE_VALUES else ""
            if "recurrence" in clean:
                clean["recurrence"] = rec
            next_task_id = None
            was_done = bool(current["done"])
            now_done = bool(clean.get("done", was_done))
            if now_done and not was_done and rec:
                # Completing a recurring task schedules the next occurrence and
                # keeps the completed row as an immutable history entry.
                base_due = clean["due"] if "due" in clean else current["due"]
                following = {field: current[field] for field in spec["fields"]
                             if field in current.keys()}
                following.update({k: v for k, v in clean.items() if k != "done"})
                following["due"] = _next_due(base_due, rec)
                following["done"] = 0
                following["recurrence"] = rec
                following["ts"] = utcnow()
                following.pop("id", None)
                next_task_id = d.insert("tasks", following)
                d.audit("TASK_RECURRENCE", f"tasks #{row_id} -> next #{next_task_id} ({rec})")
            d.update(table, row_id, clean)
            d.audit("RECORD_UPDATE", f"{table} #{row_id}")
            return {"ok": True, "item": dict(d.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))),
                    "next_task_id": next_task_id}
        d.update(table, row_id, clean)
        d.audit("RECORD_UPDATE", f"{table} #{row_id}")
        return {"ok": True, "item": dict(d.one(f"SELECT * FROM {table} WHERE id=?", (row_id,)))}

    @app.delete("/api/records/{table}/{row_id}")
    def records_delete(request: Request, table: str, row_id: int):
        _table(table)
        d = db(request)
        current = d.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        require(bool(current), "Record not found", 404)
        if table == "trip_plans":
            require(current["status"] != "ACTIVE", "Finish or cancel the active trip before deleting its plan", 409)
        d.delete(table, row_id)
        d.audit("RECORD_DELETE", f"{table} #{row_id}")
        return {"deleted": row_id}

    # ------------------------------------------------- maintenance / expiry
    @app.get("/api/maintenance/due")
    def maintenance_due(request: Request, days: int = Query(30, ge=0, le=365)):
        """Open tasks and inventory lines that are due or expiring soon."""
        d = db(request)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        horizon = now + timedelta(days=days)
        due_tasks = []
        for row in d.query("SELECT * FROM tasks WHERE done=0 AND due IS NOT NULL AND due<>''"):
            parsed = _parse_due(row["due"])
            if parsed is None or parsed > horizon:
                continue
            item = dict(row)
            item["due_in_days"] = round((parsed - now).total_seconds() / 86400, 1)
            item["overdue"] = parsed < now
            due_tasks.append(item)
        due_tasks.sort(key=lambda r: r["due"] or "")
        expiring = []
        for row in d.query("SELECT * FROM inventory WHERE expires IS NOT NULL AND expires<>''"):
            parsed = _parse_expiry(row["expires"])
            if parsed is None or parsed > horizon:
                continue
            item = dict(row)
            item["expires_in_days"] = round((parsed - now).total_seconds() / 86400, 1)
            item["expired"] = parsed < now
            expiring.append(item)
        expiring.sort(key=lambda r: (r["expires_in_days"], r.get("name") or ""))
        recurring = d.query("SELECT * FROM tasks WHERE done=0 AND recurrence IS NOT NULL "
                            "AND recurrence<>'' ORDER BY due ASC")
        return {
            "tasks": due_tasks,
            "inventory": expiring,
            "recurring": [dict(r) for r in recurring],
            "overdue_tasks": sum(1 for r in due_tasks if r["overdue"]),
            "expired_inventory": sum(1 for r in expiring if r["expired"]),
            "window_days": days,
        }

    # --------------------------------------------------------- field trips
    @app.get("/api/trips")
    def trips_list(request: Request):
        items = trip_items(db(request))
        return {"items": items, "active": sum(1 for trip in items if trip["status"] == "ACTIVE")}

    @app.post("/api/trips/{trip_id}/start")
    def trip_start(request: Request, trip_id: int):
        d = db(request)
        row = d.one("SELECT * FROM trip_plans WHERE id=?", (trip_id,))
        require(bool(row), "Trip plan not found", 404)
        require(row["status"] == "PLANNED", "Only a planned trip can be started", 409)
        now = utcnow()
        d.update("trip_plans", trip_id, {
            "status": "ACTIVE", "started_at": now, "last_checkin_at": now, "updated": now,
        })
        d.audit("TRIP_START", f"trip #{trip_id}")
        return {"item": next(t for t in trip_items(d) if t["id"] == trip_id)}

    @app.post("/api/trips/{trip_id}/check-in")
    def trip_checkin(request: Request, trip_id: int):
        d = db(request)
        row = d.one("SELECT * FROM trip_plans WHERE id=?", (trip_id,))
        require(bool(row), "Trip plan not found", 404)
        require(row["status"] == "ACTIVE", "Only an active trip can check in", 409)
        now = utcnow()
        d.update("trip_plans", trip_id, {"last_checkin_at": now, "updated": now})
        d.audit("TRIP_CHECKIN", f"trip #{trip_id}; operator recorded local check-in")
        return {"item": next(t for t in trip_items(d) if t["id"] == trip_id),
                "note": "Recorded locally only. No contact was notified and no location was verified."}

    @app.post("/api/trips/{trip_id}/finish")
    def trip_finish(request: Request, trip_id: int):
        d = db(request)
        row = d.one("SELECT * FROM trip_plans WHERE id=?", (trip_id,))
        require(bool(row), "Trip plan not found", 404)
        require(row["status"] == "ACTIVE", "Only an active trip can be finished", 409)
        now = utcnow()
        d.update("trip_plans", trip_id, {"status": "COMPLETED", "updated": now})
        d.audit("TRIP_FINISH", f"trip #{trip_id}")
        return {"item": next(t for t in trip_items(d) if t["id"] == trip_id)}

    @app.post("/api/trips/{trip_id}/cancel")
    def trip_cancel(request: Request, trip_id: int):
        d = db(request)
        row = d.one("SELECT * FROM trip_plans WHERE id=?", (trip_id,))
        require(bool(row), "Trip plan not found", 404)
        require(row["status"] in ("PLANNED", "ACTIVE"), "Trip is already closed", 409)
        now = utcnow()
        d.update("trip_plans", trip_id, {"status": "CANCELLED", "updated": now})
        d.audit("TRIP_CANCEL", f"trip #{trip_id}")
        return {"item": next(t for t in trip_items(d) if t["id"] == trip_id)}

    # ------------------------------------------------------------ calc API
    @app.get("/api/calc")
    def calc_catalog():
        return {"catalog": CALC_CATALOG}

    @app.post("/api/calc/{name}")
    def calc_run(name: str, payload: dict = Body(default={})):
        fn = CALC_FUNCS.get(name)
        require(fn is not None, "Unknown calculator", 404)
        try:
            import inspect
            sig = inspect.signature(fn)
            kwargs = {}
            for pname, param in sig.parameters.items():
                if pname in payload and payload[pname] not in ("", None):
                    value = payload[pname]
                    # Check annotation as type or string (due to __future__ annotations)
                    ann = param.annotation
                    if ann in (int, float) or ann in ("int", "float"):
                        value = float(value)
                        if ann is int or ann == "int":
                            value = int(value)
                    kwargs[pname] = value
            result = fn(**kwargs)
            return {"ok": True, "name": name, "result": result}
        except Exception as exc:
            return JSONResponse({"ok": False, "name": name, "error": str(exc)}, status_code=400)

    # ---------------------------------------------------------------- feeds
    @app.get("/api/feeds")
    def feeds_list(request: Request):
        d = db(request)
        feed_rows = feeds.list_feeds(d)
        return {"feeds": feed_rows, "summary": feeds.summary(d, feed_rows)}

    @app.post("/api/feeds/refresh")
    async def feeds_refresh(request: Request, payload: dict = Body(default={})):
        feed_id = payload.get("id")
        result = await run_in_threadpool(feeds.refresh, db(request), feed_id)
        db(request).audit("FEED_REFRESH", json.dumps(result)[:2000])
        return {"results": result, "feeds": feeds.list_feeds(db(request))}

    @app.put("/api/feeds/{feed_id}")
    def feed_update(request: Request, feed_id: str, payload: dict = Body(...)):
        d = db(request)
        row = d.one("SELECT * FROM feeds WHERE id=?", (feed_id,))
        require(bool(row), "Feed not found", 404)
        name = str(payload.get("name", row["name"]) or "").strip()[:200]
        url = str(payload.get("url", row["url"]) or "").strip()[:4000]
        kind = str(payload.get("kind", row["kind"]) or "").strip()[:64]
        require(bool(name), "Feed name is required")
        require(bool(kind), "Feed kind is required")
        source_changed = url != (row["url"] or "") or kind != (row["kind"] or "")
        if source_changed:
            # Changing where a feed points invalidates its cache. Retaining the
            # old payload would present stale items as if they came from the new
            # source, so clear it and require an explicit refresh.
            d.execute("UPDATE feeds SET name=?, url=?, kind=?, status='NEVER', "
                      "fetched_at=NULL, payload=NULL, error=NULL WHERE id=?",
                      (name, url, kind, feed_id))
        else:
            d.execute("UPDATE feeds SET name=? WHERE id=?", (name, feed_id))
        d.audit("FEED_UPDATE", f"{feed_id}: {name} <{url}> ({kind})")
        return {"ok": True, "source_changed": source_changed,
                "feed": dict(d.one("SELECT id,name,url,kind,status,error FROM feeds WHERE id=?",
                                   (feed_id,)))}

    # ---------------------------------------------------------------- cctv
    def _cctv_frame_cache_key(camera_id: str, url: str) -> str:
        return hashlib.sha256(f"{camera_id}\n{url}".encode("utf-8", errors="ignore")).hexdigest()[:32]

    def _cctv_frame_set_status(camera_id: str, *, db: Database | None = None, kind: str = "frame", **fields):
        with _CCTV_FRAME_LOCK:
            cur = dict(_CCTV_FRAME_STATUS.get(camera_id) or {"id": camera_id})
            cur.update(fields)
            now = utcnow()
            cur["updated_at"] = now
            cur["at"] = now
            cur["camera_id"] = camera_id
            cur.setdefault("kind", kind)
            _CCTV_FRAME_STATUS[camera_id] = cur
            if len(_CCTV_FRAME_STATUS) > 4000:
                for k in list(_CCTV_FRAME_STATUS.keys())[:500]:
                    _CCTV_FRAME_STATUS.pop(k, None)
        if db is not None:
            try:
                db.upsert_cctv_media_health(
                    camera_id, kind=kind,
                    status=str(cur.get("status") or ""),
                    message=str(cur.get("message") or ""),
                    source_url=str(cur.get("source_url") or cur.get("url") or ""),
                    extra={k: v for k, v in cur.items() if k not in ("camera_id", "id") and not isinstance(v, (bytes, bytearray))},
                )
            except Exception:
                pass

    def _cctv_frame_store(camera_id: str, url: str, body: bytes, ctype: str, refresh_ms: int):
        now = time.time()
        entry = {
            "id": camera_id, "url": url, "body": body, "ctype": ctype,
            "fetched_at": now, "refresh_ms": refresh_ms,
            "next_allowed_at": now + max(5.0, refresh_ms / 1000.0),
            "bytes": len(body),
        }
        key = _cctv_frame_cache_key(camera_id, url)
        with _CCTV_FRAME_LOCK:
            old = _CCTV_FRAME_CACHE.pop(key, None)
            total = sum(v.get("bytes", 0) for v in _CCTV_FRAME_CACHE.values()) + len(body)
            while _CCTV_FRAME_CACHE and (len(_CCTV_FRAME_CACHE) >= _CCTV_FRAME_MAX_ENTRIES or total > _CCTV_FRAME_MAX_BYTES):
                k0, v0 = next(iter(_CCTV_FRAME_CACHE.items()))
                _CCTV_FRAME_CACHE.pop(k0, None)
                total -= int(v0.get("bytes", 0) or 0)
            _CCTV_FRAME_CACHE[key] = entry
        _cctv_frame_set_status(camera_id, status="ok", source_kind="registered_snapshot", message="", content_type=ctype, bytes=len(body), refresh_ms=refresh_ms, source_url=url)

    def _cctv_frame_get(camera_id: str, url: str):
        key = _cctv_frame_cache_key(camera_id, url)
        with _CCTV_FRAME_LOCK:
            ent = _CCTV_FRAME_CACHE.get(key)
            if not ent:
                return None
            # move to end (LRU-ish)
            _CCTV_FRAME_CACHE.pop(key, None)
            _CCTV_FRAME_CACHE[key] = ent
            return ent

    def _find_registered_cctv_media(d: Database, camera_id: str) -> dict | None:
        cid = str(camera_id or "")
        if not cid:
            return None
        rows = d.query("SELECT id, payload FROM feeds WHERE kind='cctv'")
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                continue
            items = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                continue
            fid = row["id"]
            for it in items:
                if not isinstance(it, dict):
                    continue
                composite = f"{fid}:{it.get('id') or it.get('camera_id') or it.get('title')}"
                candidates = {composite, str(it.get("id") or ""), str(it.get("camera_id") or "")}
                if cid not in candidates:
                    continue
                url = None
                for key in ("snapshot_url", "snapshotUrl", "image_url", "imageUrl", "feed_url"):
                    val = it.get(key)
                    if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                        url = val.strip(); break
                if not url:
                    # Generic `url` is media for curated catalog/stream entries,
                    # but for OSM cameras it is the openstreetmap.org node page.
                    # Treating that page as a snapshot caused HTML content-type
                    # errors (and slow page fetches) for every OSM camera.
                    cand = it.get("url")
                    feed_type = str(it.get("feed_type") or it.get("feedType") or "").lower()
                    if isinstance(cand, str) and cand.strip().startswith(("http://", "https://")):
                        cand = cand.strip()
                        host = (urlsplit(cand).hostname or "").lower()
                        is_page = host in ("openstreetmap.org", "www.openstreetmap.org", "osm.org", "www.osm.org")
                        is_media = (
                            feed_type in ("image", "jpeg", "jpg", "png", "snapshot",
                                          "hls", "m3u8", "mp4", "video")
                            or bool(re.search(r"\.(jpe?g|png|gif|webp|bmp|m3u8|mp4|m4v|webm)(\?|$)", cand, re.I))
                        )
                        if not is_page and is_media:
                            url = cand
                if not url:
                    continue
                refresh_ms = it.get("frame_refresh_ms", it.get("frameRefreshMs"))
                try:
                    refresh_ms = int(float(refresh_ms)) if refresh_ms not in (None, "") else None
                except Exception:
                    refresh_ms = None
                if not refresh_ms or refresh_ms < 5000:
                    refresh_ms = 300000
                refresh_ms = min(refresh_ms, 3600000)
                return {
                    "url": url, "refresh_ms": refresh_ms,
                    "title": it.get("title") or it.get("name"),
                    "credit": it.get("credit"), "license": it.get("license"),
                    "feed_type": it.get("feed_type") or it.get("feedType"),
                    "composite_id": composite,
                }
        return None

    def _fetch_cctv_frame(media: dict, force: bool = False) -> tuple[bytes, str, bool]:
        camera_id = str(media.get("composite_id") or media.get("id") or "")
        url = str(media.get("url") or "")
        refresh_ms = int(media.get("refresh_ms") or 300000)
        cached = _cctv_frame_get(camera_id, url)
        now = time.time()
        if cached and not force:
            if now - float(cached.get("fetched_at") or 0) < max(5.0, refresh_ms / 1000.0):
                _cctv_frame_set_status(camera_id, status="cached", source_kind="registered_snapshot", message="", source_url=url)
                return cached["body"], cached["ctype"], True
        lock_key = _cctv_frame_cache_key(camera_id, url)
        with _CCTV_FRAME_LOCK:
            if lock_key in _CCTV_FRAME_INFLIGHT and cached:
                return cached["body"], cached["ctype"], True
            if lock_key not in _CCTV_FRAME_INFLIGHT:
                _CCTV_FRAME_INFLIGHT.add(lock_key)
        try:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or parts.username or parts.password:
                raise ValueError("Invalid snapshot URL")
            host = (parts.hostname or "").lower()
            if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "metadata.google.internal"):
                raise ValueError("Snapshot host not allowed")
            r = requests.get(
                url,
                headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "image/*,*/*;q=0.8"},
                timeout=(5, 20),
                stream=True,
                allow_redirects=True,
            )
            r.raise_for_status()
            ctype = (r.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()
            if not (ctype.startswith("image/") or ctype in ("application/octet-stream", "binary/octet-stream")):
                raise ValueError(f"Unsupported snapshot content type: {ctype}")
            data = r.raw.read(_CCTV_FRAME_MAX_ITEM_BYTES + 1, decode_content=True)
            if len(data) > _CCTV_FRAME_MAX_ITEM_BYTES:
                raise ValueError("Snapshot too large")
            _cctv_frame_store(camera_id, url, data, ctype, refresh_ms)
            return data, ctype, False
        except Exception as exc:
            _cctv_frame_set_status(camera_id, status="error", source_kind="registered_snapshot", message=str(exc)[:220], source_url=url)
            if cached:
                return cached["body"], cached["ctype"], True
            raise
        finally:
            with _CCTV_FRAME_LOCK:
                _CCTV_FRAME_INFLIGHT.discard(lock_key)

    @app.get("/api/cctv/catalog/status")
    def cctv_catalog_status(request: Request):
        d = db(request)
        row = d.one("SELECT id,name,kind,status,error,fetched_at,payload FROM feeds WHERE id='cctv_catalog'")
        count = 0
        source_note = ""
        pack_stats = []
        disabled_sources = []
        if row:
            try:
                payload = json.loads(row["payload"] or "{}")
                items = payload.get("items") if isinstance(payload, dict) else payload
                if isinstance(items, list):
                    count = len(items)
                if isinstance(payload, dict):
                    source_note = payload.get("source_note") or ""
                    pack_stats = payload.get("pack_stats") or []
                    disabled_sources = payload.get("disabled_sources") or []
            except Exception:
                pass
        try:
            files = [str(p) for p in feeds._cctv_catalog_candidates(d)]
        except Exception:
            files = []
        # attach last frame status summary counts + recent errors (operational visibility)
        with _CCTV_FRAME_LOCK:
            frame_status = {"cached": 0, "ok": 0, "error": 0}
            recent_errors = []
            for v in _CCTV_FRAME_STATUS.values():
                s = str((v or {}).get("status") or "")
                if s in frame_status: frame_status[s] += 1
                elif s: frame_status["ok"] += 1
                if s == "error":
                    recent_errors.append({"id": v.get("id") or v.get("camera_id"), "at": v.get("at") or v.get("updated_at"), "message": (v.get("message") or "")[:180]})
            recent_errors.sort(key=lambda x: x.get("at") or "", reverse=True)
            recent_errors = recent_errors[:12]
        try:
            db_errors = d.recent_cctv_media_errors(limit=12)
            seen = {str(e.get("id") or "") for e in recent_errors}
            for e in db_errors:
                eid = str(e.get("camera_id") or e.get("id") or "")
                if eid and eid in seen: continue
                recent_errors.append({"id": eid, "at": e.get("updated_at") or e.get("error_at"), "message": (e.get("message") or "")[:180], "kind": e.get("kind")})
            recent_errors.sort(key=lambda x: x.get("at") or "", reverse=True)
            recent_errors = recent_errors[:12]
        except Exception:
            pass
        catalog_refresh_hours = d.all_settings().get("cctv_catalog_refresh_hours") or ""
        return {"feed": dict(row) if row else None, "item_count": count, "source_note": source_note,
                "candidate_files": files, "pack_stats": pack_stats, "disabled_sources": disabled_sources,
                "frame_cache": {"entries": len(_CCTV_FRAME_CACHE), "status_counts": frame_status, "recent_errors": recent_errors},
                "catalog_refresh_hours": catalog_refresh_hours}

    @app.get("/api/cctv/catalog/packs")
    def cctv_catalog_packs(request: Request):
        d = db(request)
        row = d.one("SELECT payload FROM feeds WHERE ctid='cctv_catalog'".replace("ctid", "id"))
        payload = {}
        try:
            if row:
                payload = json.loads(row["payload"] or "{}")
        except Exception:
            payload = {}
        pack_stats = payload.get("pack_stats") if isinstance(payload, dict) else []
        disabled = []
        try:
            raw = d.all_settings().get("cctv_catalog_disabled_sources") or "[]"
            disabled = json.loads(raw) if isinstance(raw, str) else (raw or [])
        except Exception:
            disabled = payload.get("disabled_sources") if isinstance(payload, dict) else []
        # if no pack_stats yet, derive from candidates
        if not pack_stats:
            try:
                disabled_set = {str(x) for x in (disabled or [])}
                for p in feeds._cctv_catalog_candidates(d):
                    pack_stats.append({"source": p.name, "path": str(p), "enabled": p.name not in disabled_set and str(p) not in disabled_set, "count": None, "error": None})
            except Exception:
                pass
        return {"packs": pack_stats, "disabled": disabled}

    @app.put("/api/cctv/catalog/packs")
    async def cctv_catalog_packs_update(request: Request, body: dict = Body(...)):
        d = db(request)
        disabled = body.get("disabled")
        if not isinstance(disabled, list):
            raise HTTPException(status_code=400, detail="disabled must be a list of source names/paths")
        disabled = [str(x) for x in disabled if x is not None and str(x).strip()][:500]
        d.set_setting("cctv_catalog_disabled_sources", json.dumps(disabled))
        d.audit("CCTV_CATALOG_PACKS", json.dumps({"disabled_count": len(disabled)}), request.client.host if request.client else "")
        return {"disabled": disabled}

    @app.post("/api/cctv/catalog/import")
    async def cctv_catalog_import(request: Request):
        result = await run_in_threadpool(feeds.refresh, db(request), "cctv_catalog")
        return {"results": result}

    @app.get("/api/cctv/frame/status")
    def cctv_frame_status(request: Request, id: str = Query(..., min_length=1, max_length=500)):
        with _CCTV_FRAME_LOCK:
            st = dict(_CCTV_FRAME_STATUS.get(id) or {})
        if not st:
            st = {"id": id, "status": "unknown"}
        return st

    @app.get("/api/cctv/frame")
    async def cctv_frame(request: Request, id: str = Query(..., min_length=1, max_length=500), force: int = Query(0)):
        d = db(request)
        media = await run_in_threadpool(_find_registered_cctv_media, d, id)
        if not media:
            raise HTTPException(status_code=404, detail="Frame URL not registered for this camera")
        cam_id = str(media.get("composite_id") or id)
        try:
            data, ctype, cached = await run_in_threadpool(_fetch_cctv_frame, media, bool(force))
        except Exception as exc:
            _cctv_frame_set_status(cam_id, db=d, status="error", kind="frame", source_kind="registered_snapshot", message=str(exc)[:220], source_url=str(media.get("url") or ""))
            raise HTTPException(status_code=502, detail=f"Frame fetch failed: {exc}")
        _cctv_frame_set_status(cam_id, db=d, status="cached" if cached else "ok", kind="frame", source_kind="registered_snapshot", message="", content_type=ctype, bytes=len(data), source_url=str(media.get("url") or ""), refresh_ms=int(media.get("refresh_ms") or 300000))
        headers = {
            "Cache-Control": "private, max-age=60",
            "X-CCTV-Frame": "cached" if cached else "fresh",
            "X-CCTV-Refresh-Ms": str(media.get("refresh_ms") or 300000),
        }
        return Response(content=data, media_type=ctype, headers=headers)

    @app.get("/api/cctv/snapshot")
    async def cctv_snapshot(request: Request, id: str = Query(..., min_length=1, max_length=500)):
        # Backward-compatible alias; uses cached/throttled frame logic.
        return await cctv_frame(request, id=id, force=0)

    def _find_registered_cctv_stream(d: Database, camera_id: str) -> dict | None:
        cid = str(camera_id or "")
        if not cid:
            return None
        rows = d.query("SELECT id, payload FROM feeds WHERE kind='cctv'")
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except Exception:
                continue
            items = payload.get("items") if isinstance(payload, dict) else payload
            if not isinstance(items, list):
                continue
            fid = row["id"]
            for it in items:
                if not isinstance(it, dict):
                    continue
                composite = f"{fid}:{it.get('id') or it.get('camera_id') or it.get('title')}"
                candidates = {composite, str(it.get("id") or ""), str(it.get("camera_id") or "")}
                if cid not in candidates:
                    continue
                feed_type = str(it.get("feed_type") or it.get("feedType") or "").lower()
                stream = it.get("stream_url") or it.get("streamUrl")
                url = it.get("url")
                # Accept explicit stream_url, or url when feed type/extension indicates stream.
                cand = stream or (url if (feed_type in ("hls", "m3u8", "mp4", "video") or (isinstance(url, str) and re.search(r"\.(m3u8|mp4|m4v|webm)(\?|$)", url, re.I))) else None)
                if not (isinstance(cand, str) and cand.strip().startswith(("http://", "https://"))):
                    # still return snapshot metadata for image feeds
                    return {"composite_id": composite, "feed_type": feed_type or "image", "stream_url": None,
                            "snapshot_url": it.get("snapshot_url") or it.get("snapshotUrl"), "title": it.get("title"), "credit": it.get("credit"), "license": it.get("license")}
                stype = feed_type or ("hls" if ".m3u8" in cand.lower() else "mp4")
                return {"composite_id": composite, "feed_type": stype, "stream_url": cand.strip(),
                        "snapshot_url": it.get("snapshot_url") or it.get("snapshotUrl"), "title": it.get("title"),
                        "credit": it.get("credit"), "license": it.get("license")}
        return None

    def _cctv_stream_allow_hosts(d: Database | None) -> set[str]:
        if d is None:
            return set()
        try:
            raw = d.all_settings().get("cctv_stream_host_allowlist") or "[]"
            vals = json.loads(raw) if isinstance(raw, str) else (raw or [])
            out = set()
            for v in vals or []:
                s = str(v or "").strip().lower()
                if s:
                    out.add(s)
            return out
        except Exception:
            return set()

    def _cctv_stream_allowed(registered_url: str, candidate_url: str, allow_hosts: set[str] | None = None) -> bool:
        try:
            rp = urlsplit(registered_url)
            cp = urlsplit(candidate_url)
        except Exception:
            return False
        if cp.scheme not in ("http", "https") or cp.username or cp.password:
            return False
        rh = (rp.hostname or "").lower()
        ch = (cp.hostname or "").lower()
        if not rh or not ch:
            return False
        # Primary safety control: same host as the registered stream/manifest source.
        if ch == rh:
            return True
        # Optional operator-configured allowlist for known CDNs/subresources (conservative).
        for ah in (allow_hosts or set()):
            if not ah:
                continue
            if ah.startswith("."):
                if ch == ah[1:] or ch.endswith(ah):
                    return True
            elif ch == ah or ch.endswith("." + ah):
                return True
        return False

    def _cctv_proxy_stream_resource(target_url: str, registered_url: str, request: Request, allow_hosts: set[str] | None = None):
        if not _cctv_stream_allowed(registered_url, target_url, allow_hosts):
            raise HTTPException(status_code=400, detail="Stream subresource host not allowed for this registered source")
        headers = {"User-Agent": "DarkSecNightwatch/2.0", "Accept": "*/*"}
        rng = request.headers.get("range")
        if rng:
            headers["Range"] = rng
        r = requests.get(target_url, headers=headers, timeout=(5, 20), stream=True, allow_redirects=False)
        # Do not follow redirects to arbitrary hosts.
        if r.is_redirect or r.is_permanent_redirect:
            loc = r.headers.get("location") or ""
            if loc:
                abs_loc = urljoin(target_url, loc)
                if not _cctv_stream_allowed(registered_url, abs_loc, allow_hosts):
                    r.close()
                    raise HTTPException(status_code=400, detail="Stream redirect target not allowed")
            r.close()
            r = requests.get(abs_loc, headers=headers, timeout=(5, 20), stream=True, allow_redirects=False)
        if r.status_code >= 400:
            detail = f"Upstream stream error {r.status_code}"
            r.close()
            raise HTTPException(status_code=502, detail=detail)
        ctype = (r.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        lower = target_url.lower()
        if ".m3u8" in lower and "mpegurl" not in ctype.lower() and "text" not in ctype.lower():
            ctype = "application/vnd.apple.mpegurl"
        if lower.endswith(".ts") or ".ts?" in lower:
            ctype = "video/mp2t"
        if lower.endswith(".mp4") or ".mp4?" in lower:
            ctype = "video/mp4"
        out_headers = {"Cache-Control": "no-store", "X-Registered-Stream": "1"}
        for h in ("content-range", "accept-ranges", "content-length"):
            v = r.headers.get(h)
            if v: out_headers[h.title() if h != "content-range" else "Content-Range"] = v
        if "accept-ranges" not in {k.lower() for k in out_headers}:
            out_headers["Accept-Ranges"] = "bytes"

        def iter_bytes():
            try:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        yield chunk
            finally:
                r.close()
        return StreamingResponse(iter_bytes(), status_code=r.status_code, media_type=ctype, headers=out_headers)

    def _rewrite_hls_manifest(text: str, manifest_url: str, camera_id: str, registered_url: str, allow_hosts: set[str] | None = None) -> str:
        lines = []
        for raw in text.splitlines():
            line = raw.strip("\r")
            if not line:
                lines.append(line); continue
            if line.startswith("#"):
                def repl(m):
                    uri = m.group(2)
                    absu = urljoin(manifest_url, uri)
                    if not _cctv_stream_allowed(registered_url, absu, allow_hosts):
                        return m.group(0)
                    enc = base64.urlsafe_b64encode(absu.encode()).decode().rstrip("=")
                    return f'{m.group(1)}/api/cctv/stream/resource?id={quote(camera_id)}&u={enc}&m={base64.urlsafe_b64encode(manifest_url.encode()).decode().rstrip("=")}{m.group(3)}'
                line = re.sub(r'(URI=")([^"]+)(")', repl, line)
                lines.append(line)
                continue
            absu = urljoin(manifest_url, line)
            if _cctv_stream_allowed(registered_url, absu, allow_hosts):
                enc = base64.urlsafe_b64encode(absu.encode()).decode().rstrip("=")
                menc = base64.urlsafe_b64encode(manifest_url.encode()).decode().rstrip("=")
                lines.append(f"/api/cctv/stream/resource?id={quote(camera_id)}&u={enc}&m={menc}")
            else:
                lines.append(line)
        return "\n".join(lines) + "\n"

    @app.get("/api/cctv/stream/info")
    async def cctv_stream_info(request: Request, id: str = Query(..., min_length=1, max_length=500)):
        d = db(request)
        info = await run_in_threadpool(_find_registered_cctv_stream, d, id)
        if not info:
            raise HTTPException(status_code=404, detail="Camera not registered")
        if not info.get("stream_url"):
            return {"ok": True, "id": info.get("composite_id"), "feed_type": info.get("feed_type") or "image", "stream_url": None,
                    "snapshot_url": info.get("snapshot_url"), "proxied_url": None, "title": info.get("title"), "credit": info.get("credit"), "license": info.get("license")}
        stype = info.get("feed_type") or ("hls" if ".m3u8" in str(info["stream_url"]).lower() else "mp4")
        proxied = f"/api/cctv/stream/manifest?id={quote(str(info.get('composite_id') or id))}" if stype in ("hls", "m3u8") else f"/api/cctv/stream/media?id={quote(str(info.get('composite_id') or id))}"
        return {"ok": True, "id": info.get("composite_id"), "feed_type": stype, "stream_url": info.get("stream_url"),
                "snapshot_url": info.get("snapshot_url"), "proxied_url": proxied, "title": info.get("title"), "credit": info.get("credit"), "license": info.get("license")}

    @app.get("/api/cctv/stream/manifest")
    async def cctv_stream_manifest(request: Request, id: str = Query(..., min_length=1, max_length=500)):
        d = db(request)
        info = await run_in_threadpool(_find_registered_cctv_stream, d, id)
        if not info or not info.get("stream_url"):
            raise HTTPException(status_code=404, detail="Registered stream not found")
        target = str(info["stream_url"])
        cam_id = str(info.get("composite_id") or id)
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https"):
            raise HTTPException(status_code=400, detail="Unsupported stream scheme")
        allow_hosts = await run_in_threadpool(_cctv_stream_allow_hosts, d)
        def fetch_text():
            r = requests.get(target, headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"}, timeout=(4, 15))
            r.raise_for_status()
            body = r.text
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("Manifest too large")
            return body
        try:
            text = await run_in_threadpool(fetch_text)
        except Exception as exc:
            _cctv_frame_set_status(cam_id, db=d, status="error", kind="stream", source_kind="registered_stream", message=str(exc)[:220], source_url=target)
            raise HTTPException(status_code=502, detail=f"Manifest fetch failed: {exc}")
        _cctv_frame_set_status(cam_id, db=d, status="ok", kind="stream", source_kind="registered_stream", message="", source_url=target)
        rewritten = _rewrite_hls_manifest(text, target, cam_id, target, allow_hosts)
        return Response(content=rewritten, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "X-Registered-Stream": "1"})

    @app.get("/api/cctv/stream/resource")
    async def cctv_stream_resource(request: Request, id: str = Query(..., min_length=1, max_length=500), u: str = Query(..., min_length=1), m: str = Query("", max_length=4000)):
        d = db(request)
        info = await run_in_threadpool(_find_registered_cctv_stream, d, id)
        if not info or not info.get("stream_url"):
            raise HTTPException(status_code=404, detail="Registered stream not found")
        try:
            pad = "=" * (-len(u) % 4)
            target = base64.urlsafe_b64decode((u + pad).encode()).decode("utf-8", errors="ignore")
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid stream resource reference")
        registered = str(info["stream_url"])
        cam_id = str(info.get("composite_id") or id)
        allow_hosts = await run_in_threadpool(_cctv_stream_allow_hosts, d)
        # If this is a nested playlist, rewrite it too.
        if ".m3u8" in target.lower().split("?")[0]:
            if not _cctv_stream_allowed(registered, target, allow_hosts):
                raise HTTPException(status_code=400, detail="Stream subresource host not allowed")
            def fetch_text():
                r = requests.get(target, headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"}, timeout=(4, 15))
                r.raise_for_status()
                body = r.text
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Manifest too large")
                return body
            try:
                text = await run_in_threadpool(fetch_text)
            except Exception as exc:
                _cctv_frame_set_status(cam_id, db=d, status="error", kind="stream", source_kind="registered_stream", message=str(exc)[:220], source_url=target)
                raise HTTPException(status_code=502, detail=f"Manifest fetch failed: {exc}")
            rewritten = _rewrite_hls_manifest(text, target, cam_id, registered, allow_hosts)
            return Response(content=rewritten, media_type="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store", "X-Registered-Stream": "1"})
        return await run_in_threadpool(_cctv_proxy_stream_resource, target, registered, request, allow_hosts)

    @app.get("/api/cctv/stream/media")
    async def cctv_stream_media(request: Request, id: str = Query(..., min_length=1, max_length=500)):
        d = db(request)
        info = await run_in_threadpool(_find_registered_cctv_stream, d, id)
        if not info or not info.get("stream_url"):
            raise HTTPException(status_code=404, detail="Registered stream not found")
        target = str(info["stream_url"])
        cam_id = str(info.get("composite_id") or id)
        allow_hosts = await run_in_threadpool(_cctv_stream_allow_hosts, d)
        try:
            resp = await run_in_threadpool(_cctv_proxy_stream_resource, target, target, request, allow_hosts)
            _cctv_frame_set_status(cam_id, db=d, status="ok", kind="stream", source_kind="registered_stream", message="", source_url=target)
            return resp
        except HTTPException as exc:
            _cctv_frame_set_status(cam_id, db=d, status="error", kind="stream", source_kind="registered_stream", message=str(exc.detail)[:220], source_url=target)
            raise
        except Exception as exc:
            _cctv_frame_set_status(cam_id, db=d, status="error", kind="stream", source_kind="registered_stream", message=str(exc)[:220], source_url=target)
            raise HTTPException(status_code=502, detail=f"Stream fetch failed: {exc}")

    # ---------------------------------------------------------------- intel
    @app.get("/api/intel/overview")
    def intel_overview(request: Request):
        d = db(request)
        s = d.all_settings()
        lat = float(s["lat"]) if s.get("lat") is not None else None
        lon = float(s["lon"]) if s.get("lon") is not None else None
        try:
            lat = float(lat); lon = float(lon)
        except (TypeError, ValueError):
            lat = None; lon = None
        try:
            out = intel.overview(d, lat, lon, float(s.get("elevation_m") or 0.0))
        except Exception as exc:
            out = {"error": str(exc)[:400], "feeds": feeds.list_feeds(d)}
        out["online"] = tilecache.is_online()
        out["sync"] = autosync.engine.snapshot(d)
        return out

    @app.post("/api/intel/refresh")
    async def intel_refresh(request: Request):
        result = await run_in_threadpool(feeds.refresh, db(request), None)
        db(request).audit("FEED_REFRESH", json.dumps(result)[:2000])
        return {"results": result}

    @app.get("/api/intel/aircraft")
    def intel_aircraft(request: Request):
        return {"items": intel.live_aircraft(db(request))}

    @app.get("/api/intel/satellites")
    def intel_satellites(request: Request):
        d = db(request)
        s = d.all_settings()
        try:
            lat = float(s.get("lat") or 0); lon = float(s.get("lon") or 0)
        except Exception:
            lat, lon = 0.0, 0.0
        return {"items": intel.live_satellites(d, lat, lon)}

    @app.get("/api/intel/satellite-passes")
    def intel_satellite_passes(request: Request,
                               hours: int = Query(12, ge=1, le=48),
                               min_elevation: float = Query(10.0, ge=0.0, le=90.0),
                               limit: int = Query(40, ge=1, le=200),
                               q: str = ""):
        d = db(request)
        s = d.all_settings()
        try:
            lat = float(s.get("lat")); lon = float(s.get("lon"))
        except (TypeError, ValueError):
            lat = lon = None
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(400, "operator position is missing or invalid; set latitude/longitude in settings")
        passes = intel.next_satellite_passes(
            d, lat, lon, hours=hours, min_elevation_deg=min_elevation,
            limit=limit, query=q,
        )
        return {"items": passes, "count": len(passes),
                "observer": {"lat": lat, "lon": lon},
                "hours": hours, "min_elevation_deg": min_elevation}

    @app.get("/api/intel/nasa")
    def intel_nasa(request: Request):
        """NASA Near-Earth Object tracking - live from NASA API"""
        from . import nasa
        summary = nasa.get_neo_summary()
        objects = nasa.fetch_neo_objects()
        return {"summary": summary, "objects": objects[:10]}

    @app.get("/api/intel/news")
    def intel_news(request: Request):
        """Real-time conflict and disaster news from NewsAPI"""
        from . import newsapi
        conflict = newsapi.fetch_conflict_news()
        disaster = newsapi.fetch_disaster_news()
        summary = newsapi.get_news_summary()
        return {
            "summary": summary,
            "conflict": conflict[:10],
            "disaster": disaster[:10],
        }

    @app.get("/api/intel/disease")
    def intel_disease(request: Request):
        """Disease outbreak tracking - COVID-19 and influenza"""
        from . import who_outbreaks
        covid = who_outbreaks.fetch_covid_stats()
        flu = who_outbreaks.fetch_flu_stats()
        summary = who_outbreaks.get_disease_summary()
        return {
            "summary": summary,
            "covid": covid,
            "flu": flu,
        }

    @app.get("/api/cbrn/status")
    def cbrn_status(request: Request):
        """CBRN common operating picture: space weather, public radiation
        reports, volcanic activity, smoke/dust hazards and local dosimetry."""
        d = db(request)
        s = d.all_settings()
        try:
            lat = float(s.get("lat") or 0); lon = float(s.get("lon") or 0)
        except Exception:
            lat, lon = 0.0, 0.0
        out = intel.cbrn_status(d, lat, lon)
        out["sync"] = autosync.engine.snapshot(d)
        return out

    @app.get("/api/sync/status")
    def sync_status(request: Request):
        return autosync.engine.snapshot(db(request))

    @app.post("/api/sync/now")
    def sync_now(request: Request):
        autosync.engine.run_now()
        return {"started": True, "sync": autosync.engine.snapshot(db(request))}

    @app.get("/api/map/intel")
    def map_intel(request: Request,
                  bbox: str = Query("", max_length=120),
                  limit_cctv: int = Query(8000, ge=0, le=20000),
                  limit_cables: int = Query(5000, ge=0, le=20000),
                  limit_maritime: int = Query(400, ge=0, le=5000)):
        d = db(request)
        s = d.all_settings()
        try:
            lat = float(s.get("lat") or 0); lon = float(s.get("lon") or 0)
        except Exception:
            lat, lon = 0.0, 0.0
        bbox_vals = None
        if bbox:
            try:
                parts = [float(x) for x in bbox.split(",")]
                if len(parts) == 4:
                    minlon, minlat, maxlon, maxlat = parts
                    if minlon <= maxlon and minlat <= maxlat:
                        bbox_vals = (minlon, minlat, maxlon, maxlat)
            except Exception:
                bbox_vals = None
        def in_bbox(latv, lonv):
            if not bbox_vals: return True
            try:
                la = float(latv); lo = float(lonv)
            except Exception:
                return False
            return (bbox_vals[0] <= lo <= bbox_vals[2]) and (bbox_vals[1] <= la <= bbox_vals[3])
        events = intel.map_events(d)
        # Keep every hazard point but cap the two conflict sets separately so
        # monthly verified events are not buried by the live media stream.
        hazards = [e for e in events if e.get("kind") != "conflict"
                   and e.get("feed_id") not in ("gdelt_conflict", "ucdp_candidate")]
        live_conflict = [e for e in events if e.get("feed_id") == "gdelt_conflict"][:400]
        verified_conflict = [e for e in events if e.get("feed_id") == "ucdp_candidate"][:400]
        events = hazards + live_conflict + verified_conflict
        events.sort(key=lambda e: intel._sort_time(e.get("time")), reverse=True)
        features = []
        for ev in events:
            if ev.get("lat") is None or ev.get("lon") is None:
                continue
            if not in_bbox(ev["lat"], ev["lon"]):
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [float(ev["lon"]), float(ev["lat"])]},
                "properties": {
                    "source": ev.get("source") or "?", "kind": ev.get("kind") or "intel",
                    "severity": ev.get("severity") or "INFO", "title": ev.get("title") or "Event",
                    "time": ev.get("time") or "", "age_hours": ev.get("age_hours"),
                    "mag": ev.get("mag"), "aqi": ev.get("aqi"),
                    "feed_id": ev.get("feed_id") or "", "place": ev.get("place") or "",
                    "depth_km": ev.get("depth_km"), "precision": ev.get("precision") or "",
                    "verified": bool(ev.get("verified")), "fatalities": ev.get("fatalities"),
                    "country": ev.get("country") or "", "event_type": ev.get("event_type") or "",
                    "event_code": ev.get("event_code") or "", "mentions": ev.get("mentions"),
                    "url": ev.get("url") or "", "summary": ev.get("summary") or "",
                },
            })
        maritime = [m for m in intel.maritime_positions(d, limit=max(0, limit_maritime)) if in_bbox(m.get("lat"), m.get("lon"))]
        cables = [c for c in intel.cable_features(d, limit=max(0, limit_cables)) if in_bbox(c.get("lat"), c.get("lon"))]
        cctv = [c for c in intel.cctv_features(d, limit=max(0, limit_cctv)) if in_bbox(c.get("lat"), c.get("lon"))]
        return {
            "type": "FeatureCollection",
            "generated_at": utcnow(),
            "summary": {**intel.map_summary(events), "maritime": len(maritime), "cables": len(cables), "cctv": len(cctv)},
            "features": features,
            "aircraft": intel.live_aircraft(d, limit=60),
            "satellites": intel.live_satellites(d, lat, lon, limit=30),
            "maritime": maritime,
            "cables": cables,
            "cctv": cctv,
            "bbox_applied": bool(bbox_vals),
        }

    # -------------------------------------------------------------- library
    @app.get("/api/library/stats")
    def library_stats(request: Request):
        return library.stats(db(request))

    @app.post("/api/library/index")
    def library_index(request: Request, payload: dict = Body(default={})):
        state = library.start_index(deep=bool(payload.get("deep", True)),
                                    force=bool(payload.get("force", False)))
        db(request).audit("LIBRARY_INDEX", json.dumps(payload))
        return state

    @app.get("/api/library/search")
    def library_search(request: Request, q: str = "", category: str = "",
                       limit: int = Query(40, ge=1, le=1000)):
        d = db(request)
        items = library.search(d, q, category or None, limit)
        return {"items": items, "query": q, "count": len(items)}

    @app.get("/api/library/categories")
    def library_categories(request: Request):
        return {"categories": library.categories(db(request))}

    @app.get("/api/library/doc/{doc_id}")
    def library_doc(request: Request, doc_id: int):
        row = db(request).one("SELECT * FROM library_docs WHERE id=?", (doc_id,))
        require(bool(row), "Document not found", 404)
        out = dict(row)
        out.pop("text", None)
        out["text_available"] = bool(row["text"])
        return out

    @app.get("/api/library/doc/{doc_id}/text", response_class=PlainTextResponse)
    def library_doc_text(request: Request, doc_id: int):
        row = db(request).one("SELECT name,text FROM library_docs WHERE id=?", (doc_id,))
        require(bool(row), "Document not found", 404)
        if not row["text"]:
            raise HTTPException(status_code=404, detail="No extracted text for this document")
        return PlainTextResponse(row["text"], media_type="text/plain; charset=utf-8")

    @app.get("/api/library/doc/{doc_id}/file")
    def library_doc_file(request: Request, doc_id: int, download: bool = False):
        row = db(request).one("SELECT * FROM library_docs WHERE id=?", (doc_id,))
        require(bool(row), "Document not found", 404)
        path = library.resolve_doc(row["rel_path"] or row["path"])
        require(path is not None, "Document file is missing or outside the library", 404)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=path.name,
                            content_disposition_type="attachment" if download else "inline")

    # ------------------------------------------------------------------ map
    @app.get("/api/map/vector")
    def map_vector(request: Request):
        path = config.MAP_VECTOR_DIR / "world.geojson"
        require(path.exists(), "Vector basemap not installed", 404)
        return Response(content=path.read_bytes(), media_type="application/geo+json",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/map/layers")
    def map_layers():
        from . import tilecache
        d = Database()
        active_source = d.get_setting("tile_source", tilecache.DEFAULT_SOURCE) or tilecache.DEFAULT_SOURCE
        d.close()
        if active_source not in tilecache.TILE_SOURCES:
            active_source = tilecache.DEFAULT_SOURCE
        vectors = []
        if (config.MAP_VECTOR_DIR / "world.geojson").exists():
            vectors.append({"id": "world", "name": "World vector", "url": "/api/map/vector"})
        layers = []
        for dirname, label, max_zoom, attr in (
            ("overview", "Bundled world overview", 5, "USGS/Esri basemap, bundled"),
            ("us_tiles", "Bundled US overview (legacy)", 6, "Bundled overview tiles"),
        ):
            bundled = config.MAP_TILE_DIR / dirname
            if bundled.exists() and any(bundled.rglob("*.*")):
                layers.append({
                    "id": dirname,
                    "name": label,
                    "url": f"/api/map/tiles/{dirname}/{{z}}/{{x}}/{{y}}.png",
                    "maxZoom": max_zoom,
                    "attribution": attr,
                })
        stats = {s["source"]: s for s in tilecache.cache_stats()["sources"]}
        for src in tilecache.sources_info():
            s = stats.get(src["id"], {})
            if not s.get("tile_count"):
                continue
            layers.append({
                "id": src["id"],
                "name": f"{src['name']} (cached)",
                "url": f"/api/map/cache/tiles/{src['id']}/{{z}}/{{x}}/{{y}}.png",
                "maxZoom": src["max_zoom"],
                "attribution": src["attribution"],
                "tileCount": s.get("tile_count", 0),
            })
        return {
            "raster": layers,
            "vector": vectors,
            "activeSource": active_source,
            "cache": {
                "tile_count": sum(s.get("tile_count", 0) for s in stats.values()),
                "total_mb": round(sum(s.get("total_bytes", 0) for s in stats.values()) / (1024 * 1024), 2),
            },
        }

    def _mbtiles_tile(z: int, x: int, y: int) -> bytes | None:
        path = config.DB_PATH.parent / "does-not-exist"
        try:
            # Settings lookups are cheap and the path can change at runtime.
            d = Database()
            raw = d.get_setting("mbtiles_path", "")
            d.close()
            if not raw:
                return None
            path = Path(raw).expanduser()
            if not path.exists():
                return None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                row = conn.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, (1 << z) - 1 - y),
                ).fetchone()
                return bytes(row[0]) if row else None
            finally:
                conn.close()
        except Exception:
            return None

    def _tile_response(data: bytes, source: str = ""):
        ctype = "image/png"
        if data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
        headers = {"Cache-Control": "public, max-age=604800"}
        if source:
            headers["X-Tile-Source"] = source
        return Response(content=data, media_type=ctype, headers=headers)

    @app.get("/api/map/tiles/{layer}/{z}/{x}/{y}.png")
    def map_tile_layer(layer: str, z: int, x: int, y: int):
        require(0 <= z <= 22 and x >= 0 and y >= 0, "Invalid tile coordinates", 400)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", layer)
        for ext in ("jpg", "png", "webp"):
            path = config.MAP_TILE_DIR / safe / str(z) / str(x) / f"{y}.{ext}"
            if path.exists() and path.is_file():
                return _tile_response(path.read_bytes(), safe)
        data = _mbtiles_tile(z, x, y)
        if data:
            return _tile_response(data, "mbtiles")
        raise HTTPException(status_code=404, detail="Tile not found")

    _tile_source_cache = {"at": 0.0, "value": None}

    def _active_tile_source() -> str:
        """Resolve the configured basemap provider, cached for 30s (tile hot path)."""
        from . import tilecache
        now = time.monotonic()
        if _tile_source_cache["value"] and now - _tile_source_cache["at"] < 30:
            return _tile_source_cache["value"]
        src = tilecache.DEFAULT_SOURCE
        try:
            d = Database()
            configured = d.get_setting("tile_source", tilecache.DEFAULT_SOURCE)
            d.close()
            if configured in tilecache.TILE_SOURCES:
                src = configured
        except Exception:
            pass
        _tile_source_cache.update({"at": now, "value": src})
        return src

    @app.get("/api/map/tiles/{z}/{x}/{y}.png")
    def map_tile(z: int, x: int, y: int):
        """Basemap tile: bundled overview, MBTiles, then the offline cache."""
        from . import tilecache
        require(0 <= z <= 22 and x >= 0 and y >= 0, "Invalid tile coordinates", 400)
        for dirname in ("overview", "us_tiles"):
            for ext in ("jpg", "png", "webp"):
                path = config.MAP_TILE_DIR / dirname / str(z) / str(x) / f"{y}.{ext}"
                if path.exists() and path.stat().st_size > 0:
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    return _tile_response(data, dirname)
        data = _mbtiles_tile(z, x, y)
        if data:
            return _tile_response(data, "mbtiles")
        hit = tilecache.find_cached(z, x, y, _active_tile_source())
        if hit:
            src, blob = hit
            return _tile_response(blob, src)
        # Nothing cached: fetch only if the operator is online, never block the map.
        if tilecache.is_online():
            src = _active_tile_source()
            try:
                blob = tilecache.fetch_tile(z, x, y, src)
            except tilecache.TileBlocked:
                blob = None
            if blob:
                return _tile_response(blob, src)
        raise HTTPException(status_code=404, detail="Tile not cached")

    # ---------------------------------------------------------------- tile cache
    @app.get("/api/map/cache/tiles/{source}/{z}/{x}/{y}.png")
    def cached_tile(source: str, z: int, x: int, y: int):
        """Serve one cached tile; fetch only when online and not cached."""
        from . import tilecache
        require(0 <= z <= 22 and x >= 0 and y >= 0, "Invalid tile coordinates", 400)
        require(source in tilecache.TILE_SOURCES, "Unknown tile source", 400)
        data = tilecache.get_or_fetch(z, x, y, source)
        if data:
            return _tile_response(data, source)
        raise HTTPException(status_code=404, detail="Tile not available")

    @app.get("/api/map/cache/status")
    def cache_status(source: str | None = None):
        """Report cache size and coverage (per source and total)."""
        from . import tilecache
        if source and source not in tilecache.TILE_SOURCES:
            raise HTTPException(status_code=400, detail="Unknown tile source")
        return tilecache.cache_stats(source)

    @app.get("/api/map/cache/sources")
    def cache_sources():
        from . import tilecache
        return {"sources": tilecache.sources_info(), "default": tilecache.DEFAULT_SOURCE,
                "online": tilecache.is_online()}

    @app.post("/api/map/cache/estimate")
    def cache_estimate(body: dict = Body(default={})):
        from . import tilecache
        try:
            lat_min = float(body["lat_min"]); lat_max = float(body["lat_max"])
            lon_min = float(body["lon_min"]); lon_max = float(body["lon_max"])
            zoom_min = int(body.get("zoom_min", 10)); zoom_max = int(body.get("zoom_max", 15))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid bounding box or zoom range")
        source = body.get("source", tilecache.DEFAULT_SOURCE)
        require(source in tilecache.TILE_SOURCES, "Unknown tile source", 400)
        require(0 <= zoom_min <= zoom_max <= 19, "Invalid zoom range", 400)
        return tilecache.estimate_tiles(lat_min, lon_min, lat_max, lon_max,
                                        zoom_min, zoom_max, source)

    @app.post("/api/map/cache/download")
    def download_tiles(body: dict = Body(default={})):
        """Start a background tile download. Poll /api/map/cache/job/{id}."""
        from . import tilecache
        try:
            lat_min = float(body["lat_min"]); lat_max = float(body["lat_max"])
            lon_min = float(body["lon_min"]); lon_max = float(body["lon_max"])
            zoom_min = int(body.get("zoom_min", 10)); zoom_max = int(body.get("zoom_max", 15))
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid bounding box or zoom range")
        source = body.get("source", tilecache.DEFAULT_SOURCE)
        require(source in tilecache.TILE_SOURCES, "Unknown tile source", 400)
        require(-90 <= lat_min <= 90 and -90 <= lat_max <= 90, "Invalid latitude", 400)
        require(-180 <= lon_min <= 180 and -180 <= lon_max <= 180, "Invalid longitude", 400)
        require(lat_min < lat_max and lon_min < lon_max, "Bounding box is inverted", 400)
        require(0 <= zoom_min <= zoom_max <= tilecache.TILE_SOURCES[source]["max_zoom"],
                "Invalid zoom range for this source", 400)
        # Guard against accidental continent-scale jobs.
        est = tilecache.estimate_tiles(lat_min, lon_min, lat_max, lon_max,
                                       zoom_min, zoom_max, source)
        require(est["total"] <= 40000, f"Area too large ({est['total']} tiles). Narrow the box or lower max zoom.", 400)
        job = tilecache.start_download(lat_min, lon_min, lat_max, lon_max,
                                       zoom_min, zoom_max, source)
        job["estimate"] = est
        return job

    @app.get("/api/map/cache/job/{job_id}")
    def cache_job(job_id: str):
        from . import tilecache
        job = tilecache.job_status(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Unknown download job")
        return job

    @app.post("/api/map/cache/clear")
    def cache_clear(body: dict = Body(default={})):
        from . import tilecache
        source = body.get("source") or None
        if source and source not in tilecache.TILE_SOURCES:
            raise HTTPException(status_code=400, detail="Unknown tile source")
        return tilecache.clear_cache(source)

    # -------------------------------------------------------------- DEM / terrain (offline raster-dem tiles)
    def _dem_mbtiles_tile(d: Database, z: int, x: int, y: int) -> bytes | None:
        raw = d.get_setting("dem_mbtiles_path", "") or ""
        path = Path(str(raw)).expanduser()
        if not raw or not path.is_file():
            return None
        try:
            import sqlite3 as _sqlite3
            uri = f"file:{path}?mode=ro"
            with _sqlite3.connect(uri, uri=True, timeout=2) as conn:
                tile_row = (1 << z) - 1 - y
                row = conn.execute(
                    "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                    (z, x, tile_row),
                ).fetchone()
            return bytes(row[0]) if row else None
        except Exception:
            return None

    @app.get("/api/map/dem/status")
    def dem_status(request: Request):
        d = db(request)
        s = d.all_settings()
        dem_path = Path(str(s.get("dem_mbtiles_path") or "")).expanduser()
        dem_dir = config.MAP_TILE_DIR / "dem"
        has_mbtiles = bool(str(s.get("dem_mbtiles_path") or "").strip()) and dem_path.is_file()
        has_dir = dem_dir.is_dir() and any(dem_dir.rglob("*.png")) or (dem_dir.is_dir() and any(dem_dir.rglob("*.webp")))
        available = bool(s.get("dem_enabled", True)) and (has_mbtiles or has_dir)
        return {
            "available": available,
            "enabled": bool(s.get("dem_enabled", True)),
            "encoding": str(s.get("dem_encoding") or "terrarium"),
            "mbtiles": has_mbtiles,
            "directory": has_dir,
            "dem_mbtiles_path": str(s.get("dem_mbtiles_path") or ""),
            "tile_url": "/api/map/dem/tiles/{z}/{x}/{y}.png" if available else None,
            "note": "Provide local raster-dem tiles (Terrarium/Mapbox PNG) via dem_mbtiles_path or maps/tiles/dem/{z}/{x}/{y}.png. No elevation data is invented.",
        }

    @app.get("/api/map/dem/tiles/{z}/{x}/{y}.png")
    def dem_tile(z: int, x: int, y: int, request: Request):
        require(0 <= z <= 22 and x >= 0 and y >= 0, "Invalid tile coordinates")
        d = db(request)
        s = d.all_settings()
        if not bool(s.get("dem_enabled", True)):
            raise HTTPException(status_code=404, detail="DEM disabled")
        # Filesystem DEM cache
        for ext in ("png", "webp", "jpg"):
            path = config.MAP_TILE_DIR / "dem" / str(z) / str(x) / f"{y}.{ext}"
            if path.is_file():
                try:
                    data = path.read_bytes()
                    return _tile_response(data, "dem")
                except Exception:
                    pass
        data = _dem_mbtiles_tile(d, z, x, y)
        if data:
            return _tile_response(data, "dem-mbtiles")
        raise HTTPException(status_code=404, detail="DEM tile not cached")

    # -------------------------------------------------------------- map overlays (cacheable wind/rain/cloud)
    def _overlay_row(d: Database, kind: str) -> dict | None:
        row = d.one("SELECT * FROM map_overlay_cache WHERE kind=?", (kind,))
        if not row:
            return None
        payload = None
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = None
        meta = {}
        if row["meta"]:
            try:
                meta = json.loads(row["meta"])
            except Exception:
                meta = {}
        return {
            "kind": row["kind"], "name": row["name"], "fetched_at": row["fetched_at"],
            "status": row["status"], "error": row["error"], "meta": meta, "data": payload,
        }

    @app.get("/api/map/overlays/status")
    def overlays_status(request: Request):
        d = db(request)
        kinds = ["wind", "rain", "cloud"]
        rows = []
        for k in kinds:
            r = _overlay_row(d, k)
            if r:
                r.pop("data", None)
                r["has_data"] = bool(d.one("SELECT 1 FROM map_overlay_cache WHERE kind=? AND payload IS NOT NULL AND payload!=''", (k,)))
                rows.append(r)
            else:
                rows.append({"kind": k, "name": k.title(), "fetched_at": None, "status": "NEVER", "error": None, "meta": {}, "has_data": False})
        return {"overlays": rows, "generated_at": utcnow()}

    @app.get("/api/map/overlays/{kind}")
    def overlay_get(kind: str, request: Request):
        require(kind in ("wind", "rain", "cloud"), "Unknown overlay kind")
        d = db(request)
        row = _overlay_row(d, kind)
        if not row or not row.get("data"):
            return {"kind": kind, "fetched_at": None, "status": "NEVER", "meta": {}, "data": None,
                    "note": "Overlay not cached. Use refresh to fetch/cache data when online."}
        return {"kind": kind, "fetched_at": row.get("fetched_at"), "status": row.get("status"), "meta": row.get("meta") or {}, "data": row["data"]}

    @app.post("/api/map/overlays/refresh")
    def overlay_refresh(body: dict = Body(default={}), request: Request = None):
        # Operator-triggered, online-only fetch around current/selected position. Cached in DB for offline reuse.
        kind = str((body or {}).get("kind") or "all")
        kinds = ["wind", "rain", "cloud"] if kind == "all" else [kind]
        for k in kinds:
            require(k in ("wind", "rain", "cloud"), f"Unknown overlay kind: {k}")
        d = db(request)
        s = d.all_settings()
        try:
            lat = float((body or {}).get("lat", s.get("lat")))
            lon = float((body or {}).get("lon", s.get("lon")))
        except Exception:
            lat, lon = 0.0, 0.0
        if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-9 and abs(lon) < 1e-9):
            raise HTTPException(status_code=400, detail="Valid lat/lon (or configured operator position) required for overlay refresh")
        span = max(1.0, min(30.0, float((body or {}).get("span_deg") or 8.0)))
        steps = max(3, min(9, int((body or {}).get("steps") or 5)))
        results = []
        try:
            import requests as _requests
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"requests unavailable: {e}")
        lats = [lat + (i - (steps - 1) / 2) * (span / max(1, steps - 1)) for i in range(steps)]
        lons = [lon + (i - (steps - 1) / 2) * (span / max(1, steps - 1)) for i in range(steps)]
        lat_s = ",".join(f"{v:.4f}" for v in lats)
        lon_s = ",".join(f"{v:.4f}" for v in lons)
        for k in kinds:
            try:
                if k == "wind":
                    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lat_s + "&longitude=" + lon_s +
                           "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m&wind_speed_unit=kmh&timezone=UTC")
                elif k == "rain":
                    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lat_s + "&longitude=" + lon_s +
                           "&current=precipitation,rain,showers,cloud_cover&timezone=UTC")
                else:
                    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + lat_s + "&longitude=" + lon_s +
                           "&current=cloud_cover,relative_humidity_2m&timezone=UTC")
                resp = _requests.get(url, headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/json"}, timeout=20)
                resp.raise_for_status()
                payload = resp.json()
                # Open-Meteo returns arrays for multi-point current? Depending on API shape; normalize to per-point list.
                def _arr(v, n):
                    if isinstance(v, list):
                        return v + [None] * max(0, n - len(v))
                    return [v] * n
                cur = payload.get("current") or {}
                # For multi-lat/lon, API may return arrays at top-level in some forms; handle common case.
                # Fallback: if scalar, replicate; if we got a single point, sample center only.
                pts = []
                # Try multi-point: build grid by pairing index-wise if arrays align with lat_s/lon_s count
                lat_list = payload.get("latitude")
                lon_list = payload.get("longitude")
                cur_list = payload.get("current")
                if isinstance(lat_list, list) and isinstance(lon_list, list) and isinstance(cur_list, list):
                    for i, (la, lo, cc) in enumerate(zip(lat_list, lon_list, cur_list)):
                        if not isinstance(cc, dict):
                            continue
                        pts.append({"lat": float(la), "lon": float(lo), **{kk: cc.get(kk) for kk in cc.keys()}})
                elif isinstance(lat_list, list) and isinstance(lon_list, list) and isinstance(cur, dict):
                    # current may be dict of arrays
                    keys = list(cur.keys())
                    for i, (la, lo) in enumerate(zip(lat_list, lon_list)):
                        p = {"lat": float(la), "lon": float(lo)}
                        for kk in keys:
                            vv = cur.get(kk)
                            p[kk] = vv[i] if isinstance(vv, list) and i < len(vv) else vv
                        pts.append(p)
                else:
                    # single point fallback
                    la1 = payload.get("latitude", lat)
                    lo1 = payload.get("longitude", lon)
                    if isinstance(la1, list): la1 = la1[0] if la1 else lat
                    if isinstance(lo1, list): lo1 = lo1[0] if lo1 else lon
                    pts.append({"lat": float(la1), "lon": float(lo1), **(cur if isinstance(cur, dict) else {})})
                features = []
                for p in pts:
                    pla, plo = float(p.get("lat", 0)), float(p.get("lon", 0))
                    props = {"kind": k, "source": "Open-Meteo", "time": payload.get("current", {}).get("time") if isinstance(payload.get("current"), dict) else utcnow()}
                    if k == "wind":
                        props.update({"wind_kmh": p.get("wind_speed_10m"), "wind_dir": p.get("wind_direction_10m"), "gust_kmh": p.get("wind_gusts_10m")})
                    elif k == "rain":
                        props.update({"precip_mm": p.get("precipitation"), "rain_mm": p.get("rain"), "showers_mm": p.get("showers"), "cloud_pct": p.get("cloud_cover")})
                    else:
                        props.update({"cloud_pct": p.get("cloud_cover"), "humidity_pct": p.get("relative_humidity_2m")})
                    features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [plo, pla]}, "properties": props})
                data = {"type": "FeatureCollection", "features": features, "meta": {"center": [lat, lon], "span_deg": span, "steps": steps, "source": "Open-Meteo", "kind": k}}
                d.execute("INSERT INTO map_overlay_cache(kind,name,fetched_at,status,payload,error,meta) VALUES(?,?,?,?,?,?,?) "
                          "ON CONFLICT(kind) DO UPDATE SET name=excluded.name,fetched_at=excluded.fetched_at,status='OK',payload=excluded.payload,error=NULL,meta=excluded.meta",
                          (k, f"{k.title()} overlay (cached)", utcnow(), "OK", json.dumps(data), json.dumps(data.get("meta") or {})))
                results.append({"kind": k, "status": "OK", "count": len(features)})
            except Exception as exc:
                d.execute("INSERT INTO map_overlay_cache(kind,name,fetched_at,status,payload,error,meta) VALUES(?,?,?,?,NULL,?,?) "
                          "ON CONFLICT(kind) DO UPDATE SET fetched_at=excluded.fetched_at,status='ERROR',error=excluded.error",
                          (k, f"{k.title()} overlay", utcnow(), "ERROR", str(exc)[:400], json.dumps({"center": [lat, lon]})))
                results.append({"kind": k, "status": "ERROR", "error": str(exc)[:300]})
        d.audit("MAP_OVERLAY_REFRESH", json.dumps({"kinds": kinds, "lat": lat, "lon": lon}), request.client.host if request and request.client else "")
        return {"ok": True, "results": results, "generated_at": utcnow()}


    @app.get("/api/map/coords")
    def map_coords(request: Request, lat: float, lon: float):
        require(-90 <= lat <= 90 and -180 <= lon <= 180, "Invalid coordinates")
        return geo.format_coord(lat, lon)

    @app.post("/api/map/gpx/import")
    def gpx_import(request: Request, body: dict = Body(...)):
        text = str(body.get("gpx") or "")
        require(0 < len(text) <= 5_000_000, "GPX body is empty or too large")
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid GPX: {exc}")
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        d = db(request)
        added = {"waypoints": 0, "tracks": 0}
        for wpt in root.iter(f"{ns}wpt"):
            lat = wpt.get("lat"); lon = wpt.get("lon")
            if lat is None or lon is None:
                continue
            name = (wpt.findtext(f"{ns}name") or "Imported waypoint").strip()[:120]
            ele = wpt.findtext(f"{ns}ele")
            desc = (wpt.findtext(f"{ns}desc") or "")[:1000]
            d.insert("waypoints", {"name": name, "lat": float(lat), "lon": float(lon),
                                   "ele": float(ele) if ele else None, "type": "IMPORTED",
                                   "note": desc, "created": utcnow(), "updated": utcnow()})
            added["waypoints"] += 1
        for trk in root.iter(f"{ns}trk"):
            name = (trk.findtext(f"{ns}name") or "Imported track").strip()[:120]
            coords = []
            for seg in trk.iter(f"{ns}trkseg"):
                for pt in seg.iter(f"{ns}trkpt"):
                    try:
                        coords.append([float(pt.get("lon")), float(pt.get("lat"))])
                    except (TypeError, ValueError):
                        continue
            if len(coords) >= 2:
                d.insert("tracks", {"name": name, "geojson": json.dumps(
                    {"type": "LineString", "coordinates": coords}), "color": "#48d7ff",
                    "created": utcnow()})
                added["tracks"] += 1
        d.audit("GPX_IMPORT", json.dumps(added))
        return {"ok": True, **added}

    @app.get("/api/map/gpx/export")
    def gpx_export(request: Request):
        d = db(request)
        esc = lambda s: (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                         .replace(">", "&gt;").replace('"', "&quot;"))
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<gpx version="1.1" creator="DarkSec NIGHTWATCH" xmlns="http://www.topografix.com/GPX/1/1">']
        for w in d.query("SELECT * FROM waypoints ORDER BY name"):
            out.append(f'<wpt lat="{w["lat"]}" lon="{w["lon"]}">'
                       + (f"<ele>{w['ele']}</ele>" if w["ele"] is not None else "")
                       + f"<name>{esc(w['name'])}</name><desc>{esc(w['note'])}</desc></wpt>")
        for t in d.query("SELECT * FROM tracks ORDER BY created"):
            try:
                gj = json.loads(t["geojson"])
                coords = gj.get("coordinates") or []
            except Exception:
                coords = []
            if not coords:
                continue
            out.append(f"<trk><name>{esc(t['name'])}</name><trkseg>")
            for c in coords:
                if isinstance(c, (list, tuple)) and len(c) >= 2:
                    out.append(f'<trkpt lat="{c[1]}" lon="{c[0]}"></trkpt>')
            out.append("</trkseg></trk>")
        out.append("</gpx>")
        return Response("\n".join(out), media_type="application/gpx+xml",
                        headers={"Content-Disposition": 'attachment; filename="darksec-waypoints.gpx"'})

    # ------------------------------------------------------------------ nav
    @app.get("/api/nav/coords")
    def nav_coords(lat: float, lon: float):
        require(-90 <= lat <= 90 and -180 <= lon <= 180, "Invalid coordinates")
        return geo.format_coord(lat, lon)

    @app.post("/api/nav/geocode")
    def nav_geocode(request: Request, payload: dict = Body(...)):
        d = db(request)
        if bool(d.all_settings().get("opsec_mode")):
            raise HTTPException(status_code=423, detail="Address lookup is disabled in OPSEC mode.")
        if not tilecache.is_online():
            raise HTTPException(status_code=503, detail="Offline. Enter coordinates manually or retry when connected.")
        try:
            result = geocoding.search(payload.get("address", ""))
        except geocoding.GeocodingError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        d.audit("ADDRESS_LOOKUP", "One-shot OpenStreetMap geocoding lookup; query not persisted",
                request.client.host if request.client else "")
        return result

    @app.get("/api/phone-gps/interfaces")
    def phone_gps_interfaces(request: Request):
        if bool(db(request).all_settings().get("opsec_mode")):
            raise HTTPException(status_code=423, detail="Phone GPS pairing is disabled in OPSEC mode.")
        return {"interfaces": phonegps.local_ipv4_interfaces(), "transport": "temporary local HTTPS"}

    @app.post("/api/phone-gps/pair")
    def phone_gps_pair(request: Request, payload: dict = Body(...)):
        if bool(db(request).all_settings().get("opsec_mode")):
            raise HTTPException(status_code=423, detail="Phone GPS pairing is disabled in OPSEC mode.")
        try:
            return phonegps.pairing.start(str(payload.get("ip") or ""))
        except phonegps.PhoneGpsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    @app.get("/api/phone-gps/status")
    def phone_gps_status(request: Request):
        if bool(db(request).all_settings().get("opsec_mode")):
            return {"state": "disabled", "latest_fix": None, "cloud_relay": False}
        return phonegps.pairing.status()

    @app.get("/api/phone-gps/qr.svg")
    def phone_gps_qr(request: Request, session_id: str):
        if bool(db(request).all_settings().get("opsec_mode")):
            raise HTTPException(status_code=423, detail="Phone GPS pairing is disabled in OPSEC mode.")
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage
            payload = phonegps.pairing.qr_payload(session_id)
            qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
            qr.add_data(payload)
            qr.make(fit=True)
            image = qr.make_image(image_factory=SvgPathImage)
            out = io.BytesIO()
            image.save(out)
            return Response(out.getvalue(), media_type="image/svg+xml",
                            headers={"Cache-Control": "no-store, max-age=0"})
        except phonegps.PhoneGpsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Could not render pairing QR ({type(exc).__name__}).") from exc

    @app.post("/api/phone-gps/accept")
    def phone_gps_accept(request: Request):
        d = db(request)
        if bool(d.all_settings().get("opsec_mode")):
            phonegps.pairing.stop()
            raise HTTPException(status_code=423, detail="Phone GPS pairing is disabled in OPSEC mode.")
        try:
            fix = phonegps.pairing.accepted_fix()
        except phonegps.PhoneGpsError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        fix_at = fix.get("timestamp") or utcnow()
        received_at = fix.get("received_at") or utcnow()
        old_settings = d.all_settings()
        position_patch = {
            "lat": float(fix["lat"]),
            "lon": float(fix["lon"]),
            "position_source": "phone",
            "position_updated_at": received_at,
            "position_fix_at": fix_at,
            "position_accuracy_m": float(fix["accuracy_m"]),
            "position_mode": 3,
        }
        with d.transaction() as conn:
            for key, value in position_patch.items():
                conn.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
        try:
            old_sig = (round(float(old_settings.get("lat")), 4), round(float(old_settings.get("lon")), 4))
            new_sig = (round(float(position_patch["lat"]), 4), round(float(position_patch["lon"]), 4))
            if old_sig != new_sig:
                autosync.engine.position_changed()
        except (TypeError, ValueError):
            pass
        d.audit("PHONE_GPS_ACCEPT", f"phone GPS fix accepted; estimated accuracy={float(fix['accuracy_m']):.1f}m; listener kept open")
        return {k: fix.get(k) for k in ("lat", "lon", "accuracy_m", "timestamp", "received_at")} | {"saved": True, "listener_open": True}

    @app.post("/api/phone-gps/stop")
    def phone_gps_stop(request: Request):
        phonegps.pairing.stop()
        db(request).audit("PHONE_GPS_STOP", "phone GPS pairing stopped")
        return {"ok": True, "state": "stopped"}

    @app.post("/api/nav/convert")
    def nav_convert(payload: dict = Body(...)):
        kind = str(payload.get("type") or "").lower()
        value = payload.get("value")
        if kind in ("dd", "decimal"):
            text = str(value).replace(",", " ").split()
            require(len(text) >= 2, "Provide latitude and longitude")
            lat, lon = geo.parse_angle(text[0], True), geo.parse_angle(text[1], False)
        elif kind == "dms":
            text = str(value).replace(",", " ")
            parts = re.findall(r"[+-]?\d+(?:\.\d+)?[^\d+-]*[NSEW]?", text)
            require(len(parts) >= 2, "Provide latitude and longitude")
            lat, lon = geo.parse_angle(parts[0], True), geo.parse_angle(parts[1], False)
        elif kind == "utm":
            m = re.match(r"^\s*(\d{1,2})\s*([NS])?\s+(\d+(?:\.\d+)?)\s*[Ee]?\s+(\d+(?:\.\d+)?)\s*[Nn]?\s*$",
                         str(value))
            require(bool(m), "UTM format: ZONE HEMI EASTING NORTHING")
            u = geo.utm_inverse(int(m.group(1)), m.group(2) or "N", float(m.group(3)), float(m.group(4)))
            lat, lon = u["lat"], u["lon"]
        elif kind == "mgrs":
            u = geo.mgrs_inverse(str(value))
            lat, lon = u["lat"], u["lon"]
        else:
            raise HTTPException(status_code=400, detail="type must be dd, dms, utm or mgrs")
        return geo.format_coord(lat, lon)

    @app.get("/api/nav/celestial")
    def nav_celestial(request: Request, lat: float | None = None, lon: float | None = None,
                      elevation: float | None = None, when: str | None = None):
        s = db(request).all_settings()
        return astro.celestial(float(lat if lat is not None else s["lat"]),
                               float(lon if lon is not None else s["lon"]),
                               float(elevation if elevation is not None else s.get("elevation_m") or 0),
                               when)

    @app.get("/api/nav/declination")
    def nav_declination(lat: float, lon: float, elevation: float = 0, date: str | None = None):
        d = None
        if date:
            try:
                d = datetime.fromisoformat(date).date()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid date")
        return geo.declination(lat, lon, elevation, d)

    @app.get("/api/nav/tide")
    def nav_tide(hours_since_low: float, range_m: float):
        return astro.tide_rule_of_twelfths(hours_since_low, range_m)

    def _live_phone_fix(max_age_seconds: int = 30) -> dict | None:
        """Return a fresh fix from an active phone browser session, if any."""
        try:
            status = phonegps.pairing.status()
            fix = status.get("latest_fix")
            if not fix or float(fix.get("age_seconds") or 0) > max_age_seconds:
                return None
            return fix
        except Exception:
            return None

    @app.get("/api/nav/gps")
    def nav_gps():
        if not shutil.which("gpspipe"):
            fix = _live_phone_fix()
            if fix:
                return {"available": True, "fix": True,
                        "lat": fix["lat"], "lon": fix["lon"],
                        "alt_m": fix.get("alt_m"), "speed_mps": fix.get("speed_mps"),
                        "track": fix.get("heading_deg"), "time": fix.get("timestamp"),
                        "mode": 3, "accuracy_m": fix.get("accuracy_m"),
                        "vertical_accuracy_m": None, "satellites": None,
                        "source": "phone browser (temporary local HTTPS)",
                        "note": "Fresh phone browser fix. Review it and use it as the saved position; it is not yet saved."}
            return {"available": False, "fix": False,
                    "note": "gpspipe is not installed. Open Settings → Position → Phone GPS · Browser to use the phone as a temporary receiver."}
        try:
            proc = subprocess.run(["gpspipe", "-w", "-n", "3"], capture_output=True,
                                  text=True, timeout=5)
            lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            last = {}
            for line in lines:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("class") == "TPV":
                    last = obj
            if not last:
                fix = _live_phone_fix()
                if fix:
                    return {"available": True, "fix": True,
                            "lat": fix["lat"], "lon": fix["lon"],
                            "alt_m": fix.get("alt_m"), "speed_mps": fix.get("speed_mps"),
                            "track": fix.get("heading_deg"), "time": fix.get("timestamp"),
                            "mode": 3, "accuracy_m": fix.get("accuracy_m"),
                            "vertical_accuracy_m": None, "satellites": None,
                            "source": "phone browser (temporary local HTTPS)",
                            "note": "No gpsd fix; using a fresh phone browser fix instead. It is not yet saved."}
                return {"available": True, "fix": False,
                        "note": "No GPS fix; start gpsd and check the receiver, or use Settings → Position → Phone GPS · Browser."}
            mode = int(last.get("mode") or 0)
            has_coords = last.get("lat") is not None and last.get("lon") is not None
            try:
                gps_lat, gps_lon = float(last.get("lat")), float(last.get("lon"))
                has_coords = has_coords and -90 <= gps_lat <= 90 and -180 <= gps_lon <= 180
            except (TypeError, ValueError):
                has_coords = False
                gps_lat = gps_lon = None
            fix = mode >= 2 and has_coords
            return {"available": True, "fix": fix,
                    "lat": gps_lat if fix else None, "lon": gps_lon if fix else None,
                    "alt_m": last.get("altMSL"),
                    "speed_mps": last.get("speed"), "track": last.get("track"),
                    "time": last.get("time"), "mode": mode,
                    "accuracy_m": last.get("eph"), "vertical_accuracy_m": last.get("epv"),
                    "satellites": None, "source": "gpsd/gpspipe"}
        except Exception as exc:
            return {"available": True, "fix": False, "note": str(exc)[:300]}

    @app.post("/api/nav/resection")
    def nav_resection(payload: dict = Body(...)):
        try:
            p1 = (float(payload["lat1"]), float(payload["lon1"]))
            p2 = (float(payload["lat2"]), float(payload["lon2"]))
            b1 = float(payload["bearing1"]); b2 = float(payload["bearing2"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=400, detail="lat1/lon1/bearing1 and lat2/lon2/bearing2 required")
        out = geo.resection(p1, b1, p2, b2)
        return {**out, "format": geo.format_coord(out["lat"], out["lon"]),
                "note": "Two-bearing resection. Small bearing angles multiply error."}

    # ---------------------------------------------------------------- comms
    @app.get("/api/comms/reference")
    def comms_reference():
        return FREQ_DATA

    @app.get("/api/comms/soi")
    def comms_soi(request: Request):
        rows = db(request).query("SELECT * FROM comms_plans ORDER BY created DESC")
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/reference")
    def reference(category: str = ""):
        cards = REFERENCE_DATA["cards"]
        if category:
            cards = [c for c in cards if c["category"].upper() == category.upper()]
        return {"cards": cards, "version": REFERENCE_DATA.get("version")}

    @app.get("/api/checklists")
    def checklists(request: Request):
        state = {f"{r['kit_id']}:{r['item_id']}": int(r["checked"])
                 for r in db(request).query("SELECT * FROM checklist_state")}
        return {"kits": CHECKLIST_DATA["kits"], "state": state, "note": CHECKLIST_DATA.get("note")}

    @app.post("/api/checklists/toggle")
    def checklist_toggle(request: Request, payload: dict = Body(...)):
        kit = str(payload.get("kit_id") or "")[:100]
        item = str(payload.get("item_id") or "")[:100]
        checked = 1 if payload.get("checked") else 0
        require(bool(kit and item), "kit_id and item_id required")
        d = db(request)
        d.execute("INSERT INTO checklist_state(kit_id,item_id,checked,updated) VALUES(?,?,?,?) "
                  "ON CONFLICT(kit_id,item_id) DO UPDATE SET checked=excluded.checked, updated=excluded.updated",
                  (kit, item, checked, utcnow()))
        return {"ok": True, "checked": bool(checked)}

    @app.post("/api/checklists/reset")
    def checklist_reset(request: Request, payload: dict = Body(default={})):
        d = db(request)
        kit = payload.get("kit_id")
        if kit:
            d.execute("DELETE FROM checklist_state WHERE kit_id=?", (kit,))
        else:
            d.execute("DELETE FROM checklist_state")
        return {"ok": True}

    # ------------------------------------------------------------------ SDR
    @app.get("/api/sdr/presets")
    def sdr_presets():
        path = config.PKG_DIR / "data" / "sdr_presets.json"
        if not path.exists():
            return {"presets": [], "note": "sdr_presets.json not found"}
        return _cached_json(path)

    @app.get("/api/sdr/devices")
    def sdr_devices(force: bool = False):
        return hardware(force=force)

    @app.get("/api/sdr/sweep")
    def sdr_sweep_stream(request: Request, start_mhz: float = 88.0, end_mhz: float = 108.0,
                         bin_khz: float = 100.0, lna: int = 32, vga: int = 40, amp: bool = False):
        require(0.1 <= start_mhz < end_mhz <= 6000, "Sweep range must be 0.1-6000 MHz")
        require(10 <= bin_khz <= 5000, "Bin width must be 10-5000 kHz")
        require(0 <= lna <= 40 and 0 <= vga <= 62, "Gain out of range")
        if request.app.state.sweep_active:
            raise HTTPException(status_code=409, detail="A sweep is already running")
        request.app.state.sweep_active = True

        def generate():
            try:
                for frame in sdr.sweep_stream(int(start_mhz * 1e6), int(end_mhz * 1e6),
                                              int(bin_khz * 1e3), lna, vga, amp):
                    yield json.dumps(frame) + "\n"
            finally:
                request.app.state.sweep_active = False

        return StreamingResponse(generate(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/sdr/capture")
    def sdr_capture(request: Request, payload: dict = Body(...)):
        try:
            freq = float(payload.get("freq_hz") or float(payload.get("freq_mhz", 100)) * 1e6)
            job = sdr.start_capture(
                freq_hz=freq,
                seconds=float(payload.get("seconds", 5)),
                rate=float(payload.get("rate", 2_400_000)),
                demod=str(payload.get("demod", "nfm")).lower(),
                lna=int(payload.get("lna", 32)), vga=int(payload.get("vga", 40)),
                amp=bool(payload.get("amp", False)), keep_iq=bool(payload.get("keep_iq", False)),
                name=str(payload.get("name", ""))[:120],
            )
            db(request).audit("SDR_CAPTURE", f"job={job['id']} freq={freq}")
            return job
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/sdr/jobs")
    def sdr_jobs(limit: int = Query(20, ge=1, le=100)):
        return {"items": sdr.list_jobs(limit)}

    @app.get("/api/sdr/jobs/{job_id}")
    def sdr_job(job_id: str):
        job = sdr.get_job(job_id)
        require(bool(job), "Job not found", 404)
        return job

    @app.post("/api/sdr/jobs/{job_id}/stop")
    def sdr_job_stop(job_id: str):
        return sdr.stop_job(job_id)

    def _recording_file(kind: str, rec_id: int, download: bool = False):
        d = Database()
        row = d.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
        d.close()
        require(bool(row), "Recording not found", 404)
        path = row[f"{kind}_path"]
        require(bool(path), f"No {kind} for this recording", 404)
        p = Path(path)
        require(p.exists() and p.is_file(), "File missing", 404)
        try:
            p.resolve().relative_to(Path(config.RECORDING_DIR).resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="File outside recording directory")
        media = {"audio": "audio/wav", "image": "image/png", "iq": "application/octet-stream"}[kind]
        return FileResponse(p, media_type=media, filename=p.name,
                            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/sdr/recordings")
    def sdr_recordings():
        return {"items": sdr.list_recordings()}

    @app.get("/api/sdr/recordings/{rec_id}/{kind}")
    def sdr_recording_file(rec_id: int, kind: str, download: bool = False):
        require(kind in ("audio", "image", "iq"), "kind must be audio, image or iq")
        return _recording_file(kind, rec_id, download)

    @app.delete("/api/sdr/recordings/{rec_id}")
    def sdr_recording_delete(request: Request, rec_id: int):
        out = sdr.delete_recording(rec_id)
        db(request).audit("SDR_DELETE", f"recording={rec_id}")
        return out

    @app.get("/api/sdr/jobs/{job_id}/{kind}")
    def sdr_job_file(job_id: str, kind: str, download: bool = False):
        require(kind in ("audio", "image"), "kind must be audio or image")
        d = Database()
        row = d.one("SELECT * FROM sdr_jobs WHERE id=?", (job_id,))
        d.close()
        require(bool(row), "Job not found", 404)
        path = row[f"{kind}_path"]
        require(bool(path) and Path(path).exists(), "File not available", 404)
        p = Path(path)
        try:
            p.resolve().relative_to(Path(config.RECORDING_DIR).resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="File outside recording directory")
        media = "audio/wav" if kind == "audio" else "image/png"
        return FileResponse(p, media_type=media, filename=p.name,
                            content_disposition_type="attachment" if download else "inline")

    @app.get("/api/sdr/adsb")
    def sdr_adsb():
        return sdr.adsb_snapshot()

    @app.post("/api/sdr/tx")
    def sdr_tx(request: Request, payload: dict = Body(...)):
        d = db(request)
        rec_id = payload.get("recording_id")
        iq_path = payload.get("iq_path")
        freq = payload.get("freq_hz")
        rate = payload.get("rate")
        if rec_id:
            row = d.one("SELECT * FROM sdr_recordings WHERE id=?", (int(rec_id),))
            require(bool(row), "Recording not found", 404)
            require(bool(row["iq_path"]), "That recording has no retained IQ file")
            iq_path = row["iq_path"]; freq = row["freq_hz"]; rate = row["rate"]
        require(bool(iq_path), "recording_id or iq_path required")
        p = Path(str(iq_path)).expanduser().resolve()
        try:
            p.relative_to(Path(config.RECORDING_DIR).resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="IQ file must be inside the recordings directory")
        require(p.exists(), "IQ file not found", 404)
        out = sdr.transmit(str(p), float(freq), float(rate), int(payload.get("vga", 40)),
                           bool(payload.get("amp", False)), str(payload.get("confirm", "")),
                           bool(payload.get("attest", False)))
        d.audit("SDR_TX_REQUEST", f"freq={freq} file={p.name}",
                request.client.host if request.client else "")
        return out

    # -------------------------------------------------------------- medical
    @app.get("/api/medical/patients/{patient_id}/vitals")
    def patient_vitals(request: Request, patient_id: int):
        d = db(request)
        rows = d.query("SELECT * FROM vitals WHERE patient_id=? ORDER BY ts DESC LIMIT 200", (patient_id,))
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/medical/patients/{patient_id}/medications")
    def patient_medications(request: Request, patient_id: int):
        d = db(request)
        rows = d.query("SELECT * FROM medication_log WHERE patient_id=? ORDER BY ts DESC LIMIT 200",
                       (patient_id,))
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/medical/summary")
    def medical_summary(request: Request):
        d = db(request)
        rows = d.query("SELECT * FROM patients")
        triage: dict[str, int] = {}
        for r in rows:
            triage[r["triage"] or "UNASSESSED"] = triage.get(r["triage"] or "UNASSESSED", 0) + 1
        recent = d.query("SELECT * FROM vitals ORDER BY ts DESC LIMIT 10")
        meds = d.query("SELECT * FROM medication_log ORDER BY ts DESC LIMIT 10")
        return {"patients": len(rows), "triage": triage,
                "recent_vitals": [dict(r) for r in recent],
                "recent_medications": [dict(r) for r in meds]}

    # --------------------------------------------------------------- vault
    @app.get("/api/vault/status")
    def vault_status(request: Request):
        return vault(request).status()

    @app.post("/api/vault/setup")
    def vault_setup(request: Request, payload: dict = Body(...)):
        out = vault(request).setup(str(payload.get("passphrase") or ""))
        db(request).audit("VAULT_SETUP_UI", "")
        return out

    @app.post("/api/vault/unlock")
    def vault_unlock(request: Request, payload: dict = Body(...)):
        return vault(request).unlock(str(payload.get("passphrase") or ""))

    @app.post("/api/vault/lock")
    def vault_lock(request: Request):
        return vault(request).lock()

    @app.post("/api/vault/change")
    def vault_change(request: Request, payload: dict = Body(...)):
        return vault(request).change_passphrase(str(payload.get("old") or ""),
                                                str(payload.get("new") or ""))

    @app.get("/api/notes")
    def notes_list(request: Request):
        d = db(request)
        v = vault(request)
        unlocked = v.unlocked()
        out = []
        for row in d.query("SELECT id,ts,updated,cipher,tags FROM notes ORDER BY updated DESC, id DESC"):
            item = {"id": row["id"], "ts": row["ts"], "updated": row["updated"]}
            if unlocked:
                try:
                    payload = json.loads(v.decrypt(row["cipher"]))
                    item.update({"title": payload.get("title", ""), "body": payload.get("body", ""),
                                 "tags": payload.get("tags", "")})
                except Exception:
                    item.update({"title": "[DECRYPT FAILED]", "body": "", "tags": ""})
            else:
                item.update({"title": "[VAULT LOCKED]", "body": "", "tags": ""})
            out.append(item)
        return {"items": out, "vault": v.status()}

    @app.post("/api/notes")
    def notes_create(request: Request, payload: dict = Body(...)):
        v = vault(request)
        require(v.unlocked(), "Vault is locked", 403)
        title = str(payload.get("title") or "UNTITLED")[:300]
        body = str(payload.get("body") or "")[:2_000_000]
        tags = str(payload.get("tags") or "")[:500]
        cipher = v.encrypt(json.dumps({"title": title, "body": body, "tags": tags}))
        new_id = db(request).insert("notes", {"ts": utcnow(), "updated": utcnow(),
                                              "title": "ENCRYPTED", "cipher": cipher, "tags": ""})
        db(request).audit("NOTE_NEW", f"note #{new_id}")
        return {"id": new_id}

    @app.put("/api/notes/{note_id}")
    def notes_update(request: Request, note_id: int, payload: dict = Body(...)):
        d = db(request); v = vault(request)
        require(v.unlocked(), "Vault is locked", 403)
        row = d.one("SELECT * FROM notes WHERE id=?", (note_id,))
        require(bool(row), "Note not found", 404)
        title = str(payload.get("title") or "UNTITLED")[:300]
        body = str(payload.get("body") or "")[:2_000_000]
        tags = str(payload.get("tags") or "")[:500]
        cipher = v.encrypt(json.dumps({"title": title, "body": body, "tags": tags}))
        d.execute("UPDATE notes SET cipher=?, updated=?, tags=? WHERE id=?",
                  (cipher, utcnow(), "", note_id))
        d.audit("NOTE_UPDATE", f"note #{note_id}")
        return {"ok": True}

    @app.delete("/api/notes/{note_id}")
    def notes_delete(request: Request, note_id: int):
        d = db(request)
        d.delete("notes", note_id)
        d.audit("NOTE_DELETE", f"note #{note_id}")
        return {"deleted": note_id}

    @app.post("/api/security/purge")
    def security_purge(request: Request, payload: dict = Body(...)):
        mode = str(payload.get("mode") or "operator")
        require(str(payload.get("confirm") or "").upper() == "PURGE", 'Type PURGE to confirm')
        v = vault(request)
        out = v.purge_everything() if mode == "everything" else v.purge_operator_data()
        db(request).audit("PURGE_UI", f"mode={mode}")
        return out

    # ------------------------------------------------------------ logistics
    @app.get("/api/logistics/low-stock")
    def low_stock(request: Request):
        rows = db(request).query(
            "SELECT * FROM inventory WHERE min_qty > 0 AND qty <= min_qty ORDER BY (qty-min_qty)")
        return {"items": [dict(r) for r in rows]}

    @app.get("/api/logistics/export")
    def logistics_export(request: Request, fmt: str = "json"):
        d = db(request)
        payload: dict[str, Any] = {"app": APP_NAME, "version": __version__, "exported": utcnow()}
        for table in RECORD_TABLES:
            payload[table] = [dict(r) for r in d.query(f"SELECT * FROM {table}")]
        payload["settings"] = {k: v for k, v in d.all_settings().items()
                               if not k.startswith("vault_")}
        payload["checklists"] = [dict(r) for r in d.query("SELECT * FROM checklist_state")]
        if fmt == "csv":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["table", "id", "json"])
            for table in RECORD_TABLES:
                for row in payload[table]:
                    writer.writerow([table, row.get("id"), json.dumps(row, ensure_ascii=False)])
            return Response(out.getvalue(), media_type="text/csv",
                            headers={"Content-Disposition": 'attachment; filename="darksec-export.csv"'})
        raw = json.dumps(payload, indent=2, ensure_ascii=False).encode()
        return Response(raw, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="darksec-export.json"'})

    @app.post("/api/logistics/backup")
    def logistics_backup(request: Request, payload: dict = Body(default={})):
        d = db(request)
        data: dict[str, Any] = {"app": APP_NAME, "version": __version__, "created": utcnow(),
                                "format": "darksec-backup-v1"}
        for table in RECORD_TABLES:
            data[table] = [dict(r) for r in d.query(f"SELECT * FROM {table}")]
        data["settings"] = {k: v for k, v in d.all_settings().items() if not k.startswith("vault_")}
        data["checklists"] = [dict(r) for r in d.query("SELECT * FROM checklist_state")]
        data["notes_present"] = d.one("SELECT count(*) c FROM notes")["c"]
        name = payload.get("name") or f"darksec-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:120]
        if not safe.endswith(".json"):
            safe += ".json"
        path = Path(config.BACKUP_DIR) / safe
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        d.audit("BACKUP", safe)
        return {"ok": True, "file": safe, "bytes": path.stat().st_size,
                "note": "Operator data only. Encrypted notes remain encrypted and need the vault passphrase."}

    @app.get("/api/logistics/backups")
    def logistics_backups():
        out = []
        for p in sorted(Path(config.BACKUP_DIR).glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            out.append({"name": p.name, "bytes": p.stat().st_size,
                        "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()})
        return {"items": out}

    @app.get("/api/logistics/backups/{name}")
    def logistics_backup_file(name: str):
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", name)
        path = Path(config.BACKUP_DIR) / safe
        require(path.exists() and path.is_file(), "Backup not found", 404)
        return FileResponse(path, media_type="application/json", filename=path.name,
                            content_disposition_type="attachment")

    @app.post("/api/logistics/restore")
    def logistics_restore(request: Request, payload: dict = Body(...)):
        require(str(payload.get("confirm") or "").upper() == "RESTORE",
                "Type RESTORE to confirm")
        data = payload.get("data")
        require(isinstance(data, dict), "Backup data must be a JSON object")
        mode = str(payload.get("mode") or "replace").lower()
        require(mode in ("replace", "merge"), "mode must be replace or merge")
        d = db(request)
        counts: dict[str, int] = {}
        skipped: dict[str, int] = {}
        safe_settings = {k for k in config.default_settings()
                         if not k.startswith("sdr_tx") and k != "epoch"}

        def clean(value: Any) -> Any:
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            return json.dumps(value, ensure_ascii=False)

        # Patients can be referenced by vitals. Track old->new ids so a merge
        # keeps those links intact instead of pointing at stale record numbers.
        patient_map: dict[int, int] = {}
        for table, spec in RECORD_TABLES.items():
            rows = data.get(table)
            if not isinstance(rows, list):
                continue
            if mode == "replace":
                d.execute(f"DELETE FROM {table}")
                d.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
            n = 0
            for row in rows:
                if not isinstance(row, dict):
                    skipped[table] = skipped.get(table, 0) + 1
                    continue
                values = {f: clean(row[f]) for f in spec["fields"] if f in row}
                if not values:
                    skipped[table] = skipped.get(table, 0) + 1
                    continue
                stamp = spec.get("stamp")
                if stamp and not values.get(stamp):
                    values[stamp] = utcnow()
                if any(values.get(req) in (None, "") for req in spec.get("required", [])):
                    skipped[table] = skipped.get(table, 0) + 1
                    continue
                old_id = row.get("id")
                if table == "vitals" and mode == "merge" and values.get("patient_id") is not None:
                    try:
                        values["patient_id"] = patient_map.get(int(values["patient_id"]),
                                                              values["patient_id"])
                    except (TypeError, ValueError):
                        values["patient_id"] = None
                if mode == "replace" and isinstance(old_id, int) and old_id > 0:
                    values["id"] = old_id
                try:
                    new_id = d.insert(table, values)
                except sqlite3.IntegrityError:
                    skipped[table] = skipped.get(table, 0) + 1
                    continue
                if table == "patients" and isinstance(old_id, int):
                    patient_map[old_id] = new_id
                n += 1
            counts[table] = n

        checklist_rows = data.get("checklists")
        if isinstance(checklist_rows, list):
            if mode == "replace":
                d.execute("DELETE FROM checklist_state")
            n = 0
            for row in checklist_rows:
                if not isinstance(row, dict) or not row.get("kit_id") or not row.get("item_id"):
                    continue
                d.execute(
                    "INSERT INTO checklist_state(kit_id,item_id,checked,updated) VALUES(?,?,?,?) "
                    "ON CONFLICT(kit_id,item_id) DO UPDATE SET checked=excluded.checked, updated=excluded.updated",
                    (str(row["kit_id"])[:100], str(row["item_id"])[:100],
                     1 if row.get("checked") else 0, str(row.get("updated") or utcnow())),
                )
                n += 1
            counts["checklists"] = n

        settings = data.get("settings")
        if isinstance(settings, dict):
            n = 0
            for key, value in settings.items():
                if key not in safe_settings:
                    continue
                d.set_setting(key, value)
                n += 1
            counts["settings"] = n

        total = sum(counts.get(t, 0) for t in RECORD_TABLES)
        d.audit("RESTORE", f"mode={mode} records={total}")
        return {"ok": True, "mode": mode, "records": total, "counts": counts,
                "skipped": skipped,
                "note": "Encrypted vault notes are not restored by this endpoint. "
                        "TX enablement and attestation settings are deliberately preserved."}

    # --------------------------------------------------------------- LAN / mobile access
    @app.get("/api/lan/status")
    def lan_status(request: Request):
        lm = getattr(request.app.state, "lan_manager", None)
        if lm is None:
            from .lan import LanManager
            lm = LanManager()
            request.app.state.lan_manager = lm
        st = lm.status(request.app)
        # If a LAN listener is active, make sure its host/origin are allowed.
        if st.get("enabled") and st.get("ip") and st.get("port"):
            request.app.state.allowed_hosts.add(st["ip"])
            request.app.state.allowed_origins.add(f"https://{st['ip']}:{st['port']}")
            token = getattr(request.app.state, "token", None)
            if token and not (st.get("url") or "").count("token="):
                st["url"] = f"https://{st['ip']}:{st['port']}/?token={token}"
        return st

    @app.post("/api/lan/start")
    def lan_start(request: Request):
        lm = getattr(request.app.state, "lan_manager", None)
        if lm is None:
            from .lan import LanManager
            lm = LanManager()
            request.app.state.lan_manager = lm
        d = db(request)
        if d.get_setting("opsec_mode"):
            raise HTTPException(status_code=403, detail="LAN access disabled in OPSEC mode")
        d.set_setting("lan_access_enabled", True)
        ip = request.app.state.lan_ip
        ok = lm.start(request.app, ip=ip)
        if ok and lm.ip and lm.port:
            request.app.state.allowed_hosts.add(lm.ip)
            request.app.state.allowed_origins.add(f"https://{lm.ip}:{lm.port}")
        else:
            d.set_setting("lan_access_enabled", False)
        st = lm.status(request.app)
        if st.get("enabled") and st.get("ip") and st.get("port"):
            token = getattr(request.app.state, "token", None)
            if token and not (st.get("url") or "").count("token="):
                st["url"] = f"https://{st['ip']}:{st['port']}/?token={token}"
            request.app.state.allowed_hosts.add(st["ip"])
            request.app.state.allowed_origins.add(f"https://{st['ip']}:{st['port']}")
        return st

    @app.post("/api/lan/stop")
    def lan_stop(request: Request):
        lm = getattr(request.app.state, "lan_manager", None)
        if lm is not None:
            lm.stop()
        db(request).set_setting("lan_access_enabled", False)
        return (lm.status(request.app) if lm is not None
                else {"enabled": False, "ip": None, "port": None, "url": None, "error": None})

    @app.get("/api/lan/qr")
    def lan_qr(request: Request):
        st = lan_status(request)
        url = st.get("url")
        if not url:
            raise HTTPException(status_code=404, detail="LAN access is not active")
        try:
            import io
            import qrcode
            from qrcode.constants import ERROR_CORRECT_H
            qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=10, border=2)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return Response(content=buf.getvalue(), media_type="image/png",
                            headers={"Cache-Control": "no-store"})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"QR generation failed: {exc}")

    # --------------------------------------------------------------- pizza intelligence (PizzINT)
    @app.get("/api/pizzint")
    def pizzint_get(request: Request):
        from . import pizzint
        return pizzint.fetch()

    @app.post("/api/pizzint/refresh")
    def pizzint_refresh(request: Request):
        from . import pizzint
        return pizzint.fetch(force=True)

    # --------------------------------------------------------------- manual
    @app.get("/api/manual")
    def manual_index():
        return MANUAL_INDEX

    @app.get("/api/manual/{doc_id}", response_class=PlainTextResponse)
    def manual_doc(doc_id: str):
        safe = re.sub(r"[^a-z0-9_-]", "", doc_id.lower())
        entry = next((d for d in MANUAL_INDEX["docs"] if d["id"] == safe), None)
        require(entry is not None, "Manual document not found", 404)
        path = config.PKG_DIR / "data" / "manual" / entry["file"]
        require(path.exists(), "Manual file missing", 404)
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")

    # --------------------------------------------------------------- search
    @app.get("/api/search")
    def global_search(request: Request, q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
        d = db(request)
        ql = q.strip().lower()
        results = []
        for card in REFERENCE_DATA["cards"]:
            hay = f"{card['title']} {card['category']} {card['body']}".lower()
            if ql in hay:
                results.append({"type": "REFERENCE", "id": card["id"], "title": card["title"],
                                "subtitle": card["category"], "view": "manual", "payload": f"card:{card['id']}"})
        for doc in MANUAL_INDEX["docs"]:
            if ql in (doc["title"] + " " + doc["tagline"]).lower():
                results.append({"type": "MANUAL", "id": doc["id"], "title": doc["title"],
                                "subtitle": doc["tagline"], "view": "manual", "payload": f"doc:{doc['id']}"})
        try:
            for item in library.search(d, q, None, min(limit, 10)):
                results.append({"type": "DOCUMENT", "id": str(item["id"]), "title": item["name"],
                                "subtitle": item["category"], "view": "library",
                                "payload": f"doc:{item['id']}", "snippet": item.get("snippet", "")})
        except Exception:
            pass
        for row in d.query("SELECT * FROM waypoints WHERE name LIKE ? LIMIT 5", (f"%{q}%",)):
            results.append({"type": "WAYPOINT", "id": str(row["id"]), "title": row["name"],
                            "subtitle": f"{row['lat']:.4f}, {row['lon']:.4f}",
                            "view": "map", "payload": f"wp:{row['id']}"})
        return {"query": q, "results": results[:limit]}

    # ---------------------------------------------------------------- purge
    @app.post("/api/security/backup")
    def security_backup(request: Request):
        return logistics_backup(request, {})

    # --------------------------------------------------------------- static
    ui_dir = Path(config.UI_DIR)
    if ui_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    @app.get("/")
    def root():
        if (ui_dir / "index.html").exists():
            return FileResponse(ui_dir / "index.html", media_type="text/html")
        return HTMLResponse("<h1>DarkSec UI not installed</h1>", status_code=503)

    @app.get("/favicon.svg")
    def favicon():
        path = ui_dir / "img" / "favicon.svg"
        if path.exists():
            return FileResponse(path, media_type="image/svg+xml")
        return Response(status_code=404)

    return app
