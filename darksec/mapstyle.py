"""MapLibre style for offline OpenMapTiles-schema vector MBTiles.

The archive carries geometry only; labels need a glyph pack, so this style
renders land, water, boundaries, roads and buildings without text. Colours
follow the active light/dark scheme. Tile URLs stay relative so the browser
resolves them against the DarkSec origin.
"""
from __future__ import annotations

from typing import Any

_DARK = {
    "bg": "#050a07",
    "land": "#0a140e",
    "wood": "#0c1f14",
    "grass": "#0d2013",
    "sand": "#1d1d12",
    "ice": "#12222a",
    "wetland": "#0a1c19",
    "residential": "#0b120e",
    "commercial": "#121712",
    "industrial": "#141410",
    "park": "#0c2416",
    "water": "#07222f",
    "waterway": "#0a3342",
    "boundary": "#2b4a39",
    "road": "#39463c",
    "motorway": "#6d5f36",
    "rail": "#3a3a3a",
    "building": "#111a14",
    "aeroway": "#26302a",
}

_LIGHT = {
    "bg": "#e9e5d9",
    "land": "#eeeade",
    "wood": "#cfe0c4",
    "grass": "#d9e6ca",
    "sand": "#e8dfc2",
    "ice": "#dfeef2",
    "wetland": "#cfe3da",
    "residential": "#e6e1d5",
    "commercial": "#eadfd2",
    "industrial": "#e3ded4",
    "park": "#cfe3c2",
    "water": "#b7d6e2",
    "waterway": "#a8ccda",
    "boundary": "#9aa89c",
    "road": "#b9b2a4",
    "motorway": "#d3b978",
    "rail": "#a9a49a",
    "building": "#ded8ca",
    "aeroway": "#cfd3cc",
}


def _color(theme: str) -> dict[str, str]:
    return _LIGHT if str(theme or "").lower() == "light" else _DARK


def _fill(layer_id: str, source_layer: str, color: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": layer_id,
        "type": "fill",
        "source": "omt",
        "source-layer": source_layer,
        "paint": {"fill-color": color},
    }
    out.update(extra)
    return out


def _line(layer_id: str, source_layer: str, color: str, width: float, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": layer_id,
        "type": "line",
        "source": "omt",
        "source-layer": source_layer,
        "paint": {"line-color": color, "line-width": width},
    }
    out.update(extra)
    return out


def _match(layer_id: str, source_layer: str, classes: list[str], color: str, **extra: Any) -> dict[str, Any]:
    return _fill(layer_id, source_layer, color,
                 filter=["match", ["get", "class"], classes, True, False], **extra)


def vector_style(*, theme: str = "dark", minzoom: int | None = 0,
                 maxzoom: int | None = 14, attribution: str = "") -> dict[str, Any]:
    """Build a MapLibre style for an OpenMapTiles vector archive."""
    c = _color(theme)
    lo = max(0, int(minzoom or 0))
    hi = max(lo, int(maxzoom or 14))
    layers: list[dict[str, Any]] = [
        {"id": "background", "type": "background",
         "paint": {"background-color": c["bg"]}},
        _fill("land", "landcover", c["land"]),
        _match("landcover-wood", "landcover", ["wood", "forest"], c["wood"]),
        _match("landcover-grass", "landcover", ["grass", "meadow", "scrub"], c["grass"]),
        _match("landcover-sand", "landcover", ["sand", "beach"], c["sand"]),
        _match("landcover-ice", "landcover", ["ice", "glacier"], c["ice"]),
        _match("landcover-wetland", "landcover", ["wetland", "marsh", "bog"], c["wetland"]),
        _match("landuse-residential", "landuse", ["residential", "suburb", "neighbourhood"], c["residential"]),
        _match("landuse-commercial", "landuse", ["commercial", "retail"], c["commercial"]),
        _match("landuse-industrial", "landuse", ["industrial", "quarry", "railway"], c["industrial"]),
        _fill("park", "park", c["park"]),
        _fill("water", "water", c["water"]),
        _line("waterway", "waterway", c["waterway"], 1.0),
        _line("boundary", "boundary", c["boundary"], 0.8,
              filter=["match", ["get", "admin_level"], [0, 1, 2, 3, 4], True, False]),
        _line("transportation", "transportation", c["road"], 0.8,
              filter=["match", ["get", "class"], ["rail", "transit"], False, True]),
        _line("transportation-rail", "transportation", c["rail"], 0.8,
              filter=["match", ["get", "class"], ["rail", "transit"], True, False]),
        _line("transportation-motorway", "transportation", c["motorway"], 1.6,
              filter=["match", ["get", "class"], ["motorway", "trunk"], True, False]),
        _line("aeroway", "aeroway", c["aeroway"], 1.0, minzoom=9),
        _fill("building", "building", c["building"], minzoom=13),
    ]
    attribution = str(attribution or "").strip()
    return {
        "version": 8,
        "name": "DarkSec Offline Vector",
        "metadata": {"darksec:basemap": "vector-mbtiles", "darksec:labels": False},
        "sources": {
            "omt": {
                "type": "vector",
                "tiles": ["/api/map/vector/tiles/{z}/{x}/{y}.pbf"],
                "minzoom": lo,
                "maxzoom": hi,
                "attribution": attribution,
            }
        },
        "layers": layers,
    }
