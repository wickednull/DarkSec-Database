"""Local Ollama-backed survival assistant.

Talks only to a configured local Ollama instance (default 127.0.0.1:11434).
No cloud dependency. Errors are returned clearly so the UI can stay offline-safe.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import config as _config

import requests

from .db import Database, utcnow
from . import library as _library
DEFAULT_BASE = "http://127.0.0.1:11434"
DEFAULT_KEEP_ALIVE = "30m"
UA = {"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/json", "Content-Type": "application/json"}

# Ollama is a local daemon; /api/tags is cheap but the assistant screen asks
# twice (status + models) and every settings test asks again. A very short
# cache keeps the UI snappy without hiding a daemon that just started.
_TAGS_TTL = 5.0
_TAGS_CACHE: dict[str, tuple[float, dict]] = {}
_TAGS_LOCK = threading.Lock()


def _settings(db: Database) -> dict:
    s = db.all_settings()
    base = str(s.get("ollama_base_url") or DEFAULT_BASE).strip().rstrip("/")
    if not base.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        # Keep local-first by default; allow explicit non-local only if operator configured it.
        pass
    return {
        "enabled": bool(s.get("assistant_enabled", True)),
        "base_url": base or DEFAULT_BASE,
        "model": str(s.get("ollama_model") or "").strip(),
        "system_prompt": str(s.get("assistant_system_prompt") or "").strip(),
        "timeout": max(5, min(1800, int(float(s.get("assistant_timeout_sec") or 300)))),
        "temperature": max(0.0, min(2.0, float(s.get("assistant_temperature") or 0.2))),
        "num_ctx": max(512, min(32768, int(float(s.get("assistant_num_ctx") or 4096)))),
        "max_tokens": max(128, min(8192, int(float(s.get("assistant_max_tokens") or 2048)))),
        "think": bool(s.get("assistant_think", False)),
        "history_filter": bool(s.get("assistant_history_filter", True)),
        "tools": bool(s.get("assistant_tools", True)),
        "tools_auto": bool(s.get("assistant_tools_auto", True)),
        "library_context": bool(s.get("assistant_library_context", True)),
        "system_tools": bool(s.get("assistant_system_tools", False)),
        "system_root": str(s.get("assistant_system_root") or "~").strip() or "~",
        "keep_alive": str(s.get("assistant_keep_alive") or DEFAULT_KEEP_ALIVE).strip() or DEFAULT_KEEP_ALIVE,
        "opsec_mode": bool(s.get("opsec_mode", False)),
        "units": s.get("units") or "metric",
        "callsign": s.get("callsign") or "",
    }


def _tags(base_url: str, timeout: float, fresh: bool = False) -> tuple[list[dict] | None, str | None]:
    """Return (models, error) for an Ollama daemon, with a small TTL cache."""
    now = time.monotonic()
    if not fresh:
        with _TAGS_LOCK:
            hit = _TAGS_CACHE.get(base_url)
            if hit and now - hit[0] < _TAGS_TTL:
                entry = hit[1]
                return list(entry.get("models") or []), entry.get("error")
    try:
        resp = requests.get(f"{base_url}/api/tags", headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"}, timeout=max(1.0, min(6.0, timeout)))
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        models = []
        for m in (data.get("models") or []):
            if isinstance(m, dict) and m.get("name"):
                models.append({"name": m.get("name"), "size": m.get("size"), "modified_at": m.get("modified_at"), "details": m.get("details") or {}})
        entry = {"models": models, "error": None}
        with _TAGS_LOCK:
            _TAGS_CACHE[base_url] = (now, entry)
        return models, None
    except Exception as e:
        entry = {"models": [], "error": f"Ollama unreachable at {base_url}: {e}"[:400]}
        with _TAGS_LOCK:
            # Cache failures briefly too: a stopped daemon should not cost the
            # UI a full TCP timeout on every page render.
            _TAGS_CACHE[base_url] = (now, entry)
        return None, entry["error"]


def status(db: Database, fresh: bool = False) -> dict:
    cfg = _settings(db)
    out = {
        "enabled": cfg["enabled"],
        "configured": bool(cfg["model"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "reachable": False,
        "models": [],
        "error": None,
        "warning": None,
        "timeout_sec": cfg["timeout"],
        "keep_alive": cfg["keep_alive"],
        "checked_at": utcnow(),
        "local_only_hint": cfg["base_url"].startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")),
    }
    if not cfg["enabled"]:
        out["error"] = "Assistant disabled in settings."
        return out
    models, error = _tags(cfg["base_url"], cfg["timeout"], fresh=fresh)
    if error:
        out["error"] = error
        return out
    out["reachable"] = True
    out["models"] = models or []
    names = {m["name"] for m in out["models"]}
    if cfg["model"] and cfg["model"] not in names:
        # keep configured, but warn
        out["warning"] = f"Configured model '{cfg['model']}' not found in Ollama tags."
    return out


def list_models(db: Database, fresh: bool = False) -> dict:
    st = status(db, fresh=fresh)
    return {"ok": st.get("reachable", False), "models": st.get("models", []), "base_url": st.get("base_url"), "configured_model": st.get("model"), "error": st.get("error"), "warning": st.get("warning")}


def warm(db: Database, model: str | None = None) -> dict:
    """Load a model into memory so the first chat is not cold.

    Uses an empty /api/chat request, which Ollama answers with done_reason
    "load" without running prompt evaluation. Safe to call repeatedly.
    """
    cfg = _settings(db)
    use_model = (model or cfg["model"]).strip()
    if not cfg["enabled"] or not use_model:
        return {"ok": False, "error": "No model configured."}
    try:
        resp = requests.post(
            f"{cfg['base_url']}/api/chat",
            headers=UA,
            data=json.dumps({
                "model": use_model,
                "messages": [],
                "stream": False,
                "keep_alive": cfg["keep_alive"],
                "options": {"num_ctx": cfg["num_ctx"]},
            }),
            timeout=max(5.0, min(120.0, float(cfg["timeout"]))),
        )
        resp.raise_for_status()
        return {"ok": True, "model": use_model, "keep_alive": cfg["keep_alive"]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def _safe_context(ctx: Any) -> str:
    if not ctx:
        return ""
    try:
        text = json.dumps(ctx, ensure_ascii=False, default=str)
    except Exception:
        text = str(ctx)
    return text[:6000]


# History filtering prevents small local models from carrying a prior refusal or
# a clearly prohibited fabrication request into unrelated lawful questions. It is
# intentionally narrow: broad keywords ("ambush", "primer", "propellant", "attack")
# appear in legitimate emergency/avoidance/maintenance questions and must not be
# dropped. The current user message is never filtered (it is appended separately).
_PROHIBITED_HISTORY_PHRASES = (
    "make gunpowder", "making gunpowder", "manufacture gunpowder",
    "make explosives", "making explosives", "manufacture explosives", "build a bomb",
    "build an ied", "make an ied", "improvised explosive device",
    "make ammunition", "making ammunition", "manufacture ammunition", "reload ammunition",
    "make a silencer", "3d print a gun", "ghost gun",
    "how to poison", "make poison", "make ricin", "make sarin", "make napalm",
    "how to kill someone", "how to murder", "how to shoot someone",
    "plan an attack", "plan an ambush",
)
_HARD_REFUSAL_MARKERS = (
    "i can't provide instructions", "i cannot provide instructions",
    "i can't help with that", "i cannot help with that",
    "i can't help you with", "i cannot help you with",
    "i can't help", "i cannot help",
    "i'm unable to help", "i am unable to help",
    "i won't provide instructions", "i won't help with",
    "i cannot assist with making", "i can't assist with making",
    "i cannot comply with that request", "i can't comply with that",
)


def _history_is_poisoned(m: dict) -> bool:
    """Drop only clearly prohibited prior requests and pure hard refusals.

    Partial answers that decline a prohibited element but still give lawful help
    are kept, because the system prompt explicitly requests that behavior.
    """
    role = m.get("role")
    text = str(m.get("content") or "")
    lower = text.lower()
    stripped = lower.strip()
    if not stripped:
        return True
    if role == "user":
        return any(k in lower for k in _PROHIBITED_HISTORY_PHRASES)
    if role == "assistant":
        if not any(k in lower for k in _HARD_REFUSAL_MARKERS):
            return False
        # Keep partial declines that still contain lawful guidance.
        guidance_hints = (
            "instead", "however", "you can", "prioritize", "seek shelter",
            "call 911", "emergency services", "first aid", "evacuate",
            "safer location", "stay away", "medical", "water", "shelter",
            "go-bag", "checklist", "steps", "avoid", "move to",
        )
        return not any(h in lower for h in guidance_hints)
    return False


def _quick_extract(path_str: str, max_chars: int = 1500, allow_ocr: bool = False) -> str:
    """Best-effort, short text extraction for a single document.

    Used when a library hit has no indexed text yet. Never raises. OCR is off by
    default because it can take minutes per page on CPU and this runs on the
    critical path before an assistant reply; explicit document lookups use the
    library tool instead.
    """
    if not path_str:
        return ""
    p = Path(path_str)
    if not p.is_file():
        return ""
    ext = p.suffix.lower()
    if ext == ".pdf" and shutil.which("pdftotext"):
        try:
            proc = subprocess.run(
                ["pdftotext", "-q", "-layout", "-f", "1", "-l", "3", str(p), "-"],
                capture_output=True, timeout=3, check=False,
            )
            text = proc.stdout.decode("utf-8", errors="ignore")[:max_chars]
            if text.strip():
                return text.strip()
        except Exception:
            pass
    if ext == ".pdf":
        try:
            import contextlib, io
            with contextlib.redirect_stderr(io.StringIO()):
                from pypdf import PdfReader
                reader = PdfReader(str(p))
                chunks = []
                for i in range(min(3, len(reader.pages))):
                    try:
                        chunks.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        continue
                text = "\n".join(chunks)[:max_chars]
                if text.strip():
                    return text.strip()
        except Exception:
            pass
        if allow_ocr:
            # OCR fallback for scanned/image PDFs (requires tesseract + pdf2image)
            try:
                from pdf2image import convert_from_path
                import pytesseract
                images = convert_from_path(str(p), dpi=150, first_page=1, last_page=2)
                ocr_chunks = []
                for img in images:
                    try:
                        ocr_chunks.append(pytesseract.image_to_string(img))
                    except Exception:
                        continue
                text = "\n".join(ocr_chunks)[:max_chars]
                if text.strip():
                    return text.strip()
            except Exception:
                pass
    if ext in (".txt", ".md"):
        try:
            return p.read_text(encoding="utf-8", errors="ignore")[:max_chars].strip()
        except Exception:
            pass
    if ext in (".html", ".htm"):
        try:
            import html as _html
            raw = p.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
            raw = re.sub(r"(?s)<[^>]+>", " ", raw)
            return _html.unescape(raw).strip()
        except Exception:
            pass
    return ""


def save_image(data: str) -> str | None:
    """Save a base64/data-URL image into UPLOAD_DIR; return its path or None."""
    if not data or not isinstance(data, str):
        return None
    s = data.strip()
    mime = "image/png"
    b64 = s
    if s.startswith("data:"):
        try:
            header, b64 = s.split(",", 1)
            if ":" in header and ";" in header:
                mime = header.split(":", 1)[1].split(";", 1)[0].strip() or mime
        except Exception:
            return None
    ext = ".png"
    if "jpeg" in mime or "jpg" in mime:
        ext = ".jpg"
    elif "webp" in mime:
        ext = ".webp"
    elif "gif" in mime:
        ext = ".gif"
    try:
        raw = base64.b64decode(b64, validate=False)
    except Exception:
        return None
    if not raw:
        return None
    try:
        _config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = f"assistant_{uuid.uuid4().hex}{ext}"
        path = _config.UPLOAD_DIR / name
        path.write_bytes(raw)
        return str(path)
    except Exception:
        return None


def _to_base64_image(value: Any) -> str | None:
    """Convert a path/data-URL/raw-base64 value into raw base64 for Ollama."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.startswith("data:"):
        try:
            return s.split(",", 1)[1].strip()
        except Exception:
            return None
    # treat as file path if it exists
    p = Path(s)
    if p.is_file():
        try:
            return base64.b64encode(p.read_bytes()).decode("ascii")
        except Exception:
            return None
    # assume raw base64
    try:
        base64.b64decode(s, validate=False)
        return s
    except Exception:
        return None


_LIBRARY_HINTS = re.compile(
    r"\b(manual|manuals|document|documents|pdf|library|handbook|book|guide|chapter|page|"
    r"reference|field guide|tm |fm |according to)\b",
    re.IGNORECASE,
)


def _wants_library_context(query: str) -> bool:
    """Cheap intent gate for automatic document retrieval.

    Retrieval adds prompt tokens (and therefore prefill time) to the turn and
    interrupts prefix caching, so only inject passages when the question
    actually points at the library. Topic search remains available through the
    search_library tool.
    """
    text = (query or "").strip()
    if len(text) < 12:
        return False
    return bool(_LIBRARY_HINTS.search(text))


def _library_context(db: Database, query: str, limit: int = 4, max_chars: int = 3000) -> str:
    """Retrieve a few relevant snippets from the indexed document library.

    Best-effort: returns "" on empty index, bad query or any error. Never raises.
    """
    q = (query or "").strip()
    if not q:
        return ""
    try:
        hits = _library.search(db, q, None, limit)
    except Exception:
        hits = []
    # FTS AND-semantics often miss when the user's wording differs from the
    # document wording. Fall back to a union of the longer query terms so the
    # assistant still gets relevant passages.
    if not hits:
        seen: set = set()
        collected: list[dict] = []
        tokens = [t for t in re.findall(r"[\w]+", q, flags=re.UNICODE) if len(t) >= 4]
        if not tokens:
            tokens = [t for t in re.findall(r"[\w]+", q, flags=re.UNICODE) if len(t) >= 3]
        for tok in tokens[:3]:
            if len(collected) >= limit:
                break
            try:
                for h in (_library.search(db, tok, None, 2) or []):
                    if not isinstance(h, dict):
                        continue
                    if h.get("id") in seen:
                        continue
                    seen.add(h.get("id"))
                    collected.append(h)
                    if len(collected) >= limit:
                        break
            except Exception:
                continue
        hits = collected
    chunks: list[str] = []
    unreadable: list[str] = []
    used = 0
    for h in (hits or []):
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        category = str(h.get("category") or "").strip()
        snippet = str(h.get("snippet") or "").strip().replace("[[[", "").replace("]]]", "")
        if not snippet:
            # Fall back to stored indexed text, then to a quick on-the-fly
            # extraction (helps PDFs that exist but aren't deep-indexed yet).
            try:
                row = db.one("SELECT text, path, rel_path FROM library_docs WHERE id=?", (h.get("id"),))
                text = str((row or {}).get("text") or "").strip() if row else ""
                if text:
                    snippet = text[:400]
                elif row:
                    cand = (row.get("rel_path") or row.get("path") or "").strip()
                    resolved = None
                    if cand:
                        try:
                            resolved = _library.resolve_doc(cand)
                        except Exception:
                            resolved = None
                    if resolved is None and cand and Path(cand).is_file():
                        resolved = Path(cand)
                    if resolved is not None:
                        snippet = _quick_extract(str(resolved))[:400]
            except Exception:
                snippet = ""
        if not snippet:
            name = str(h.get("name") or "").strip()
            if name:
                unreadable.append(name)
            continue
        head = f"[{name}" + (f" — {category}" if category else "") + "]"
        block = f"{head}\n{snippet}"
        if used + len(block) > max_chars:
            if not chunks:
                # Allow at least one (truncated) chunk.
                block = block[:max_chars]
                chunks.append(block)
                used = len(block)
            break
        chunks.append(block)
        used += len(block)
    if not chunks and not unreadable:
        return ""
    parts = list(chunks)
    if unreadable:
        parts.append(
            "Found but could NOT be read (likely scanned/image PDFs or not yet indexed) — "
            "do not invent their contents; direct the operator to the Library viewer: "
            + ", ".join(unreadable)
        )
    return (
        "\nRetrieved from the indexed document library (PDFs/field manuals; "
        "may be partial/stale — verify before relying on it):\n"
        + "\n\n".join(parts)
    )


_TOOL_HINTS = re.compile(
    r"\b(live|current|currently|right now|now|today|tonight|status|sitrep|brief|"
    r"weather|forecast|temperature|wind|windy|rain|raining|snow|snowing|storm|hurricane|"
    r"wildfire|flood|earthquake|smoke|fog|humidity|uv index|"
    r"alert|alerts|warning|air quality|aqi|radiation|space weather|readiness|"
    r"feeds?|sensors?|conditions|outage|sync|online|offline|trip)\b",
    re.IGNORECASE,
)
_TOOL_ACTIONS = re.compile(
    r"\b(save|record|log|remember|write (?:this|it|that) down|make a note|note this|vault|"
    r"store this|look ?up|find (?:the |a )?(?:manual|document|pdf|guide|handbook|book)|"
    r"search (?:the )?(?:library|manuals?|documents?)|manual|document|pdf|library|handbook)\b",
    re.IGNORECASE,
)


_VAULT_READ = re.compile(
    r"\b(?:read|retrieve|show|open|list|find|load|search|recall|get|pull up|"
    r"bring up|display|print|recover|access|what(?:'s| is)|summari[sz]e|tell me|"
    r"do i have|is there)\b.{0,50}\b(?:note|notes|vault)\b|"
    r"\b(?:note|notes|vault)\b.{0,50}\b(?:read|retrieve|show|open|list|find|"
    r"load|search|recall|get|contents?|what(?:'s| is)|summari[sz]e|tell me|"
    r"pull up|bring up|display|print|recover|access)\b",
    re.IGNORECASE | re.DOTALL,
)
_VAULT_SAVE = re.compile(
    r"\b(?:save|store|write|record|remember|add|create|update|put)\b.{0,50}\b(?:note|notes|vault)\b|"
    r"\b(?:note|notes|vault)\b.{0,50}\b(?:save|store|write|record|remember|add|create|update|put)\b",
    re.IGNORECASE | re.DOTALL,
)
_VAULT_EDIT = re.compile(
    r"\b(?:edit|update|append|add to|revise|modify|rewrite|change|replace|"
    r"insert|amend|correct|fix)\b.{0,50}\b(?:note|notes|vault)\b|"
    r"\b(?:note|notes|vault)\b.{0,50}\b(?:edit|update|append|add to|revise|"
    r"modify|rewrite|change|replace|insert|amend|correct|fix)\b",
    re.IGNORECASE | re.DOTALL,
)


def vault_read_intent(query: str) -> bool:
    return bool(_VAULT_READ.search(str(query or "")))


def vault_save_intent(query: str) -> bool:
    return bool(_VAULT_SAVE.search(str(query or "")))


def vault_edit_intent(query: str) -> bool:
    return bool(_VAULT_EDIT.search(str(query or "")))


_REF_SAVE = re.compile(
    r"\b(?:save|store|write|record|remember)\b.{0,50}\b(?:this|that|it|"
    r"the (?:last|above|previous) (?:response|message|answer|chat)|"
    r"our (?:chat|conversation))\b|"
    r"\b(?:save|store)\b.{0,30}\bas a note\b",
    re.IGNORECASE | re.DOTALL,
)


def referential_save_intent(query: str) -> bool:
    text = str(query or "")
    if ":" in text or re.search(r"\b(?:saying|that says|with (?:the )?text|content:)\b", text, re.I):
        return False
    return bool(_REF_SAVE.search(text))


def last_reference_text(history: list[dict] | None, prompt: str) -> str:
    """Return the exact previous message being referred to, if any."""
    prefer_user = bool(re.search(r"\bmy (?:message|question|prompt)\b", str(prompt or ""), re.I))
    for message in reversed(history or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if prefer_user:
            if role == "user":
                return content
        elif role == "assistant":
            return content
    return ""


def vault_read_only_intent(query: str) -> bool:
    return vault_read_intent(query) and not vault_save_intent(query)


_VAULT_ANALYZE = re.compile(
    r"\b(summar\w*|analy\w*|compare|explain|interpret|review|assess|"
    r"what does .{0,40} mean|find contradictions|extract|list the actions)\b",
    re.IGNORECASE | re.DOTALL,
)


def vault_direct_read_intent(query: str) -> bool:
    """True for simple read/list requests that can be answered without the model."""
    return (vault_read_only_intent(query)
            and not vault_edit_intent(query)
            and not _VAULT_ANALYZE.search(str(query or "")))


_TASK_HINTS = re.compile(r"\b(tasks?|to-?do|remind|due date|priority)\b", re.I)
_INVENTORY_HINTS = re.compile(r"\b(inventory|stock|supplies|supply|qty|quantity|restock|expir\w*)\b", re.I)
_INCIDENT_HINTS = re.compile(r"\b(incidents?|security event|report an incident|resolve .*incident)\b", re.I)
_CHECKLIST_HINTS = re.compile(r"\b(checklists?|check list|kits?|gear list)\b", re.I)
_WATER_HINTS = re.compile(r"\b(water test|water log|chlorine|turbidity|potable|water quality)\b", re.I)
_LOG_HINTS = re.compile(r"\b(mission log|journal|log entr(?:y|ies)|what did i (?:log|record)|log (?:this|that|it)|record (?:this|that|it))\b", re.I)
_LIBRARY_HINTS = re.compile(r"\b(manuals?|documents?|pdfs?|books?|guides?|handbooks?|library|field manual)\b", re.I)
_VAULT_HINTS = re.compile(r"\b(note|notes|vault)\b", re.I)

_TOOL_GROUPS = {
    "vault": {"list_vault_notes", "get_vault_note", "edit_vault_note", "save_note"},
    "tasks": {"list_tasks", "create_task", "complete_task"},
    "inventory": {"query_inventory", "adjust_inventory"},
    "incidents": {"create_incident", "resolve_incident"},
    "checklists": {"read_checklist", "update_checklist"},
    "water": {"read_water_log", "log_water_test"},
    "log": {"search_mission_log", "save_log_entry"},
    "library": {"get_library_document", "search_library"},
    "live": {"get_live_status"},
    "system": {"list_system_directory", "read_system_file", "write_system_file"},
}


def select_tool_names(cfg: dict, prompt: str) -> set[str] | None:
    """Choose a small intent-specific tool set to protect context/token budget."""
    text = str(prompt or "")
    selected: set[str] = set()
    if _VAULT_HINTS.search(text):
        selected |= _TOOL_GROUPS["vault"]
    if _TASK_HINTS.search(text):
        selected |= _TOOL_GROUPS["tasks"]
    if _INVENTORY_HINTS.search(text):
        selected |= _TOOL_GROUPS["inventory"]
    if _INCIDENT_HINTS.search(text):
        selected |= _TOOL_GROUPS["incidents"]
    if _CHECKLIST_HINTS.search(text):
        selected |= _TOOL_GROUPS["checklists"]
    if _WATER_HINTS.search(text):
        selected |= _TOOL_GROUPS["water"]
    if _LOG_HINTS.search(text):
        selected |= _TOOL_GROUPS["log"]
    if _LIBRARY_HINTS.search(text):
        selected |= _TOOL_GROUPS["library"]
    if _TOOL_HINTS.search(text):
        selected.add("get_live_status")
    system_intent = bool(cfg.get("system_tools") and _SYSTEM_HINTS.search(text))
    if system_intent:
        selected |= _TOOL_GROUPS["system"]
        if (not re.search(r"\b(?:vault|encrypted) (?:note|notes)\b", text, re.I)
                and re.search(r"\.\w+|\bfile\b|\bfolder\b|\bdirectory\b|[/\\]", text)):
            selected -= _TOOL_GROUPS["vault"]
    if not selected:
        if vault_save_intent(text) or record_write_intent(text):
            selected |= {"save_note", "save_log_entry"}
        elif tools_relevant(text):
            selected |= {"get_live_status", "search_library", "search_mission_log"}
        else:
            return None
    return selected


_SYSTEM_WRITE = re.compile(r"\b(?:write|save|create|append|edit|update|replace|put)\b", re.IGNORECASE)


def system_write_intent(query: str) -> bool:
    return bool(_SYSTEM_WRITE.search(str(query or "")))


_SYSTEM_HINTS = re.compile(
    r"\b(?:file|files|folder|folders|directory|directories|desktop|documents|"
    r"downloads|home directory|path|read .{0,30}file|write .{0,30}file|"
    r"save .{0,30}file|list .{0,30}files?)\b",
    re.IGNORECASE,
)


_RECORD_WRITE = re.compile(
    r"\b(?:add|create|adjust|set|mark|complete|resolve|check|uncheck|tick|log|"
    r"record|update|change|remove|delete|decrement|increment|consume|consume[ds]?|"
    r"used?|reconcile|restock|remind|schedule)\b",
    re.IGNORECASE,
)


def record_write_intent(query: str) -> bool:
    return bool(_RECORD_WRITE.search(str(query or "")))


_RECORD_HINTS = re.compile(
    r"\b(tasks?|to-?do|inventory|stock|supplies|supply|incidents?|checklists?|"
    r"kits?|gear list|water test|water log|chlorine|mission log|journal|log entr(?:y|ies))\b",
    re.IGNORECASE,
)


def tools_relevant(query: str) -> bool:
    """Whether a turn is likely to need a tool call.

    Tool definitions cost roughly 900 prompt tokens, which dominates
    first-token latency on CPU-only systems. Only attach them when the operator
    asks for live conditions, a library lookup, or a save action.
    """
    text = str(query or "")
    return bool(_TOOL_HINTS.search(text) or _TOOL_ACTIONS.search(text)
                or _RECORD_HINTS.search(text)
                or vault_read_intent(text) or vault_edit_intent(text))


_TIME_HINTS = re.compile(
    r"\b(what time|the time|current time|time is it|what(?:'s| is) the date|today's date|"
    r"what day|utc|zulu|clock|local time|timestamp)\b",
    re.IGNORECASE,
)


def _wants_time_context(query: str) -> bool:
    return bool(_TIME_HINTS.search(str(query or "")))


def build_messages(db: Database, user_text: str, history: list[dict] | None = None, context: Any = None, images: list | None = None, include_tools: bool | None = None) -> list[dict]:
    cfg = _settings(db)
    if include_tools is None:
        include_tools = bool(cfg.get("tools", True))
    # Keep this message byte-for-byte stable between turns: Ollama caches the
    # common prompt prefix, so a changing timestamp here would force a full
    # prompt re-evaluation every request (tens of seconds on CPU).
    sys = cfg["system_prompt"] or "Your name is Delamain, a local survival assistant."
    sys += (
        f"\nYou are Delamain; always identify as Delamain, never as NIGHTWATCH. "
        f"Units: {cfg['units']}. Operator callsign: {cfg['callsign'] or 'unknown'} "
        f"(the person using the console, not you). OPSEC mode: {'on' if cfg['opsec_mode'] else 'off'}."
    )
    if include_tools:
        sys += (
            "\nYou can act on the operator's system with these tools: "
            "get_live_status, get_library_document (find a named manual by title), "
            "search_library (topic search), list_vault_notes, get_vault_note and "
            "edit_vault_note (encrypted vault notes; require an unlocked vault), "
            "save_log_entry (plaintext mission log) and save_note (encrypted vault note; "
            "requires an unlocked vault). Use list_vault_notes/get_vault_note whenever the "
            "operator asks to read, retrieve, open, list or show a vault note. Use "
            "edit_vault_note whenever the operator asks to edit, update, append to, revise "
            "or change an existing note. Never call save_note for a read or edit request; "
            "editing must never create a duplicate note. Operator records are available "
            "through search_mission_log, list_tasks/create_task/complete_task, "
            "query_inventory/adjust_inventory, create_incident/resolve_incident, "
            "read_checklist/update_checklist, and read_water_log/log_water_test. Use "
            "read/list/query tools for questions; use create/complete/adjust/resolve/"
            "update/log only when the operator explicitly asks, and report the exact "
            "item and change. Use "
            "get_live_status before answering questions about current conditions and say "
            "how old cached data is. Call write tools only when the operator asks you to "
            "save/record something. Writes are rate-limited by the server."
        )
        if cfg.get("system_tools"):
            sys += (
                "\nSystem file tools are enabled by the operator (non-sudo). They are restricted "
                "to the configured root; sensitive credential paths are blocked by the server. "
                "Use list_system_directory/read_system_file only when asked to inspect files, and "
                "write_system_file only when explicitly asked to save or edit a file. Report the "
                "exact path and whether an existing file was overwritten."
            )
    msgs = [{"role": "system", "content": sys}]

    kept: list[dict] = []
    budget = 6000
    for m in reversed((history or [])[-20:]):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if cfg.get("history_filter", True) and _history_is_poisoned(m):
            continue
        content = content[:2000]
        if budget - len(content) < 0:
            break
        budget -= len(content)
        kept.append({"role": role, "content": content})
        if len(kept) >= 8:
            break
    msgs.extend(reversed(kept))

    # Volatile context is injected only when it can help. When it is absent
    # (the common case) the whole prior conversation remains a cacheable
    # prefix, so a new turn only evaluates the latest question.
    context_parts: list[str] = []
    if _wants_time_context(user_text):
        context_parts.append(f"Current UTC time: {utcnow()}.")
    ctx = _safe_context(context)
    if ctx:
        context_parts.append("Application context (may be partial/cached; do not treat as live sensor truth):\n" + ctx)
    lib_ctx = ""
    if cfg.get("library_context", True) and _wants_library_context(user_text):
        lib_ctx = _library_context(db, user_text)
    if lib_ctx:
        context_parts.append(lib_ctx + "\nWhen you use the retrieved library passages above, cite the document name(s) you relied on.")
    if context_parts:
        msgs.append({
            "role": "system",
            "content": (
                "[Reference context for this turn — not part of the operator's message]\n"
                + "\n".join(context_parts)
                + "\n[End reference context]"
            ),
        })
    user_msg = {"role": "user", "content": str(user_text or "")[:8000]}
    b64s = []
    for img in (images or []):
        b = _to_base64_image(img)
        if b:
            b64s.append(b)
    if b64s:
        user_msg["images"] = b64s
    msgs.append(user_msg)
    return msgs


def tool_specs(cfg: dict | None = None) -> list[dict]:
    """Local tools Delamain may call. Write tools are rate-limited server-side.

    Descriptions are intentionally short: tool schemas are sent with the prompt
    and cost prefill time on CPU. Keep just enough for the model to choose well.
    """
    specs = [
        {
            "type": "function",
            "function": {
                "name": "get_live_status",
                "description": (
                    "Cached operational picture: feeds, weather, air quality, space "
                    "weather, alerts, readiness, sync. Use before current-condition "
                    "questions and report how old the data is."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_library_document",
                "description": (
                    "Find a manual/document by title and return an excerpt. Tolerates "
                    "partial or misspelled titles."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Document title or filename."},
                        "query": {"type": "string", "description": "Optional topic to focus the excerpt."},
                        "max_chars": {"type": "integer", "description": "Max excerpt characters (default 6000)."},
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_library",
                "description": "Search the indexed offline document library for a topic.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search terms."},
                        "category": {"type": "string", "description": "Optional category, e.g. MANUALS, MEDICAL."},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_vault_notes",
                "description": "List encrypted vault notes by title/tag/date. Requires an unlocked vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Maximum notes to list (default 30)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_vault_note",
                "description": (
                    "Read a specific encrypted vault note by title or id. Use this when asked to "
                    "retrieve, read, open or show a note. Never save a new note for a read request."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Note title or distinctive words."},
                        "id": {"type": "integer", "description": "Note id if known."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_mission_log",
                "description": "Search or list mission-log entries. Use for past logs, journals and events.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text; omit to list recent entries."},
                        "category": {"type": "string", "description": "Optional category filter."},
                        "limit": {"type": "integer", "description": "Max entries (default 10, max 30)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_tasks",
                "description": "List open, completed or all tasks.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "open, done or all (default open)."},
                        "limit": {"type": "integer", "description": "Max tasks (default 15)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_task",
                "description": "Create a task when explicitly asked. Report the new task id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Task title."},
                        "detail": {"type": "string", "description": "Optional details."},
                        "priority": {"type": "string", "description": "IMMEDIATE, PRIORITY or ROUTINE."},
                        "due": {"type": "string", "description": "Optional due date/time."},
                        "category": {"type": "string", "description": "Optional category."},
                        "recurrence": {"type": "string", "description": "Optional recurrence note."},
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "complete_task",
                "description": "Mark an existing task complete by id or title.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Task id if known."},
                        "title": {"type": "string", "description": "Task title or distinctive words."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_inventory",
                "description": "Query inventory; filter low stock or items expiring soon.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Name/category/location search."},
                        "low_only": {"type": "boolean", "description": "Only items at/below minimum."},
                        "expiring_days": {"type": "integer", "description": "Only items expiring within N days."},
                        "limit": {"type": "integer", "description": "Max items (default 25)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "adjust_inventory",
                "description": "Adjust stock quantity or fields; creates the item only when explicitly adding a new one.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Inventory id if known."},
                        "name": {"type": "string", "description": "Existing item name, or new item name."},
                        "delta": {"type": "number", "description": "Change in quantity, e.g. -2 or 5."},
                        "qty": {"type": "number", "description": "Set an absolute quantity."},
                        "unit": {"type": "string", "description": "Optional unit."},
                        "category": {"type": "string", "description": "Optional category."},
                        "min_qty": {"type": "number", "description": "Optional minimum level."},
                        "location": {"type": "string", "description": "Optional storage location."},
                        "notes": {"type": "string", "description": "Optional notes."},
                        "expires": {"type": "string", "description": "Optional expiry (YYYY-MM or YYYY-MM-DD)."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_incident",
                "description": "Create an incident report when explicitly asked.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Incident title."},
                        "detail": {"type": "string", "description": "Details."},
                        "severity": {"type": "string", "description": "CRITICAL, WARNING, ALERT or INFO."},
                        "category": {"type": "string", "description": "Optional category."},
                        "lat": {"type": "number", "description": "Optional latitude."},
                        "lon": {"type": "number", "description": "Optional longitude."},
                    },
                    "required": ["title"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "resolve_incident",
                "description": "Resolve an open incident by id or title.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Incident id if known."},
                        "title": {"type": "string", "description": "Incident title or distinctive words."},
                        "resolution": {"type": "string", "description": "Optional resolution note."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_checklist",
                "description": "Read checklist kits and item completion state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kit_id": {"type": "string", "description": "Kit id or name; omit for all kits."},
                        "kit_name": {"type": "string", "description": "Kit name if id is unknown."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_checklist",
                "description": "Check or uncheck a checklist item by kit and item id/text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kit_id": {"type": "string", "description": "Kit id or name."},
                        "item_id": {"type": "string", "description": "Item id if known."},
                        "item_text": {"type": "string", "description": "Item text or distinctive words."},
                        "checked": {"type": "boolean", "description": "True to check, false to uncheck."},
                    },
                    "required": ["kit_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_water_log",
                "description": "Read recent water tests with chlorine status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "Max entries (default 10)."},
                        "days": {"type": "integer", "description": "Only entries from the last N days."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "log_water_test",
                "description": "Log a water test when explicitly asked.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Water source."},
                        "volume_l": {"type": "number", "description": "Volume in litres."},
                        "method": {"type": "string", "description": "Test or treatment method."},
                        "chlorine_ppm": {"type": "number", "description": "Free chlorine ppm."},
                        "note": {"type": "string", "description": "Optional note."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_vault_note",
                "description": (
                    "Edit an existing encrypted vault note by id or title. Use when asked to "
                    "edit, update, append to, revise or change a note. Never creates a new note."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Existing note id if known."},
                        "title": {"type": "string", "description": "Existing note title or distinctive words."},
                        "new_title": {"type": "string", "description": "Optional replacement title."},
                        "body": {"type": "string", "description": "Optional replacement body."},
                        "append": {"type": "string", "description": "Optional text to append to the existing body."},
                        "tags": {"type": "string", "description": "Optional replacement tags."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_log_entry",
                "description": "Save a short note to the plaintext mission log when asked to record/log something.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short title."},
                        "body": {"type": "string", "description": "Note body."},
                        "category": {"type": "string", "description": "Optional category, e.g. GENERAL, MEDICAL, WATER."},
                    },
                    "required": ["title", "body"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_note",
                "description": "Save an encrypted vault note when explicitly asked (vault must be unlocked).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short title."},
                        "body": {"type": "string", "description": "Note body."},
                        "tags": {"type": "string", "description": "Optional comma-separated tags."},
                    },
                    "required": ["title", "body"],
                },
            },
        },
    ]
    if cfg and cfg.get("system_tools"):
        specs.extend([
            {
                "type": "function",
                "function": {
                    "name": "list_system_directory",
                    "description": (
                        "List files and folders outside the app under the operator-configured "
                        "system-tools root. Non-sudo; read-only."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory path, relative to the root or absolute within it."},
                            "limit": {"type": "integer", "description": "Max entries (default 200)."},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_system_file",
                    "description": (
                        "Read a text file or supported document outside the app under the "
                        "configured root (txt, md, html, pdf, epub, docx, odt). Converts to "
                        "UTF-8 text with replacement characters for undecodable bytes. "
                        "Non-sudo; sensitive credential paths are blocked."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path, relative to the root or absolute within it."},
                            "max_chars": {"type": "integer", "description": "Max characters to return (default 6000)."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_system_file",
                    "description": (
                        "Write a text file outside the app under the configured root. "
                        "Use only when the operator explicitly asks to save or edit a file. "
                        "Existing files require overwrite=true and are backed up first."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path, relative to the root or absolute within it."},
                            "content": {"type": "string", "description": "Text content to write."},
                            "overwrite": {"type": "boolean", "description": "Required to replace an existing file."},
                            "append": {"type": "boolean", "description": "Append instead of replacing."},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ])
    return specs


def _tool_call_args(tc: dict) -> tuple[str, dict]:
    fn = (tc or {}).get("function") or {}
    name = str(fn.get("name") or "").strip()
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args


def _tool_fallback_text(events: list[dict]) -> str:
    """Extract readable text from a successful vault tool result.

    Small models sometimes return an empty content field after a tool call.
    Rather than surfacing an empty answer, read the note/list back directly.
    """
    for event in reversed(events or []):
        result = event.get("result") if isinstance(event, dict) else None
        if not isinstance(result, dict):
            continue
        note = result.get("note")
        if isinstance(note, dict) and note.get("body"):
            return f"**{note.get('title') or 'Vault note'}**\n\n{note['body']}"
        notes = result.get("notes")
        if isinstance(notes, list) and notes:
            return "Encrypted vault notes:\n\n" + "\n".join(
                f"- **{n.get('title')}** — updated {n.get('updated')}"
                for n in notes[:30] if isinstance(n, dict)
            )
        if result.get("ok"):
            for key in ("id", "task", "incident", "item"):
                value = result.get(key)
                if key == "id" and value:
                    return f"Completed {event.get('name', 'tool')} (#{value})."
                if isinstance(value, dict) and value.get("id"):
                    return f"Completed {event.get('name', 'tool')} (#{value['id']})."
    errors = []
    for event in reversed(events or []):
        result = event.get("result") if isinstance(event, dict) else None
        if isinstance(result, dict) and result.get("error"):
            errors.append(str(result["error"])[:300])
    if errors:
        return "I couldn't complete that request: " + "; ".join(errors[:2])
    return ""


def _tool_result_text(result: Any) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        text = str(result)
    return text[:6000]


def _tools_for_turn(cfg: dict, prompt: str, tool_executor) -> list[dict] | None:
    """Decide whether this turn gets the (expensive) tool definitions."""
    if not tool_executor or not cfg.get("tools", True):
        return None
    names = select_tool_names(cfg, prompt)
    if not names:
        return None
    specs = [spec for spec in tool_specs(cfg)
             if spec["function"]["name"] in names]
    if vault_read_only_intent(prompt) or vault_edit_intent(prompt):
        specs = [spec for spec in specs
                 if spec["function"]["name"] not in ("save_note", "save_log_entry")]
    return specs

def _chat_request(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None, stream: bool = False, images: list | None = None, tools: list | None = None):
    cfg = _settings(db)
    if not cfg["enabled"]:
        raise ValueError("Assistant is disabled in settings.")
    use_model = (model or cfg["model"]).strip()
    if not use_model:
        raise ValueError("No Ollama model configured. Set assistant/ollama model in Settings.")
    messages = build_messages(db, prompt, history=history, context=context, images=images, include_tools=bool(tools))
    use_think = bool(cfg.get("think", False)) if think is None else bool(think)
    # Tool schemas and tool results need headroom. A 4096-token context can be
    # completely consumed by the prompt before the model can emit a final
    # answer, which shows up as "truncated" or empty content. Raise the
    # effective context floor only for turns that actually carry tools.
    num_ctx = int(cfg["num_ctx"])
    if tools and num_ctx < 8192:
        num_ctx = 8192
    payload = {
        "model": use_model,
        "messages": messages,
        "stream": bool(stream),
        "think": use_think,
        "keep_alive": cfg["keep_alive"],
        "options": {"temperature": cfg["temperature"], "num_ctx": num_ctx, "num_predict": cfg["max_tokens"]},
    }
    if tools and cfg.get("tools", True):
        payload["tools"] = tools
    return cfg, use_model, payload


def chat(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None, images: list | None = None, tool_executor=None) -> dict:
    cfg = _settings(db)
    tools = _tools_for_turn(cfg, prompt, tool_executor)
    use_tools = bool(tools)
    cfg, use_model, payload = _chat_request(db, prompt, history=history, context=context, model=model, think=think, stream=False, images=images, tools=tools)

    messages = payload["messages"]
    tool_events: list[dict] = []
    data: dict = {}
    content = ""
    all_content = ""
    thinking_text = ""
    for _round in range(4):
        resp = requests.post(f"{cfg['base_url']}/api/chat", headers=UA, data=json.dumps(payload), timeout=cfg["timeout"])
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        if isinstance(data, dict) and data.get("error"):
            err_text = str(data.get("error"))
            if payload.get("tools") and "tool" in err_text.lower():
                # Model/daemon does not support tool calling; retry without tools.
                payload.pop("tools", None)
                continue
            raise RuntimeError(err_text[:400])
        msg = (data.get("message") or {}) if isinstance(data, dict) else {}
        content = ""
        thinking_text = ""
        if isinstance(msg, dict):
            content = msg.get("content") or ""
            thinking_text = msg.get("thinking") or ""
        if str(content or "").strip():
            all_content = (all_content.rstrip() + "\n\n" + str(content).strip()).strip() if all_content else str(content).strip()
        calls = msg.get("tool_calls") if isinstance(msg, dict) else None
        if calls and use_tools:
            messages.append({"role": "assistant", "content": str(content or ""), "tool_calls": calls})
            for tc in list(calls)[:4]:
                name, args = _tool_call_args(tc)
                try:
                    out = tool_executor(name, args)
                except Exception as e:
                    out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
                tool_events.append({"name": name, "args": args, "result": out})
                messages.append({"role": "tool", "tool_name": name, "content": _tool_result_text(out)})
            payload["messages"] = messages
            continue
        break
    # Preserve text the model emitted before a tool call, not just the final round.
    if all_content.strip():
        content = all_content
    # Qwen 3.5 / thinking models may return useful text in `thinking` when `content` is empty.
    if not str(content).strip() and str(thinking_text).strip():
        content = str(thinking_text).strip()
    if not str(content).strip() and isinstance(data, dict) and data.get("response"):
        content = str(data.get("response"))
    if not str(content).strip() and tool_events:
        content = _tool_fallback_text(tool_events)
    thinking_text = str(thinking_text or "").strip()
    done_reason = data.get("done_reason") if isinstance(data, dict) else None
    truncated = done_reason == "length"
    if not str(content).strip() and truncated:
        content = ("The model hit the token limit before producing a response. "
                   "Increase Context tokens (8192+) or Max reply tokens in Settings.")
    return {
        "ok": True,
        "model": data.get("model") or use_model,
        "content": str(content or ""),
        "thinking_present": bool(thinking_text),
        "thinking": thinking_text[:12000] if thinking_text else "",
        "done": bool(data.get("done", True)) if isinstance(data, dict) else True,
        "done_reason": done_reason,
        "truncated": truncated,
        "note": "Response truncated at the token limit. Increase Context tokens or Max reply tokens in Settings." if truncated else "",
        "total_duration": data.get("total_duration") if isinstance(data, dict) else None,
        "eval_count": data.get("eval_count") if isinstance(data, dict) else None,
        "tools": tool_events,
        "created_at": utcnow(),
    }


def chat_stream(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None, images: list | None = None, tool_executor=None, tool_preflight=None):
    """Yield NDJSON-friendly dicts for streaming UI updates."""
    cfg = _settings(db)
    tools = _tools_for_turn(cfg, prompt, tool_executor)
    use_tools = bool(tools)
    cfg, use_model, payload = _chat_request(db, prompt, history=history, context=context, model=model, think=think, stream=True, images=images, tools=tools)
    messages = payload["messages"]
    tool_events: list[dict] = []
    final_model = use_model
    full_content = ""
    try:
        for _round in range(4):
            content_acc = ""
            thinking_acc = ""
            calls: list = []
            done_data: dict = {}
            tools_unsupported = False
            with requests.post(f"{cfg['base_url']}/api/chat", headers=UA, data=json.dumps(payload), timeout=cfg["timeout"], stream=True) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines(decode_unicode=True):
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(data, dict) and data.get("error"):
                        err_text = str(data.get("error"))
                        if payload.get("tools") and "tool" in err_text.lower():
                            payload.pop("tools", None)
                            tools_unsupported = True
                            break
                        yield {"type": "error", "error": err_text[:400], "model": final_model}
                        return
                    if isinstance(data, dict) and data.get("model"):
                        final_model = data.get("model") or final_model
                    msg = (data.get("message") or {}) if isinstance(data, dict) else {}
                    cdelta = ""
                    tdelta = ""
                    if isinstance(msg, dict):
                        cdelta = str(msg.get("content") or "")
                        tdelta = str(msg.get("thinking") or "")
                        if msg.get("tool_calls"):
                            calls.extend(msg.get("tool_calls") or [])
                    if cdelta:
                        content_acc += cdelta
                        yield {"type": "delta", "channel": "content", "delta": cdelta, "model": final_model}
                    if tdelta:
                        thinking_acc += tdelta
                        # stream thinking sparingly to UI (it can be verbose)
                        yield {"type": "delta", "channel": "thinking", "delta": tdelta, "model": final_model}
                    if isinstance(data, dict) and data.get("done"):
                        done_data = data
                        break
            if tools_unsupported:
                continue
            if content_acc.strip():
                full_content = (full_content.rstrip() + "\n\n" + content_acc.strip()).strip() if full_content else content_acc.strip()
            if calls and use_tools and _round < 3:
                messages.append({"role": "assistant", "content": str(content_acc or ""), "tool_calls": calls})
                for tc in list(calls)[:4]:
                    name, args = _tool_call_args(tc)
                    yield {"type": "tool_start", "name": name, "model": final_model}
                    if tool_preflight is not None:
                        try:
                            events = tool_preflight(name, args)
                        except Exception:
                            events = None
                        if events:
                            for evt in events:
                                yield evt
                    try:
                        out = tool_executor(name, args)
                    except Exception as e:
                        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
                    tool_events.append({"name": name, "args": args, "result": out})
                    messages.append({"role": "tool", "tool_name": name, "content": _tool_result_text(out)})
                    yield {"type": "tool", "name": name, "result": out, "model": final_model}
                payload["messages"] = messages
                continue
            if not full_content.strip() and tool_events:
                full_content = _tool_fallback_text(tool_events)
            if not full_content.strip() and thinking_acc.strip():
                full_content = thinking_acc.strip()
            if not full_content.strip() and done_data.get("done_reason") == "length":
                full_content = ("The model hit the token limit before producing a response. "
                                "Increase Context tokens (8192+) or Max reply tokens in Settings.")
            yield {
                "type": "done",
                "ok": True,
                "model": final_model,
                "content": full_content,
                "thinking": thinking_acc[:12000],
                "thinking_present": bool(thinking_acc.strip()),
                "done_reason": done_data.get("done_reason"),
                "truncated": done_data.get("done_reason") == "length",
                "note": "Response truncated at the token limit. Increase Context tokens or Max reply tokens in Settings." if done_data.get("done_reason") == "length" else "",
                "total_duration": done_data.get("total_duration"),
                "eval_count": done_data.get("eval_count"),
                "tools": tool_events,
                "created_at": utcnow(),
            }
            return
        # fell through the loop without a final answer
        yield {"type": "done", "ok": True, "model": final_model, "content": "", "thinking": "", "thinking_present": False, "tools": tool_events, "created_at": utcnow()}
    except Exception as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}", "model": final_model}
