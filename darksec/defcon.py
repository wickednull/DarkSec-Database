"""DEFCON tracker - current alert status from defconlevel.com.

Official DEFCON levels are classified. defconlevel.com publishes an
OSINT-estimated global level plus per-combatant-command postures on its public
current-level page. This module scrapes that page (no documented API) and
returns a small, cacheable summary. It is advisory OSINT only.
"""
from __future__ import annotations

import html as html_lib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

URL = "https://www.defconlevel.com/current-level"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
CACHE_TTL = 300
TIMEOUT = 8
_cache: dict[str, Any] = {"ts": 0.0, "data": None}

_TAG_RE = re.compile(r"<[^>]+>")
_LEAD_PREFIX_RE = re.compile(r"^DEFCON\s*\d+\s*\([^)]*\)\s*:?\s*", re.I)
_DRIVER_PREFIX_RE = re.compile(r"^Primary driver:\s*", re.I)

NOTE = (
    "Official DEFCON levels are classified. This is defconlevel.com's public "
    "OSINT estimate, not a government-confirmed alert level."
)


def _text(fragment: str) -> str:
    """Strip tags and normalize whitespace/entities in an HTML fragment."""
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", fragment)).split())


def _first(pattern: str, page: str, flags: int = re.S) -> str | None:
    m = re.search(pattern, page, flags)
    return m.group(1).strip() if m else None


def _parse(page: str) -> dict[str, Any]:
    level_text = _first(r'<div class="current-badge level-(\d+)"', page)
    if level_text is None:
        level_text = _first(r'<span class="badge-number">DEFCON\s*(\d+)</span>', page)
    if level_text is None:
        level_text = _first(r'href="/defcon-level/(\d+)"', page)
    level = int(level_text) if level_text and level_text.isdigit() else None

    codename = _first(r'<span class="badge-codename">([^<]+)</span>', page) or ""
    codename = " ".join(html_lib.unescape(codename).split())
    updated = _first(r'<time datetime="([^"]+)"', page)

    lead_raw = _first(r'<p class="hero-lead">(.*?)</p>', page)
    summary = ""
    readiness = ""
    if lead_raw:
        lead = _LEAD_PREFIX_RE.sub("", _text(lead_raw)).strip()
        # The first sentence is the readiness descriptor (e.g. "heightened
        # readiness."); the rest is the situation summary.
        parts = re.split(r"(?<=\.)\s", lead, maxsplit=1)
        first_sentence = parts[0].strip()
        if first_sentence.endswith("."):
            first_sentence = first_sentence[:-1].strip()
        if 0 < len(first_sentence) <= 120:
            readiness = first_sentence
            summary = parts[1].strip() if len(parts) > 1 else ""
        else:
            summary = lead
        summary = re.sub(r"\s*Read more\s*\u2192?\s*$", "", summary, flags=re.I).strip()

    driver_raw = _first(r'<p class="hero-driver">(.*?)</p>', page)
    driver = _DRIVER_PREFIX_RE.sub("", _text(driver_raw)) if driver_raw else ""

    raised = total = None
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+commands currently raised", page, re.I)
    if m:
        raised, total = int(m.group(1)), int(m.group(2))

    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slug, chunk in re.findall(
        r'<a href="/commands/([^"]+)" class="command-card">(.*?)</a>', page, re.S
    ):
        alt = _first(r'alt="([^"]+)"', chunk) or ""
        lm = re.search(r"DEFCON\s*(\d+)", alt, re.I)
        if lm is None:
            lm = re.search(r"level-indicator--(\d)", chunk)
        name = _first(r"<h3>([^<]+)</h3>", chunk) or (alt.split(" - ")[0] if alt else slug)
        description = _first(r"<p>([^<]+)</p>", chunk) or ""
        name = " ".join(html_lib.unescape(name).split())
        description = " ".join(html_lib.unescape(description).split())
        if name in seen:
            continue
        seen.add(name)
        commands.append({
            "slug": slug,
            "name": name,
            "description": description,
            "level": int(lm.group(1)) if lm else None,
        })

    return {
        "source": "defconlevel.com",
        "url": URL,
        "level": level,
        "codename": codename,
        "readiness": readiness,
        "summary": summary,
        "driver": driver,
        "updated": updated,
        "commands": commands,
        "commands_raised": raised,
        "commands_total": total,
        "note": NOTE,
    }


def fetch(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]
    try:
        resp = requests.get(URL, headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        data = _parse(resp.text)
        if data["level"] is None:
            raise ValueError("DEFCON level not found in page")
        data["fetched_at"] = datetime.now(timezone.utc).isoformat()
        data["error"] = None
        data["stale"] = False
        _cache.update(ts=now, data=data)
        return data
    except Exception as exc:
        cached = _cache["data"]
        data = dict(cached) if cached else {
            "source": "defconlevel.com",
            "url": URL,
            "level": None,
            "codename": "",
            "readiness": "",
            "summary": "",
            "driver": "",
            "updated": None,
            "commands": [],
            "commands_raised": None,
            "commands_total": None,
            "note": NOTE,
            "fetched_at": None,
        }
        data["error"] = str(exc)[:200]
        data["stale"] = bool(cached)
        return data
