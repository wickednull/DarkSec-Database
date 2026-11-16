"""Document bundle installer tests (no external network)."""
from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from darksec import config, docbundle


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _serve(directory: Path):
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *args, directory=str(directory), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _wait_for_job(timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = docbundle.job_state()
        if not state.get("running"):
            return state
        time.sleep(0.05)
    raise AssertionError("document bundle job did not finish")


def test_docbundle_installs_and_extracts(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    library_dir = tmp_path / "library"
    monkeypatch.setattr(config, "DOC_BUNDLE_DIR", library_dir)
    started = []
    monkeypatch.setattr(docbundle.library, "start_index", lambda **kwargs: started.append(kwargs) or {})

    part = source / "bundle.part01.zip"
    with zipfile.ZipFile(part, "w") as zf:
        zf.writestr("medical/test-doc.pdf", b"%PDF-1.4 test")
        zf.writestr("comms/readme.txt", b"hello")
        zf.writestr("comms/ignored.exe", b"not a document")
    manifest = {
        "format": docbundle.MANIFEST_FORMAT,
        "bundle_id": "test-bundle",
        "created": "2026-01-01T00:00:00+00:00",
        "total_files": 2,
        "total_bytes": part.stat().st_size,
        "parts": [{
            "name": part.name,
            "url": part.name,
            "bytes": part.stat().st_size,
            "sha256": _sha256(part),
            "files": 2,
        }],
    }
    (source / "document-library.bundle.json").write_text(json.dumps(manifest), encoding="utf-8")

    server, thread = _serve(source)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/document-library.bundle.json"
        result = docbundle.start(url)
        assert result["ok"] is True
        job = _wait_for_job()
        assert job["phase"] == "complete", job
        assert (library_dir / "medical/test-doc.pdf").read_bytes() == b"%PDF-1.4 test"
        assert (library_dir / "comms/readme.txt").read_text() == "hello"
        assert not (library_dir / "comms/ignored.exe").exists()
        state = json.loads((library_dir / ".bundle-state.json").read_text())
        assert state["status"] == "installed"
        assert state["bundle_id"] == "test-bundle"
        assert started, "metadata index should be requested after install"

        # A second run with the same bundle is a no-op unless forced.
        assert docbundle.start(url)["ok"] is True
        job = _wait_for_job()
        assert job["phase"] == "complete"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_docbundle_rejects_zip_slip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DOC_BUNDLE_DIR", tmp_path / "library")
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape.pdf", b"x")
    with pytest.raises(docbundle.BundleError):
        docbundle._extract_part(archive)


def test_docbundle_rejects_non_https_manifest_url():
    with pytest.raises(docbundle.BundleError):
        docbundle.start("http://example.com/document-library.bundle.json")
