"""Trip-plan deadlines and UTC parsing helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def trip_items(db: Database) -> list[dict]:
    """Return trip records with local check-in/return deadlines attached."""
    now = datetime.now(timezone.utc)
    rows = db.query(
        "SELECT t.*, s.name AS start_waypoint_name, e.name AS end_waypoint_name "
        "FROM trip_plans t "
        "LEFT JOIN waypoints s ON s.id=t.start_waypoint_id "
        "LEFT JOIN waypoints e ON e.id=t.end_waypoint_id "
        "ORDER BY CASE t.status WHEN 'ACTIVE' THEN 0 WHEN 'PLANNED' THEN 1 ELSE 2 END, t.departure_at DESC"
    )
    out = []
    for row in rows:
        item = dict(row)
        item["alert"] = item["status"]
        item["alert_level"] = "muted"
        item["next_checkin_at"] = None
        item["minutes_to_checkin"] = None
        item["minutes_overdue"] = 0
        if item["status"] == "ACTIVE":
            try:
                interval = int(item.get("checkin_interval_min") or 60)
            except (TypeError, ValueError):
                interval = 60
            interval = max(5, min(1440, interval))
            last = parse_utc(item.get("last_checkin_at") or item.get("started_at"))
            next_checkin = last + timedelta(minutes=interval) if last else None
            return_at = parse_utc(item.get("return_at"))
            if next_checkin:
                item["next_checkin_at"] = next_checkin.isoformat(timespec="seconds")
                delta_min = (next_checkin - now).total_seconds() / 60
                item["minutes_to_checkin"] = int(delta_min) if delta_min >= 0 else None
                item["minutes_overdue"] = max(0, int(-delta_min))
            if return_at and now > return_at:
                item["alert"] = "RETURN OVERDUE"
                item["alert_level"] = "red"
                item["return_minutes_overdue"] = max(0, int((now - return_at).total_seconds() / 60))
            elif next_checkin and now > next_checkin:
                item["alert"] = "CHECK-IN OVERDUE"
                item["alert_level"] = "amber"
            else:
                item["alert"] = "ON TRACK"
                item["alert_level"] = "green"
        elif item["status"] == "PLANNED":
            departure = parse_utc(item.get("departure_at"))
            if departure and departure < now:
                item["alert"] = "DEPARTURE PASSED"
                item["alert_level"] = "amber"
        out.append(item)
    return out
