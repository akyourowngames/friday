from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from assistant_cli.config import AssistantSettings
from assistant_cli.memory_writer import AutoMemoryWriter
from assistant_cli.rag import KnowledgeRAG
from assistant_cli.session_store import SessionStore


def make_settings(tmp_path: Path) -> AssistantSettings:
    return AssistantSettings(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="meta/llama-4-maverick-17b-128e-instruct",
        temperature=0.1,
        max_tokens=100,
        storage_dir=tmp_path / "storage",
        db_path=tmp_path / "storage" / "friday.sqlite3",
        sessions_dir=tmp_path / "storage" / "sessions",
        memory_dir=tmp_path / "memory",
        knowledge_dir=tmp_path / "knowledge",
        rag_index_dir=tmp_path / "storage" / "assistant_rag_index",
        rag_top_k=3,
        last_messages=20,
        agentic_rag_enabled=False,
        agentic_query_count=2,
        auto_memory_enabled=True,
    )


def test_session_store_writes_jsonl_sqlite_and_last_20(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = SessionStore(settings, session_id="unit-session")

    for idx in range(25):
        store.append_message("user", f"message {idx}")

    recent = store.recent_messages(limit=20)
    assert len(recent) == 20
    assert recent[0]["content"] == "message 5"
    assert recent[-1]["content"] == "message 24"

    lines = store.jsonl_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 25
    assert json.loads(lines[0])["session_id"] == "unit-session"

    hits = store.search_messages("message 24")
    assert hits
    assert hits[0].content == "message 24"


def test_auto_memory_name_fallback_without_network(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    writer = AutoMemoryWriter(settings)

    def fail_call(*args, **kwargs):
        raise RuntimeError("network disabled")

    monkeypatch.setattr(writer.client.chat.completions, "create", fail_call)
    facts = writer.extract("ahh myself krish verma", "nice to meet you")

    assert facts
    assert facts[0].bucket == "personal"
    assert facts[0].fact == "The user's name is Krish Verma."


def test_llamaindex_minilm_rag_retrieves_local_knowledge(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "identity.md").write_text(
        "# Identity\n\nKrish Verma is the saved test user for Friday.\n",
        encoding="utf-8",
    )
    rag = KnowledgeRAG(settings)
    rag.rebuild()

    hits = rag.search("saved test user name")

    assert hits
    assert any("Krish Verma" in hit.text for hit in hits)


def test_append_fact_updates_memory_file_and_rebuilds(tmp_path: Path):
    settings = replace(make_settings(tmp_path), agentic_rag_enabled=False)
    rag = KnowledgeRAG(settings)

    path = rag.append_fact("personal", "The user's name is Krish Verma.")

    assert path.name == "personal.txt"
    assert "Krish Verma" in path.read_text(encoding="utf-8")
    assert not rag.needs_rebuild()
