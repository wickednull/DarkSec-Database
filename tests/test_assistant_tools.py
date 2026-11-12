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
    assert set(names) == {
        "get_live_status", "get_library_document", "search_library",
        "list_vault_notes", "get_vault_note", "edit_vault_note",
        "search_mission_log", "list_tasks", "create_task", "complete_task",
        "query_inventory", "adjust_inventory", "create_incident",
        "resolve_incident", "read_checklist", "update_checklist",
        "read_water_log", "log_water_test", "save_log_entry", "save_note",
    }
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


def test_ai_note_save_requires_configured_and_unlocked_vault(tmpdb):
    vault = Vault(tmpdb)
    with pytest.raises(ValueError, match="not configured"):
        server.ai_note_save(tmpdb, vault, "t", "b")
    vault.setup("correct horse battery staple")
    vault.lock()
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


def test_tool_schemas_are_gated_by_intent():
    # Tool schemas cost roughly 900 prompt tokens, so they are only attached
    # when the turn plausibly needs one.
    assert assistant.tools_relevant("help me plan 72 hours of water and food") is False
    assert assistant.tools_relevant("what is the current weather?") is True
    assert assistant.tools_relevant("save a note that the filter is fixed") is True
    assert assistant.tools_relevant("search the library for pump specs") is True


def test_system_prompt_is_stable_and_volatile_context_is_separate(tmpdb):
    a = assistant.build_messages(tmpdb, "how much water per day?", include_tools=False)
    b = assistant.build_messages(tmpdb, "how much food per day?", include_tools=False)
    # The first system message must stay byte-identical so Ollama can reuse its
    # cached prompt prefix between turns.
    assert a[0] == b[0]
    assert "Current UTC time" not in a[0]["content"]
    assert a[-1]["role"] == "user"

    timed = assistant.build_messages(tmpdb, "what time is it in UTC?", include_tools=False)
    assert any("Current UTC time" in m["content"] for m in timed)
    assert timed[-1]["role"] == "user"


def test_library_context_is_only_injected_for_document_questions(tmpdb):
    assert assistant._wants_library_context("how much water per day?") is False
    assert assistant._wants_library_context("according to the water manual, which filter?") is True


def test_vault_preflight_prompts_before_save_note(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    vault.lock()
    preflight = server.assistant_vault_preflight(vault)
    events = preflight("save_note", {"title": "Pending", "body": "secret"})
    assert events is not None
    first = next(events)
    assert first["type"] == "vault_unlock_required"
    events.close()
    assert preflight("save_log_entry", {"title": "x", "body": "y"}) is None

    vault.unlock("correct horse battery staple")
    assert preflight("save_note", {"title": "Pending", "body": "secret"}) is None


def test_vault_preflight_releases_after_unlock(monkeypatch):
    class FakeVault:
        checks = 0
        def configured(self):
            return True
        def unlocked(self):
            self.checks += 1
            return self.checks > 2
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    events = list(server.assistant_vault_preflight(FakeVault())(
        "save_note", {"title": "Pending", "body": "secret"}))
    assert events[0]["type"] == "vault_unlock_required"
    assert events[-1] == {"type": "vault_unlock_result", "tool": "save_note", "unlocked": True}


def test_vault_read_tools_list_and_get(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    server.ai_note_save(tmpdb, vault, "Water Plan", "Boil water for one minute.", "water")
    server.ai_note_save(tmpdb, vault, "Radio Plan", "Channel 3 at 19:00.", "comms")

    listed = server.ai_vault_notes_list(tmpdb, vault)
    assert listed["ok"] and listed["count"] == 2
    assert {n["title"] for n in listed["notes"]} == {"Water Plan", "Radio Plan"}

    got = server.ai_vault_note_get(tmpdb, vault, title="water plan")
    assert got["ok"] and got["note"]["title"] == "Water Plan"
    assert "Boil water" in got["note"]["body"]
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == 2  # read did not create a note

    listed_reply = server.ai_vault_read_reply(tmpdb, vault, "list my vault notes")
    assert "Water Plan" in listed_reply and "Radio Plan" in listed_reply
    read_reply = server.ai_vault_read_reply(tmpdb, vault, "read me the water plan note")
    assert "Boil water" in read_reply
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == 2

    vault.lock()
    with pytest.raises(ValueError, match="Vault is locked"):
        server.ai_vault_read_reply(tmpdb, vault, "read the water plan note")


def test_read_intent_does_not_expose_save_tools():
    cfg = {"tools": True, "tools_auto": False}
    specs = assistant._tools_for_turn(cfg, "read me my water vault note", lambda name, args: {})
    names = {spec["function"]["name"] for spec in specs}
    assert "get_vault_note" in names and "list_vault_notes" in names
    assert "save_note" not in names and "save_log_entry" not in names

    save_specs = assistant._tools_for_turn(cfg, "save a note titled water plan", lambda name, args: {})
    save_names = {spec["function"]["name"] for spec in save_specs}
    assert "save_note" in save_names

    edit_specs = assistant._tools_for_turn(cfg, "edit my water vault note", lambda name, args: {})
    edit_names = {spec["function"]["name"] for spec in edit_specs}
    assert "edit_vault_note" in edit_names
    assert "save_note" not in edit_names and "save_log_entry" not in edit_names

    assert assistant.vault_direct_read_intent("read the water note") is True
    assert assistant.vault_direct_read_intent("summarize the water note") is False
    assert assistant.vault_edit_intent("edit the water note") is True
    assert assistant.vault_direct_read_intent("edit the water note") is False


def test_vault_fast_events_returns_note_without_model(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    server.ai_note_save(tmpdb, vault, "Water Plan", "Boil water for one minute.", "water")
    events = list(server.vault_fast_events(tmpdb, vault, "read me the water plan note"))
    assert events[-1]["type"] == "done"
    assert "Boil water" in events[-1]["content"]
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == 1


def test_vault_note_update_edits_without_creating(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    server.ai_note_save(tmpdb, vault, "Water Plan", "Boil water for one minute.", "water")
    before = tmpdb.one("SELECT count(*) c FROM notes")["c"]

    out = server.ai_vault_note_update(tmpdb, vault, title="water plan",
                                      new_title="Water Plan v2", body="Filter and boil.",
                                      tags="water,updated")
    assert out["ok"] and out["note"]["title"] == "Water Plan v2"
    assert out["note"]["body"] == "Filter and boil."
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == before

    appended = server.ai_vault_note_update(tmpdb, vault, title="Water Plan v2",
                                           append="Store four litres per person.")
    assert appended["ok"]
    assert "Store four litres" in appended["note"]["body"]
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == before

    missing = server.ai_vault_note_update(tmpdb, vault, title="radio plan", body="x")
    assert missing["ok"] is False
    assert tmpdb.one("SELECT count(*) c FROM notes")["c"] == before


def test_mission_log_and_task_tools(tmpdb):
    server.ai_log_save(tmpdb, "Water filter check", "Changed the sediment filter.")
    logs = server.ai_mission_log_search(tmpdb, query="sediment")
    assert logs["ok"] and logs["count"] == 1
    assert "sediment" in logs["items"][0]["body"].lower()

    created = server.ai_task_create(tmpdb, "Inspect generator", priority="PRIORITY")
    assert created["ok"]
    task_id = created["task"]["id"]
    listed = server.ai_tasks_list(tmpdb)
    assert any(t["id"] == task_id for t in listed["tasks"])
    done = server.ai_task_complete(tmpdb, title="Inspect generator")
    assert done["ok"] and done["task"]["done"] == 1
    assert not any(t["id"] == task_id for t in server.ai_tasks_list(tmpdb)["tasks"])


def test_inventory_tools(tmpdb):
    created = server.ai_inventory_adjust(tmpdb, name="Water filters", qty=3, unit="ea", min_qty=2)
    assert created["ok"] and created["created"] and created["item"]["qty"] == 3
    item_id = created["item"]["id"]

    low = server.ai_inventory_query(tmpdb, low_only=True)
    assert low["ok"]
    adjusted = server.ai_inventory_adjust(tmpdb, item_id=item_id, delta=-2)
    assert adjusted["ok"] and adjusted["new_qty"] == 1
    low = server.ai_inventory_query(tmpdb, low_only=True)
    assert any(i["id"] == item_id and i["status"] == "LOW" for i in low["items"])


def test_incident_checklist_and_water_tools(tmpdb):
    inc = server.ai_incident_create(tmpdb, "Gate sensor fault", severity="WARNING")
    assert inc["ok"]
    inc_id = inc["incident"]["id"]
    resolved = server.ai_incident_resolve(tmpdb, incident_id=inc_id, resolution="Replaced battery.")
    assert resolved["ok"] and resolved["incident"]["resolved"] == 1
    assert "Replaced battery" in resolved["incident"]["detail"]

    kit = server.CHECKLIST_DATA["kits"][0]
    item = kit["items"][0]
    read = server.ai_checklist_read(tmpdb, kit_id=kit["id"])
    assert read["ok"] and read["kits"][0]["id"] == kit["id"]
    updated = server.ai_checklist_update(tmpdb, kit_id=kit["id"], item_id=item["id"], checked=True)
    assert updated["ok"] and updated["checked"] is True
    read = server.ai_checklist_read(tmpdb, kit_id=kit["id"])
    assert next(i for i in read["kits"][0]["items"] if i["id"] == item["id"])["checked"] is True

    logged = server.ai_water_test_log(tmpdb, source="Well", volume_l=2, chlorine_ppm=0.1, method="DPD")
    assert logged["ok"] and logged["item"]["chlorine_status"] == "LOW"
    recent = server.ai_water_log_read(tmpdb)
    assert recent["ok"] and recent["items"][0]["id"] == logged["item"]["id"]


def test_system_file_tools_are_opt_in_and_rooted(tmpdb, tmp_path):
    from pathlib import Path

    root = Path(tmpdb.path).parent
    if root == tmp_path or root not in Path(tmpdb.path).parents:
        root = tmp_path
    (root / "notes.txt").write_text("hello field", encoding="utf-8")
    (root / ".ssh").mkdir(exist_ok=True)
    (root / ".ssh" / "id_rsa").write_text("secret", encoding="utf-8")

    disabled = server.ai_system_read(tmpdb, str(root / "notes.txt"))
    assert disabled["ok"] is False and "disabled" in disabled["error"]

    tmpdb.set_setting("assistant_system_tools", True)
    tmpdb.set_setting("assistant_system_root", str(root))

    listed = server.ai_system_list(tmpdb, ".")
    assert listed["ok"] and any(e["name"] == "notes.txt" for e in listed["entries"])
    assert not any(e["name"] == ".ssh" for e in listed["entries"])

    read = server.ai_system_read(tmpdb, "notes.txt")
    assert read["ok"] and read["content"] == "hello field"
    denied = server.ai_system_read(tmpdb, ".ssh/id_rsa")
    assert denied["ok"] is False

    outside = server.ai_system_read(tmpdb, "../outside.txt")
    assert outside["ok"] is False

    new = server.ai_system_write(tmpdb, "new.txt", "first")
    assert new["ok"]
    exists = server.ai_system_write(tmpdb, "new.txt", "second")
    assert exists["ok"] is False and exists.get("exists") is True
    replaced = server.ai_system_write(tmpdb, "new.txt", "second", overwrite=True)
    assert replaced["ok"] and (root / "new.txt.bak").read_text(encoding="utf-8") == "first"
    appended = server.ai_system_write(tmpdb, "new.txt", " plus", append=True)
    assert appended["ok"] and (root / "new.txt").read_text(encoding="utf-8") == "second plus"


def test_system_tools_only_in_spec_when_enabled():
    base = assistant.tool_specs()
    base_names = {s["function"]["name"] for s in base}
    assert "read_system_file" not in base_names
    enabled = assistant.tool_specs({"system_tools": True})
    enabled_names = {s["function"]["name"] for s in enabled}
    assert {"list_system_directory", "read_system_file", "write_system_file"} <= enabled_names
    assert assistant.system_write_intent("write this to a file") is True
    assert assistant.system_write_intent("list my files") is False


def test_system_read_converts_encodings_and_documents(tmpdb):
    from pathlib import Path
    import zipfile

    root = Path(tmpdb.path).parent
    tmpdb.set_setting("assistant_system_tools", True)
    tmpdb.set_setting("assistant_system_root", str(root))

    (root / "latin.txt").write_bytes("café field note".encode("cp1252"))
    latin = server.ai_system_read(tmpdb, "latin.txt")
    assert latin["ok"] and "café" in latin["content"]

    (root / "utf16.txt").write_text("UTF-16 text", encoding="utf-16")
    utf16 = server.ai_system_read(tmpdb, "utf16.txt")
    assert utf16["ok"] and "UTF-16 text" in utf16["content"]

    with zipfile.ZipFile(root / "sample.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document><w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body></w:document>",
        )
    docx = server.ai_system_read(tmpdb, "sample.docx")
    assert docx["ok"] and "Hello DOCX" in docx["content"] and docx["format"] == "docx"

    (root / "binary.bin").write_bytes(b"\x00\x01\x02\xff" * 100)
    binary = server.ai_system_read(tmpdb, "binary.bin")
    assert binary["ok"] is False
