"""Paths and runtime configuration.

Everything the application needs at runtime lives under DATA_DIR so the whole
kit can be copied to a USB stick and carried into the field. The document
library stays in the repository/installation directory and is never modified.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DarkSec"
APP_ID = "darksec"


def _resource_root() -> Path:
    """Root of bundled read-only resources (works frozen and from source)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


RESOURCE_ROOT = _resource_root()
PKG_DIR = Path(__file__).resolve().parent
UI_DIR = RESOURCE_ROOT / "ui"
DATA_DIR = Path(os.environ.get("DARKSEC_DATA", RESOURCE_ROOT / "fielddata")).expanduser()
DB_PATH = DATA_DIR / "darksec.db"
UPLOAD_DIR = DATA_DIR / "uploads"
RECORDING_DIR = DATA_DIR / "recordings"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"

# The document library. Multiple roots are supported so the same install can
# index the original folders and any additional field library.
LIBRARY_ROOTS = [
    RESOURCE_ROOT / "chembio",
    RESOURCE_ROOT / "comms",
    RESOURCE_ROOT / "engineering",
    RESOURCE_ROOT / "hacking",
    RESOURCE_ROOT / "manuals",
    RESOURCE_ROOT / "medical",
    RESOURCE_ROOT / "prepper",
    RESOURCE_ROOT / "water_food",
    RESOURCE_ROOT / "maps",
    RESOURCE_ROOT / "isos",
]
EXTRA_LIBRARY_ROOTS = [
    Path(p).expanduser()
    for p in os.environ.get("DARKSEC_LIBRARY", "").split(os.pathsep)
    if p.strip()
]
DOC_EXTENSIONS = {".pdf", ".epub", ".txt", ".md", ".html", ".htm", ".djvu", ".cbz"}

MAP_TILE_DIR = RESOURCE_ROOT / "maps" / "tiles"
MAP_VECTOR_DIR = RESOURCE_ROOT / "maps" / "vector"

for _d in (DATA_DIR, UPLOAD_DIR, RECORDING_DIR, LOG_DIR, BACKUP_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def default_settings() -> dict:
    return {
        "callsign": "NIGHTWATCH",
        "team": "FIELD TEAM 01",
        "operator": "",
        "lat": 39.8283,
        "lon": -98.5795,
        "elevation_m": 0,
        "position_source": "unverified",
        "position_updated_at": "",
        "position_fix_at": "",
        "position_accuracy_m": None,
        "position_mode": 0,
        "timezone": "auto",
        "units": "metric",
        "date_format": "iso",
        "grid_format": "mgrs",
        "declination": 0.0,
        "accent": "green",
        "sound": True,
        "scanlines": True,
        "sdr_rx_gain": 40,
        "sdr_lna_gain": 32,
        "sdr_amp": False,
        "sdr_tx_enabled": False,
        "sdr_tx_attested": False,
        "tile_source": "local",
        "mbtiles_path": "",
        "map_vector": True,
        "auto_deep_index": True,
        "feed_auto_refresh": True,
        "feed_refresh_min": 10,
        "vault_timeout_min": 15,
        "epoch": "",
        "opsec_mode": False,
    }
