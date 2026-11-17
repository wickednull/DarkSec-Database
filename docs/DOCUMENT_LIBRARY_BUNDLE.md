# Document library bundles

DarkSec installs the packaged document library from its GitHub release
manifest. The manifest and ZIP parts are release assets.

## Build the bundle

From the repository root:

```bash
python3 tools/build_document_bundle.py \
  --out /path/to/document-library-bundle \
  --part-size-mb 900 \
  --base-url https://github.com/USER/REPO/releases/download/docs-2026-11-16/
```

The script scans the configured library roots, de-duplicates by name+size,
creates split ZIP parts (stored, not recompressed), and writes
`document-library.bundle.json` plus `README.txt`.

Omit `--base-url` to use relative part names, or set it to the public location
where the parts will be uploaded.

## Host the bundle

Create a GitHub release and upload every `.zip` part and
`document-library.bundle.json` as release assets. GitHub gives direct asset
URLs and a 2 GB per-asset limit; `--part-size-mb 900` keeps each part well below
that limit.

## Install in DarkSec

1. Open **Settings → Document library**.
2. The project release URL is preconfigured in **Manifest URL**. Leave it
   unless a newer release provides a different manifest.
3. Press **DOWNLOAD DOCUMENT LIBRARY**.
4. The installer downloads each part, verifies SHA-256, extracts only document
   files into `fielddata/library/`, deletes the archives, and starts a metadata
   index. Press **START INDEX** with deep extraction if you want full-text
   search immediately.

Downloads resume partial `.download` files when the host supports HTTP Range.
Existing files are kept, so a newer bundle does not overwrite your edits; to
refresh, remove the old files under `fielddata/library` and download again.

## Manifest format

```json
{
  "format": "darksec-document-bundle-v1",
  "bundle_id": "darksec-docs-2026-11-16",
  "created": "2026-11-16T00:00:00+00:00",
  "total_files": 1224,
  "total_bytes": 1800000000,
  "license_note": "Only redistribute documents you may share.",
  "parts": [
    {
      "name": "darksec-docs-2026-11-16.part01.zip",
      "url": "https://…/darksec-docs-2026-11-16.part01.zip",
      "bytes": 900000000,
      "sha256": "…64 hex characters…",
      "files": 600
    }
  ]
}
```

ZIP entries are library-relative paths such as `medical/foo.pdf`. Extraction is
restricted to configured document extensions, rejects absolute paths, `..`,
symlinks and path escapes, and caps archive size/file count.

## Legal

Only publish or install documents you have the right to redistribute. Many
government and public-domain manuals can be shared; commercial books and
third-party PDFs may not. Check every source before publishing a bundle.
