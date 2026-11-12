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
        "get_live_status", "get_sitrep", "save_sitrep",
        "get_library_document", "search_library",
        "list_vault_notes", "get_vault_note", "edit_vault_note",
        "search_mission_log", "list_tasks", "create_task", "complete_task", "update_task",
        "query_inventory", "adjust_inventory", "delete_inventory",
        "create_incident", "resolve_incident", "list_incidents", "update_incident",
        "update_mission_log", "delete_mission_log",
        "read_checklist", "update_checklist", "reset_checklist",
        "read_water_log", "log_water_test", "list_calculators", "calculate",
        "convert_coordinates", "distance_bearing", "list_waypoints", "create_waypoint",
        "save_log_entry", "save_note",
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
    preflight = server.assistant_tool_preflight(tmpdb, vault)
    events = preflight("save_note", {"title": "Pending", "body": "secret"})
    assert events is not None
    first = next(events)
    assert first["type"] == "vault_unlock_required"
    events.close()
    assert preflight("save_log_entry", {"title": "x", "body": "y"}) is None

    vault.unlock("correct horse battery staple")
    assert preflight("save_note", {"title": "Pending", "body": "secret"}) is None


def test_vault_preflight_releases_after_unlock(tmpdb, monkeypatch):
    class FakeVault:
        checks = 0
        def configured(self):
            return True
        def unlocked(self):
            self.checks += 1
            return self.checks > 2
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)
    events = list(server.assistant_tool_preflight(tmpdb, FakeVault())(
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
    assert done.get("undo", {}).get("token")
    assert not any(t["id"] == task_id for t in server.ai_tasks_list(tmpdb)["tasks"])


def test_inventory_tools(tmpdb):
    created = server.ai_inventory_adjust(tmpdb, name="Water filters", qty=3, unit="ea", min_qty=2)
    assert created["ok"] and created["created"] and created["item"]["qty"] == 3
    assert created.get("undo", {}).get("token")
    item_id = created["item"]["id"]

    low = server.ai_inventory_query(tmpdb, low_only=True)
    assert low["ok"]
    adjusted = server.ai_inventory_adjust(tmpdb, item_id=item_id, delta=-2)
    assert adjusted["ok"] and adjusted["new_qty"] == 1
    assert adjusted.get("undo", {}).get("token")
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


def test_referential_save_uses_exact_previous_text(tmpdb):
    vault = Vault(tmpdb)
    vault.setup("correct horse battery staple")
    history = [{"role": "user", "content": "plan?"},
               {"role": "assistant", "content": "EXACT NOTE\nBoil water for one minute."}]
    events = list(server.vault_save_fast_events(
        tmpdb, vault, "save this as a note", history))
    assert events[-1]["type"] == "done"
    assert "Saved to the encrypted vault" in events[-1]["content"]
    listed = server.ai_vault_notes_list(tmpdb, vault)
    assert listed["count"] == 1
    got = server.ai_vault_note_get(tmpdb, vault, title="EXACT NOTE")
    assert got["ok"] and got["note"]["body"] == "EXACT NOTE\nBoil water for one minute."

    assert assistant.referential_save_intent("save this as a note") is True
    assert assistant.referential_save_intent("save a note about water") is False
    assert assistant.referential_save_intent("save this note: buy water") is False


def test_tool_selection_is_intent_scoped():
    base = {"tools": True, "tools_auto": True, "system_tools": False}
    note_names = {s["function"]["name"] for s in assistant._tools_for_turn(
        base, "save a note titled water plan", lambda name, args: {})}
    assert "save_note" in note_names
    assert "query_inventory" not in note_names
    assert len(note_names) <= 5

    task_names = {s["function"]["name"] for s in assistant._tools_for_turn(
        base, "what tasks are open", lambda name, args: {})}
    assert task_names == {"list_tasks", "create_task", "complete_task", "update_task"}

    file_names = {s["function"]["name"] for s in assistant._tools_for_turn(
        {**base, "system_tools": True}, "read the file notes.txt", lambda name, args: {})}
    assert file_names == {"list_system_directory", "read_system_file", "write_system_file"}

    calc_names = {s["function"]["name"] for s in assistant._tools_for_turn(
        base, "calculate wind chill for 0 C and 20 km/h", lambda name, args: {})}
    assert "calculate" in calc_names and "adjust_inventory" not in calc_names

    nav_names = {s["function"]["name"] for s in assistant._tools_for_turn(
        base, "convert 43.48 -111.98 to mgrs and show the bearing", lambda name, args: {})}
    assert "convert_coordinates" in nav_names and "distance_bearing" in nav_names


def test_write_preflight_requires_confirmation(tmpdb):
    vault = Vault(tmpdb)
    preflight = server.assistant_tool_preflight(tmpdb, vault)

    events = preflight("create_task", {"title": "Inspect filter"})
    first = next(events)
    assert first["type"] == "tool_confirm_required"
    with server._CONFIRM_LOCK:
        server._CONFIRM_PENDING[first["id"]]["approved"] = True
    assert list(events) == []

    denied = preflight("create_task", {"title": "Second task"})
    first = next(denied)
    with server._CONFIRM_LOCK:
        server._CONFIRM_PENDING[first["id"]]["approved"] = False
    with pytest.raises(assistant.ToolDenied):
        list(denied)


def test_calculators_and_navigation_tools(tmpdb):
    calc_list = server.ai_calc_list()
    assert calc_list["ok"] and calc_list["count"] > 10
    assert any(c["name"] == "wind_chill_c" for c in calc_list["calculators"])
    calc = server.ai_calculate("wind_chill_c", {"temp_c": 0, "wind_kmh": 20})
    assert calc["ok"] and "result" in calc
    unknown = server.ai_calculate("not_a_calculator")
    assert unknown["ok"] is False

    coords = server.ai_convert_coordinates(43.48, -111.98)
    assert coords["ok"] and coords["mgrs"] and coords["utm"]

    dist = server.ai_distance_bearing(tmpdb, 43.48, -111.98, 43.50, -112.00)
    assert dist["ok"] and 0 < dist["distance_km"] < 10
    assert dist["bearing_cardinal"]

    created = server.ai_waypoint_create(tmpdb, "Ridge Cache", 43.5, -112.0, type="CACHE")
    assert created["ok"] and created.get("undo", {}).get("token")
    waypoints = server.ai_waypoints_list(tmpdb, query="ridge")
    assert waypoints["ok"] and any(w["name"] == "Ridge Cache" for w in waypoints["waypoints"])


def test_medical_read_tools(tmpdb):
    tmpdb.set_setting("assistant_medical_tools", True)
    pid = tmpdb.insert("patients", {
        "callsign": "RAVEN", "name": "Test Patient", "age": 40, "sex": "F",
        "weight_kg": 70, "blood_type": "O+", "allergies": "penicillin",
        "meds": "none", "history": "hypertension", "triage": "PRIORITY",
        "status": "OPEN", "note": "stable", "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
    })
    tmpdb.insert("vitals", {
        "ts": "2026-01-01T01:00:00+00:00", "patient_id": pid, "hr": 88, "rr": 16,
        "sys": 120, "dia": 80, "spo2": 98, "temp_c": 37.0, "glucose": 100,
        "pain": 2, "avpu": "A", "note": "ok",
    })
    tmpdb.insert("medication_log", {
        "ts": "2026-01-01T02:00:00+00:00", "patient_id": pid, "medication": "aspirin",
        "dose": "81 mg", "route": "PO", "administered_by": "operator",
        "indication": "headache", "reaction": "none", "note": "",
    })
    listed = server.ai_patients_list(tmpdb, query="raven")
    assert listed["ok"] and listed["patients"][0]["callsign"] == "RAVEN"

    patient = server.ai_patient_get(tmpdb, callsign="RAVEN")
    assert patient["ok"] and patient["patient"]["allergies"] == "penicillin"
    assert patient["vitals"][0]["hr"] == 88
    assert patient["medications"][0]["medication"] == "aspirin"

    vitals = server.ai_vitals_list(tmpdb, callsign="RAVEN")
    assert vitals["ok"] and vitals["vitals"][0]["spo2"] == 98
    meds = server.ai_medications_list(tmpdb, callsign="RAVEN")
    assert meds["ok"] and meds["medications"][0]["dose"] == "81 mg"

    tmpdb.set_setting("assistant_medical_tools", False)
    disabled = server.ai_patients_list(tmpdb)
    assert disabled["ok"] is False and "disabled" in disabled["error"]


def test_medical_tools_are_opt_in():
    base = assistant.tool_specs()
    assert "get_patient" not in {s["function"]["name"] for s in base}
    enabled = assistant.tool_specs({"medical_tools": True})
    assert {"list_patients", "get_patient", "list_vitals", "list_medications"} <= {
        s["function"]["name"] for s in enabled}

    cfg = {"tools": True, "tools_auto": True, "medical_tools": True}
    names = {s["function"]["name"] for s in assistant._tools_for_turn(
        cfg, "show patient RAVEN vitals and meds", lambda name, args: {})}
    assert {"list_patients", "get_patient", "list_vitals", "list_medications"} <= names
    assert "adjust_inventory" not in names


def test_record_editing_and_listing_tools(tmpdb):
    task = server.ai_task_create(tmpdb, "Inspect filter")["task"]
    updated = server.ai_task_update(tmpdb, task_id=task["id"], detail="Check gasket",
                                    priority="PRIORITY", done=False)
    assert updated["ok"] and updated["task"]["detail"] == "Check gasket"
    assert updated.get("undo", {}).get("token")

    inc = server.ai_incident_create(tmpdb, "Gate fault", severity="WARNING")["incident"]
    listed = server.ai_incidents_list(tmpdb)
    assert listed["ok"] and any(i["id"] == inc["id"] for i in listed["incidents"])
    edited = server.ai_incident_update(tmpdb, incident_id=inc["id"], severity="CRITICAL",
                                       detail="Sensor failed")
    assert edited["ok"] and edited["incident"]["severity"] == "CRITICAL"

    log = server.ai_log_save(tmpdb, "Old log title", "Body text")
    edited_log = server.ai_mission_log_update(tmpdb, log["id"], title="New log title",
                                              body="Updated text")
    assert edited_log["ok"] and edited_log["item"]["title"] == "New log title"
    deleted_log = server.ai_mission_log_delete(tmpdb, log["id"])
    assert deleted_log["ok"] and deleted_log.get("undo", {}).get("token")
    assert tmpdb.one("SELECT count(*) c FROM log_entries WHERE id=?", (log["id"],))["c"] == 0

    item = server.ai_inventory_adjust(tmpdb, name="Old rope", qty=2)["item"]
    deleted_item = server.ai_inventory_delete(tmpdb, item_id=item["id"])
    assert deleted_item["ok"] and deleted_item.get("undo", {}).get("token")
    assert tmpdb.one("SELECT count(*) c FROM inventory WHERE id=?", (item["id"],))["c"] == 0

    kit = server.CHECKLIST_DATA["kits"][0]
    item_id = kit["items"][0]["id"]
    server.ai_checklist_update(tmpdb, kit_id=kit["id"], item_id=item_id, checked=True)
    reset = server.ai_checklist_reset(tmpdb, kit["id"])
    assert reset["ok"] and reset.get("undo", {}).get("token")
    assert tmpdb.one("SELECT count(*) c FROM checklist_state WHERE kit_id=?", (kit["id"],))["c"] == 0


def test_library_page_and_section_citations(tmpdb):
    from darksec import library

    doc = _add_doc(
        tmpdb, "FIELD MANUAL.pdf",
        "CHAPTER 1 SAFETY\nWater purification steps.\f"
        "CHAPTER 2 SHELTER\nShelter construction notes.\f"
        "CHAPTER 3 FOOD\nFood storage guidance.",
    )
    tmpdb.execute("UPDATE library_docs SET pages=3 WHERE id=?", (doc,))

    focused = library.document_excerpt(tmpdb, doc, query="construction notes", max_chars=600)
    assert focused["focus"] is True
    assert focused["pages"] == [2, 2]
    assert focused["page_count"] == 3
    assert "page 2" in focused["citation"]
    assert any("SHELTER" in section for section in focused["sections"])

    page_two = library.document_excerpt(tmpdb, doc, page=2, max_chars=600)
    assert "Shelter construction" in page_two["text"]
    assert "Water purification" not in page_two["text"]
    assert page_two["pages"] == [2, 2]
    assert page_two["unit"] == "page"

    span = library.document_excerpt(tmpdb, doc, page=2, span=2, max_chars=600)
    assert span["pages"] == [2, 3]
    assert "Food storage" in span["text"]

    try:
        library.document_excerpt(tmpdb, doc, page=9)
    except ValueError as exc:
        assert "outside this document" in str(exc)
    else:
        raise AssertionError("out-of-range page should fail")

    assert library.page_for_query(str(tmpdb.one(
        "SELECT text FROM library_docs WHERE id=?", (doc,))["text"]), "food") == 3
    assert library._normalize_page_breaks("one\n\f\n\ntwo\f") == "one\ftwo"


def test_library_tool_declares_page_citation_parameters():
    spec = next(s for s in assistant.tool_specs()
                if s["function"]["name"] == "get_library_document")
    props = spec["function"]["parameters"]["properties"]
    assert {"query", "page", "span", "max_chars"} <= set(props)
    assert "page" in spec["function"]["description"].lower()


def test_sitrep_generation_and_save_tools(tmpdb):
    out = server.ai_sitrep(tmpdb)
    assert out["ok"] is True
    assert "FIELD SITUATION REPORT" in out["brief"]
    assert "STANCE:" in out["brief"]

    saved = server.ai_sitrep_save(tmpdb, note="shift change")
    assert saved["ok"] is True and saved["id"]
    row = tmpdb.one("SELECT * FROM log_entries WHERE id=?", (saved["id"],))
    assert "OPERATOR NOTE: shift change" in row["body"]
    assert "sitrep" in row["tags"]
    assert saved["undo"]["token"]

    cfg = {"tools": True, "tools_auto": True}
    names = {s["function"]["name"] for s in assistant._tools_for_turn(
        cfg, "give me a sitrep and save it to the log", lambda name, args: {})}
    assert {"get_sitrep", "save_sitrep"} <= names
