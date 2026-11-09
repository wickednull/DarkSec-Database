"""SQLite storage layer.

One portable database holds every operator-entered record. The read-only PDF
library is indexed in a separate FTS5 table and can be rebuilt at any time.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT DEFAULT 'operator',
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    category TEXT DEFAULT 'GENERAL',
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    lat REAL,
    lon REAL,
    tags TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    priority TEXT DEFAULT 'ROUTINE',
    due TEXT,
    done INTEGER DEFAULT 0,
    category TEXT DEFAULT 'GENERAL',
    recurrence TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    name TEXT DEFAULT '',
    age INTEGER,
    sex TEXT DEFAULT '',
    weight_kg REAL,
    blood_type TEXT DEFAULT '',
    allergies TEXT DEFAULT '',
    meds TEXT DEFAULT '',
    history TEXT DEFAULT '',
    triage TEXT DEFAULT 'UNASSESSED',
    status TEXT DEFAULT 'OPEN',
    note TEXT DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS vitals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    patient_id INTEGER,
    hr INTEGER, rr INTEGER, sys INTEGER, dia INTEGER,
    spo2 REAL, temp_c REAL, glucose REAL, pain INTEGER,
    avpu TEXT DEFAULT 'A',
    note TEXT DEFAULT '',
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS medication_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    patient_id INTEGER,
    medication TEXT NOT NULL,
    dose TEXT DEFAULT '',
    route TEXT DEFAULT '',
    administered_by TEXT DEFAULT '',
    indication TEXT DEFAULT '',
    reaction TEXT DEFAULT '',
    note TEXT DEFAULT '',
    FOREIGN KEY(patient_id) REFERENCES patients(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS weather_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    temp_c REAL, pressure_hpa REAL, humidity REAL,
    wind_dir INTEGER, wind_kmh REAL, gust_kmh REAL,
    precip_mm REAL, cloud_cover INTEGER, cloud_type TEXT,
    visibility_km REAL, note TEXT DEFAULT '', lat REAL, lon REAL
);

CREATE TABLE IF NOT EXISTS water_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    source TEXT, volume_l REAL, method TEXT,
    chlorine_ppm REAL, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS power_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    battery_pct REAL, fuel_l REAL, generator_hours REAL,
    solar_kwh REAL, load_w REAL, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rad_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    dose_rate_usv_h REAL, cumulative_usv REAL,
    location TEXT, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS waypoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    ele REAL,
    type TEXT DEFAULT 'WAYPOINT',
    note TEXT DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    geojson TEXT NOT NULL,
    color TEXT DEFAULT '#38ff9c',
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radio_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    freq_hz REAL, mode TEXT, callsign TEXT,
    summary TEXT DEFAULT '', lat REAL, lon REAL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    updated TEXT,
    title TEXT NOT NULL,
    cipher TEXT NOT NULL,
    tags TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    severity TEXT DEFAULT 'INFO',
    category TEXT DEFAULT 'SECURITY',
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    lat REAL, lon REAL,
    resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT DEFAULT 'GENERAL',
    qty REAL DEFAULT 0,
    unit TEXT DEFAULT 'ea',
    min_qty REAL DEFAULT 0,
    expires TEXT,
    location TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    updated TEXT
);

CREATE TABLE IF NOT EXISTS caches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    lat REAL, lon REAL,
    contents TEXT DEFAULT '',
    status TEXT DEFAULT 'ACTIVE',
    note TEXT DEFAULT '',
    updated TEXT
);

CREATE TABLE IF NOT EXISTS comms_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    data TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    data TEXT NOT NULL,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trip_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_waypoint_id INTEGER,
    end_waypoint_id INTEGER,
    route TEXT DEFAULT '',
    departure_at TEXT NOT NULL,
    return_at TEXT NOT NULL,
    checkin_interval_min INTEGER DEFAULT 60,
    emergency_contact TEXT DEFAULT '',
    party_size INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    status TEXT DEFAULT 'PLANNED',
    started_at TEXT,
    last_checkin_at TEXT,
    created TEXT NOT NULL,
    updated TEXT
);

CREATE TABLE IF NOT EXISTS sdr_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    freq_hz REAL, rate REAL, seconds REAL,
    status TEXT DEFAULT 'queued',
    progress REAL DEFAULT 0,
    iq_path TEXT, audio_path TEXT, image_path TEXT,
    spectrum TEXT, error TEXT,
    created TEXT NOT NULL, finished TEXT
);

CREATE TABLE IF NOT EXISTS sdr_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    name TEXT NOT NULL,
    freq_hz REAL, rate REAL, kind TEXT,
    iq_path TEXT, audio_path TEXT, image_path TEXT,
    size INTEGER DEFAULT 0, note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS feeds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    fetched_at TEXT,
    status TEXT DEFAULT 'NEVER',
    payload TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS checklist_state (
    kit_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    checked INTEGER DEFAULT 0,
    updated TEXT,
    PRIMARY KEY (kit_id, item_id)
);

CREATE TABLE IF NOT EXISTS library_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    rel_path TEXT,
    size INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0,
    pages INTEGER,
    chars INTEGER DEFAULT 0,
    text TEXT DEFAULT '',
    indexed_ts TEXT,
    text_ts TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS library_fts USING fts5(
    name, path, category, text,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Cached map overlay snapshots (wind/rain/cloud grids, operator-triggered refresh).
-- Payload is JSON (GeoJSON FeatureCollection or {points:[...], meta:{...}}).
CREATE TABLE IF NOT EXISTS map_overlay_cache (
    kind TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    fetched_at TEXT,
    status TEXT DEFAULT 'NEVER',
    payload TEXT,
    error TEXT,
    meta TEXT
);

-- Persistent CCTV/surveillance media health (frame/stream fetch outcomes).
-- Keeps recent operational status across restarts; bounded by upserts per camera.
CREATE TABLE IF NOT EXISTS cctv_media_health (
    camera_id TEXT PRIMARY KEY,
    kind TEXT DEFAULT 'frame',
    status TEXT DEFAULT '',
    message TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    updated_at TEXT,
    ok_at TEXT,
    error_at TEXT,
    ok_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    extra TEXT DEFAULT '{}'
);

-- Query indexes are created in _ensure_indexes() so an older database with a
-- missing column cannot stop the application from starting.
"""


# Query indexes for the views that sort/filter the record tables. Without
# these, SQLite scans every row each time the dashboard, audit log or vitals
# trend is opened as the operator's history grows.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit(action)",
    "CREATE INDEX IF NOT EXISTS idx_log_ts ON log_entries(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_log_category ON log_entries(category)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_done_priority ON tasks(done, priority)",
    "CREATE INDEX IF NOT EXISTS idx_vitals_patient_ts ON vitals(patient_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_medication_patient_ts ON medication_log(patient_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_water_ts ON water_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_power_ts ON power_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_rad_ts ON rad_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_radio_ts ON radio_log(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_resolved_ts ON incidents(resolved, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory(name)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_stock ON inventory(qty, min_qty)",
    "CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated DESC)",
    "CREATE INDEX IF NOT EXISTS idx_log_category_ts ON log_entries(category, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_waypoints_created ON waypoints(created DESC)",
    "CREATE INDEX IF NOT EXISTS idx_trip_status ON trip_plans(status, departure_at)",
    "CREATE INDEX IF NOT EXISTS idx_sdr_recordings_ts ON sdr_recordings(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_library_category ON library_docs(category)",
    "CREATE INDEX IF NOT EXISTS idx_library_rel_path ON library_docs(rel_path)",
    "CREATE INDEX IF NOT EXISTS idx_feeds_kind ON feeds(kind)",
    "CREATE INDEX IF NOT EXISTS idx_cctv_media_health_status ON cctv_media_health(status, updated_at DESC)",
]


FEED_SEED = [
    ("usgs_quakes", "USGS Significant Earthquakes", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson", "usgs"),
    ("swpc_kp", "NOAA SWPC Planetary K-index", "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", "swpc_kp"),
    ("swpc_scales", "NOAA Space Weather Scales", "https://services.swpc.noaa.gov/products/noaa-scales.json", "swpc_scales"),
    ("nws_alerts", "NWS Active Alerts (US)", "https://api.weather.gov/alerts/active?status=actual&message_type=alert", "nws"),
    ("gdacs", "GDACS Disaster Alerts", "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH", "gdacs"),
    ("eonet", "NASA EONET Natural Events", "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=30", "eonet"),
    ("rsoe_edis", "RSOE EDIS Global Disaster Events", "https://rsoe-edis.org/eventList", "rsoe"),
    ("safecast", "Safecast Radiation Reports", "https://api.safecast.org/measurements.json", "safecast"),
    ("smithsonian_volcano", "Smithsonian/USGS Volcanic Activity", "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml", "rss"),
    ("who_don", "WHO News (English)", "https://www.who.int/rss-feeds/news-english.xml", "rss"),
    ("reliefweb", "ReliefWeb Updates", "https://reliefweb.int/updates/rss.xml", "rss"),
    ("opensky_states", "OpenSky Live Aircraft (public positions)", "https://opensky-network.org/api/states/all?limit=4000", "opensky"),
    ("celestrak_tle", "CelesTrak Active Satellite TLE", "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle", "celestrak"),
    ("celestrak_stations", "CelesTrak Crewed Stations", "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle", "celestrak"),
    ("celestrak_weather", "CelesTrak Weather Satellites", "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle", "celestrak"),
    ("celestrak_amateur", "CelesTrak Amateur Radio", "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle", "celestrak"),
    ("bbc_world", "BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml", "rss"),
    ("guardian_world", "The Guardian World", "https://www.theguardian.com/world/rss", "rss"),
    ("france24_world", "France24 World", "https://www.france24.com/en/rss", "rss"),
    ("dw_news", "Deutsche Welle World", "https://rss.dw.com/rdf/rss-en-all", "rss"),
    ("aljazeera_world", "Al Jazeera English", "https://www.aljazeera.com/xml/rss/all.xml", "rss"),
    ("crisisgroup", "International Crisis Group", "https://www.crisisgroup.org/rss.xml", "rss"),
    ("thehackernews", "The Hacker News (Cybersecurity)", "https://feeds.feedburner.com/TheHackersNews", "rss"),
    ("bbc_middle_east", "BBC Middle East", "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", "rss"),
    ("twz_defense", "The War Zone (Defense/Conflict)", "https://www.twz.com/feed", "rss"),
    ("breaking_defense", "Breaking Defense", "https://breakingdefense.com/feed/", "rss"),
    ("defense_one", "Defense One", "https://www.defenseone.com/rss/all/", "rss"),
    ("long_war_journal", "Long War Journal", "https://www.longwarjournal.org/feed", "rss"),
    ("war_on_the_rocks", "War on the Rocks", "https://warontherocks.com/feed/", "rss"),
    ("gdelt_conflict", "GDELT Live Conflict Events (media-derived)",
     "https://data.gdeltproject.org/gdeltv2/lastupdate.txt", "gdelt"),
    ("ucdp_candidate", "UCDP Candidate Conflict Events (monthly verified)",
     "https://ucdp.uu.se/downloads/", "ucdp"),
    ("open_meteo_air", "Open-Meteo Air Quality", "https://air-quality-api.open-meteo.com/v1/air-quality", "air_quality"),
    ("open_meteo_weather", "Open-Meteo Local Weather", "https://api.open-meteo.com/v1/forecast", "weather"),
    # Scaffolding: operator-configurable or future integration points.
    # Set a real URL/kind config per deployment; empty URL refreshes report not configured.
    ("maritime_ais", "Maritime / AIS Positions (scaffold)", "", "maritime"),
    ("infra_cables", "Subsea/Data/Power Cables (scaffold)", "", "cables"),
    ("public_cctv", "Public CCTV / Camera Feeds (scaffold)", "", "cctv"),
    ("cctv_catalog", "CCTV / Camera Catalog (local/imported packs)", "", "cctv"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str | None = None):
        self.path = str(path or config.DB_PATH)
        self._lock = threading.RLock()
        self.migration_warnings: list[str] = []
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._seed()
        self._migrate()
        self._ensure_indexes()
        self._fts_rebuild_if_needed()

    # ------------------------------------------------------------------ core
    def executescript(self, sql: str) -> None:
        with self._lock:
            self.conn.executescript(sql)
            self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)).fetchall())

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur.lastrowid

    def insert(self, table: str, data: dict) -> int:
        keys = [k for k in data]
        sql = f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})"
        return self.execute(sql, [data[k] for k in keys])

    @contextmanager
    def transaction(self):
        """Run several statements atomically (commit on success, rollback on error)."""
        with self._lock:
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def update(self, table: str, row_id: int, data: dict) -> None:
        if not data:
            return
        keys = list(data)
        sql = f"UPDATE {table} SET {','.join(f'{k}=?' for k in keys)} WHERE id=?"
        self.execute(sql, [data[k] for k in keys] + [row_id])

    def delete(self, table: str, row_id: int) -> None:
        self.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))

    def audit(self, action: str, detail: str = "", ip: str = "", actor: str = "operator") -> None:
        self.execute(
            "INSERT INTO audit (ts,actor,action,detail,ip) VALUES (?,?,?,?,?)",
            (utcnow(), actor, action, detail, ip),
        )

    # -------------------------------------------------------------- settings
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all_settings(self) -> dict:
        out = dict(config.default_settings())
        for row in self.query("SELECT key,value FROM settings"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except Exception:
                out[row["key"]] = row["value"]
        return out

    # -------------------------------------------------------------- migrations
    def _migrate(self) -> None:
        # Recurring tasks: add the column to databases created before the
        # field existed. CREATE TABLE IF NOT EXISTS does not alter old tables.
        task_cols = {r["name"] for r in self.query("PRAGMA table_info(tasks)")}
        if "recurrence" not in task_cols:
            self.execute("ALTER TABLE tasks ADD COLUMN recurrence TEXT DEFAULT ''")

        # Auto-sync used to be an inert preference (the old build only fetched
        # on explicit request). Adopt the automatic behaviour once, and add the
        # interval setting, without disturbing other operator choices.
        if self.one("SELECT 1 FROM meta WHERE key='auto_sync_v1'") is None:
            self.set_setting("feed_auto_refresh", True)
            self.set_setting("feed_refresh_min", 10)
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('auto_sync_v1',?)",
                         (utcnow(),))

        # Feed URLs that changed upstream: GDACS RSS moved behind a WAF, WHO
        # DON RSS is dead. Point both at their working endpoints once.
        if self.one("SELECT 1 FROM meta WHERE key='feeds_v2'") is None:
            self.execute(
                "UPDATE feeds SET url=?, kind=?, name=? WHERE id='gdacs'",
                ("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
                 "gdacs", "GDACS Disaster Alerts"),
            )
            self.execute(
                "UPDATE feeds SET url=?, name=? WHERE id='who_don'",
                ("https://www.who.int/rss-feeds/news-english.xml", "WHO News (English)"),
            )
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feeds_v2',?)",
                         (utcnow(),))

        # France24 rotated its feed path; the old one now returns HTTP 409.
        if self.one("SELECT 1 FROM meta WHERE key='feeds_v3'") is None:
            self.execute(
                "UPDATE feeds SET url=? WHERE id='france24_world'",
                ("https://www.france24.com/en/rss",),
            )
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feeds_v3',?)",
                         (utcnow(),))

        # Feed reliability fixes: Crisis Group canonical RSS path, and ensure
        # CelesTrak uses the modern gp.php TLE endpoint.
        if self.one("SELECT 1 FROM meta WHERE key='feeds_v4'") is None:
            self.execute(
                "UPDATE feeds SET url=?, name=? WHERE id='crisisgroup'",
                ("https://www.crisisgroup.org/rss.xml", "International Crisis Group"),
            )
            self.execute(
                "UPDATE feeds SET url=?, kind=?, name=? WHERE id='celestrak_tle'",
                ("https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle",
                 "celestrak", "CelesTrak Active Satellite TLE"),
            )
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feeds_v4',?)",
                         (utcnow(),))

        # Delamain prompt refinement: older defaults were easy for small local
        # models to overgeneralize into refusals. Upgrade only saved prompts that
        # still look like the previous app default; leave custom operator prompts.
        # This check is migration-only (no provider/model provenance claims).
        new_default = config.default_settings()["assistant_system_prompt"]
        if self.one("SELECT 1 FROM meta WHERE key='assistant_prompt_v4'") is None:
            try:
                cur = str(self.get_setting("assistant_system_prompt", "") or "")
                cur_lower = cur.lower()
                looks_old_default = (
                    (
                        cur.startswith("Your name is Delamain")
                        or cur.startswith("You are Delamain")
                        or "your name is delamain" in cur_lower
                        or "you are delamain" in cur_lower
                    )
                    and "Answer any lawful question helpfully and directly" not in cur
                    and "For simple questions" in cur
                )
                if looks_old_default:
                    self.set_setting("assistant_system_prompt", new_default)
            except Exception:
                pass
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('assistant_prompt_v4',?)",
                         (utcnow(),))

        # Longer answers by default: early installs saved 512 tokens, which cuts
        # off many useful responses. Raise very low saved values to the current
        # default once; leave operator-chosen higher values untouched.
        if self.one("SELECT 1 FROM meta WHERE key='assistant_max_tokens_v1'") is None:
            try:
                current = self.get_setting("assistant_max_tokens", None)
                try:
                    value = int(float(current)) if current is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is None or value <= 512:
                    self.set_setting("assistant_max_tokens", config.default_settings()["assistant_max_tokens"])
            except Exception:
                pass
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('assistant_max_tokens_v1',?)",
                         (utcnow(),))

    # ------------------------------------------------------------------ indexes
    def _ensure_indexes(self) -> None:
        """Create query indexes, tolerating older schemas with missing columns."""
        with self._lock:
            for sql in INDEXES:
                try:
                    self.conn.execute(sql)
                except sqlite3.OperationalError:
                    continue
            self.conn.commit()

    # ------------------------------------------------------------------ seed
    def _seed(self) -> None:
        if self.one("SELECT 1 FROM meta WHERE key='seeded'") is None:
            for fid, name, url, kind in FEED_SEED:
                self.execute(
                    "INSERT OR IGNORE INTO feeds(id,name,url,kind,status) VALUES(?,?,?,?,'NEVER')",
                    (fid, name, url, kind),
                )
            self.execute("INSERT INTO meta(key,value) VALUES('seeded',?)", (utcnow(),))
            self.execute("INSERT INTO meta(key,value) VALUES('created',?)", (utcnow(),))
        # New feed definitions ship with the app; add any that an existing
        # install does not have yet without touching operator data.
        for fid, name, url, kind in FEED_SEED:
            self.execute(
                "INSERT OR IGNORE INTO feeds(id,name,url,kind,status) VALUES(?,?,?,?,'NEVER')",
                (fid, name, url, kind),
            )
        # settings defaults only; never overwrite operator choices
        for key, value in config.default_settings().items():
            if self.one("SELECT 1 FROM settings WHERE key=?", (key,)) is None:
                self.set_setting(key, value)
        if self.one("SELECT 1 FROM meta WHERE key='epoch'") is None:
            self.execute("INSERT INTO meta(key,value) VALUES('epoch',?)", (utcnow(),))
        self.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('instance','" + str(time.time()) + "')")

    # ------------------------------------------------------------------ fts
    def _fts_rebuild_if_needed(self) -> None:
        row = self.one("SELECT count(*) c FROM library_docs")
        fts = self.one("SELECT count(*) c FROM library_fts")
        if row and fts and row["c"] != fts["c"]:
            self.rebuild_fts()

    def rebuild_fts(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM library_fts")
            self.conn.execute(
                "INSERT INTO library_fts(rowid,name,path,category,text) "
                "SELECT id,name,path,category,text FROM library_docs"
            )
            self.conn.commit()

    def fts_upsert(self, doc_id: int, name: str, path: str, category: str, text: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM library_fts WHERE rowid=?", (doc_id,))
            self.conn.execute(
                "INSERT INTO library_fts(rowid,name,path,category,text) VALUES(?,?,?,?,?)",
                (doc_id, name, path, category, text),
            )
            self.conn.commit()

    def upsert_cctv_media_health(self, camera_id: str, *, kind: str = "frame", status: str = "", message: str = "", source_url: str = "", extra: dict | None = None) -> None:
        if not camera_id:
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = self.one("SELECT ok_count, error_count, ok_at, error_at FROM cctv_media_health WHERE camera_id=?", (camera_id,))
        ok_count = int((row["ok_count"] if row else 0) or 0)
        error_count = int((row["error_count"] if row else 0) or 0)
        ok_at = (row["ok_at"] if row else None) or None
        error_at = (row["error_at"] if row else None) or None
        if status in ("ok", "cached"):
            ok_count += 1
            ok_at = now
        elif status == "error":
            error_count += 1
            error_at = now
        payload = json.dumps(extra or {}, ensure_ascii=False)
        self.execute(
            """INSERT INTO cctv_media_health(camera_id, kind, status, message, source_url, updated_at, ok_at, error_at, ok_count, error_count, extra)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(camera_id) DO UPDATE SET
                 kind=excluded.kind, status=excluded.status, message=excluded.message, source_url=excluded.source_url,
                 updated_at=excluded.updated_at,
                 ok_at=COALESCE(excluded.ok_at, cctv_media_health.ok_at),
                 error_at=COALESCE(excluded.error_at, cctv_media_health.error_at),
                 ok_count=excluded.ok_count, error_count=excluded.error_count, extra=excluded.extra""",
            (camera_id, kind, status, (message or "")[:500], (source_url or "")[:2000], now, ok_at, error_at, ok_count, error_count, payload),
        )

    def recent_cctv_media_errors(self, limit: int = 12) -> list[dict]:
        try:
            rows = self.query("SELECT camera_id, kind, status, message, updated_at, error_at, error_count FROM cctv_media_health WHERE status='error' ORDER BY updated_at DESC LIMIT ?", (max(1, min(100, int(limit))),))
            return [dict(r) for r in rows]
        except Exception:
            return []

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
