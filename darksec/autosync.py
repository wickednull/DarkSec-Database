"""Background online sync engine.

One daemon thread runs for the life of the app. When automatic sync is enabled,
the machine is online and OPSEC mode is off, it refreshes due public feeds in
small batches, warms the cached intel modules and records its state for the UI.

Offline operation never depends on this module: cached feeds stay readable and
the thread simply idles.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from . import feeds, tilecache
from .db import Database, utcnow

TICK_SECONDS = 15
MAX_FEEDS_PER_TICK = 4
MODULE_WARM_SECONDS = 2 * 3600

# Per-feed cadence in seconds. Deliberately conservative to respect upstream
# rate limits (OpenSky anonymous credit budget, NASA DEMO_KEY, NewsAPI free
# tier) and to avoid pointless traffic on slow-changing sources.
CADENCE = {
    "swpc_scales": 900,
    "swpc_kp": 900,
    "usgs_quakes": 600,
    "nws_alerts": 900,
    "gdacs": 1800,
    "eonet": 1800,
    "safecast": 1800,
    "smithsonian_volcano": 6 * 3600,
    "who_don": 6 * 3600,
    "reliefweb": 30 * 60,
    "open_meteo_air": 1800,
    "open_meteo_weather": 1800,
    "opensky_states": 15 * 60,
    "celestrak_tle": 6 * 3600,
    "bbc_world": 20 * 60,
    "guardian_world": 20 * 60,
    "france24_world": 20 * 60,
    "dw_news": 20 * 60,
    "aljazeera_world": 20 * 60,
    "bbc_middle_east": 15 * 60,
    "twz_defense": 20 * 60,
    "breaking_defense": 20 * 60,
    "defense_one": 20 * 60,
    "long_war_journal": 30 * 60,
    "war_on_the_rocks": 60 * 60,
    "crisisgroup": 6 * 3600,
    "gdelt_conflict": 15 * 60,
    "ucdp_candidate": 24 * 3600,
}

# Safety/health sources first when several are due at once.
PRIORITY = {
    "swpc_scales": 0, "swpc_kp": 1,
    "usgs_quakes": 2, "nws_alerts": 3,
    "gdacs": 4, "eonet": 5,
    "smithsonian_volcano": 6, "safecast": 7,
    "open_meteo_air": 8, "open_meteo_weather": 9,
    "who_don": 10, "reliefweb": 11,
    "gdelt_conflict": 12, "ucdp_candidate": 13,
    "bbc_middle_east": 14, "twz_defense": 15, "breaking_defense": 16, "defense_one": 17,
    "aljazeera_world": 24, "crisisgroup": 25,
    "bbc_world": 20, "guardian_world": 21, "france24_world": 22, "dw_news": 23,
    "long_war_journal": 26, "war_on_the_rocks": 27,
    "opensky_states": 30, "celestrak_tle": 31,
}


class SyncEngine:
    def __init__(self) -> None:
        self._db: Database | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._fail_counts: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        self._last_audit = 0.0
        self._module_next = 0.0
        self._force_since: float | None = None
        self._position_refresh_pending: set[str] = set()
        self._position_revision = 0
        self._state: dict = {
            "enabled": True,
            "opsec": False,
            "online": False,
            "running": False,
            "reason": "starting",
            "started": None,
            "last_cycle": None,
            "last_success": None,
            "last_error": None,
            "next_due": None,
            "refreshed": 0,
            "failed": 0,
            "interval_min": 10,
        }

    # ------------------------------------------------------------- lifecycle
    def start(self, db: Database) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._db = db
        self._stop.clear()
        try:
            row = db.one("SELECT value FROM meta WHERE key='sync_last_success'")
            if row and row["value"]:
                self._state["last_success"] = row["value"]
        except Exception:
            pass
        self._state["started"] = utcnow()
        self._state["reason"] = "starting"
        self._thread = threading.Thread(target=self._run, name="darksec-autosync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=4)
        self._thread = None

    def run_now(self) -> bool:
        """Wake the loop for an immediate, one-shot cycle.

        A manual request is explicit intent, so it also runs when automatic
        sync is switched off. OPSEC mode and a genuinely offline link still
        win, and the automatic setting is not changed.
        """
        self._force_since = time.time()
        self._wake.set()
        return True

    def position_changed(self) -> None:
        """Queue local-position feeds for refresh after a new reference/fix."""
        with self._state_lock:
            self._position_revision += 1
            self._position_refresh_pending.update({"open_meteo_air", "open_meteo_weather", "safecast"})
        self._wake.set()

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        # Give the API and UI a moment to finish starting.
        self._stop.wait(6)
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as exc:  # never let the thread die
                with self._state_lock:
                    self._state["last_error"] = str(exc)[:300]
                    self._state["reason"] = "error"
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def _cycle(self) -> None:
        db = self._db
        if db is None:
            return
        try:
            settings = db.all_settings()
        except Exception:
            settings = {}
        enabled = bool(settings.get("feed_auto_refresh", True))
        opsec = bool(settings.get("opsec_mode", False))
        try:
            interval = max(3, min(240, int(settings.get("feed_refresh_min") or 10)))
        except (TypeError, ValueError):
            interval = 10
        online = (not opsec) and tilecache.is_online()
        now = time.time()
        force_since = self._force_since
        if force_since is not None and now - force_since > 600:
            self._force_since = None
            force_since = None

        with self._state_lock:
            self._state.update({
                "enabled": enabled,
                "opsec": opsec,
                "online": online,
                "interval_min": interval,
                "running": False,
            })

        if opsec:
            self._set_reason("opsec")
            return
        if not enabled and force_since is None:
            self._set_reason("disabled")
            return
        if not online:
            self._set_reason("offline")
            return

        rows = db.query("SELECT * FROM feeds")
        due: list = []
        next_due_ts: float | None = None
        with self._state_lock:
            position_pending = set(self._position_refresh_pending)
            position_revision = self._position_revision
        if force_since is not None:
            # A manual SYNC NOW refreshes every feed that was not already
            # updated after the request, in small batches across ticks.
            for row in rows:
                if self._retry_after.get(row["id"], 0) > now:
                    continue
                fetched = row["fetched_at"]
                if not fetched:
                    due.append(row)
                    continue
                try:
                    ts = datetime.fromisoformat(fetched).timestamp()
                except (TypeError, ValueError):
                    due.append(row)
                    continue
                if ts < force_since:
                    due.append(row)
            if not due:
                self._force_since = None
        else:
            for row in rows:
                fid = row["id"]
                if self._retry_after.get(fid, 0) > now:
                    ts = self._retry_after[fid]
                    next_due_ts = ts if next_due_ts is None else min(next_due_ts, ts)
                    continue
                if fid in position_pending:
                    due.append(row)
                    continue
                cadence = CADENCE.get(fid, interval * 60)
                fetched = row["fetched_at"]
                if not fetched:
                    due.append(row)
                    continue
                try:
                    ts = datetime.fromisoformat(fetched).timestamp()
                except (TypeError, ValueError):
                    due.append(row)
                    continue
                due_at = ts + cadence
                if due_at <= now:
                    due.append(row)
                else:
                    next_due_ts = due_at if next_due_ts is None else min(next_due_ts, due_at)

        module_due = now >= self._module_next
        if not due and not module_due:
            self._set_reason("idle")
            self._set_next_due(next_due_ts)
            return

        with self._state_lock:
            self._state["running"] = True
            self._state["reason"] = ""
        ok = fail = 0

        due.sort(key=lambda r: (PRIORITY.get(r["id"], 50), r["fetched_at"] or ""))
        for row in due[:MAX_FEEDS_PER_TICK]:
            fid = row["id"]
            try:
                result = feeds.refresh(db, fid)
                item = result[0] if result else {}
                if item.get("status") == "OK":
                    ok += 1
                    self._fail_counts.pop(fid, None)
                    self._retry_after.pop(fid, None)
                    with self._state_lock:
                        if self._position_revision == position_revision:
                            self._position_refresh_pending.discard(fid)
                else:
                    fail += 1
                    self._backoff(fid)
                    with self._state_lock:
                        self._state["last_error"] = f"{fid}: {item.get('error') or item.get('status') or 'failed'}"[:300]
            except Exception as exc:
                fail += 1
                self._backoff(fid)
                with self._state_lock:
                    self._state["last_error"] = f"{fid}: {exc}"[:300]

        warmed = False
        if module_due:
            self._module_next = now + MODULE_WARM_SECONDS
            warmed = self._warm_modules()

        with self._state_lock:
            self._state["running"] = False
            self._state["refreshed"] = ok
            self._state["failed"] = fail
            self._state["last_cycle"] = utcnow()
            if ok or warmed:
                stamp = utcnow()
                self._state["last_success"] = stamp
                self._state["last_error"] = None
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO meta(key,value) VALUES('sync_last_success',?)",
                        (stamp,),
                    )
                except Exception:
                    pass
            self._state["reason"] = "ok" if (ok or warmed) else ("error" if fail else "idle")
        self._set_next_due(next_due_ts)

        if ok and now - self._last_audit > 1800:
            try:
                db.audit("AUTO_SYNC", f"refreshed={ok} failed={fail}")
            except Exception:
                pass
            self._last_audit = now

    def _warm_modules(self) -> bool:
        """Refresh the in-memory intel module caches (each has its own TTL)."""
        try:
            from . import nasa, newsapi, who_outbreaks
        except Exception:
            return False
        warmed = False
        for fn in (
            nasa.fetch_neo_objects,
            nasa.get_neo_summary,
            newsapi.fetch_conflict_news,
            newsapi.fetch_disaster_news,
            who_outbreaks.fetch_covid_stats,
            who_outbreaks.get_disease_summary,
        ):
            try:
                fn()
                warmed = True
            except Exception:
                continue
        return warmed

    def _backoff(self, fid: str) -> None:
        count = self._fail_counts.get(fid, 0) + 1
        self._fail_counts[fid] = count
        delay = min(3600, 60 * (2 ** min(count, 6)))
        self._retry_after[fid] = time.time() + delay

    def _set_reason(self, reason: str) -> None:
        with self._state_lock:
            self._state["reason"] = reason
            self._state["running"] = False
            self._state["next_due"] = None

    def _set_next_due(self, ts: float | None) -> None:
        with self._state_lock:
            self._state["next_due"] = (
                datetime.fromtimestamp(ts, tz=timezone.utc).replace(microsecond=0).isoformat()
                if ts else None
            )

    # --------------------------------------------------------------- reading
    def snapshot(self, db: Database | None = None) -> dict:
        with self._state_lock:
            state = dict(self._state)
        state["thread_alive"] = bool(self._thread and self._thread.is_alive())
        if db is not None:
            try:
                listing = feeds.list_feeds(db)
                state["feeds"] = [
                    {
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "status": f.get("status"),
                        "fetched_at": f.get("fetched_at"),
                        "age_seconds": f.get("age_seconds"),
                        "count": f.get("count"),
                        "error": f.get("error"),
                    }
                    for f in listing
                ]
                state["feed_total"] = len(listing)
                state["feed_ok"] = sum(1 for f in listing if f.get("status") == "OK")
                state["feed_stale"] = sum(1 for f in listing if f.get("status") == "STALE")
                state["critical_count"] = sum(
                    1 for f in listing for i in (f.get("items") or [])
                    if str(i.get("severity") or "INFO").upper() in ("CRITICAL", "SEVERE", "EXTREME")
                )
            except Exception as exc:
                state["feed_error"] = str(exc)[:200]
        return state


engine = SyncEngine()
