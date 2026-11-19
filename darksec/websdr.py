"""Public WebSDR / KiwiSDR directory and tuned links.

DarkSec does not proxy or record third-party receivers. It builds a local,
cached directory so an operator with no radio can open a public receiver tuned
to the selected frequency. KiwiSDR's directory mirror is fetched at most every
six hours; no per-user requests go to kiwisdr.com.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from . import config

MIRROR_URL = "https://cdn.aethersdr.com/kiwi.json"
CACHE_TTL = 6 * 3600
REFRESH_FLOOR = 60.0
USER_AGENT = "DarkSec-FieldConsole/2.0 (offline field console; public WebSDR directory)"

# Small, verified fallback set. The KiwiSDR mirror normally supplies hundreds
# more; these keep the panel useful when the network or mirror is unavailable.
CURATED = [
    {
        "id": "curated-twente",
        "kind": "websdr",
        "name": "University of Twente WebSDR",
        "loc": "Enschede, Netherlands",
        "url": "http://websdr.ewi.utwente.nl:8901/",
        "bands": [0, 30_000_000],
        "users": None,
        "users_max": None,
        "ext_api": None,
        "note": "Long-running wideband HF WebSDR. Browser use only.",
    },
    {
        "id": "curated-na5b",
        "kind": "websdr",
        "name": "NA5B WebSDR",
        "loc": "Washington, DC, USA",
        "url": "http://na5b.com:8901/",
        "bands": [0, 30_000_000],
        "users": None,
        "users_max": None,
        "ext_api": None,
        "note": "Public HF WebSDR. Browser use only.",
    },
]

_state = {"fetched_at": 0.0, "source": "curated", "error": None, "receivers": []}
_last_attempt = 0.0


def _cache_file() -> Path:
    return Path(config.DATA_DIR) / "websdr" / "kiwi.json"


def _load_cache() -> dict | None:
    path = _cache_file()
    try:
        if path.is_file() and time.time() - path.stat().st_mtime < CACHE_TTL * 2:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def _save_cache(payload: dict) -> None:
    path = _cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _valid_url(text: str) -> bool:
    try:
        parts = urlsplit(str(text or "").strip())
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def _normalise(raw: dict, kind: str = "kiwi") -> dict | None:
    url = str(raw.get("url") or "").strip().rstrip("/")
    if not _valid_url(url):
        return None
    if raw.get("flagged") is True:
        return None
    status = str(raw.get("status") or "active").lower()
    if status not in ("active", "online") or raw.get("offline") is True:
        return None
    bands = raw.get("bands") or [0, 30_000_000]
    try:
        bands = [int(bands[0]), int(bands[1])]
    except (TypeError, ValueError, IndexError):
        bands = [0, 30_000_000]
    try:
        ext_api = int(raw.get("ext_api")) if raw.get("ext_api") is not None else None
    except (TypeError, ValueError):
        ext_api = None
    return {
        "id": str(raw.get("id") or url),
        "kind": kind,
        "name": str(raw.get("name") or urlsplit(url).hostname or "WebSDR")[:120],
        "loc": str(raw.get("loc") or "").strip()[:120],
        "url": url,
        "bands": bands,
        "users": raw.get("users"),
        "users_max": raw.get("users_max"),
        "ext_api": ext_api,
        "api_allowed": bool(ext_api and ext_api > 0),
        "sdr_hw": str(raw.get("sdr_hw") or "")[:80],
        "gps": raw.get("gps") if isinstance(raw.get("gps"), list) and len(raw.get("gps")) == 2 else None,
        "snr": raw.get("snr"),
        "note": "Web-browser receiver. Respect the operator's user limit and terms.",
    }


def refresh(force: bool = False) -> dict:
    """Fetch the KiwiSDR mirror, with a hard floor between attempts."""
    global _last_attempt
    now = time.time()
    if not force and _state.get("receivers") and now - _state.get("fetched_at", 0) < CACHE_TTL:
        return _state
    if now - _last_attempt < REFRESH_FLOOR:
        return _state
    _last_attempt = now
    try:
        response = requests.get(MIRROR_URL, timeout=15,
                                headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        payload = response.json()
        if payload.get("schema") != 1 or not isinstance(payload.get("receivers"), list):
            raise ValueError("unexpected directory schema")
        receivers = []
        for raw in payload["receivers"]:
            item = _normalise(raw, "kiwi")
            if item:
                receivers.append(item)
        def snr_value(item):
            raw = item.get("snr")
            if isinstance(raw, list):
                return float(raw[0] or 0) if raw else 0.0
            try:
                return float(raw or 0)
            except (TypeError, ValueError):
                return 0.0

        receivers.sort(key=lambda r: (-snr_value(r), r.get("users") or 0))
        _state.update({
            "fetched_at": now,
            "source": MIRROR_URL,
            "error": None,
            "receivers": receivers[:600],
            "remote_count": payload.get("receiver_count"),
        })
        _save_cache({"schema": 1, "fetched_at": now, "source": MIRROR_URL,
                     "receivers": receivers[:600]})
    except Exception as exc:
        cached = _load_cache()
        if cached and isinstance(cached.get("receivers"), list):
            _state.update({"fetched_at": float(cached.get("fetched_at") or 0),
                           "source": cached.get("source") or MIRROR_URL,
                           "error": str(exc)[:240],
                           "receivers": [r for r in cached["receivers"] if isinstance(r, dict)]})
        else:
            _state.update({"error": str(exc)[:240], "source": "curated",
                           "receivers": []})
    return _state


def _band_name(bands: list[int]) -> str:
    lo, hi = (bands + [0, 30_000_000])[:2]
    if hi <= 30_000_000:
        return "HF"
    if lo >= 300_000_000:
        return "UHF"
    if lo >= 30_000_000:
        return "VHF"
    return "Wide"


def receivers(refresh_now: bool = False, query: str = "", band: str = "") -> dict:
    if refresh_now or not _state.get("receivers"):
        refresh(force=refresh_now)
    items = list(_state.get("receivers") or [])
    seen = {r.get("url") for r in items}
    for curated in CURATED:
        if curated["url"] not in seen:
            items.insert(0, dict(curated, api_allowed=None))
            seen.add(curated["url"])
    q = str(query or "").strip().lower()
    band = str(band or "").strip().upper()
    out = []
    for item in items:
        if not _valid_url(item.get("url", "")):
            continue
        item_band = _band_name(item.get("bands") or [0, 30_000_000])
        if band and band not in ("ALL", "ANY") and item_band != band:
            continue
        if q and q not in (str(item.get("name", "")) + " " + str(item.get("loc", "")) + " " + str(item.get("url", ""))).lower():
            continue
        out.append(dict(item, band=item_band))
    return {
        "receivers": out[:400],
        "count": len(out),
        "source": _state.get("source"),
        "fetched_at": _state.get("fetched_at"),
        "error": _state.get("error"),
        "stale": bool(_state.get("fetched_at") and time.time() - _state["fetched_at"] > CACHE_TTL),
        "note": ("Public receivers are run by volunteers. Use only their web interface, "
                 "respect user limits and do not automate connections."),
    }


def tune_url(receiver: dict, freq_hz: float, mode: str = "am") -> str:
    """Build a tuned receiver URL (KiwiSDR/WebSDR use ?f=<kHz><mode>)."""
    base = str(receiver.get("url") or "").strip()
    if not _valid_url(base):
        raise ValueError("Receiver URL is not a public http(s) URL")
    parts = urlsplit(base)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("f", "m")]
    khz = max(1, int(round(float(freq_hz) / 1000.0)))
    mode = str(mode or "am").lower()
    if mode not in ("am", "sam", "usb", "lsb", "cw", "nfm", "wfm"):
        mode = "am"
    pairs.append(("f", f"{khz}{mode}"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/",
                       urlencode(pairs), parts.fragment))
