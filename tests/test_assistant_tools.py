"""Tests for Delamain's local write tools and their anti-spam caps."""
from __future__ import annotations

import json

import pytest

from darksec import assistant
from darksec.db import Database
from darksec.security import Vault
from darksec import server


@pytest.fixture()
def tmpdb(tmp_path):
    db = Database(str(tmp_path / "tools.db"))
    yield db
    db.close()


def test_tool_specs_shape():
    names = [t["function"]["name"] for t in assistant.tool_specs()]
    assert set(names) == {"get_live_status", "get_library_document", "search_library",
                          "save_log_entry", "save_note"}
    for spec in assistant.tool_specs():
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["required"] is not None


def test_tool_call_args_parses_json_strings():
    tc = {"function": {"name": "save_log_entry", "arguments": '{"title": "a", "body": "b"}'}}
    assert assistant._tool_call_args(tc) == ("save_log_entry", {"title": "a", "body": "b"})
    assert assistant._tool_call_args({"function": {"name": "x", "arguments": "not json"}}) == ("x", {})


def test_ai_log_save_and_hourly_cap(tmpdb, monkeypatch):
    monkeypatch.setattr(server, "AI_LOG_MAX_PER_HOUR", 2)
    server.ai_log_save(tmpdb, "One", "first")
    server.ai_log_save(tmpdb, "Two", "second")
    with pytest.raises(ValueError, match="limit reached"):
        server.ai_log_save(tmpdb, "Three", "third")
    assert tmpdb.one("SELECT count(*) c FROM log_entries WHERE category='AI'")["c"] == 2


def test_ai_log_save_prunes_total_cap(tmpdb, monkeypatch):
    monkeypatch.setattr(server, "AI_LOG_TOTAL_CAP", 2)
    server.ai_log_save(tmpdb, "One", "first")
    server.ai_log_save(tmpdb, "Two", "second")
    server.ai_log_save(tmpdb, "Three", "third")
    assert tmpdb.one("SELECT count(*) c FROM log_entries WHERE category='AI'")["c"] == 2


def test_ai_note_save_requires_unlocked_vault(tmpdb):
    vault = Vault(tmpdb)
    with pytest.raises(ValueError, match="Vault is locked"):
        server.ai_note_save(tmpdb, vault, "t", "b")


def test_ai_note_save_encrypts_and_caps(tmpdb, monkeypatch):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    vault.unlock("correct horse battery staple")
    monkeypatch.setattr(server, "AI_NOTE_MAX_PER_HOUR", 1)
    out = server.ai_note_save(tmpdb, vault, "Title", "Body", "tag1")
    assert out["ok"] and out["id"]
    row = tmpdb.one("SELECT * FROM notes WHERE id=?", (out["id"],))
    assert row["tags"] == "delamain"
    assert "Body" not in row["cipher"]
    payload = json.loads(vault.decrypt(row["cipher"]))
    assert payload["title"] == "Title" and payload["body"] == "Body"
    with pytest.raises(ValueError, match="limit reached"):
        server.ai_note_save(tmpdb, vault, "Another", "Body")


def _add_doc(db, name, text, category="MANUALS"):
    return db.insert("library_docs", {
        "path": f"/tmp/{name}", "name": name, "category": category,
        "rel_path": f"manuals/{name}", "size": len(text), "mtime": 1.0,
        "pages": 1, "chars": len(text), "text": text,
        "indexed_ts": "2026-01-01T00:00:00+00:00",
        "text_ts": "2026-01-01T00:00:00+00:00",
    })


def test_library_find_document_tolerates_misspelled_titles(tmpdb):
    from darksec import library

    lathe = _add_doc(
        tmpdb, "GRIZZLEY WOOD LATHE MANUAL.pdf",
        "Wood lathe safety. Set spindle speed before turning. Grizzly machine.",
    )
    heli = _add_doc(
        tmpdb, "THE ELECTRIC HELICOPTER BEGINEERS GUIDE VOL 16.pdf",
        "Electric helicopter beginner guide. Rotor tracking and battery care.",
    )
    _add_doc(tmpdb, "UNRELATED WATER FILTER NOTES.pdf", "Filter media replacement.")

    lathe_hits = library.find_document(tmpdb, "Grizzly Wood Lathe Manual")
    assert lathe_hits and lathe_hits[0]["id"] == lathe
    heli_hits = library.find_document(tmpdb, "Electric Helicopter Beginner's Guide")
    assert heli_hits and heli_hits[0]["id"] == heli
    assert library.find_document(tmpdb, "nuclear submarine reactor") == []


def test_library_document_excerpt_focuses_on_query(tmpdb):
    from darksec import library

    doc = _add_doc(
        tmpdb, "GRIZZLEY WOOD LATHE MANUAL.pdf",
        ("Introduction. " * 200) + "Spindle speed must be set before turning. " + ("Appendix. " * 200),
    )
    out = library.document_excerpt(tmpdb, doc, query="spindle speed", max_chars=800)
    assert "spindle speed" in out["text"].lower()
    assert out["focus"] is True
    assert out["chars"] > 1000
    assert out["truncated"] is True


def test_assistant_live_status_offline_shape(tmpdb, monkeypatch):
    monkeypatch.setattr(server.tilecache, "is_online", lambda: False)
    out = server.assistant_live_status(tmpdb)
    assert out["online"] is False
    assert out["position"].get("lat") is not None or out["position"].get("missing") is True
    assert isinstance(out["feeds"], dict) and "problems" in out["feeds"]
    assert "readiness" in out and "signals" in out["readiness"]
    assert "Local cache only" in out["note"]


def test_vault_has_no_idle_autolock(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    assert vault.unlocked() is True
    assert "timeout_min" not in vault.status()


def test_sitrep_warns_when_vault_is_open(tmpdb):
    from darksec import sitrep

    open_actions = sitrep.synthesize_actions(tmpdb, 39.8, -98.5, vault={"unlocked": True})
    assert any("vault is open" in a["text"].lower() for a in open_actions)
    locked_actions = sitrep.synthesize_actions(tmpdb, 39.8, -98.5, vault={"unlocked": False})
    assert not any("vault is open" in a["text"].lower() for a in locked_actions)
