"""Offline document library: scanning, text extraction and FTS5 search.

Metadata indexing is fast. Deep text indexing uses poppler's pdftotext when
available (falling back to pypdf) and runs in the background so the console
stays responsive.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import threading
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

from . import config
from .db import Database, utcnow

MAX_TEXT_CHARS = 1_200_000
MAX_PDF_PAGES = 300
EXTRACT_TIMEOUT = 45

_JOB_LOCK = threading.Lock()
_JOB: dict = {
    "running": False,
    "mode": None,
    "total": 0,
    "done": 0,
    "errors": 0,
    "current": "",
    "started": None,
    "finished": None,
    "message": "idle",
}


def job_state() -> dict:
    with _JOB_LOCK:
        return dict(_JOB)


def _set_job(**kwargs) -> None:
    with _JOB_LOCK:
        _JOB.update(kwargs)


def library_roots() -> list[Path]:
    roots = [p for p in config.LIBRARY_ROOTS + config.EXTRA_LIBRARY_ROOTS if p.exists()]
    return roots


def _category(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    if len(parts) > 1:
        return parts[0].upper().replace("_", " ")
    return root.name.upper().replace("_", " ")


def scan_files() -> list[dict]:
    docs = []
    seen = set()
    for root in library_roots():
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in config.DOC_EXTENSIONS:
                continue
            if path.name.lower() in ("index.html", "index.htm"):
                continue
            key = path.name.lower()
            # The Books tree duplicates the top-level library; de-duplicate by
            # name+size but keep genuinely distinct documents.
            try:
                stat = path.stat()
            except OSError:
                continue
            dedupe = (key, stat.st_size)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            try:
                rel = path.relative_to(config.RESOURCE_ROOT).as_posix()
            except ValueError:
                rel = str(path)
            docs.append({
                "path": str(path.resolve()),
                "rel_path": rel,
                "name": path.name,
                "category": _category(path, root),
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "ext": path.suffix.lower(),
            })
    return docs


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def extract_text(path: Path, ext: str) -> tuple[str, int | None]:
    if ext in (".txt", ".md", ".html", ".htm"):
        raw = path.read_text(encoding="utf-8", errors="ignore")[:MAX_TEXT_CHARS]
        return (_strip_html(raw) if ext in (".html", ".htm") else raw), None
    if ext == ".pdf":
        if shutil.which("pdftotext"):
            try:
                # pdftotext emits a form feed between pages. Keep it: page
                # breaks are what later become citations ("p. 42").
                proc = subprocess.run(
                    ["pdftotext", "-q", "-layout", "-f", "1", "-l", str(MAX_PDF_PAGES), str(path), "-"],
                    capture_output=True, timeout=EXTRACT_TIMEOUT, check=False,
                )
                text = proc.stdout.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
                if text.strip():
                    return _normalize_page_breaks(text), None
            except Exception:
                pass
        try:
            import contextlib, io
            with contextlib.redirect_stderr(io.StringIO()):
                from pypdf import PdfReader
                reader = PdfReader(str(path))
                pages = min(len(reader.pages), MAX_PDF_PAGES)
                chunks = []
                for i in range(pages):
                    try:
                        chunks.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        continue
                return _normalize_page_breaks("\f".join(chunks))[:MAX_TEXT_CHARS], len(reader.pages)
        except Exception:
            return "", None
    if ext in (".epub", ".cbz"):
        try:
            import zipfile
            chunks = []
            with zipfile.ZipFile(path) as z:
                for name in z.namelist():
                    if name.lower().endswith((".xhtml", ".html", ".htm", ".txt")):
                        chunks.append(_strip_html(z.read(name).decode("utf-8", errors="ignore")))
                    if sum(len(c) for c in chunks) > MAX_TEXT_CHARS:
                        break
            # Each archive member is a chapter/section; form feeds preserve
            # those boundaries for section citations.
            return _normalize_page_breaks("\f".join(chunks))[:MAX_TEXT_CHARS], None
        except Exception:
            return "", None
    return "", None


def _normalize_page_breaks(text: str) -> str:
    """Collapse runs of page breaks and normalise line endings."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n*\f[\s\f]*", "\f", text)
    # A trailing breaker is a terminator, not an extra page.
    return text.rstrip("\f\n ")


def _page_offsets(text: str) -> list[int]:
    return [idx for idx, char in enumerate(text) if char == "\f"]


def page_number(text: str, offset: int) -> int:
    """1-based page/section number for a character offset."""
    if not text:
        return 1
    return bisect_left(_page_offsets(text), max(0, offset)) + 1


def page_count(text: str, fallback: int | None = None) -> int:
    breaks = text.count("\f")
    if breaks:
        return breaks + 1
    return int(fallback or 1)


def _looks_like_heading(line: str) -> bool:
    line = line.strip().strip("#").strip()
    if not (3 <= len(line) <= 90):
        return False
    if line.endswith((".", ",", ";", ":")) and not line.isupper():
        return False
    letters = sum(ch.isalpha() for ch in line)
    if letters < 3 or letters < len(line) * 0.35:
        return False
    if line.isupper():
        return True
    if re.match(r"^(chapter|section|part|appendix|unit|lesson)\b", line, flags=re.I):
        return True
    if re.match(r"^(\d+(\.\d+)*|[IVXLC]+)[.)]?\s+\S", line):
        return True
    return False


def section_for_offset(text: str, offset: int) -> str:
    """Nearest preceding heading line, if one is close enough to be useful."""
    start = max(0, offset - 6000)
    block = text[start:offset]
    for line in reversed(block.splitlines()):
        if _looks_like_heading(line):
            return " ".join(line.strip().strip("#").split())[:120]
    return ""


def _upsert_meta(db: Database, doc: dict, force: bool = False) -> tuple[int, bool]:
    """Insert/update metadata. Returns (row id, needs_text_extraction)."""
    row = db.one("SELECT * FROM library_docs WHERE path=?", (doc["path"],))
    if row:
        text = row["text"] or ""
        attempted = bool(row["text_ts"])
        same = int(row["size"] or 0) == int(doc["size"]) and abs(float(row["mtime"] or 0) - doc["mtime"]) < 1
        fields = {
            "name": doc["name"], "category": doc["category"], "rel_path": doc["rel_path"],
            "size": doc["size"], "mtime": doc["mtime"],
        }
        if not same or force:
            text = ""
            fields.update({"text": "", "chars": 0, "text_ts": None})
        db.update("library_docs", row["id"], fields)
        db.fts_upsert(row["id"], doc["name"], doc["rel_path"], doc["category"], text)
        # An empty text_ts means extraction has not been attempted yet. Once a
        # scanned/image-only PDF has been tried, do not re-run it on every
        # startup; a size/mtime change or FORCE RE-EXTRACT will retry it.
        needs_text = (not text) and (force or not same or not attempted)
        return row["id"], needs_text
    new_id = db.insert("library_docs", {
        "path": doc["path"], "name": doc["name"], "category": doc["category"],
        "rel_path": doc["rel_path"], "size": doc["size"], "mtime": doc["mtime"],
        "text": "", "chars": 0, "indexed_ts": utcnow(),
    })
    db.fts_upsert(new_id, doc["name"], doc["rel_path"], doc["category"], "")
    return new_id, True


def _index_worker(db: Database, docs: list[dict], deep: bool, force: bool) -> None:
    errors = 0
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=2 if deep else 4) as pool:
            future_map = {}
            for doc in docs:
                future = pool.submit(_upsert_meta, db, doc, force)
                future_map[future] = doc
            metadata = []
            for future in as_completed(future_map):
                try:
                    doc_id, needs_text = future.result()
                    metadata.append((doc_id, future_map[future], needs_text))
                except Exception as exc:
                    errors += 1
                    _set_job(errors=errors, message=f"metadata error: {exc}")
                done += 1
                _set_job(done=done, total=len(docs), message="metadata pass")
            if not deep:
                return

            _set_job(done=0, total=len(metadata), message="text extraction")
            done = 0
            for doc_id, doc, needs_text in metadata:
                if not needs_text:
                    done += 1
                    _set_job(done=done)
                    continue
                _set_job(current=doc["name"])
                try:
                    text, pages = extract_text(Path(doc["path"]), doc["ext"])
                    update = {"text": text, "chars": len(text), "text_ts": utcnow()}
                    if pages is not None:
                        update["pages"] = pages
                    db.update("library_docs", doc_id, update)
                    db.fts_upsert(doc_id, doc["name"], doc["rel_path"], doc["category"], text)
                except Exception:
                    errors += 1
                done += 1
                _set_job(done=done, errors=errors)
    finally:
        _set_job(running=False, finished=utcnow(), current="",
                 message="complete" if not errors else f"complete with {errors} errors")


def start_index(deep: bool = True, force: bool = False) -> dict:
    if _JOB["running"]:
        return job_state()
    docs = scan_files()
    _set_job(running=True, mode="deep" if deep else "metadata", total=len(docs), done=0,
             errors=0, current="", started=utcnow(), finished=None,
             message="scanning")
    db = Database()
    thread = threading.Thread(target=_index_worker, args=(db, docs, deep, force), daemon=True)
    thread.start()
    return job_state()


def metadata_count(db: Database) -> int:
    row = db.one("SELECT count(*) c FROM library_docs")
    return int(row["c"]) if row else 0


def text_count(db: Database) -> int:
    row = db.one("SELECT count(*) c FROM library_docs WHERE chars > 0")
    return int(row["c"]) if row else 0


def categories(db: Database) -> list[dict]:
    rows = db.query(
        "SELECT category, count(*) c, sum(chars) chars FROM library_docs GROUP BY category ORDER BY category"
    )
    return [{"category": r["category"], "count": r["c"], "chars": r["chars"] or 0} for r in rows]


def _fts_query(q: str) -> str:
    tokens = re.findall(r"[\w]+", q, flags=re.UNICODE)
    tokens = [t for t in tokens if len(t) >= 1][:20]
    return " AND ".join(f'"{t}"*' for t in tokens)


def search(db: Database, q: str, category: str | None = None, limit: int = 40) -> list[dict]:
    q = (q or "").strip()
    if not q:
        rows = db.query(
            "SELECT id,name,rel_path,category,size,chars,text_ts FROM library_docs "
            + ("WHERE category=? " if category else "")
            + "ORDER BY text_ts DESC, name LIMIT ?",
            ([category] if category else []) + [limit],
        )
        return [dict(r) | {"snippet": ""} for r in rows]
    match = _fts_query(q)
    if not match:
        return []
    sql = (
        "SELECT d.id, d.name, d.rel_path, d.category, d.size, d.chars, d.text_ts, "
        "snippet(library_fts, 3, '[[[', ']]]', ' … ', 24) AS snip, "
        "bm25(library_fts, 8.0, 4.0, 2.0, 1.0) AS score "
        "FROM library_fts JOIN library_docs d ON d.id = library_fts.rowid "
        "WHERE library_fts MATCH ? "
    )
    params: list = [match]
    if category:
        sql += "AND d.category = ? "
        params.append(category)
    sql += "ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        rows = db.query(sql, params)
    except Exception:
        # Fall back to LIKE if the FTS syntax is rejected.
        like = f"%{q}%"
        rows = db.query(
            "SELECT id,name,rel_path,category,size,chars,text_ts,'' snip,0 score FROM library_docs "
            "WHERE name LIKE ? OR text LIKE ? ORDER BY name LIMIT ?",
            (like, like, limit),
        )
    out = []
    for r in rows:
        d = dict(r)
        d.pop("score", None)
        d["snippet"] = d.get("snip") or ""
        d.pop("snip", None)
        out.append(d)
    return out


def _norm_title(value: str) -> str:
    """Lowercase and strip punctuation/extensions for fuzzy title matching."""
    text = re.sub(r"\.(pdf|epub|cbz|txt|md|html?|docx?|djvu)$", "",
                  str(value or "").strip(), flags=re.I)
    text = text.lower().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def find_document(db: Database, query: str, limit: int = 6) -> list[dict]:
    """Find documents by title, tolerating misspellings and partial names.

    FTS keyword search fails on names like "GRIZZLEY WOOD LATHE MANUAL" when
    the operator types "Grizzly" or on "BEGINEERS" when they type "Beginners".
    This scores normalized titles with token overlap and edit similarity.
    """
    q = _norm_title(query)
    if not q:
        return []
    q_set = set(q.split())
    out = []
    for row in db.query(
        "SELECT id,name,rel_path,category,size,chars,text_ts FROM library_docs"
    ):
        name = _norm_title(row["name"])
        if not name:
            continue
        n_set = set(name.split())
        exact = q == name
        contains = q in name or (len(name) >= 4 and name in q)
        overlap = len(q_set & n_set) / max(1, len(q_set))
        ratio = SequenceMatcher(None, q, name).ratio()
        hits = 0
        for token in q_set:
            if token in n_set or any(
                SequenceMatcher(None, token, nt).ratio() >= 0.82 for nt in n_set
            ):
                hits += 1
        token_ratio = hits / max(1, len(q_set))
        score = 1.0 if exact else 0.94 if contains else max(
            ratio * 0.82, overlap * 0.8, token_ratio * 0.92
        )
        if score < 0.34:
            continue
        out.append({
            "id": row["id"],
            "name": row["name"],
            "category": row["category"],
            "rel_path": row["rel_path"],
            "size": row["size"],
            "chars": row["chars"],
            "text_ts": row["text_ts"],
            "score": round(score, 3),
        })
    out.sort(key=lambda item: (-item["score"], item["name"]))
    return out[:limit]


def document_excerpt(db: Database, doc_id: int, query: str = "",
                     max_chars: int = 6000, page: int | None = None,
                     span: int = 1) -> dict:
    """Return extracted text, optionally focused around query terms.

    Text keeps the page breaks written by ``extract_text`` (form feeds), so
    excerpts can report the page or section they came from. ``page`` reads a
    specific 1-based page instead of searching.
    """
    try:
        budget = max(500, min(int(max_chars or 6000), 20_000))
    except (TypeError, ValueError):
        budget = 6000
    row = db.one(
        "SELECT id,name,rel_path,path,category,chars,pages,text FROM library_docs WHERE id=?",
        (doc_id,),
    )
    if not row:
        raise ValueError("Document not found")
    text = str(row["text"] or "")
    if not text.strip() and row["rel_path"]:
        path = resolve_doc(str(row["rel_path"]))
        if path and path.is_file():
            text = extract_text(path, path.suffix.lower())[0]
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return {"text": "", "chars": 0, "truncated": False, "focus": False,
                "pages": [], "page_count": 0, "sections": [], "unit": "page",
                "note": "No extractable text (scanned/image PDF or not yet indexed)."}

    suffix = Path(str(row["rel_path"] or row["path"] or "")).suffix.lower()
    unit = "section" if suffix in (".epub", ".cbz") else "page"
    breaks = _page_offsets(text)
    total_pages = page_count(text, row["pages"])
    available = bool(breaks) or total_pages <= 1

    def page_at(offset: int) -> int:
        return bisect_left(breaks, max(0, offset)) + 1

    def clean(piece: str) -> str:
        return re.sub(r"\n*\f[\s\f]*", "\n\n", piece).strip()

    # Explicit page/section request: slice between breaks.
    if page not in (None, "", 0):
        try:
            first = max(1, int(page))
            count = max(1, min(int(span or 1), 20))
        except (TypeError, ValueError):
            raise ValueError("page and span must be numbers")
        if not available:
            raise ValueError(
                f"This document has no {unit} breaks in its index; re-index it to enable citations."
            )
        if first > total_pages:
            raise ValueError(f"{unit.title()} {first} is outside this document ({total_pages} {unit}s).")
        start = 0 if first == 1 else breaks[first - 2] + 1
        last = min(total_pages, first + count - 1)
        end = len(text) if last >= total_pages else breaks[last - 1]
        body = clean(text[start:end])[:budget]
        heading = section_for_offset(text, start)
        return {"text": body, "chars": len(body), "truncated": len(body) >= budget,
                "focus": False, "pages": [first, last], "page_count": total_pages,
                "sections": [heading] if heading else [], "unit": unit,
                "citation": _citation(row["name"], unit, first, last)}

    q = str(query or "").strip()
    if q:
        tokens = [t for t in re.findall(r"[\w]+", q, flags=re.UNICODE) if len(t) >= 3][:6]
        windows: list[tuple[int, int]] = []
        lower = text.lower()
        for token in tokens:
            start = 0
            while len(windows) < 12:
                idx = lower.find(token.lower(), start)
                if idx < 0:
                    break
                windows.append((max(0, idx - 350), min(len(text), idx + 650)))
                start = idx + len(token)
            if len(windows) >= 12:
                break
        if windows:
            merged: list[tuple[int, int]] = []
            for start, end in sorted(windows):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            parts = []
            used = 0
            page_refs: list[int] = []
            sections: list[str] = []
            for start, end in merged:
                match_at = None
                for token in tokens:
                    idx = lower.find(token.lower(), start, end)
                    if idx >= 0 and (match_at is None or idx < match_at):
                        match_at = idx
                anchor = match_at if match_at is not None else start
                piece = text[start:end]
                if used + len(piece) > budget:
                    piece = piece[:max(0, budget - used)]
                piece = clean(piece)
                if not piece:
                    continue
                parts.append(("…" if start > 0 else "") + piece
                             + ("…" if end < len(text) else ""))
                page_refs.append(page_at(anchor))
                heading = section_for_offset(text, anchor)
                if heading and heading not in sections:
                    sections.append(heading)
                used += len(piece)
                if used >= budget:
                    break
            excerpt = "\n\n".join(parts)
            pages = ([min(page_refs), max(page_refs)] if page_refs and breaks else [])
            return {"text": excerpt, "chars": len(text),
                    "truncated": used < len(text), "focus": True,
                    "pages": pages, "page_numbers": page_refs,
                    "page_count": total_pages, "sections": sections[:6], "unit": unit,
                    "citation": _citation(row["name"], unit, pages[0] if pages else None,
                                          pages[1] if pages and len(pages) > 1 else None)}

    excerpt = clean(text[:budget])
    pages = [1, page_at(budget)] if breaks else []
    heading = section_for_offset(text, 0)
    return {"text": excerpt, "chars": len(text),
            "truncated": len(excerpt) < len(text), "focus": False,
            "pages": pages, "page_count": total_pages,
            "sections": [heading] if heading else [], "unit": unit,
            "citation": _citation(row["name"], unit, pages[0] if pages else None,
                                  pages[1] if pages and len(pages) > 1 else None)}


def _citation(name: str, unit: str, first=None, last=None) -> str:
    short = str(name or "Document")
    if not first:
        return short
    span = f"{first}–{last}" if last and last != first else str(first)
    return f"{short} — {unit} {span}"


def page_for_query(text: str, query: str) -> int | None:
    """Approximate page number of the first query token in a document."""
    if not text or "\f" not in text:
        return None
    tokens = [t for t in re.findall(r"[\w]+", query or "", flags=re.UNICODE) if len(t) >= 3][:3]
    lower = text.lower()
    for token in tokens:
        idx = lower.find(token.lower())
        if idx >= 0:
            return page_number(text, idx)
    return None


def resolve_doc(rel_path: str) -> Path | None:
    """Resolve a doc path safely inside a configured library root."""
    candidate = Path(rel_path)
    if candidate.is_absolute():
        candidate = candidate.resolve()
    else:
        candidate = (config.RESOURCE_ROOT / rel_path).resolve()
    for root in library_roots():
        try:
            root_res = root.resolve()
            if candidate.is_relative_to(root_res) and candidate.is_file():
                return candidate
        except ValueError:
            continue
    return None


def stats(db: Database) -> dict:
    total = metadata_count(db)
    deep = text_count(db)
    row = db.one("SELECT sum(chars) chars FROM library_docs")
    db_size = Path(db.path).stat().st_size if Path(db.path).exists() else 0
    return {"documents": total, "text_indexed": deep, "chars": int(row["chars"] or 0),
            "db_bytes": db_size, "job": job_state(), "categories": categories(db)}
