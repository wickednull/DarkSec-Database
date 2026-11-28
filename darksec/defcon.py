"""DEFCON tracker - alert status and command readiness from defconlevel.com.

Official DEFCON levels are classified. defconlevel.com publishes an
OSINT-estimated global level, per-command Readiness Alerts, a rapid-alert
stream and market signals on its public pages. This module scrapes those pages
(no documented API) and returns a small, cacheable intelligence summary. It is
advisory OSINT only.

The site moved its command cards from "DEFCON n" image alt text to
"Readiness Alert: n (Label)" in late 2026; both shapes are parsed so the
CENTCOM/EUCOM/INDOPACOM rows do not silently disappear again.
"""
from __future__ import annotations

import html as html_lib
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

URL = "https://www.defconlevel.com/current-level"
COMMANDS_URL = "https://www.defconlevel.com/commands"
SITE = "https://www.defconlevel.com"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.defconlevel.com/",
}
CACHE_TTL = 300
RETRY_TTL = 60
COMMAND_TTL = 6 * 3600
TIMEOUT = 8
_cache: dict[str, Any] = {"ts": 0.0, "attempt": 0.0, "data": None, "failure": None}
_command_cache: dict[str, Any] = {"ts": 0.0, "data": {}}

_TAG_RE = re.compile(r"<[^>]+>")
_LEAD_PREFIX_RE = re.compile(r"^DEFCON\s*\d+\s*\([^)]*\)\s*:?\s*", re.I)
_DRIVER_PREFIX_RE = re.compile(r"^Primary driver:\s*", re.I)

NOTE = (
    "Official DEFCON levels are classified. This is defconlevel.com's public "
    "OSINT estimate, not a government-confirmed alert level. Readiness Alerts "
    "are the site's own per-command estimates, separate from DEFCON."
)

READINESS_SCALE = [
    {"level": 5, "label": "Normal", "definition": "Routine conditions, with no unusual activity reported in this area."},
    {"level": 4, "label": "Watch", "definition": "Rising tension or unusual activity is being watched closely."},
    {"level": 3, "label": "Elevated", "definition": "Serious incidents are under way in part of this area."},
    {"level": 2, "label": "High", "definition": "Sustained, serious activity is threatening U.S. or allied interests in this area."},
    {"level": 1, "label": "Severe", "definition": "The most serious activity this scale tracks is under way in this area."},
]
READINESS_LABELS = {row["level"]: row["label"] for row in READINESS_SCALE}

GEOGRAPHIC_SLUGS = {"africom", "centcom", "eucom", "indopacom", "northcom", "southcom"}
MONITORING_SLUGS = {"bio-command", "disaster-command", "fin-command"}


def _text(fragment: str) -> str:
    """Strip tags and normalize whitespace/entities in an HTML fragment."""
    return " ".join(html_lib.unescape(_TAG_RE.sub(" ", fragment)).split())


def _first(pattern: str, page: str, flags: int = re.S) -> str | None:
    m = re.search(pattern, page, flags)
    return m.group(1).strip() if m else None


def _abs_url(href: str | None) -> str:
    if not href:
        return ""
    href = html_lib.unescape(href).strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return SITE + href if href.startswith("/") else f"{SITE}/{href}"


def _command_category(slug: str) -> str:
    if slug in MONITORING_SLUGS:
        return "monitoring"
    if slug in GEOGRAPHIC_SLUGS:
        return "geographic"
    return "functional"


def _level_label(level: int | None, label: str | None = None) -> str:
    if label and label.strip():
        return label.strip()
    return READINESS_LABELS.get(level, "") if level else ""


def _parse_level(chunk: str, alt: str = "") -> tuple[int | None, str]:
    """Extract a command readiness level from any of the site's card shapes."""
    # Current cards: alt="CENTCOM Readiness Alert: 2 (High)".
    m = re.search(r"Readiness Alert:\s*(\d)\s*(?:\(([^)]+)\))?", alt, re.I)
    if m:
        return int(m.group(1)), _level_label(int(m.group(1)), m.group(2))
    # Older cards: alt="CENTCOM - DEFCON 2".
    m = re.search(r"DEFCON\s*(\d)", alt, re.I)
    if m:
        return int(m.group(1)), ""
    # Badge markup on the full commands page: data-readiness-level="2" / 2 (High).
    m = re.search(r'data-readiness-level="(\d)"', chunk)
    if m:
        label = _first(r'data-readiness-level="\d"[^>]*>\s*\d\s*\(([^)]+)\)', chunk) or ""
        return int(m.group(1)), _level_label(int(m.group(1)), label)
    m = re.search(r"badge--level-(\d)", chunk)
    if m:
        label = _first(r"badge--level-\d[^>]*>\s*\d\s*\(([^)]+)\)", chunk) or ""
        return int(m.group(1)), _level_label(int(m.group(1)), label)
    m = re.search(r"level-indicator--(\d)", chunk)
    if m:
        return int(m.group(1)), ""
    return None, ""


def _parse_commands(page: str) -> list[dict[str, Any]]:
    """Parse command cards from the current-level or /commands page."""
    commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for slug, chunk in re.findall(
        r'<a href="/commands/([^"]+)"[^>]*class="command-card"[^>]*>(.*?)</a>', page, re.S
    ):
        slug = html_lib.unescape(slug).strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        alt = _first(r'alt="([^"]+)"', chunk) or ""
        level, label = _parse_level(chunk, alt)
        name = (
            _first(r'<h3 class="command-card__name">([^<]+)</h3>', chunk)
            or _first(r"<h3>([^<]+)</h3>", chunk)
            or (re.split(r"\s+(?:Readiness Alert|DEFCON)\s*[:\-]", alt, maxsplit=1, flags=re.I)[0] if alt else slug)
        )
        descs = [
            _text(item) for item in re.findall(
                r'<p class="command-card__desc(?: command-card__fullname)?"[^>]*>(.*?)</p>',
                chunk, re.S,
            )
        ]
        if not descs:
            descs = [_text(item) for item in re.findall(r"<p(?:\s[^>]*)?>(.*?)</p>", chunk, re.S)]
        descs = [d for d in descs if d]
        name = " ".join(html_lib.unescape(name).split())
        description = descs[0] if descs else ""
        area = descs[1] if len(descs) > 1 else ""
        category = _command_category(slug)
        commands.append({
            "slug": slug,
            "name": name,
            "description": description,
            "level": level,
            "label": label,
            "area": area or "",
            "category": category,
            "monitoring": category == "monitoring",
            "url": _abs_url(f"/commands/{slug}"),
        })
    return commands


def _parse_alerts(page: str) -> list[dict[str, Any]]:
    """Parse the livebox alert rows embedded in the current-level page."""
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for block in re.findall(r'(<li[^>]*class="livebox__row[^"]*"[^>]*>.*?</li>)', page, re.S):
        opening = block[: block.find(">") + 1]
        alert_id = _first(r'data-alert-id="([^"]+)"', opening)
        if not alert_id or alert_id in seen:
            continue
        seen.add(alert_id)
        title = _first(r'class="livebox__title"[^>]*>(.*?)</a>', block)
        href = _first(r'class="livebox__title"[^>]*href="([^"]+)"', block) or _first(r'href="([^"]+)"[^>]*class="livebox__title"', block)
        category = _first(r'data-category="([^"]*)"', opening) or _first(r'class="livebox__cat"[^>]*>(.*?)</a>', block) or ""
        cat_key = _first(r'data-cat-key="([^"]*)"', opening) or ""
        published = _first(r'data-published="([^"]*)"', opening) or _first(r'<time[^>]*datetime="([^"]+)"', block) or ""
        priority = _first(r"livebox__row[^\"]*rapid-level--([a-z]+)", opening) or _first(r'data-priority="([^"]*)"', opening) or "routine"
        title = _text(title) if title else ""
        if not title:
            continue
        alerts.append({
            "id": alert_id,
            "title": title,
            "category": _text(category),
            "category_key": html_lib.unescape(cat_key),
            "priority": priority,
            "level": priority,
            "published_at": html_lib.unescape(published),
            "url": _abs_url(href),
        })
    return alerts


def _parse_alert_counts(page: str) -> list[dict[str, Any]]:
    counts: list[dict[str, Any]] = []
    for opening, chunk in re.findall(
        r'(<a[^>]*class="livebox__chip"[^>]*>)(.*?)</a>', page, re.S
    ):
        key = _first(r'data-cat-key="([^"]*)"', opening)
        label = _first(r"^(.*?)\s*<span", chunk, re.S) or chunk
        count = _first(r'class="livebox__chip-n"[^>]*>\s*([\d,]+)', chunk)
        if key and count:
            counts.append({
                "key": html_lib.unescape(key),
                "label": _text(label),
                "count": int(count.replace(",", "")),
                "url": _abs_url(_first(r'href="([^"]+)"', opening) or ""),
            })
    return counts


def _parse_market_signals(page: str) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, chunk in re.findall(
        r'<a href="([^"]+)" class="hero-signals__pill">(.*?)</a>', page, re.S
    ):
        label = _first(r'class="hero-signals__label"[^>]*>([^<]+)', chunk)
        value = _first(r'data-live-price="[^"]*"[^>]*>([^<]+)', chunk)
        trend = _first(r"hero-signals__trend--(up|down)", chunk) or "flat"
        if label and value:
            label, value = _text(label), _text(value)
            key = f"{label}|{value}"
            if key in seen:
                continue
            seen.add(key)
            signals.append({
                "label": label,
                "value": value,
                "trend": trend,
                "url": _abs_url(href),
            })
    return signals


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

    # The site's own "Estimate last changed" date answers "has the level moved?"
    # independently of the page's rolling update timestamp.
    changed = _first(r"<dt>\s*Estimate last changed\s*</dt>\s*<dd>\s*([^<]+?)\s*</dd>", page)
    changed = " ".join(html_lib.unescape(changed).split()) if changed else ""

    nuclear = _first(r"nuclear risk level is\s*(\d)", page)
    nuclear_risk = int(nuclear) if nuclear else None

    raised = total = None
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+commands currently raised", page, re.I)
    if m:
        raised, total = int(m.group(1)), int(m.group(2))

    commands = _parse_commands(page)
    alerts = _parse_alerts(page)
    alert_counts = _parse_alert_counts(page)
    markets = _parse_market_signals(page)

    alert_total = None
    m = re.search(r"Rapid Alerts:\s*([\d,]+)\s+in the last 24 hours", page, re.I)
    if m:
        alert_total = int(m.group(1).replace(",", ""))
    if alert_total is None:
        m = re.search(r"of\s+([\d,]+)\s+from the last 24 hours", page, re.I)
        if m:
            alert_total = int(m.group(1).replace(",", ""))
    alerts_updated = _first(r'data-livebox-updated\s+data-at="([^"]+)"', page) or _first(
        r'class="livebox__updated"[^>]*data-at="([^"]+)"', page
    )

    if not commands:
        # A page shape change must not make the UI claim there are no commands.
        commands = []

    command_summary = {
        "total": len(commands),
        "raised": raised if raised is not None else sum(
            1 for c in commands if c.get("level") is not None and c["level"] <= 3
        ),
        "high": sum(1 for c in commands if c.get("level") == 2),
        "severe": sum(1 for c in commands if c.get("level") == 1),
        "elevated": sum(1 for c in commands if c.get("level") == 3),
        "watch": sum(1 for c in commands if c.get("level") == 4),
        "normal": sum(1 for c in commands if c.get("level") == 5),
        "monitoring": sum(1 for c in commands if c.get("monitoring")),
    }

    return {
        "source": "defconlevel.com",
        "url": URL,
        "level": level,
        "codename": codename,
        "readiness": readiness,
        "summary": summary,
        "driver": driver,
        "updated": updated,
        "changed": changed,
        "nuclear_risk": nuclear_risk,
        "commands": commands,
        "commands_raised": raised if raised is not None else command_summary["raised"],
        "commands_total": total if total is not None else len(commands),
        "command_summary": command_summary,
        "alerts": alerts,
        "alert_counts": alert_counts,
        "alerts_total_24h": alert_total,
        "alerts_updated": alerts_updated,
        "market_signals": markets,
        "readiness_scale": READINESS_SCALE,
        "note": NOTE,
    }


def _parse_commands_page(page: str) -> dict[str, dict[str, Any]]:
    """Return command details keyed by slug, for enriching the current page."""
    details: dict[str, dict[str, Any]] = {}
    for command in _parse_commands(page):
        details[command["slug"]] = command
    return details


def _enrich_commands(data: dict[str, Any], force: bool = False) -> None:
    """Add area-of-responsibility text from the /commands page.

    The AOR text only changes when the site edits a command page, so it is
    cached separately for ``COMMAND_TTL``. A failure leaves the current-level
    cards intact rather than failing the whole tracker.
    """
    now = time.time()
    cached = _command_cache.get("data") or {}
    if not force and cached and now - _command_cache.get("ts", 0.0) < COMMAND_TTL:
        details = cached
    else:
        try:
            resp = requests.get(COMMANDS_URL, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
            page = resp.text
            if resp.headers.get("cf-mitigated") or "<title>Just a moment..." in page:
                raise ValueError("defconlevel.com returned a bot challenge")
            details = _parse_commands_page(page)
            _command_cache.update(ts=now, data=details)
        except Exception:
            details = cached
    for command in data.get("commands") or []:
        extra = details.get(command.get("slug"))
        if not extra:
            continue
        if extra.get("area"):
            command["area"] = extra["area"]
        if extra.get("description") and not command.get("description"):
            command["description"] = extra["description"]
        if command.get("level") is None and extra.get("level") is not None:
            command["level"] = extra["level"]
            command["label"] = _level_label(extra["level"], extra.get("label"))


def _empty_data() -> dict[str, Any]:
    return {
        "source": "defconlevel.com",
        "url": URL,
        "level": None,
        "codename": "",
        "readiness": "",
        "summary": "",
        "driver": "",
        "updated": None,
        "changed": "",
        "nuclear_risk": None,
        "commands": [],
        "commands_raised": None,
        "commands_total": None,
        "command_summary": {},
        "alerts": [],
        "alert_counts": [],
        "alerts_total_24h": None,
        "alerts_updated": None,
        "market_signals": [],
        "readiness_scale": READINESS_SCALE,
        "note": NOTE,
        "fetched_at": None,
    }


def _failure(exc: Exception) -> dict[str, Any]:
    cached = _cache.get("data")
    data = dict(cached) if cached else _empty_data()
    data["error"] = str(exc)[:200]
    data["stale"] = bool(cached)
    data["cache_age_s"] = int(max(0, time.time() - _cache["ts"])) if cached else None
    return data


def fetch(force: bool = False) -> dict[str, Any]:
    """Return the current estimate, marking cached data clearly when stale.

    A 5-minute success cache keeps the status poll light. Failures back off for
    ``RETRY_TTL`` seconds so an unreachable site is not retried on every 30 s
    status refresh; a manual refresh (``force=True``) always tries now.
    """
    now = time.time()
    if not force:
        if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
        failure = _cache.get("failure")
        if failure and now - _cache.get("attempt", 0.0) < RETRY_TTL:
            return dict(failure)
    try:
        resp = requests.get(URL, headers=UA, timeout=TIMEOUT)
        resp.raise_for_status()
        page = resp.text
        if resp.headers.get("cf-mitigated") or "<title>Just a moment..." in page:
            raise ValueError("defconlevel.com returned a bot challenge; try again later")
        data = _parse(page)
        if data["level"] is None:
            raise ValueError("DEFCON level not found in page")
        _enrich_commands(data, force=force)
        data["fetched_at"] = datetime.now(timezone.utc).isoformat()
        data["error"] = None
        data["stale"] = False
        data["cache_age_s"] = 0
        _cache.update(ts=now, attempt=now, data=data, failure=None)
        return data
    except Exception as exc:
        failure = _failure(exc)
        _cache.update(attempt=now, failure=failure)
        return failure
