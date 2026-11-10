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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                proc = subprocess.run(
                    ["pdftotext", "-q", "-layout", "-f", "1", "-l", str(MAX_PDF_PAGES), str(path), "-"],
                    capture_output=True, timeout=EXTRACT_TIMEOUT, check=False,
                )
                text = proc.stdout.decode("utf-8", errors="ignore")[:MAX_TEXT_CHARS]
                if text.strip():
                    return text, None
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
                return ("\n".join(chunks))[:MAX_TEXT_CHARS], len(reader.pages)
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
            return ("\n".join(chunks))[:MAX_TEXT_CHARS], None
        except Exception:
            return "", None
    return "", None


def _upsert_meta(db: Database, doc: dict, force: bool = False) -> tuple[int, bool]:
    """Insert/update metadata. Returns (row id, needs_text_extraction)."""
    row = db.one("SELECT * FROM library_docs WHERE path=?", (doc["path"],))
    if row:
        text = row["text"] or ""
        same = int(row["size"] or 0) == int(doc["size"]) and abs(float(row["mtime"] or 0) - doc["mtime"]) < 1
        db.update("library_docs", row["id"], {
            "name": doc["name"], "category": doc["category"], "rel_path": doc["rel_path"],
            "size": doc["size"], "mtime": doc["mtime"],
        })
        if not same or force:
            text = ""
        db.fts_upsert(row["id"], doc["name"], doc["rel_path"], doc["category"], text)
        return row["id"], (not text)
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
