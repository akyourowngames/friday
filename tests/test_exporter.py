"""Tests for JSON export/import."""

import json

from ares.tools import exporter as exporter_module
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
        config=AppConfig(
            api_key="secret-key",
            tavily_api_key="tvly-secret",
            browser_extension_token="extension-secret",
        ),
        path=tmp_path / "export.json",
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["config"].get("api_key") is None
    assert payload["config"].get("tavily_api_key") is None
    assert payload["config"].get("browser_extension_token") is None
    assert "tavily_api_key" in payload["secrets_redacted"]
    assert "browser_extension_token" in payload["secrets_redacted"]
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


def test_export_profiles_and_redaction_preview(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "source.db", embedding_provider=fake_embedding_provider)
    memory.store("User prefers dark mode", category="preference")

    output = export_data(
        memory_store=memory,
        config=AppConfig(api_key="secret-key", tavily_api_key=""),
        path=tmp_path / "memories.json",
        profile="memories",
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["export_profile"] == "memories"
    assert payload["memories"]
    assert payload["config"] == {}
    assert payload["redaction_preview"] == {}

    config_output = export_data(
        memory_store=memory,
        config=AppConfig(api_key="secret-key", tavily_api_key=""),
        path=tmp_path / "config.json",
        profile="config",
    )
    config_payload = json.loads(config_output.read_text(encoding="utf-8"))
    assert config_payload["redaction_preview"]["api_key"] == "redacted"
    assert config_payload["redaction_preview"]["tavily_api_key"] == "empty"


def test_import_config_preserves_local_browser_extension_token(tmp_path, fake_embedding_provider, monkeypatch):
    memory = MemoryStore(db_path=tmp_path / "source.db", embedding_provider=fake_embedding_provider)
    exported = export_data(
        memory_store=memory,
        config=AppConfig(browser_extension_token="exported-secret"),
        path=tmp_path / "config.json",
        profile="config",
    )
    saved: list[AppConfig] = []
    monkeypatch.setattr(
        exporter_module,
        "load_config",
        lambda: AppConfig(browser_extension_token="local-extension-secret"),
    )
    monkeypatch.setattr(exporter_module, "save_config", saved.append)

    counts = import_data(exported, memory_store=memory, import_config=True)

    assert counts["config"] == 1
    assert saved[0].browser_extension_token == "local-extension-secret"
    assert saved[0].context_token_budget == 2000
    assert saved[0].telegram.bot_token == ""
