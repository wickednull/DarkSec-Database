#!/usr/bin/env python3
"""Build the bundled low-zoom world overview from a working tile provider.

The shipped `maps/tiles/us_tiles` contained one repeated error image; this
script replaces it with real tiles. Run once while online:

    PYTHONPATH=. python3 tools/build_overview.py --max-zoom 5

Tiles land in maps/tiles/overview/<z>/<x>/<y>.jpg and are served by
/api/map/tiles/... with no network afterwards.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darksec import tilecache  # noqa: E402

OUT = ROOT / "maps" / "tiles" / "overview"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-zoom", type=int, default=5)
    ap.add_argument("--min-zoom", type=int, default=0)
    ap.add_argument("--source", default="esri_imagery")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    if args.source not in tilecache.TILE_SOURCES:
        print(f"unknown source {args.source}")
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    total = sum(4 ** z for z in range(args.min_zoom, args.max_zoom + 1))
    done = skipped = failed = 0
    started = time.time()
    for z in range(args.min_zoom, args.max_zoom + 1):
        n = 1 << z
        for x in range(n):
            for y in range(n):
                dest = OUT / str(z) / str(x) / f"{y}.jpg"
                if dest.exists() and dest.stat().st_size > 1000 and not args.overwrite:
                    skipped += 1
                    done += 1
                    continue
                try:
                    data = tilecache.fetch_tile(z, x, y, args.source)
                except tilecache.TileBlocked as exc:
                    print(f"\nBLOCKED: {exc}")
                    return 3
                if not data:
                    failed += 1
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                done += 1
                if done % 50 == 0 or done == total:
                    rate = done / max(0.001, time.time() - started)
                    eta = (total - done) / max(0.001, rate)
                    print(f"\r{done}/{total} ({done*100//total}%)  "
                          f"{rate:.1f}/s  eta {eta/60:.1f} min  failed={failed}",
                          end="", flush=True)
    print(f"\nDone. written={done - skipped} skipped={skipped} failed={failed}")
    print(f"Output: {OUT}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
