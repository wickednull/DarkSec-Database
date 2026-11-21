# Screenshots

Captured from a throwaway demo profile at 1600×1000. The callsign, position,
records and Delamain conversation are sample data; no real operator position or
vault contents are shown.

## Files

- `map-world.png` — 2D world map, wide view.
- `map-focused.png` — focused map with sample waypoints and cached intel layers.
- `defcon-tracker.png` — DEFCON tracker and intel overview.
- `situation-report.png` — main dashboard / SITREP, Slate theme + Amber CRT accent.
- `situation-report-plum.png` — same dashboard, Plum theme + Green accent.
- `situation-report-light.png` — same dashboard, Daylight light theme.
- `situation-report-operations.png` — dashboard Operations tab.
- `delamain.png` — Delamain local AI assistant, Slate / Amber CRT.
- `delamain-plum.png` — Delamain local AI assistant, Plum / Green.
- `document-library.png` — document library and search/index state.
- `security-vault.png` — vault and security page.
- `global-search.png` — top-bar global search across records, documents and references.

## Regenerating

Screenshots are produced by `/tmp/opencode/shots/shoot.py` against a throwaway
backend (`DARKSEC_DATA=/tmp/opencode/shots`, `--no-index`, port 8799). The
script seeds demo records, waits for feeds to settle, indexes sample library
text for global search, and drives headless Chromium via Selenium/ChromeDriver.

Delamain shots require a local Ollama model. The current set used the locally
installed uncensored Qwen 3.5 4B model; swap `UNCENSORED` in the script to use a
different installed model.
