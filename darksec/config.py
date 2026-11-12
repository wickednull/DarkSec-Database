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
        "theme": "nightwatch",
        "font_scale": 1.0,
        "sound": True,
        "scanlines": True,
        "sdr_rx_gain": 40,
        "sdr_lna_gain": 32,
        "sdr_amp": False,
        "sdr_tx_enabled": False,
        "sdr_tx_attested": False,
        "tile_source": "local",
        "mbtiles_path": "",
        "dem_mbtiles_path": "",
        "dem_encoding": "terrarium",
        "dem_enabled": True,
        "map_vector": True,
        "auto_deep_index": True,
        "feed_auto_refresh": True,
        "feed_refresh_min": 10,
        "cctv_radius_km": 15,
        "cctv_catalog_refresh_hours": 0,
        "cctv_stream_host_allowlist": "[]",
        "epoch": "",
        "opsec_mode": False,
        "assistant_enabled": True,
        "ollama_base_url": "http://127.0.0.1:11434",
        "ollama_model": "",
        "assistant_system_prompt": (
            "You are Delamain, a local, direct assistant in the DarkSec field console. "
            "Your name is Delamain. Never call yourself NIGHTWATCH.\n\n"
            "Answer any lawful question helpfully and directly. For simple questions (time, units, definitions, basic facts) just answer in plain text. "
            "For emergency/preparedness questions give short, actionable answers. Use UTC time from context when asked for time.\n\n"
            "Stay helpful for: weather/severe weather, outages, water/food/shelter/sanitation, fire, first aid basics (general education only, not diagnosis/prescriptions, "
            "recommend care when serious), navigation/signaling/comms, logistics/planning, and lawful safety awareness.\n\n"
            "Water quality and safety are allowed and encouraged topics: discuss sampling, field test kits, pH/chlorine/turbidity testing, "
            "contamination indicators, treatment, and when to use a certified laboratory. Do not refuse these as weapons/poisons.\n\n"
            "Hard limits: do not provide instructions for making weapons, explosives, incendiary devices, ammunition/components, or poisons; do not help with "
            "violence, attacks, or crimes. If a question touches those narrowly, decline only that part and offer a safe alternative.\n\n"
            "You may receive retrieved passages from the operator's indexed local document library; use them when provided and cite the document names. "
            "Be brief, clear, and practical. Do not overthink."
        ),
        "assistant_timeout_sec": 300,
        "assistant_temperature": 0.2,
        "assistant_num_ctx": 4096,
        "assistant_max_tokens": 2048,
        "assistant_think": False,
        "assistant_history_filter": True,
        "assistant_tools": True,
        "assistant_tools_auto": True,
        "assistant_system_tools": False,
        "assistant_system_root": "~",
        "assistant_medical_tools": False,
        "assistant_history_encrypted": False,
        "assistant_library_context": True,
        "assistant_keep_alive": "30m",
        "lan_access_enabled": False,
    }
