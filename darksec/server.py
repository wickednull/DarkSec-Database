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

import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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
from .brief import build_brief
from .calc_catalog import CALC_CATALOG, CALC_FUNCS
from .db import Database, utcnow
from .security import Vault
from .status import hardware, system_info, readiness
from .trips import parse_utc as _parse_utc, trip_items

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


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

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
        if handshake:
            print(json.dumps({"ready": True, "app": APP_NAME, "version": __version__,
                              "port": port, "token": token}), flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}/?token={token}")
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
        if path.startswith("/api/") or is_html:
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
                                         sun=almanac, threat=local_threat)
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
    def brief(request: Request):
        return PlainTextResponse(build_brief(db(request)), media_type="text/plain; charset=utf-8")

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
    @app.get("/api/assistant/status")
    async def assistant_status(request: Request):
        return await run_in_threadpool(assistant.status, db(request))

    @app.get("/api/assistant/models")
    async def assistant_models(request: Request):
        return await run_in_threadpool(assistant.list_models, db(request))

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
        try:
            result = await run_in_threadpool(assistant.chat, d, prompt, history, context, model, think, image_paths)
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
            try:
                for chunk in assistant.chat_stream(d, prompt, history, context, model, think, image_paths):
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
                   "vault_timeout_min": (0, 240), "position_accuracy_m": (0, 100000),
                   "position_mode": (0, 3)}
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
                       "map_vector", "auto_deep_index", "feed_auto_refresh", "opsec_mode", "dem_enabled", "assistant_enabled", "assistant_think"):
                value = bool(value)
            if key in ("callsign", "team", "operator", "timezone", "units", "date_format",
                       "grid_format", "accent", "tile_source", "mbtiles_path", "dem_mbtiles_path",
                       "dem_encoding", "epoch", "position_source", "position_updated_at", "position_fix_at",
                       "ollama_base_url", "ollama_model"):
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
                for key in ("snapshot_url", "snapshotUrl", "image_url", "imageUrl", "url", "feed_url"):
                    val = it.get(key)
                    if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                        url = val.strip(); break
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
                timeout=12,
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
        r = requests.get(target_url, headers=headers, timeout=20, stream=True, allow_redirects=False)
        # Do not follow redirects to arbitrary hosts.
        if r.is_redirect or r.is_permanent_redirect:
            loc = r.headers.get("location") or ""
            if loc:
                abs_loc = urljoin(target_url, loc)
                if not _cctv_stream_allowed(registered_url, abs_loc, allow_hosts):
                    r.close()
                    raise HTTPException(status_code=400, detail="Stream redirect target not allowed")
            r.close()
            r = requests.get(abs_loc, headers=headers, timeout=20, stream=True, allow_redirects=False)
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
            r = requests.get(target, headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"}, timeout=15)
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
                r = requests.get(target, headers={"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain,*/*"}, timeout=15)
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
