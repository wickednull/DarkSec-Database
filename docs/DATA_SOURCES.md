# DarkSec NIGHTWATCH — Data Sources & Attribution

This document describes the main external data sources, local/imported datasets,
and operator-provided layers used by DarkSec. It is an operational reference, not
legal advice. Operators must verify provider terms before enabling, sharing, or
redistributing any data.

## Principles

- **Local-first / cached:** fetched data is cached in SQLite and remains readable
  offline with age/status indicators. A refresh failure does not erase valid cached
  layers.
- **Operator-triggered:** network fetches are initiated by refresh/actions, not
  silent background exfiltration. Some deployments may run autosync if configured.
- **Attribution preserved:** imported/catalog items keep `source`, `provider`,
  `credit`, `license`, `url`, and `import_source` metadata when available. The UI
  should display these fields.
- **No blanket redistribution:** public availability is not a redistribution
  license. Do not rehost, archive, or redistribute third-party imagery/data unless
  the source terms explicitly allow it.

## Bundled / Local Datasets

| Layer | Source | Storage | Notes |
|---|---|---|---|
| Vector basemap | Bundled Natural Earth-derived data (`maps/vector`) | Local files | No tile server contacted by default. |
| Raster tiles | Operator-supplied MBTiles/local tiles or configured local sources | Local files/cache | No external tile server unless operator configures one. |
| Subsea/data cables | Cached/public dataset import when available, or local GeoJSON (`fielddata/maps/submarine-cables.geojson` etc.) | Feed cache | Preserve dataset attribution in feed `source_note`. Verify license before redistribution. |

## Intelligence Feeds (cached)

Feed definitions are seeded in `darksec/db.py` and managed via `/api/feeds*`.
Exact URLs/kinds may vary by deployment; check Settings → Intelligence Feeds.

| Feed / Kind | Typical Source | License/Terms note |
|---|---|---|
| USGS earthquakes (`usgs_quakes`) | USGS Earthquake Hazards Program | Public U.S. government data; courtesy attribution requested. |
| NWS alerts (`nws_alerts`) | NOAA/NWS | Public U.S. government data; operationally authoritative for US alerts. |
| GDACS (`gdacs`) | Global Disaster Alert and Coordination System | Use per GDACS terms; not a sole life-safety authority. |
| NASA EONET (`eonet`) | NASA EONET | NASA open data policies generally apply; verify current terms. |
| Open-Meteo weather/air (`open_meteo_*`) | Open-Meteo APIs | Review Open-Meteo terms/attribution requirements. |
| Safecast radiation (`safecast`) | Safecast API | Community measurements; attribution/terms apply; not calibrated regulatory data. |
| Smithsonian/USGS volcanic (`smithsonian_volcano`) | Smithsonian GVP / volcanic feeds | Verify source-specific terms and attribution. |
| CelesTrak TLEs (`celestrak*`) | CelesTrak GP/TLE data | CelesTrak policies apply; propagation is local/estimated (SGP4), not observed tracking. |
| GDELT conflict (`gdelt_conflict`) | GDELT Project | Media-derived signals, not verified ground truth. Check GDELT terms/citation. |
| UCDP candidate (`ucdp_candidate`) | Uppsala Conflict Data Program | Cite per UCDP guidance; definitions matter. |
| ADS-B aircraft (`adsb_aircraft`) | Local dump1090/readsb/RTL-SDR or operator source | Local receiver data; online providers have their own terms. |
| RSOE EDIS, news RSS feeds | Publisher RSS feeds | Publisher-owned content; cache minimally and attribute source/title/URL. |
| Maritime/AIS (`maritime_ais`) | Operator-provided GeoJSON/JSON file or URL | Global real-time AIS usually requires a provider agreement/API key. No default global AIS feed. |
| Public CCTV / OSM surveillance (`public_cctv`) | OpenStreetMap via Overpass API | ODbL for OSM-derived data; Overpass has load/rate limits. Locations/tags only, no live previews. Community-mapped data may be stale/wrong. |

## CCTV / Camera Catalog

DarkSec merges CCTV layers from cached sources:

- **`public_cctv`** — OSM `man_made=surveillance` locations via Overpass around the
  configured position. Operator-triggered; bounded by `cctv_radius_km` (default 50,
  clamped 10–500). Public mirrors can time out; cached locations remain visible.
- **`cctv_catalog`** — local/imported camera catalogs (JSON/GeoJSON-like records).
  Import locations include `fielddata/maps/cctv_catalogs/`, `fielddata/maps/cctv/`,
  optional `cctv_catalog_dir`/`cctv_catalog_path` setting, and (local dev convenience)
  `/home/null/gods-eye-view/config/cctv_sources*.json` and
  `/home/null/gods-eye-view/public/quebec-cameras.json` when present.

Catalog records are normalized/deduplicated and preserve metadata when available:
`id/camera_id`, `lat/lon`, `title`, `url/viewer/snapshot`, `provider/operator`,
`credit`, `license`, `feed_type`, `heading/pitch/fov/range`, `region/city`,
`import_source`, `source_kind`.

**Important:** catalog entries may reference third-party image/video endpoints.
Fetching or displaying snapshots can have legal/privacy/terms implications.
DarkSec’s safe frame proxy (when used) only fetches URLs registered in known CCTV
catalog/feed items, with scheme/size/timeout limits and no arbitrary client URL
fetching. Honor source terms, refresh limits, attribution, and any “no
redistribution / on-view only / personal use / written permission” restrictions.

## ALPR / License Plate Readers (audited OSM tags)

OSM-tagged ALPR/ANPR locations may appear as camera metadata where present. These
are **mapped locations/tags only** (community data), not live tracking, not plate
data, and not a complete or authoritative registry. DarkSec marks likely ALPR/ANPR
items from OSM tags (`surveillance:type`, `camera:type`, brand/tag heuristics) and
provides an explicit map filter: **“Show ALPR-tagged CCTV (OSM audited tags)”**
(2D and GL). Verify locations on OpenStreetMap and comply with local law/policy.

## Caveats

- Cached data is not real-time truth. Ages, confidence, verification state, and
  provenance matter.
- Media-derived feeds can be wrong, duplicated, biased, or geographically imprecise.
- Hazard/intel layers are decision support, not a substitute for official
  emergency guidance or aviation/maritime/medical/legal authorities.
- Do not use the system for unlawful surveillance, targeted harassment, or
  identification of individuals from camera imagery.

## Updating This Document

When adding a feed/provider/importer, record: source name, endpoint/path,
auth/key requirements, license/terms summary, attribution required, refresh
cadence/limits, caching behavior, caveats, and whether frames/data may be
proxied, stored, or redistributed.


## CCTV Operational Controls

- **Settings → CCTV / camera catalog**: view status, enable/disable imported packs (`cctv_catalog_disabled_sources`), import/refresh catalog, refresh OSM CCTV, and inspect frame cache/error summaries.
- **Registered media only**: `/api/cctv/frame|snapshot` serves only DB-registered snapshot URLs (scheme/host/credential/size/type checks, in-memory transient cache, throttled by `frameRefreshMs`). `/api/cctv/stream/info|manifest|resource|media` serves only DB-registered stream URLs with HLS manifest rewrite, nested playlist rewrite, same-host subresource validation by default, and range passthrough. Optional `cctv_stream_host_allowlist` (JSON array of hostnames/suffixes) can allow known CDN subresource hosts for a registered stream; use conservatively. No arbitrary client URL proxy.
- **Live previews**: map inspectors offer snapshot LIVE PREVIEW (auto-refresh, selection-scoped) and on-demand LOAD STREAM for registered HLS/MP4. `ui/vendor/hls/hls.min.js` (Apache 2.0) is vendored for non-native HLS browsers.
- **Refresh policy**: catalog/OSM refresh is operator-triggered. `cctv_catalog_refresh_hours` can document local controlled-refresh policy; automatic background scheduling is deployment-specific and should respect source terms/rate limits.

## BYOK / Operator-Enabled Live Feeds (adapted parity)

Some gods-eye-view-style layers (e.g. global live fire detections, launches, specialized maritime/traffic providers) may require API keys, paid accounts, or restrictive terms. DarkSec should add these only as **operator-enabled, documented, cached** feeds with clear attribution and no default credentials. Do not enable feeds that violate source terms or require silent redistribution.
