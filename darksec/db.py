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

CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1,
    status INTEGER NOT NULL DEFAULT 0,
    trend TEXT NOT NULL DEFAULT 'unknown',
    note TEXT DEFAULT '',
    updated TEXT
);
CREATE TABLE IF NOT EXISTS indicator_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    status INTEGER NOT NULL,
    note TEXT,
    FOREIGN KEY(indicator_id) REFERENCES indicators(id) ON DELETE CASCADE
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
    category TEXT DEFAULT 'GENERAL'
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
"""

INDICATOR_SEED = [
    ("POWER", "Grid stability", 3.0),
    ("POWER", "Fuel availability", 3.0),
    ("POWER", "Backup power readiness", 2.5),
    ("WATER", "Municipal water pressure", 3.0),
    ("WATER", "Stored water (days)", 3.0),
    ("WATER", "Alternative source access", 2.0),
    ("FOOD", "Food stock (days)", 3.0),
    ("FOOD", "Local resupply", 2.5),
    ("FOOD", "Production / foraging", 1.5),
    ("COMMS", "Telecom / internet", 2.0),
    ("COMMS", "Radio coverage", 2.5),
    ("COMMS", "Emergency broadcasts", 2.0),
    ("SECURITY", "Local incident rate", 3.0),
    ("SECURITY", "Perimeter readiness", 2.5),
    ("SECURITY", "Community relations", 1.5),
    ("HEALTH", "Medical supplies", 3.0),
    ("HEALTH", "Clinical access", 2.0),
    ("HEALTH", "Public health alerts", 2.0),
    ("MOBILITY", "Vehicle fuel", 2.5),
    ("MOBILITY", "Route status", 2.5),
    ("MOBILITY", "Evacuation readiness", 2.0),
    ("ENVIRONMENT", "Severe weather risk", 2.0),
    ("ENVIRONMENT", "Air quality", 1.5),
    ("ENVIRONMENT", "Radiation background", 2.5),
    ("FINANCE", "Cash on hand", 2.0),
    ("FINANCE", "Bank / payment access", 2.0),
    ("FINANCE", "Supply chain", 2.5),
]

FEED_SEED = [
    ("usgs_quakes", "USGS Significant Earthquakes", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson", "usgs"),
    ("swpc_kp", "NOAA SWPC Planetary K-index", "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", "swpc_kp"),
    ("swpc_scales", "NOAA Space Weather Scales", "https://services.swpc.noaa.gov/products/noaa-scales.json", "swpc_scales"),
    ("nws_alerts", "NWS Active Alerts (US)", "https://api.weather.gov/alerts/active?status=actual&message_type=alert", "nws"),
    ("gdacs", "GDACS Disaster Alerts", "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH", "gdacs"),
    ("eonet", "NASA EONET Natural Events", "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=30", "eonet"),
    ("safecast", "Safecast Radiation Reports", "https://api.safecast.org/measurements.json", "safecast"),
    ("smithsonian_volcano", "Smithsonian/USGS Volcanic Activity", "https://volcano.si.edu/news/WeeklyVolcanoRSS.xml", "rss"),
    ("who_don", "WHO News (English)", "https://www.who.int/rss-feeds/news-english.xml", "rss"),
    ("reliefweb", "ReliefWeb Updates", "https://reliefweb.int/updates/rss.xml", "rss"),
    ("opensky_states", "OpenSky Live Aircraft (public positions)", "https://opensky-network.org/api/states/all?limit=4000", "opensky"),
    ("celestrak_tle", "CelesTrak Active Satellite TLE", "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle", "celestrak"),
    ("bbc_world", "BBC World News", "https://feeds.bbci.co.uk/news/world/rss.xml", "rss"),
    ("guardian_world", "The Guardian World", "https://www.theguardian.com/world/rss", "rss"),
    ("france24_world", "France24 World", "https://www.france24.com/en/rss", "rss"),
    ("dw_news", "Deutsche Welle World", "https://rss.dw.com/rdf/rss-en-all", "rss"),
    ("aljazeera_world", "Al Jazeera English", "https://www.aljazeera.com/xml/rss/all.xml", "rss"),
    ("crisisgroup", "International Crisis Group", "https://www.crisisgroup.org/rss", "rss"),
    ("gdelt_conflict", "GDELT Live Conflict Events (media-derived)",
     "https://data.gdeltproject.org/gdeltv2/lastupdate.txt", "gdelt"),
    ("ucdp_candidate", "UCDP Candidate Conflict Events (monthly verified)",
     "https://ucdp.uu.se/downloads/", "ucdp"),
    ("open_meteo_air", "Open-Meteo Air Quality", "https://air-quality-api.open-meteo.com/v1/air-quality", "air_quality"),
    ("open_meteo_weather", "Open-Meteo Local Weather", "https://api.open-meteo.com/v1/forecast", "weather"),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str | None = None):
        self.path = str(path or config.DB_PATH)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._seed()
        self._migrate()
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
        # Earlier builds seeded every indicator as status 0 (GREEN), which made
        # a never-assessed install report 100/100 NOMINAL. Move unassessed
        # installs to status 3 (UNKNOWN). Installs with any assessment history
        # are left untouched so real operator judgements are preserved.
        if self.one("SELECT 1 FROM meta WHERE key='indicator_unknown_v1'") is None:
            history = self.one("SELECT count(*) c FROM indicator_history")
            if (history["c"] or 0) == 0:
                self.execute("UPDATE indicators SET status=3")
            self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('indicator_unknown_v1',?)",
                         (utcnow(),))

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

    # ------------------------------------------------------------------ seed
    def _seed(self) -> None:
        if self.one("SELECT 1 FROM meta WHERE key='seeded'") is None:
            for category, name, weight in INDICATOR_SEED:
                self.execute(
                    "INSERT INTO indicators(category,name,weight,status,trend,note,updated) VALUES(?,?,?,3,'unknown','',?)",
                    (category, name, weight, utcnow()),
                )
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

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
