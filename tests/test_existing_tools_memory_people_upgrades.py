"""Focused regression coverage for the upgraded Memory and People tools."""

from __future__ import annotations

import json

import pytest

from ares.memory import MemoryConflictError, MemoryStore
from ares.models import AppConfig
from ares.memory.people import PersonConflictError
from ares.tools import ToolExecutor, get_tool_definitions
from ares.tools.results import ToolResultEnvelope


ENVELOPE_KEYS = {
    "ok",
    "status",
    "summary",
    "data",
    "artifacts",
    "warnings",
    "errors",
    "next_actions",
    "provenance",
    "metrics",
    "undo_id",
}


def _definition(name: str) -> dict:
    return next(item["function"] for item in get_tool_definitions() if item["function"]["name"] == name)


@pytest.fixture
def memory(tmp_path, fake_embedding_provider):
    store = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    yield store
    store.close()


@pytest.fixture
def executor(memory, tmp_path):
    tool_executor = ToolExecutor(memory, config=AppConfig(data_dir=str(tmp_path)))
    yield tool_executor
    tool_executor.close()


def test_upgrades_keep_existing_names_and_add_only_optional_schema_fields():
    memory_names = {"store_memory", "search_memory", "update_memory"}
    people_names = {"remember_person", "search_person", "update_person"}
    names = {item["function"]["name"] for item in get_tool_definitions()}
    assert memory_names | people_names <= names

    store_properties = _definition("store_memory")["parameters"]["properties"]
    assert {"tags", "source_message_id", "valid_from", "expires_at", "links", "merge_mode", "response_format"} <= set(store_properties)
    assert _definition("store_memory")["parameters"]["required"] == ["content"]

    person_properties = _definition("remember_person")["parameters"]["properties"]
    assert {"pronouns", "timezone", "organization", "interests", "timeline", "links", "response_format"} <= set(person_properties)
    assert _definition("remember_person")["parameters"]["required"] == ["canonical_name"]


def test_structured_result_contract_is_exact_and_pydantic_validated(executor):
    payload = json.loads(executor.execute("store_memory", {
        "content": "The Phoenix migration deadline is 2026-08-01.",
        "category": "project",
        "response_format": "structured",
    }))

    assert set(payload) == ENVELOPE_KEYS
    validated = ToolResultEnvelope.model_validate(payload)
    assert validated.ok is True
    assert validated.status == "completed"
    assert validated.metrics["calculated_importance"] >= 0.7


def test_memory_metadata_links_duplicate_merge_and_provenance(executor):
    created = json.loads(executor.execute("store_memory", {
        "content": "Always use the Phoenix staging account for release checks.",
        "category": "preference",
        "tags": ["Release Check", "release-check"],
        "source": "manual",
        "source_conversation_id": "conv-17",
        "source_message_id": "msg-4",
        "valid_from": "2026-07-16T00:00:00Z",
        "expires_at": "2027-07-16T00:00:00Z",
        "project": "phoenix",
        "links": {"person": ["7"], "goal": ["12"], "file": ["docs/release.md"]},
        "response_format": "structured",
    }))
    record = created["data"]["memory"]
    assert record["tags"] == ["release-check"]
    assert record["links"] == {"file": ["docs/release.md"], "goal": ["12"], "person": ["7"]}
    assert record["source_message_id"] == "msg-4"
    assert created["provenance"]["conversation_id"] == "conv-17"

    skipped = json.loads(executor.execute("store_memory", {
        "content": record["fact_text"],
        "category": "preference",
        "response_format": "structured",
    }))
    assert skipped["ok"] is True
    assert skipped["status"] == "conflict"
    assert skipped["data"]["duplicate"]["fact_id"] == record["fact_id"]

    merged = json.loads(executor.execute("store_memory", {
        "content": record["fact_text"],
        "category": "preference",
        "merge_mode": "merge",
        "tags": ["important"],
        "links": {"action": ["41"]},
        "response_format": "structured",
    }))
    assert merged["data"]["memory"]["tags"] == ["release-check", "important"]
    assert merged["data"]["memory"]["links"]["action"] == ["41"]


def test_memory_modes_filters_revisions_conflicts_and_merge(memory, executor):
    first = memory.store(
        "Phoenix deploys on Friday.", category="project", tags=["release"], project="phoenix",
        links={"goal": ["22"], "person": ["5"]}, source="manual",
    )
    second = memory.store(
        "Phoenix deploys on Monday.", category="project", tags=["release"], project="phoenix",
        links={"goal": ["22"]}, source="manual",
    )
    memory._add_relation(first, second, "contradiction", 0.8)
    memory._add_relation(second, first, "contradiction", 0.8)
    memory.conn.commit()

    related = memory.search_advanced(mode="related", memory_id=first)
    assert related[0]["fact_id"] == second
    assert "shared entity" in related[0]["match_reason"]
    contradictions = memory.search_advanced(mode="contradictions", memory_id=first)
    assert contradictions[0]["fact_id"] == second

    filtered = memory.search_advanced(
        "Phoenix", mode="task_context", task="prepare Phoenix release",
        filters={"tags": ["release"], "project": "phoenix", "goal": "22"},
    )
    assert {item["fact_id"] for item in filtered} == {first, second}
    assert all(item["match_reason"].startswith("semantic or keyword match for task") for item in filtered)

    appended = json.loads(executor.execute("update_memory", {
        "fact_id": first,
        "mode": "append",
        "content": "Run the smoke suite first.",
        "expected_revision": 1,
        "response_format": "structured",
    }))
    assert appended["data"]["memory"]["revision"] == 2
    assert "smoke suite" in appended["data"]["memory"]["fact_text"]
    assert len(appended["data"]["history"]) == 2

    conflict = json.loads(executor.execute("update_memory", {
        "fact_id": first,
        "content": "stale write",
        "expected_revision": 1,
        "response_format": "structured",
    }))
    assert conflict["ok"] is False
    assert conflict["status"] == "conflict"

    merged = json.loads(executor.execute("update_memory", {
        "fact_id": first,
        "mode": "merge",
        "merge_memory_ids": [second],
        "expected_revision": 2,
        "response_format": "structured",
    }))
    assert merged["ok"] is True
    assert memory.get(second)["may_be_outdated"] is True
    assert "Monday" in merged["data"]["memory"]["fact_text"]

    with pytest.raises(MemoryConflictError):
        memory.update(first, fact_text="another stale write", expected_revision=1)


def test_expired_memories_are_hidden_unless_requested(memory):
    expired = memory.store("Temporary release bridge", expires_at="2020-01-01T00:00:00Z")
    current = memory.store("Permanent release bridge")

    default = memory.search_advanced("release bridge")
    inclusive = memory.search_advanced("release bridge", filters={"include_outdated": True})

    assert expired not in {item["fact_id"] for item in default}
    assert current in {item["fact_id"] for item in default}
    assert expired in {item["fact_id"] for item in inclusive}


def test_people_profile_timeline_links_masking_and_action_aware_search(executor):
    created = json.loads(executor.execute("remember_person", {
        "canonical_name": "Ananya Rao",
        "aliases": ["Anu"],
        "relation": "design partner",
        "phone": "+15555550128",
        "email": "ananya@example.test",
        "pronouns": "she/her",
        "preferred_address": "Ananya",
        "timezone": "Asia/Kolkata",
        "communication_preferences": {"quiet_hours": "22:00-08:00"},
        "preferred_contact_method": "email",
        "organization": "Ares Labs",
        "role": "Product Designer",
        "interests": ["accessibility", "motion design"],
        "reminder_preferences": {"lead_days": 2},
        "links": {"goal": ["18"], "file": ["design/brief.md"]},
        "timeline": [{"type": "met", "date": "2026-06-10", "note": "Design review"}],
        "response_format": "structured",
    }))
    person = created["data"]["person"]
    assert set(created) == ENVELOPE_KEYS
    assert person["pronouns"] == "she/her"
    assert person["links"]["goal"] == ["18"]
    assert person["timeline"][0]["event_type"] == "met"

    result = json.loads(executor.execute("search_person", {
        "query": "Annya",
        "purpose": "email the design partner",
        "include_sensitive": False,
        "response_format": "structured",
    }))
    match = result["data"]["people"][0]
    assert match["canonical_name"] == "Ananya Rao"
    assert match["recommended_channel"] == "email"
    assert match["phone"] == ""
    assert match["email"] == ""
    assert match["phone_hint"].endswith("0128")
    assert match["email_hint"].endswith("@example.test")


def test_people_alias_updates_notes_revisions_contact_conflict_and_merge(executor):
    primary = json.loads(executor.execute("remember_person", {
        "canonical_name": "Ravi Mehta", "aliases": ["Ravi"], "email": "ravi@example.test",
    }))["person"]
    duplicate = json.loads(executor.execute("remember_person", {
        "canonical_name": "Ravi M.", "aliases": ["RM"], "notes": "Met at the launch.",
        "interests": ["robotics"],
    }))["person"]

    alias_update = json.loads(executor.execute("update_person", {
        "person_id": primary["person_id"],
        "mode": "aliases",
        "add_aliases": ["Rav"],
        "remove_aliases": ["Ravi"],
        "expected_revision": 1,
        "response_format": "structured",
    }))
    assert alias_update["data"]["person"]["aliases"] == ["Rav"]
    assert len(alias_update["data"]["history"]) == 2

    note_update = json.loads(executor.execute("update_person", {
        "person_id": primary["person_id"], "mode": "append_note", "append_note": "Prefers async updates.",
        "expected_revision": 2, "response_format": "structured",
    }))
    assert "Prefers async updates." in note_update["data"]["person"]["notes"]

    merged = json.loads(executor.execute("update_person", {
        "person_id": primary["person_id"], "mode": "merge",
        "merge_person_id": duplicate["person_id"], "expected_revision": 3,
        "response_format": "structured",
    }))
    assert "RM" in merged["data"]["person"]["aliases"]
    assert "robotics" in merged["data"]["person"]["interests"]
    assert executor.people_store.get(duplicate["person_id"]) is None

    other = executor.people_store.create("Other Person")
    with pytest.raises(PersonConflictError):
        executor.people_store.update(other["person_id"], email="ravi@example.test")
    with pytest.raises(PersonConflictError):
        executor.people_store.update(primary["person_id"], notes="stale", expected_revision=1)


def test_legacy_memory_and_people_results_remain_compatible(executor):
    memory_result = executor.execute("store_memory", {"content": "Legacy calls still work."})
    assert memory_result.startswith("Stored memory #")

    person_result = json.loads(executor.execute("remember_person", {"canonical_name": "Legacy User"}))
    assert person_result["ok"] is True
    assert person_result["action"] == "remembered"
    assert "status" not in person_result
