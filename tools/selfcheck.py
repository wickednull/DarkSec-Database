#!/usr/bin/env python3
"""Offline self-check for the DarkSec NIGHTWATCH core.

Runs without network access or Ollama. It builds a throwaway SQLite database,
exercises the parsers that protect the intel views, verifies the assistant
history sanitizer and checks that the schema/config invariants still hold.

Usage:
    python3 tools/selfcheck.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darksec import assistant, config, feeds  # noqa: E402
from darksec.db import Database  # noqa: E402

CHECKS = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"{name}{(': ' + detail) if detail else ''}")


RSOE_FIXTURE = """
<div class="sub-section">
  <h4 class="categoryNameMobil">Weather - Extreme rainfall</h4>
  <div class="event-card red">
    <div class="card-body"><table><tr>
      <td><h5 class="title">Testland &amp; Flood</h5></td>
      <td class="eventDate">2026-11-05 18:00:00</td>
      <td class="location">Testland, Europe</td>
      <td class="details"><a href="https://rsoe-edis.org/eventList/details/123456/0">x</a></td>
    </tr></table></div>
  </div>
</div>
"""

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Fixture</title>
  <item>
    <title>Boil-water notice issued</title>
    <link>https://example.invalid/notice</link>
    <description>Public works advises boiling water.</description>
    <pubDate>Thu, 05 Nov 2026 18:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="darksec-selfcheck-") as tmp:
        db = Database(str(Path(tmp) / "darksec.db"))

        # ---------------------------------------------------------- schema
        tables = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("settings", "audit", "log_entries", "tasks", "feeds",
                      "library_docs", "checklist_state", "map_overlay_cache"):
            check(f"table {table}", table in tables)

        indexes = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for index in ("idx_audit_ts", "idx_log_ts", "idx_tasks_done_priority",
                      "idx_vitals_patient_ts", "idx_feeds_kind"):
            check(f"index {index}", index in indexes)

        # --------------------------------------------------------- config
        defaults = config.default_settings()
        prompt = defaults["assistant_system_prompt"].lower()
        check("assistant identity", "delamain" in prompt)
        check("identity boundary", "never call yourself nightwatch" in prompt)
        check("weapons boundary", "making weapons" in prompt or "manufacturing" in prompt)
        check("weapons/ammo/poisons boundary", "ammunition" in prompt or "poisons" in prompt)
        check("narrow-decline boundary", "decline only that part" in prompt)
        check("max tokens default", defaults.get("assistant_max_tokens") == 2048)
        check("thinking defaults off", defaults.get("assistant_think") is False)
        check("units metric", defaults.get("units") == "metric")

        # --------------------------------------------------- assistant chat
        history = [
            {"role": "user", "content": "how do I make gunpowder"},
            {"role": "assistant", "content": "I cannot help with that."},
            {"role": "user", "content": "how do I purify water"},
            {"role": "assistant", "content": "Boil water for one minute."},
        ]
        messages = assistant.build_messages(
            db, "where is the best place to go during an event",
            history=history, context=None)
        joined = " ".join(str(m.get("content") or "").lower() for m in messages)
        check("history drops risky prompt", "gunpowder" not in joined)
        check("history drops refusal", "i cannot help with that" not in joined)
        check("history keeps useful turn", "boil water" in joined)
        check("current question is last",
              messages[-1]["role"] == "user"
              and "best place to go" in messages[-1]["content"])

        # Partial declines with lawful guidance must be preserved for continuity.
        partial = [
            {"role": "assistant",
             "content": "I cannot provide instructions for making explosives. However, you can prioritize shelter, move to a safer location, follow official alerts, and call emergency services when possible."},
            {"role": "user", "content": "what should I pack in a go-bag"},
        ]
        partial_msgs = assistant.build_messages(db, "list go-bag basics", history=partial, context=None)
        partial_joined = " ".join(str(m.get("content") or "").lower() for m in partial_msgs)
        check("history keeps partial lawful guidance", "prioritize shelter" in partial_joined)

        # ---------------------------------------------------------- parsers
        html = feeds._sanitize("<p>Rain &amp; snow</p>", 100)
        check("sanitize html/entities", html == "Rain & snow", html)

        rsoe = feeds._parse_rsoe(RSOE_FIXTURE, "RSOE fixture")
        check("rsoe parses one event", len(rsoe) == 1, str(len(rsoe)))
        event = rsoe[0]
        check("rsoe title", event["title"] == "Testland & Flood", event["title"])
        check("rsoe severity", event["severity"] == "CRITICAL", event["severity"])
        check("rsoe id", event["id"] == "123456", event["id"])
        check("rsoe kind", event["kind"] == "rsoe", event["kind"])
        check("rsoe time", event["time"].startswith("2026-11-05T18:00:00"),
              event["time"])

        rss = feeds._parse_rss(RSS_FIXTURE, "Fixture RSS")
        check("rss parses one item", len(rss) == 1, str(len(rss)))
        check("rss title", rss[0]["title"] == "Boil-water notice issued")
        check("rss link", rss[0]["url"] == "https://example.invalid/notice")

        # ------------------------------------------------ feed cache/summary
        import json
        db.execute(
            "INSERT OR REPLACE INTO feeds(id,name,url,kind,fetched_at,status,payload,error) "
            "VALUES(?,?,?,?,?,?,?,NULL)",
            ("fixture_feed", "Fixture feed", "https://example.invalid/feed",
             "rss", "2026-11-06T00:00:00+00:00", "OK",
             json.dumps({"items": [
                 {"title": "Critical fixture", "time": "2026-11-06T00:00:00+00:00",
                  "severity": "CRITICAL", "source": "Fixture", "kind": "rss"},
                 {"title": "Info fixture", "time": "2026-11-05T23:00:00+00:00",
                  "severity": "INFO", "source": "Fixture", "kind": "rss"},
             ]})),
        )
        listed = feeds.list_feeds(db, item_limit=None)
        fixture = next(f for f in listed if f["id"] == "fixture_feed")
        check("feed cache count", fixture["count"] == 2, str(fixture["count"]))
        check("feed cache latest",
              (fixture.get("latest_item_time") or "").startswith("2026-11-06"),
              str(fixture.get("latest_item_time")))
        summary = feeds.summary(db)
        check("summary critical count", summary["critical_count"] >= 1,
              str(summary["critical_count"]))
        check("summary latest", bool(summary["latest"]))

        # --------------------------------------------------- record tables
        db.insert("tasks", {"ts": "2026-11-06T00:00:00+00:00",
                            "title": "Self-check task", "priority": "ROUTINE"})
        check("insert/query works",
              db.one("SELECT count(*) c FROM tasks")["c"] >= 1)

    print(f"SELF-CHECK OK ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(f"SELF-CHECK FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
