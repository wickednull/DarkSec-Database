"""Disease outbreak tracking for global health threat monitoring.

Sources (no API key required):
- Disease.sh (COVID-19 global statistics)

Note: CDC FluView endpoint URL is unstable; CDC tracking is disabled until
a stable public endpoint is confirmed. Add one later without breaking this
module — the module already handles partial data gracefully.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

DISEASE_SH_ALL = "https://disease.sh/v3/covid-19/all"

# In-memory TTL cache — same rationale as nasa.py: don't hammer the upstream
# on every overview() call (which can happen several times per refresh).
_cache: dict = {
    "covid": None,
    "flu": None,
    "summary": None,
    "fetched_at": 0.0,
}
TTL_SECONDS = 3600  # 1 hour


def _cache_expired() -> bool:
    return time.monotonic() - _cache["fetched_at"] > TTL_SECONDS


def cached_covid_stats() -> dict | None:
    return _cache["covid"]


def cached_disease_summary() -> dict | None:
    return _cache["summary"]


def _get_api_key_for_newsapi() -> str:
    """Read NewsAPI-style key from config if present; unused by this module
    but kept in case we want to branch on it later."""
    try:
        from . import config
        cfg_path = config.DATA_ROOT / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text("utf-8"))
            return (cfg or {}).get("newsapi_key", "")
    except Exception:
        pass
    return ""


def _fetch_covid_live() -> dict:
    resp = requests.get(DISEASE_SH_ALL, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return {
        "disease": "COVID-19",
        "cases": int(data.get("cases") or 0),
        "deaths": int(data.get("deaths") or 0),
        "recovered": int(data.get("recovered") or 0),
        "active": int(data.get("active") or 0),
        "critical": int(data.get("critical") or 0),
        "cases_per_million": int(data.get("casesPerOneMillion") or 0),
        "deaths_per_million": int(data.get("deathsPerOneMillion") or 0),
        "tests": int(data.get("tests") or 0),
        "updated": data.get("updated"),
        "severity": calculate_severity(int(data.get("critical") or 0), int(data.get("active") or 0)),
        "relevance_score": calculate_relevance(data),
    }


def fetch_covid_stats() -> dict:
    """Fetch global COVID-19 statistics (cached for 1 hour)."""
    if _cache["covid"] is not None and not _cache_expired():
        return _cache["covid"]
    try:
        data = _fetch_covid_live()
        _cache["covid"] = data
        _cache["summary"] = None
        _cache["fetched_at"] = time.monotonic()
        return data
    except requests.HTTPError as exc:
        print(f"COVID-19 fetch error: {exc}")
        if _cache["covid"] is not None:
            print("COVID-19: serving stale cached data.")
            return _cache["covid"]
        return {}


def fetch_flu_stats() -> dict:
    """CDC FluView endpoint is currently unstable; return empty gracefully.

    When CDC publishes a stable public JSON endpoint, add it here and the
    UI will start displaying flu data automatically (the frontend checks
    `Object.keys(flu).length > 0`).
    """
    return {}


def calculate_severity(critical: int, active: int) -> str:
    if critical > 10000 or active > 10000000:
        return "CRITICAL"
    elif critical > 5000 or active > 5000000:
        return "HIGH"
    elif critical > 1000 or active > 1000000:
        return "MEDIUM"
    return "LOW"


def calculate_relevance(data: dict) -> int:
    score = 0
    active = int(data.get("active") or 0)
    critical = int(data.get("critical") or 0)
    deaths = int(data.get("deaths") or 0)
    if active > 10000000:
        score += 40
    elif active > 5000000:
        score += 30
    elif active > 1000000:
        score += 20
    else:
        score += 10
    if critical > 10000:
        score += 30
    elif critical > 5000:
        score += 20
    elif critical > 1000:
        score += 10
    if deaths > 5000000:
        score += 30
    elif deaths > 1000000:
        score += 20
    elif deaths > 100000:
        score += 10
    return min(score, 100)


def get_disease_summary() -> dict:
    """Summarize current tracked diseases. COVID only for now."""
    if _cache["summary"] is not None and not _cache_expired():
        return _cache["summary"]
    covid = fetch_covid_stats()
    outbreaks = [covid] if covid and len(covid) > 0 else []
    if not outbreaks:
        return {"total": 0, "critical": 0, "high": 0, "diseases": [], "updated": datetime.now(timezone.utc).isoformat()}
    critical = sum(1 for o in outbreaks if o.get("severity") == "CRITICAL")
    high = sum(1 for o in outbreaks if o.get("severity") == "HIGH")
    diseases = [o.get("disease") for o in outbreaks if o.get("disease")]
    summary = {
        "total": len(outbreaks),
        "critical": critical,
        "high": high,
        "diseases": diseases,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    _cache["summary"] = summary
    return summary
