"""PizzINT - Pentagon Pizza Index scraper.

Open-source "pizza intelligence": late-night pizza activity near the Pentagon
and other sensitive sites has long been used as an informal readiness signal.
This module scrapes the public pizzint.watch dashboard (no API available) and
returns a small, cacheable summary. It is advisory OSINT only.
"""
from __future__ import annotations

import re
import time
from typing import Any

import requests

URL = "https://www.pizzint.watch/"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
CACHE_TTL = 300
_cache: dict[str, Any] = {"ts": 0.0, "data": None}


def _scrape(html: str) -> dict[str, Any]:
    level = None
    m = re.search(r"DOUGHCON\s*(\d)", html)
    if m:
        level = int(m.group(1))
        idx = m.start()
    else:
        idx = html.find("DOUGHCON")

    status_parts = []
    if idx >= 0:
        seg = html[idx:idx + 400]
        for m in re.finditer(r"<span>([^<]{2,60})</span>", seg):
            txt = m.group(1).strip()
            if txt and txt not in status_parts and "DOUGHCON" not in txt:
                status_parts.append(txt)
    # Keep the two meaningful parts (e.g. "FADE OUT", "LOWEST STATE OF READINESS").
    status = " • ".join(status_parts) if status_parts else ""

    locations = []
    for m in re.finditer(r"([A-Z][A-Za-z0-9 '#&;\\-]{2,45}PIZZA[^<]{0,20})</h3>", html):
        name = " ".join(m.group(1).split())
        name = name.replace("&#x27;", "'").replace("&#39;", "'")
        start = m.start()
        chunk = html[start:start + 2500]
        st = None
        for w in ("NO DATA", "CLOSED", "OPEN", "BUSY", "LOADING TACTICAL DATA"):
            if w in chunk:
                st = w
                break
        dist = None
        dm = re.search(r"(\d+\.?\d*)\s*mi", chunk)
        if dm:
            dist = dm.group(0) + " mi" if "mi" not in dm.group(0) else dm.group(0)
        locations.append({"name": name, "status": st or "UNKNOWN", "distance": dist})

    # De-duplicate locations by name while preserving order.
    seen = set()
    uniq = []
    for loc in locations:
        if loc["name"] in seen:
            continue
        seen.add(loc["name"])
        uniq.append(loc)

    return {
        "source": "pizzint.watch",
        "fetched_at": None,
        "doughcon": level,
        "status": status,
        "locations": uniq,
        "note": "Advisory OSINT only. Pizza activity is not a verified military indicator.",
    }


def fetch(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    try:
        resp = requests.get(URL, headers=UA, timeout=20)
        resp.raise_for_status()
        data = _scrape(resp.text)
        data["fetched_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        data["error"] = None
        _cache.update(ts=now, data=data)
        return data
    except Exception as exc:
        data = _cache["data"] or {
            "source": "pizzint.watch",
            "doughcon": None,
            "status": "",
            "locations": [],
            "note": "Advisory OSINT only.",
        }
        data = dict(data)
        data["error"] = str(exc)[:200]
        data["fetched_at"] = None
        return data
