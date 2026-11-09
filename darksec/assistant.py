"""Local Ollama-backed survival assistant.

Talks only to a configured local Ollama instance (default 127.0.0.1:11434).
No cloud dependency. Errors are returned clearly so the UI can stay offline-safe.
"""
from __future__ import annotations

import json
import re
from typing import Any

import requests

from .db import Database, utcnow
from . import library as _library
DEFAULT_BASE = "http://127.0.0.1:11434"
UA = {"User-Agent": "DarkSecNightwatch/2.0", "Accept": "application/json", "Content-Type": "application/json"}


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
        "opsec_mode": bool(s.get("opsec_mode", False)),
        "units": s.get("units") or "metric",
        "callsign": s.get("callsign") or "",
    }


def status(db: Database) -> dict:
    cfg = _settings(db)
    out = {
        "enabled": cfg["enabled"],
        "configured": bool(cfg["model"]),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "reachable": False,
        "models": [],
        "error": None,
        "checked_at": utcnow(),
        "local_only_hint": cfg["base_url"].startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")),
    }
    if not cfg["enabled"]:
        out["error"] = "Assistant disabled in settings."
        return out
    try:
        resp = requests.get(f"{cfg['base_url']}/api/tags", headers={"User-Agent": UA["User-Agent"], "Accept": "application/json"}, timeout=min(8, cfg["timeout"]))
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        models = []
        for m in (data.get("models") or []):
            if isinstance(m, dict) and m.get("name"):
                models.append({"name": m.get("name"), "size": m.get("size"), "modified_at": m.get("modified_at"), "details": m.get("details") or {}})
        out["reachable"] = True
        out["models"] = models
        names = {m["name"] for m in models}
        if cfg["model"] and cfg["model"] not in names:
            # keep configured, but warn
            out["warning"] = f"Configured model '{cfg['model']}' not found in Ollama tags."
    except Exception as e:
        out["error"] = f"Ollama unreachable at {cfg['base_url']}: {e}"[:400]
    return out


def list_models(db: Database) -> dict:
    st = status(db)
    return {"ok": st.get("reachable", False), "models": st.get("models", []), "base_url": st.get("base_url"), "configured_model": st.get("model"), "error": st.get("error"), "warning": st.get("warning")}


def _safe_context(ctx: Any) -> str:
    if not ctx:
        return ""
    try:
        text = json.dumps(ctx, ensure_ascii=False, default=str)
    except Exception:
        text = str(ctx)
    return text[:16000]


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


def _library_context(db: Database, query: str, limit: int = 5, max_chars: int = 6000) -> str:
    """Retrieve relevant snippets from the indexed document library (PDFs etc.).

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
        for tok in tokens[:6]:
            if len(collected) >= limit:
                break
            try:
                for h in (_library.search(db, tok, None, 3) or []):
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
    used = 0
    for h in (hits or []):
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").strip()
        category = str(h.get("category") or "").strip()
        snippet = str(h.get("snippet") or "").strip().replace("[[[", "").replace("]]]", "")
        if not snippet:
            # Fall back to a truncated stored text if FTS snippet is empty.
            try:
                row = db.one("SELECT text FROM library_docs WHERE id=?", (h.get("id"),))
                text = str((row or {}).get("text") or "").strip() if row else ""
                if text:
                    snippet = text[:400]
            except Exception:
                snippet = ""
        if not snippet:
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
    if not chunks:
        return ""
    return (
        "\nRetrieved from the indexed document library (PDFs/field manuals; "
        "may be partial/stale — verify before relying on it):\n"
        + "\n\n".join(chunks)
    )


def build_messages(db: Database, user_text: str, history: list[dict] | None = None, context: Any = None) -> list[dict]:
    cfg = _settings(db)
    msgs = []
    sys = cfg["system_prompt"] or "Your name is Delamain, a local survival assistant."
    sys += (
        f"\nYou are Delamain. Always identify as Delamain, never as NIGHTWATCH. "
        f"Current UTC time: {utcnow()}. Units: {cfg['units']}. "
        f"Operator callsign: {cfg['callsign'] or 'unknown'} (the person using the console, not you). "
        f"OPSEC mode: {'on' if cfg['opsec_mode'] else 'off'}."
    )
    ctx = _safe_context(context)
    if ctx:
        sys += "\nApplication context (may be partial/cached; do not treat as live sensor truth):\n" + ctx
    lib_ctx = _library_context(db, user_text)
    if lib_ctx:
        sys += lib_ctx
    msgs.append({"role": "system", "content": sys})
    kept = 0
    for m in reversed((history or [])[-20:]):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if _history_is_poisoned(m):
            continue
        msgs.append({"role": role, "content": content[:8000]})
        kept += 1
        if kept >= 12:
            break
    # restore chronological order for the kept history
    body = msgs[1:]
    msgs = msgs[:1] + list(reversed(body))
    msgs.append({"role": "user", "content": str(user_text or "")[:8000]})
    return msgs


def _chat_request(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None, stream: bool = False):
    cfg = _settings(db)
    if not cfg["enabled"]:
        raise ValueError("Assistant is disabled in settings.")
    use_model = (model or cfg["model"]).strip()
    if not use_model:
        raise ValueError("No Ollama model configured. Set assistant/ollama model in Settings.")
    messages = build_messages(db, prompt, history=history, context=context)
    use_think = bool(cfg.get("think", False)) if think is None else bool(think)
    payload = {
        "model": use_model,
        "messages": messages,
        "stream": bool(stream),
        "think": use_think,
        "keep_alive": "10m",
        "options": {"temperature": cfg["temperature"], "num_ctx": cfg["num_ctx"], "num_predict": cfg["max_tokens"]},
    }
    return cfg, use_model, payload


def chat(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None) -> dict:
    cfg, use_model, payload = _chat_request(db, prompt, history=history, context=context, model=model, think=think, stream=False)
    resp = requests.post(f"{cfg['base_url']}/api/chat", headers=UA, data=json.dumps(payload), timeout=cfg["timeout"])
    resp.raise_for_status()
    data = resp.json() if resp.content else {}
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data.get("error"))[:400])
    msg = (data.get("message") or {}) if isinstance(data, dict) else {}
    content = ""
    thinking = ""
    if isinstance(msg, dict):
        content = msg.get("content") or ""
        thinking = msg.get("thinking") or ""
    # Qwen 3.5 / thinking models may return useful text in `thinking` when `content` is empty.
    if not str(content).strip() and str(thinking).strip():
        content = str(thinking).strip()
    if not str(content).strip() and isinstance(data, dict) and data.get("response"):
        content = str(data.get("response"))
    thinking_text = str(thinking or "").strip()
    done_reason = data.get("done_reason") if isinstance(data, dict) else None
    truncated = done_reason == "length"
    return {
        "ok": True,
        "model": data.get("model") or use_model,
        "content": str(content or ""),
        "thinking_present": bool(thinking_text),
        "thinking": thinking_text[:12000] if thinking_text else "",
        "done": bool(data.get("done", True)) if isinstance(data, dict) else True,
        "done_reason": done_reason,
        "truncated": truncated,
        "note": "Response truncated at the token limit. Increase 'Max reply tokens' in Settings for longer answers." if truncated else "",
        "total_duration": data.get("total_duration") if isinstance(data, dict) else None,
        "eval_count": data.get("eval_count") if isinstance(data, dict) else None,
        "created_at": utcnow(),
    }


def chat_stream(db: Database, prompt: str, history: list[dict] | None = None, context: Any = None, model: str | None = None, think: bool | None = None):
    """Yield NDJSON-friendly dicts for streaming UI updates."""
    cfg, use_model, payload = _chat_request(db, prompt, history=history, context=context, model=model, think=think, stream=True)
    content_acc = ""
    thinking_acc = ""
    final_model = use_model
    try:
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
                    yield {"type": "error", "error": str(data.get("error"))[:400], "model": final_model}
                    return
                if isinstance(data, dict) and data.get("model"):
                    final_model = data.get("model") or final_model
                msg = (data.get("message") or {}) if isinstance(data, dict) else {}
                cdelta = ""
                tdelta = ""
                if isinstance(msg, dict):
                    cdelta = str(msg.get("content") or "")
                    tdelta = str(msg.get("thinking") or "")
                if cdelta:
                    content_acc += cdelta
                    yield {"type": "delta", "channel": "content", "delta": cdelta, "model": final_model}
                if tdelta:
                    thinking_acc += tdelta
                    # stream thinking sparingly to UI (it can be verbose)
                    yield {"type": "delta", "channel": "thinking", "delta": tdelta, "model": final_model}
                if isinstance(data, dict) and data.get("done"):
                    if not content_acc.strip() and thinking_acc.strip():
                        content_acc = thinking_acc.strip()
                    yield {
                        "type": "done",
                        "ok": True,
                        "model": final_model,
                        "content": content_acc,
                        "thinking": thinking_acc[:12000],
                        "thinking_present": bool(thinking_acc.strip()),
                        "done_reason": data.get("done_reason"),
                        "truncated": data.get("done_reason") == "length",
                        "note": "Response truncated at the token limit. Increase 'Max reply tokens' in Settings for longer answers." if data.get("done_reason") == "length" else "",
                        "total_duration": data.get("total_duration"),
                        "eval_count": data.get("eval_count"),
                        "created_at": utcnow(),
                    }
                    return
        # stream ended without done
        if not content_acc.strip() and thinking_acc.strip():
            content_acc = thinking_acc.strip()
        yield {"type": "done", "ok": True, "model": final_model, "content": content_acc, "thinking": thinking_acc[:12000], "thinking_present": bool(thinking_acc.strip()), "created_at": utcnow()}
    except Exception as e:
        yield {"type": "error", "error": f"{type(e).__name__}: {str(e)[:400]}", "model": final_model}
