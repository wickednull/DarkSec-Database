"""Document-library extraction and OCR fallbacks."""
from __future__ import annotations

from pathlib import Path

from darksec import assistant, library, server
from darksec.db import Database


def _make_doc(db: Database, path: Path, name: str = "SCAN.pdf") -> int:
    return db.insert("library_docs", {
        "path": str(path), "name": name, "category": "MANUALS",
        "rel_path": str(path), "size": path.stat().st_size, "mtime": path.stat().st_mtime,
        "pages": None, "chars": 0, "text": "", "text_ts": None,
    })


def test_extraction_tools_reports_expected_keys():
    tools = library.extraction_tools()
    for key in ("pdftotext", "pypdf", "pdftoppm", "tesseract", "djvutxt", "ocr"):
        assert key in tools
        assert isinstance(tools[key], bool)


def test_missing_ocr_is_reported_clearly(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "darksec.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")
    doc_id = _make_doc(db, pdf)

    monkeypatch.setattr(library, "extract_text", lambda path, ext: ("", None))
    monkeypatch.setattr(library, "ocr_available", lambda: False)
    monkeypatch.setattr(library, "extraction_tools", lambda: {
        "pdftotext": True, "pypdf": True, "pdftoppm": False,
        "tesseract": False, "djvutxt": True, "ocr": False,
    })
    result = library.document_excerpt(db, doc_id, query="water", max_chars=500)

    assert result["text"] == ""
    assert "tesseract" in result["note"].lower()


def test_scanned_pdf_ocr_is_cached(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "darksec.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")
    doc_id = _make_doc(db, pdf)

    calls = {"n": 0}

    def fake_ocr(path, first_page=1, last_page=None, max_chars=120_000):
        calls["n"] += 1
        return "Scanned page about water treatment\fSecond page about filtration"

    monkeypatch.setattr(library, "extract_text", lambda path, ext: ("", None))
    monkeypatch.setattr(library, "ocr_available", lambda: True)
    monkeypatch.setattr(library, "ocr_pdf", fake_ocr)
    monkeypatch.setattr(library, "pdf_page_count", lambda path: 2)

    result = library.document_excerpt(db, doc_id, query="water", max_chars=800)
    assert result["ocr"] is True
    assert "water treatment" in result["text"]
    assert calls["n"] == 1

    row = db.one("SELECT text, chars, pages FROM library_docs WHERE id=?", (doc_id,))
    assert "water treatment" in row["text"]
    assert row["chars"] > 0
    assert row["pages"] == 2

    # The second read must use the cached text, not OCR again.
    monkeypatch.setattr(library, "ocr_pdf", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("OCR should not run when text is already cached")))
    cached = library.document_excerpt(db, doc_id, query="water", max_chars=800)
    assert cached["ocr"] is False
    assert "water treatment" in cached["text"]


def test_ai_search_library_handles_sqlite_rows(tmp_path):
    db = Database(str(tmp_path / "darksec.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")
    doc_id = _make_doc(db, pdf, name="WATER TREATMENT SCAN.pdf")
    text = "Water treatment and filtration notes"
    db.update("library_docs", doc_id, {"text": text, "chars": len(text)})
    db.fts_upsert(doc_id, "WATER TREATMENT SCAN.pdf", str(pdf), "MANUALS", text)

    out = server.ai_search_library(db, "water treatment")

    assert out["ok"] is True
    hit = next(item for item in out["results"] if item["id"] == doc_id)
    assert hit["indexed"] is True
    assert hit["snippet"]


def test_ai_search_library_flags_unindexed_documents(tmp_path):
    db = Database(str(tmp_path / "darksec.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")
    doc_id = _make_doc(db, pdf, name="SCANNED WATER MANUAL.pdf")
    db.fts_upsert(doc_id, "SCANNED WATER MANUAL.pdf", str(pdf), "MANUALS", "")

    out = server.ai_search_library(db, "scanned")

    assert out["ok"] is True
    hit = next(item for item in out["results"] if item["id"] == doc_id)
    assert hit["indexed"] is False
    assert "get_library_document" in hit["note"]


def test_library_context_handles_rows_without_text(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "darksec.db"))
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder")
    doc_id = _make_doc(db, pdf, name="SCANNED MANUAL.pdf")

    monkeypatch.setattr(assistant._library, "search", lambda *args, **kwargs: [{
        "id": doc_id, "name": "SCANNED MANUAL.pdf", "category": "MANUALS",
        "snippet": "",
    }])
    monkeypatch.setattr(assistant, "_quick_extract", lambda *args, **kwargs: "")

    context = assistant._library_context(db, "read the scanned pdf about water")

    assert "SCANNED MANUAL.pdf" in context
    assert "get_library_document" in context
