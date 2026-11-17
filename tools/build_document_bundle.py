#!/usr/bin/env python3
"""Build a split ZIP document-library bundle and manifest.

The resulting files are uploaded to the project's GitHub release. Point DarkSec
at the release manifest URL in Settings -> Document library -> Document
library bundle.

Usage:
    python3 tools/build_document_bundle.py --out dist/document-library
    python3 tools/build_document_bundle.py --out dist/document-library \
        --base-url https://github.com/USER/REPO/releases/download/docs-2026-11-16/

Only files in the configured library roots are included. Duplicate
name+size pairs (for example the separate Books tree) are skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darksec import config, library  # noqa: E402

MANIFEST_FORMAT = "darksec-document-bundle-v1"
LICENSE_NOTE = (
    "Only redistribute documents you have the right to share. Many government "
    "and public-domain manuals are redistributable; verify each source and "
    "remove copyrighted or restricted files before publishing a bundle."
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_documents() -> list[tuple[Path, Path, int]]:
    bundle_root = Path(config.DOC_BUNDLE_DIR).resolve()
    docs: list[tuple[Path, Path, int]] = []
    seen: set[tuple[str, int]] = set()
    allowed = {e.lower() for e in config.DOC_EXTENSIONS}
    for root in library.library_roots():
        try:
            if root.resolve() == bundle_root:
                continue
        except OSError:
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in allowed:
                continue
            if path.name.lower() in ("index.html", "index.htm"):
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(root)
            except (OSError, ValueError):
                continue
            key = (path.name.lower(), stat.st_size)
            if key in seen:
                continue
            seen.add(key)
            docs.append((path, Path(root.name) / rel, stat.st_size))
    docs.sort(key=lambda item: item[1].as_posix().lower())
    return docs


def build(docs: list[tuple[Path, Path, int]], out_dir: Path, part_size: int,
          bundle_id: str, base_url: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict] = []
    zf: zipfile.ZipFile | None = None
    part_path: Path | None = None
    part_files = 0
    part_bytes = 0
    part_index = 0

    def close_part() -> None:
        nonlocal zf, part_files, part_bytes
        if zf is None or part_path is None:
            return
        zf.close()
        parts.append({
            "name": part_path.name,
            "url": urljoin(base_url, part_path.name) if base_url else part_path.name,
            "bytes": part_path.stat().st_size,
            "sha256": sha256(part_path),
            "files": part_files,
        })
        print(f"  {part_path.name}: {part_path.stat().st_size / 1073741824:.2f} GB, {part_files} files")
        zf = None
        part_files = 0
        part_bytes = 0

    def open_part() -> None:
        nonlocal zf, part_path, part_index
        part_index += 1
        part_path = out_dir / f"{bundle_id}.part{part_index:02d}.zip"
        zf = zipfile.ZipFile(part_path, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True)

    for src, arcname, size in docs:
        if zf is None or (part_bytes > 0 and part_bytes + size > part_size):
            close_part()
            open_part()
        assert zf is not None
        zf.write(src, arcname=arcname.as_posix())
        part_files += 1
        part_bytes += size
    close_part()

    total_bytes = sum(p["bytes"] for p in parts)
    manifest = {
        "format": MANIFEST_FORMAT,
        "bundle_id": bundle_id,
        "created": datetime.now(timezone.utc).isoformat(),
        "total_files": len(docs),
        "total_bytes": total_bytes,
        "license_note": LICENSE_NOTE,
        "parts": parts,
    }
    manifest_path = out_dir / "document-library.bundle.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "README.txt").write_text(
        "DarkSec document-library bundle\n"
        "===============================\n\n"
        f"Bundle ID: {bundle_id}\n"
        f"Documents: {len(docs)}\n"
        f"Archive bytes: {total_bytes / 1073741824:.2f} GB\n"
        f"Parts: {len(parts)}\n\n"
        "Upload every .zip part and document-library.bundle.json to the same\n"
        "GitHub release, then use the preconfigured manifest URL in DarkSec:\n"
        "Settings -> Document library -> Manifest URL -> Download document library.\n\n"
        f"{LICENSE_NOTE}\n",
        encoding="utf-8",
    )
    print(f"Manifest: {manifest_path}")
    print(f"Total: {len(docs)} documents, {total_bytes / 1073741824:.2f} GB, {len(parts)} part(s)")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist" / "document-library"),
                    help="output directory (default: dist/document-library)")
    ap.add_argument("--part-size-mb", type=int, default=1800,
                    help="target ZIP part size in MiB (default: 1800; GitHub assets max 2 GB)")
    ap.add_argument("--bundle-id", default=f"darksec-docs-{datetime.now(timezone.utc):%Y%m%d}",
                    help="bundle identifier (default: darksec-docs-YYYYMMDD)")
    ap.add_argument("--base-url", default="",
                    help="public base URL for the parts; relative names are used when omitted")
    ap.add_argument("--dry-run", action="store_true",
                    help="scan and report without creating archives")
    args = ap.parse_args()

    docs = collect_documents()
    total = sum(size for _, _, size in docs)
    print(f"Found {len(docs)} document(s), {total / 1073741824:.2f} GB uncompressed")
    if args.dry_run:
        return 0
    if not docs:
        print("No documents found.", file=sys.stderr)
        return 1
    part_size = max(64, args.part_size_mb) * 1024 * 1024
    build(docs, Path(args.out).expanduser(), part_size, args.bundle_id, args.base_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
