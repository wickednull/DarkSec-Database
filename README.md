# DarkSec NIGHTWATCH

A local-only, offline-first survival field console. It runs a Python FastAPI
backend on `127.0.0.1`, serves a bundled single-page interface, and keeps all
operator data and hardware access on the machine.

The existing PDF library is preserved in place. Nothing in the document tree is
rewritten; indexing only records metadata and extracted text in a local SQLite
database.

> This is a planning and record-keeping tool, not a substitute for training,
> current official guidance, a clinician, a licensed radio operator, or a
> calibrated instrument. Read the safety notes inside each module. In a real
> emergency, contact local emergency services when possible.

## What is included

- **SITREP dashboard** — readiness indicator, almanac, feeds, tasks, incidents,
  recent logs, low stock and system state. Readiness is a transparent local
  estimate: threats weigh 40%, sustainment 30%, intel coverage 15% and position
  quality 15%. Critical cached headlines count as threat context, not as an
  intel-coverage failure.
- **Document library** — metadata + full-text search (SQLite FTS5) over the
  existing PDF/EPUB/etc. collection, with a bundled PDF.js reader.
- **Field manual** — 16 local Markdown guides, 40 quick-reference cards and 13
  equipment/event checklists.
- **Navigation** — WGS-84 / UTM / MGRS conversion, celestial almanac, magnetic
  declination (vendored WMM2025), resection, dead reckoning, tide estimate, GPS
  fix provenance, waypoints, GPX import/export, a bundled low-zoom world
  satellite overview (zooms 0–5) and downloadable offline tile packs.
- **Communications** — 29 bands, 60 common channels, phonetic alphabet,
  prowords, Q-codes, wilderness protocol, SOI plans and a radio log.
- **SDR / spectrum** — real HackRF/RTL detection, live sweep and waterfall,
  IQ capture, demodulation, recordings, ADS-B and a hard-gated transmit path.
- **Field medicine** — patient registry, triage board, vitals trends, clinical
  calculators and reference cards. Local records only.
- **Water / food / power** — treatment and consumption logs, stock levels,
  ration and power-system calculators.
- **CBRN** — dose-rate log, decay/fallout math, protective-action guidance and
  KI warnings.
- **Security & vault** — AES-256-GCM encrypted notes, incident log, OPSEC
  controls, audit trail and a phrase-gated purge.
- **Logistics** — mission tasks, cache register, offline trip plans and local
  check-in deadlines, low-stock view, backups, JSON/CSV export and restore.
  Check-ins are not sent to an off-site contact.
- **Weather & environment** — manual observations, charts, auto-synced
  NOAA/USGS/NWS/GDACS and Open-Meteo feeds with offline caching, plus nowcast
  calculators.
- **Global threat & live intel** — cached public live feeds (OpenSky aircraft,
  CelesTrak TLE/SGP4 satellites, USGS earthquakes, NOAA SWPC, GDACS/EONET,
  Safecast, Smithsonian volcano reports, world news and Open-Meteo), a local
  weighted threat index, solar/day-night state and live event logging into the
  mission log. Auto-sync pauses offline and in OPSEC mode.
- **Settings** — identity, position, grid format, radio limits, map tiles,
  appearance, vault timeout, diagnostics and data custody.
- **Local AI assistant (Delamain)** — optional chat backed by a local Ollama
  instance. Detects installed models, can include cached intelligence context,
  and has no cloud dependency. Configure the endpoint and model in Settings.

## Run it

Requirements: Python 3.11+ and the packages in `requirements.txt`. A virtual
environment is recommended.

```bash
python3 -m pip install -r requirements.txt
./launch-web.sh
```

The browser opens with a one-time token URL. The token is exchanged for an
HttpOnly cookie and removed from the address bar. To run without opening a
browser:

```bash
python3 -m darksec --port 8765
```

Optional flags:

```text
--no-index        do not start background library indexing
--token VALUE     fixed launch token (testing only)
--port N          fixed port (default: random free port)
--log-level ...   critical|error|warning|info|debug
```

### Local AI assistant (optional)

Delamain uses a locally running [Ollama](https://ollama.com) instance. The console
works fully without it; the assistant view will report **NO MODELS** until one is
available.

```bash
# install Ollama, then pull at least one model
ollama pull llama3.2
```

Set the endpoint (default `http://127.0.0.1:11434`) and model name in
**Settings → Assistant**. Only local endpoints are used; nothing is sent to a
cloud service. The assistant can include cached intelligence context when asked.


The Electron main process starts the Python backend itself, reads the JSON
handshake from stdout, and loads the console. Closing the window terminates the
backend. Set `DARKSEC_PYTHON` if your interpreter is not `python3`.

## Data locations

By default everything writable lives in `fielddata/` next to the application:

```text
fielddata/
  darksec.db          records, settings, audit, library index
  recordings/         SDR recordings and captures
  backups/            operator-data backups
  uploads/            imported files
  logs/
```

Set `DARKSEC_DATA` to relocate the whole writable tree (useful for a USB
stick). `DARKSEC_LIBRARY` can add extra document roots separated by `:`
(`;` on Windows).

The document library is read from `chembio/`, `comms/`, `engineering/`,
`hacking/`, `manuals/`, `medical/`, `prepper/`, `water_food/`, `maps/` and
`isos/`. The duplicate `Books/` tree is deliberately excluded.

### Offline map tiles

The app ships `maps/tiles/overview/` — a real low-zoom world satellite
overview (zooms 0–5) — and `maps/vector/world.geojson` for the dark vector
layer. Both work with no network.

For detail around your position, open **Map → Manage offline map**, choose a
radius or the current viewport and a zoom range, and download. Tiles are
validated before they are stored under `maps/tiles/cache/<source>/z/x/y.ext`,
so a provider error page can never end up in the cache. **Clear offline tiles**
in the same dialog removes downloaded tiles.

To rebuild the bundled overview from a working provider (online, once):

```bash
PYTHONPATH=. python3 tools/build_overview.py --max-zoom 5
```

### Position and address lookup

Navigation distinguishes a saved GPS fix, manually entered coordinate and
address-geocoded reference. None is presented as live tracking; GPS age and
reported accuracy are shown where available. Settings can look up an address
through OpenStreetMap Nominatim only after an explicit click. The exact query is
sent to that public service, is not written to disk and may remain in memory for
up to 24 hours; lookup is blocked in OPSEC mode. Only selected coordinates are
saved. Geocoded points have no GPS accuracy or elevation guarantee; verify the
point against terrain and a paper map before relying on it.
© OpenStreetMap contributors.

Accepting a fresh phone fix updates the global grid/coordinate display and the
position used by navigation, the map, almanac and location-based intel. It also
queues nearby Safecast and Open-Meteo refreshes when auto-sync is enabled.

Phone GPS uses the normal mobile browser—no companion app. Put the phone and
computer on the same trusted Wi-Fi or, preferably, a private phone hotspot.
**Settings → Position → Phone GPS · Browser** starts a five-minute HTTPS page
bound only to the selected local IPv4 interface; the normal DarkSec API stays
loopback-only. Scan its QR with the phone camera, review the self-signed
certificate warning, and continue only on a network you trust and when the IP
matches the selected interface. Grant browser location permission and tap
**START GPS SHARING**. The desktop displays age/accuracy and requires an
explicit **ACCEPT FIX**; no position is saved before acceptance. A phone fix is
treated as current for 24 hours before it is flagged stale (GPS receiver fixes use a
6-hour limit).

The page has no third-party assets and makes no cloud requests. Its temporary
bearer token is removed from the address bar after load; location updates are
not logged or written by the HTTPS listener. Keep the browser open for updates.
The listener stops on timeout, OPSEC activation, manual stop, or app shutdown.
Do not bypass a certificate warning on public/shared Wi-Fi; a private hotspot
is the safer choice. The self-signed certificate is temporary and is not a
public identity proof. This uses the browser-geolocation pattern of the
[Hak5 Mobile2GPS Pager payload](https://github.com/ryanpohlner/mobile2gps),
implemented as a separate, restricted DarkSec listener.

The saved source, fix age and accuracy appear in the status bar on every view.
While a phone session is still open, **Navigation → GPS receiver → QUERY** can
read that live browser fix directly (labelled as phone browser data) and use it
as the saved position; otherwise it reads local gpsd. The SITREP export includes
the same source/age/accuracy line and warns when the fix is unverified or stale.

### Indexing

Metadata indexing is quick. Deep text extraction uses `pdftotext` when
available and falls back to `pypdf`; a large collection can take hours. The
index runs in the background and can be started or stopped from the Library
screen. `DARKSEC_NO_INDEX=1` or `--no-index` disables automatic indexing.

## SDR notes

The SDR module calls the real tools on `PATH`:

- `hackrf_info`, `hackrf_sweep`, `hackrf_transfer`
- optionally `rtl_test`, `rtl_fm`, `dump1090`, `rtl_433`, `multimon-ng`,
  `direwolf`, `sox`, `ffmpeg`

No spectrum, recording or device status is fabricated. With no radio attached,
the module reports **NO DEVICE** and the sweep stream returns an honest error.

`hackrf_sweep` tunes in whole-MHz steps, so the requested sweep range is rounded
outward to the nearest MHz and the returned frame reports the actual range.
Gains are clamped and snapped to the hardware steps (LNA 8 dB, VGA 2 dB, TX VGA
1 dB). The NOAA APT decoder is explicitly experimental: use a 137 MHz polar
satellite pass, about 2.4 Msps, and expect skew — use a dedicated decoder for
operational imagery.

Transmit is disabled by default and requires all of the following:

1. Enable TX in Settings and record the legal attestation (type `I AM RESPONSIBLE`).
2. Pass the per-transmission confirmation (`TRANSMIT`) and attestation checkbox.
3. Use a frequency you are licensed or authorised to transmit on.

The application cannot verify your licence, antenna, filter or legal status.
You are responsible for every transmission. Prefer a dummy load for testing.

## Feeds

Public feeds auto-sync while online by default (10-minute base cadence, with
per-source minimums and conservative backoff). Change or disable this in
**System Settings → Intelligence feeds**. Set `DARKSEC_NO_SYNC=1` to disable
automatic sync entirely at launch. OPSEC blackout and offline status
pause outbound fetches; cached results remain readable with their source age.
Dashboard, Intel, Weather and CBRN views refresh their presentation while open.
Use **SYNC NOW** for a one-shot refresh even when automatic sync is disabled.

- USGS earthquakes
- NOAA SWPC Kp and R/S/G space-weather scales
- NWS active alerts
- GDACS disaster alerts and NASA EONET natural events
- RSOE EDIS global disaster events (severity-coded event list with enriched
  coordinates for the highest-priority events)
- Smithsonian/USGS volcanic activity
- Safecast community radiation measurements
- WHO general news RSS (not a dedicated outbreak-alert feed)
- ReliefWeb and BBC / Guardian / France24 / DW / Al Jazeera / Crisis Group news
- GDELT 2.0 live conflict event slices (media-derived, automated coding)
- UCDP Candidate Events (monthly verified, lags weeks)
- Open-Meteo weather and air quality
- OpenSky live aircraft public positions
- CelesTrak satellite TLEs (SGP4 propagation; the provider may intermittently
  reject anonymous requests)

Responses are cached locally. Freshness depends on each provider's cadence;
cached values show their age and remain available offline. Community radiation
reports can be very old and are not live monitoring or official warnings. Old
feeds or sources whose newest item is stale are flagged in the feed state, and
the map excludes stale point feeds rather than plotting historical snapshots as
if they were current.

### Global threat / live intelligence layer

The **Global Threat & Live Intel** view (`#/intel`) assembles a local threat
index from cached live events and adds live-source panels:

- **Threat index** — local weighted aggregation over cached sourced events
  (earthquakes, disaster alerts, public safety warnings), distance-weighted
  from the stored reference position when one is set. Bands: SEVERE / ELEVATED /
  GUARDED / NOMINAL. Display only; not a forecast.
- **Live aircraft** — public position snapshots from the OpenSky network,
  with altitude / speed / heading. Military traffic is often not published.
- **Satellite watch** — cached CelesTrak TLE set propagated with SGP4; shows
  overhead passes for the operator position.
- **CBRN picture** — NOAA space-weather scales, nearby Safecast reports, local
  dosimetry log, Smithsonian/USGS volcano reports, NASA EONET hazards and
  Open-Meteo air quality. These are advisory public proxies, not a confirmed
  CBRN incident feed or substitute for calibrated instruments and official
  alerts.
- **Air quality** — Open-Meteo AQI snapshot.
- **Global live events** — cached events that carry coordinates (earthquakes),
  newest first, with a `LOG` shortcut that files an incident into the mission
  log.
- **Live news & safety feeds** — cached wire/public source cards with refresh.

Automatic sync state is available at `GET /api/sync/status`; `POST /api/sync/now`
requests a one-shot sync. Explicit full or per-feed refreshes are available
through `POST /api/intel/refresh` and `POST /api/feeds/refresh` (with an `id`).
Cached data remains available offline.

Sources are third-party public feeds; some optional modules depend on configured
API keys. The application does not operate or verify these sources and cannot
guarantee their availability or accuracy.

Conflict reporting is split into a live, media-derived layer (GDELT 2.0) and a
monthly verified layer (UCDP Candidate Events, which lags weeks), plus
conflict-relevant wire and humanitarian headlines (BBC, Al Jazeera, Guardian,
France24, ReliefWeb, Crisis Group). The map renders geolocated conflict events
as diamonds: orange for UCDP verified, amber for GDELT live. Both are clearly
labeled, can be imprecise, and are never presented as confirmed targeting data.

## Backup and restore

**Logistics → Operator Data** writes a JSON backup under
`fielddata/backups/`. Backups contain records, checklists and non-secret
settings. They do **not** contain PDFs or decrypted vault notes.

Restore has two modes. **Replace** swaps the operator tables and preserves
record IDs, so patient/vitals links survive. **Merge** adds records and remaps
patient IDs for linked vitals. Missing required fields are filled with safe
defaults and incomplete rows are skipped rather than aborting the restore. TX
enablement and attestation are deliberately preserved rather than restored.
Backups never contain PDFs or decrypted vault notes. Export JSON or CSV for
interchange; keep a copy on removable media.

## Security model and limits

- The API binds to `127.0.0.1` only.
- A random per-launch token is required; it is stored in an HttpOnly,
  SameSite=Strict cookie. Mutations require same-origin or the CSRF token.
- Vault notes use AES-256-GCM with PBKDF2-HMAC-SHA256 (600,000 iterations).
  The key exists only in process memory and is dropped on lock, timeout or
  shutdown.
- There is no recovery key, duress code, remote wipe, plausible deniability or
  secure erase. A compromised OS, keylogger, screen capture, memory forensics
  or weak passphrase defeats the vault.
- The vault does not encrypt ordinary records, logs, recordings or documents.
  Use full-disk encryption and physical security.
- Host-header and cross-origin checks are enforced; the server also sends CSP
  and related security headers.
- No telemetry is included.

## Legal and safety

Radio transmission, medical care, water treatment, electrical work, firearms,
pyrotechnics, CBRN response and aviation/navigation all carry legal and
physical risk. The calculators are estimates with stated assumptions; verify
inputs, units, local law and current protocols. Do not use this software as the
sole source for a life-safety decision.

## Development checks

```bash
python3 -m pip install -r requirements-dev.txt   # pytest, httpx, pyflakes
npm run check            # Electron/JS syntax + Python compile + offline self-check
npm run test             # API regression suite (uses a throwaway database)
npm run check:all        # both of the above
python3 tools/selfcheck.py   # parsers, schema, config and assistant sanitizer
node tools/check-js.mjs      # every ui/js module without starting a browser
```

The self-check and the pytest suite build throwaway databases; they never
touch `fielddata/` and do not require network access or Ollama. The pytest
fixtures live in `tests/conftest.py`; add API-level regression tests there when
changing a route contract.

Run the backend against a throwaway data directory while testing:

```bash
DARKSEC_DATA=/tmp/darksec-test PYTHONPATH=. \
  python3 -m darksec --port 8765 --token testtoken --no-index
```
