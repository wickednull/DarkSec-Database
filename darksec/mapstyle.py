"""MapLibre style for offline OpenMapTiles-schema vector MBTiles.

The archive carries geometry only; labels need a glyph pack, so this style
renders land, water, boundaries, roads and buildings without text. Colours
follow the active light/dark scheme. Tile URLs stay relative so the browser
resolves them against the DarkSec origin.
"""
from __future__ import annotations

import re
from typing import Any

_DARK = {
    # Dark enough for night use, but with enough separation that land, water,
    # roads and boundaries are readable instead of collapsing into black.
    "bg": "#0b1510",
    "land": "#13241a",
    "wood": "#102a1a",
    "grass": "#12301c",
    "sand": "#2a2516",
    "ice": "#16303a",
    "wetland": "#0d2a24",
    "residential": "#111d15",
    "commercial": "#181d16",
    "industrial": "#1b1a14",
    "park": "#12301d",
    "water": "#0b3f52",
    "waterway": "#0e5068",
    "boundary": "#4b7a5f",
    "road": "#5d6b60",
    "motorway": "#a08a4a",
    "rail": "#555555",
    "building": "#1a241c",
    "aeroway": "#35443a",
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


_OMT_LAYERS = {
    "water", "waterway", "landcover", "landuse", "park", "boundary",
    "transportation", "building", "aeroway",
}
_LABEL_LAYERS = {
    "place", "poi", "water_name", "transportation_name", "housenumber",
    "mountain_peak",
}


def _safe_id(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)[:48] or "layer"


def _generic_kind(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("water", "ocean", "sea", "river", "lake", "coast", "bay")):
        return "water"
    if any(k in low for k in ("road", "street", "highway", "transport", "rail", "route", "path")):
        return "road"
    if "building" in low:
        return "building"
    if any(k in low for k in ("boundary", "admin", "border")):
        return "boundary"
    if any(k in low for k in ("park", "forest", "wood", "landcover", "landuse", "green", "nature")):
        return "green"
    if any(k in low for k in ("place", "poi", "label", "name", "peak")):
        return "point"
    return "generic"


def _generic_layers(names: list[str], c: dict[str, str], dark: bool) -> list[dict[str, Any]]:
    """Render any vector schema without knowing its layer names.

    A fill, line and circle layer is added per unknown source-layer, so mixed
    point/line/polygon tiles from tilemaker, tippecanoe or custom pipelines are
    visible instead of producing a black map.
    """
    fills: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    circles: list[dict[str, Any]] = []
    accent = "#8fe3b0" if dark else "#2f6b4f"
    for name in names:
        if name in _LABEL_LAYERS:
            continue
        kind = _generic_kind(name)
        lid = _safe_id(name)
        if kind in ("water", "green", "building", "generic"):
            color = {"water": c["water"], "green": c["park"],
                     "building": c["building"]}.get(kind, c["land"])
            fills.append({
                "id": f"gen-{lid}-fill", "type": "fill", "source": "omt",
                "source-layer": name,
                "paint": {"fill-color": color,
                          "fill-opacity": 0.32 if kind == "generic" else 0.75},
            })
        if kind in ("water", "road", "boundary", "generic"):
            color = {"water": c["waterway"], "road": c["road"],
                     "boundary": c["boundary"]}.get(kind, c["road"])
            lines.append({
                "id": f"gen-{lid}-line", "type": "line", "source": "omt",
                "source-layer": name,
                "paint": {"line-color": color,
                          "line-width": 1.4 if kind == "road" else 0.8,
                          "line-opacity": 0.85},
            })
        if kind in ("point", "generic"):
            circles.append({
                "id": f"gen-{lid}-point", "type": "circle", "source": "omt",
                "source-layer": name,
                "paint": {"circle-radius": 2.2, "circle-color": accent,
                          "circle-opacity": 0.7,
                          "circle-stroke-color": c["bg"], "circle-stroke-width": 0.5},
            })
    return fills + lines + circles


def vector_style(*, theme: str = "dark", minzoom: int | None = 0,
                 maxzoom: int | None = 14, attribution: str = "",
                 layers: list[str] | None = None) -> dict[str, Any]:
    """Build a MapLibre style for an OpenMapTiles or generic vector archive."""
    c = _color(theme)
    dark = str(theme or "").lower() != "light"
    requested = [str(name) for name in (layers or []) if str(name).strip()]
    unknown = [name for name in requested if name not in _OMT_LAYERS]
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
    if unknown:
        layers.extend(_generic_layers(unknown, c, dark))
    attribution = str(attribution or "").strip()
    schema = "generic" if requested and not (set(requested) & _OMT_LAYERS) else (
        "mixed" if unknown else "openmaptiles"
    )
    return {
        "version": 8,
        "name": "DarkSec Offline Vector",
        "metadata": {
            "darksec:basemap": "vector-mbtiles",
            "darksec:labels": False,
            "darksec:schema": schema,
            "darksec:layers": requested[:40],
        },
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
