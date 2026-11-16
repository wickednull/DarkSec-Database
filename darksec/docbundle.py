"""Download and install a packaged document-library bundle.

The bundle is a manifest plus one or more ZIP parts. Each part contains
library-relative paths such as ``medical/foo.pdf``. The installer downloads to
``fielddata/library/.parts``, verifies SHA-256, extracts only document files
into ``fielddata/library``, deletes the archive, and starts a metadata index.

Hosting is intentionally provider-neutral: the manifest URL can point at a
GitHub Release asset, a static host, an Internet Archive item, or any direct
HTTPS URL. Proton Drive share pages are not direct file URLs and usually need
to be exported or mirrored to a direct-download host first.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat as statmod
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from . import config, library

MANIFEST_FORMAT = "darksec-document-bundle-v1"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PART_BYTES = 8 * 1024 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024 * 1024
MAX_FILES = 100_000
CHUNK = 1024 * 1024
UA = "DarkSec/2.0 (document bundle installer)"
READ_TIMEOUT = 60
CONNECT_TIMEOUT = 20

_LOCK = threading.Lock()
_JOB: dict[str, Any] = {
    "running": False,
    "cancelled": False,
    "phase": "idle",
    "message": "idle",
    "manifest_url": "",
    "bundle_id": "",
    "part_index": 0,
    "part_count": 0,
    "part_name": "",
    "part_bytes": 0,
    "part_downloaded": 0,
    "total_bytes": 0,
    "downloaded_bytes": 0,
    "files_extracted": 0,
    "files_skipped": 0,
    "started": None,
    "finished": None,
    "error": None,
}


class BundleError(RuntimeError):
    pass


class Cancelled(BundleError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> Path:
    return Path(config.DOC_BUNDLE_DIR)


def _parts_dir() -> Path:
    return _root() / ".parts"


def _state_file() -> Path:
    return _root() / ".bundle-state.json"


def _set_job(**kwargs) -> None:
    with _LOCK:
        _JOB.update(kwargs)


def job_state() -> dict[str, Any]:
    with _LOCK:
        return dict(_JOB)


def _reset_job(manifest_url: str) -> None:
    with _LOCK:
        _JOB.update({
            "running": True, "cancelled": False, "phase": "manifest",
            "message": "Fetching manifest", "manifest_url": manifest_url,
            "bundle_id": "", "part_index": 0, "part_count": 0,
            "part_name": "", "part_bytes": 0, "part_downloaded": 0,
            "total_bytes": 0, "downloaded_bytes": 0,
            "files_extracted": 0, "files_skipped": 0,
            "started": _now(), "finished": None, "error": None,
        })


def _check_cancel() -> None:
    with _LOCK:
        if _JOB.get("cancelled"):
            raise Cancelled("cancelled by operator")


def cancel() -> dict[str, Any]:
    with _LOCK:
        if _JOB.get("running"):
            _JOB["cancelled"] = True
            _JOB["message"] = "Cancelling…"
    return job_state()


def _safe_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.username or parsed.password:
        raise BundleError("manifest URL must not contain embedded credentials")
    if parsed.scheme == "https" and parsed.hostname:
        return parsed.geturl()
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return parsed.geturl()
    raise BundleError("manifest URL must be https (localhost http is allowed for testing)")


def _read_state() -> dict[str, Any]:
    try:
        if _state_file().is_file():
            data = json.loads(_state_file().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _write_state(data: dict[str, Any]) -> None:
    _root().mkdir(parents=True, exist_ok=True)
    tmp = _state_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_state_file())


def _local_counts() -> tuple[int, int]:
    root = _root()
    files = 0
    total = 0
    if not root.is_dir():
        return 0, 0
    allowed = {e.lower() for e in config.DOC_EXTENSIONS}
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.suffix.lower() in allowed:
                files += 1
                total += path.stat().st_size
        except OSError:
            continue
    return files, total


def status() -> dict[str, Any]:
    state = _read_state()
    try:
        free = shutil.disk_usage(_root()).free
    except OSError:
        free = None
    files, total = _local_counts()
    return {
        "manifest_url": state.get("manifest_url"),
        "bundle_id": state.get("bundle_id"),
        "installed_at": state.get("installed_at"),
        "installed_parts": len(state.get("parts") or {}),
        "total_parts": len(state.get("part_names") or []),
        "status": state.get("status") or "not-installed",
        "local_files": files,
        "local_bytes": total,
        "disk_free": free,
        "job": job_state(),
        "note": "Only install document bundles you are legally allowed to redistribute. Bundle hosting is provider-neutral; a direct manifest URL is required.",
    }


def _get_manifest(url: str) -> dict[str, Any]:
    try:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                            headers={"User-Agent": UA, "Accept": "application/json"},
                            stream=True)
        resp.raise_for_status()
        length = int(resp.headers.get("content-length") or 0)
        if length and length > MAX_MANIFEST_BYTES:
            raise BundleError("manifest is too large")
        chunks = []
        total = 0
        for chunk in resp.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_MANIFEST_BYTES:
                raise BundleError("manifest is too large")
            chunks.append(chunk)
    except BundleError:
        raise
    except Exception as exc:
        raise BundleError(f"could not download manifest: {str(exc)[:200]}") from exc
    try:
        manifest = json.loads(b"".join(chunks))
    except Exception as exc:
        raise BundleError(f"manifest is not valid JSON: {str(exc)[:120]}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise BundleError(f"unsupported manifest format (expected {MANIFEST_FORMAT})")
    parts = manifest.get("parts")
    if not isinstance(parts, list) or not parts:
        raise BundleError("manifest has no parts")
    if len(parts) > 200:
        raise BundleError("manifest has too many parts")
    out_parts = []
    names = set()
    total_bytes = 0
    total_files = 0
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            raise BundleError(f"part {i + 1} is malformed")
        name = str(part.get("name") or "").strip()
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise BundleError(f"part {i + 1} has an unsafe name")
        if name in names:
            raise BundleError(f"duplicate part name: {name}")
        names.add(name)
        try:
            size = int(part.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0 or size > MAX_PART_BYTES:
            raise BundleError(f"part {name} has an invalid size")
        digest = str(part.get("sha256") or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BundleError(f"part {name} is missing a valid SHA-256")
        part_url = str(part.get("url") or urljoin(url, name))
        part_url = urljoin(url, part_url)
        if not _safe_url(part_url):
            raise BundleError(f"part {name} has an unsupported URL")
        files = int(part.get("files") or 0)
        total_bytes += size
        total_files += max(0, files)
        out_parts.append({"name": name, "url": part_url, "bytes": size,
                          "sha256": digest, "files": max(0, files)})
    if total_bytes > MAX_TOTAL_BYTES:
        raise BundleError("bundle is larger than the supported limit")
    return {
        "format": MANIFEST_FORMAT,
        "bundle_id": str(manifest.get("bundle_id") or "unnamed")[:120],
        "created": str(manifest.get("created") or "")[:80],
        "total_bytes": total_bytes,
        "total_files": total_files,
        "license_note": str(manifest.get("license_note") or "")[:500],
        "parts": out_parts,
    }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_part(part: dict[str, Any], dest: Path, base_downloaded: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".download")
    existing = tmp.stat().st_size if tmp.is_file() else 0
    if existing > part["bytes"]:
        tmp.unlink(missing_ok=True)
        existing = 0
    headers = {"User-Agent": UA, "Accept": "application/zip,application/octet-stream"}
    if existing:
        headers["Range"] = f"bytes={existing}-"
    try:
        resp = requests.get(part["url"], headers=headers, stream=True,
                            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        if resp.status_code == 416 and existing == part["bytes"]:
            resp.close()
            downloaded = existing
        else:
            resp.raise_for_status()
            if existing and resp.status_code == 200:
                existing = 0
                tmp.unlink(missing_ok=True)
            mode = "ab" if existing and resp.status_code == 206 else "wb"
            downloaded = existing
            with tmp.open(mode) as fh:
                for chunk in resp.iter_content(CHUNK):
                    if not chunk:
                        continue
                    _check_cancel()
                    fh.write(chunk)
                    downloaded += len(chunk)
                    _set_job(part_downloaded=downloaded,
                             downloaded_bytes=base_downloaded + downloaded,
                             message=f"Downloading {part['name']} ({downloaded / 1048576:.0f} MB)")
                    if downloaded > part["bytes"]:
                        raise BundleError(f"{part['name']} exceeded the expected size")
    except BundleError:
        raise
    except Exception as exc:
        raise BundleError(f"download failed for {part['name']}: {str(exc)[:200]}") from exc
    if downloaded != part["bytes"]:
        raise BundleError(f"{part['name']} size mismatch (got {downloaded}, expected {part['bytes']})")
    digest = _sha256(tmp)
    if digest != part["sha256"]:
        tmp.unlink(missing_ok=True)
        raise BundleError(f"{part['name']} SHA-256 mismatch")
    tmp.replace(dest)


def _safe_members(zf) -> list[tuple[Any, Path]]:
    selected = []
    total = 0
    allowed = {e.lower() for e in config.DOC_EXTENSIONS}
    for info in zf.infolist():
        if info.is_dir():
            continue
        if info.filename.startswith("/") or "\\" in info.filename:
            raise BundleError(f"unsafe path in archive: {info.filename[:120]}")
        rel = PurePosixPath(info.filename)
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            raise BundleError(f"unsafe path in archive: {info.filename[:120]}")
        if rel.suffix.lower() not in allowed:
            continue
        if statmod.S_ISLNK(info.external_attr >> 16):
            raise BundleError(f"symlink in archive: {info.filename[:120]}")
        total += max(0, int(info.file_size))
        if total > MAX_UNCOMPRESSED_BYTES:
            raise BundleError("archive exceeds the uncompressed size limit")
        selected.append((info, Path(*rel.parts)))
    if len(selected) > MAX_FILES:
        raise BundleError("archive contains too many files")
    return selected


def _extract_part(archive: Path) -> tuple[int, int]:
    import zipfile

    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    extracted = 0
    skipped = 0
    with zipfile.ZipFile(archive) as zf:
        members = _safe_members(zf)
        for info, rel in members:
            _check_cancel()
            target = (root / rel).resolve()
            try:
                if not target.is_relative_to(root.resolve()):
                    raise BundleError(f"path escapes library root: {info.filename[:120]}")
            except ValueError:
                raise BundleError(f"path escapes library root: {info.filename[:120]}")
            if target.exists():
                skipped += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, CHUNK)
            extracted += 1
            _set_job(files_extracted=extracted, files_skipped=skipped,
                     message=f"Extracting {info.filename[:80]}")
    return extracted, skipped


def _worker(manifest_url: str, force: bool = False) -> None:
    try:
        manifest = _get_manifest(manifest_url)
        state = _read_state()
        same = (not force and state.get("bundle_id") == manifest["bundle_id"]
                and state.get("status") == "installed"
                and set(state.get("part_names") or []) == {p["name"] for p in manifest["parts"]})
        if same:
            _set_job(running=False, phase="complete", message="Bundle already installed",
                     finished=_now(), bundle_id=manifest["bundle_id"])
            return
        _set_job(bundle_id=manifest["bundle_id"], part_count=len(manifest["parts"]),
                 total_bytes=manifest["total_bytes"])
        try:
            free = shutil.disk_usage(_root()).free
        except OSError:
            free = None
        largest = max((p["bytes"] for p in manifest["parts"]), default=0)
        needed = manifest["total_bytes"] + largest + 512 * 1024 * 1024
        if free is not None and free < needed:
            raise BundleError(
                f"not enough free disk space (need about {needed / 1073741824:.1f} GB, "
                f"have {free / 1073741824:.1f} GB)"
            )
        state = {
            "bundle_id": manifest["bundle_id"], "manifest_url": manifest_url,
            "created": manifest["created"], "part_names": [p["name"] for p in manifest["parts"]],
            "parts": {}, "status": "downloading", "started": _now(),
            "license_note": manifest["license_note"],
        }
        _write_state(state)
        base_downloaded = 0
        for idx, part in enumerate(manifest["parts"], start=1):
            _check_cancel()
            _set_job(phase="download", part_index=idx, part_name=part["name"],
                     part_bytes=part["bytes"], part_downloaded=0,
                     downloaded_bytes=base_downloaded,
                     message=f"Downloading {part['name']} ({idx}/{len(manifest['parts'])})")
            dest = _parts_dir() / part["name"]
            _download_part(part, dest, base_downloaded)
            _set_job(phase="extract", part_index=idx, part_name=part["name"],
                     message=f"Extracting {part['name']} ({idx}/{len(manifest['parts'])})")
            extracted, skipped = _extract_part(dest)
            base_downloaded += part["bytes"]
            state["parts"][part["name"]] = {
                "sha256": part["sha256"], "files": part["files"],
                "extracted": extracted, "skipped": skipped, "installed_at": _now(),
            }
            state["status"] = "installing"
            _write_state(state)
            try:
                dest.unlink()
            except OSError:
                pass
        state["status"] = "installed"
        state["installed_at"] = _now()
        _write_state(state)
        try:
            library.start_index(deep=False, force=False)
            index_note = "metadata index started"
        except Exception:
            index_note = "metadata index could not start; run START INDEX manually"
        _set_job(running=False, phase="complete", message=f"Bundle installed · {index_note}",
                 finished=_now(), error=None,
                 downloaded_bytes=manifest["total_bytes"])
    except Cancelled:
        _set_job(running=False, phase="cancelled", message="Cancelled",
                 finished=_now(), error=None)
    except Exception as exc:
        _set_job(running=False, phase="error", message=str(exc)[:300],
                 finished=_now(), error=str(exc)[:300])
        try:
            state = _read_state()
            state["status"] = "error"
            state["error"] = str(exc)[:300]
            _write_state(state)
        except Exception:
            pass


def start(manifest_url: str, force: bool = False) -> dict[str, Any]:
    url = _safe_url(manifest_url)
    with _LOCK:
        if _JOB.get("running"):
            return {"ok": False, "error": "a bundle download is already running", **job_state()}
    _reset_job(url)
    thread = threading.Thread(target=_worker, args=(url, force), daemon=True,
                              name="darksec-docbundle")
    thread.start()
    return {"ok": True, **job_state()}
