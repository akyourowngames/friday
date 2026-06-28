"""Tests for JSON export/import."""

import json

from ares.conversations import ConversationStore
from ares.exporter import export_data, import_data
from ares.memory import MemoryStore
from ares.models import AppConfig


def test_export_redacts_api_key_and_imports_data(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "source.db", embedding_provider=fake_embedding_provider)
    conversations = ConversationStore(db_path=tmp_path / "source.db")
    conv_id = conversations.start_conversation()

    memory.store("User prefers dark mode", category="preference", importance=0.8)
    conversations.add_exchange(conv_id, "hi", "hello")
    output = export_data(
        memory_store=memory,
        conversation_store=conversations,
        config=AppConfig(api_key="secret-key", tavily_api_key="tvly-secret"),
        path=tmp_path / "export.json",
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["config"].get("api_key") is None
    assert payload["config"].get("tavily_api_key") is None
    assert "tavily_api_key" in payload["secrets_redacted"]
    assert payload["memories"][0]["fact_text"] == "User prefers dark mode"
    assert payload["conversation_messages"][0]["content"] == "hi"

    imported_memory = MemoryStore(
        db_path=tmp_path / "imported.db",
        embedding_provider=fake_embedding_provider,
    )
    imported_conversations = ConversationStore(db_path=tmp_path / "imported.db")
    counts = import_data(
        output,
        memory_store=imported_memory,
        conversation_store=imported_conversations,
    )

    assert counts["memories"] == 1
    assert counts["conversations"] == 1
    assert imported_memory.list_all()[0]["fact_text"] == "User prefers dark mode"
