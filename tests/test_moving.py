"""Moving-target support: satellites need a ground velocity for animation."""
from __future__ import annotations

import json

TLE = {
    "norad": 25544,
    "name": "ISS (ZARYA)",
    "line1": "1 25544U 98067A   26322.30200954  .00012483  00000+0  23055-3 0  9993",
    "line2": "2 25544  51.6316 257.4339 0007387  38.9132 321.2387 15.49454822595647",
}


def test_satellite_snapshot_has_ground_velocity(tmp_path):
    from darksec import intel
    from darksec.db import Database

    db = Database(str(tmp_path / "sat.db"))
    db.execute("UPDATE feeds SET payload=? WHERE id='celestrak_tle'",
               (json.dumps({"items": [TLE]}),))
    items = intel.live_satellites(db, 39.8, -98.5, limit=5, visible_only=False)
    assert items, "expected the ISS to propagate"
    sat = items[0]
    assert sat["norad"] == 25544
    assert -85 <= sat["lat"] <= 85 and -180 <= sat["lon"] <= 180
    assert 20_000 <= sat["speed_kmh"] <= 30_000
    assert 0 <= sat["heading_deg"] < 360


def test_moving_endpoint_shape(client):
    out = client.get("/api/map/moving").json()
    assert "generated_at" in out
    assert isinstance(out["aircraft"], list)
    assert isinstance(out["satellites"], list)
