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
    assert names == ["save_log_entry", "save_note", "search_library"]
    for spec in assistant.tool_specs():
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["required"]


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
