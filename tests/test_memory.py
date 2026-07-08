"""Tests for the memory system with sqlite-vec and FTS5."""

import sys
import types

import pytest

from ares import embeddings
from ares.memory import MemoryStore


@pytest.fixture
def store(tmp_path, fake_embedding_provider):
    """Create a fresh MemoryStore with a temp database."""
    db_path = tmp_path / "test_memory.db"
    s = MemoryStore(db_path=db_path, embedding_provider=fake_embedding_provider)
    yield s
    s.close()


class TestMemoryStore:
    def test_store_and_retrieve(self, store):
        """Storing a fact returns an ID and the fact can be found."""
        fact_id = store.store(
            "User prefers dark mode",
            category="preference",
            importance=0.9,
            source="test",
        )
        assert fact_id is not None
        assert fact_id > 0
        fact = store.get(fact_id)
        assert fact["importance"] == 0.9
        assert fact["source"] == "test"

    def test_search_by_semantic_similarity(self, store):
        """Searching with similar text returns relevant results."""
        store.store("User's birthday is March 5", category="fact")
        store.store("User likes pizza", category="preference")
        results = store.search("birthday")
        assert len(results) >= 1
        assert "birthday" in results[0]["fact_text"].lower() or "march" in results[0]["fact_text"].lower()

    def test_search_by_keyword(self, store):
        """FTS5 keyword search finds exact matches."""
        store.store("User works at Google", category="fact")
        store.store("User has a cat named Luna", category="relationship")
        results = store.search("Google")
        assert len(results) >= 1
        assert "Google" in results[0]["fact_text"]

    def test_delete_fact(self, store):
        """Deleting a fact removes it from all tables."""
        fact_id = store.store("Temporary fact")
        assert store.delete(fact_id) is True
        # Should not be findable anymore
        results = store.search("Temporary fact")
        assert len(results) == 0

    def test_delete_nonexistent(self, store):
        """Deleting a nonexistent fact returns False."""
        assert store.delete(99999) is False

    def test_update_fact_refreshes_metadata_and_search(self, store):
        """Updating a memory changes metadata and text search indexes."""
        fact_id = store.store("User prefers tea", category="preference")
        assert store.update(
            fact_id,
            fact_text="User prefers coffee",
            confidence=0.8,
            importance=0.95,
        )

        updated = store.get(fact_id)
        assert updated["fact_text"] == "User prefers coffee"
        assert updated["confidence"] == 0.8
        assert updated["importance"] == 0.95
        results = store.search("coffee")
        assert any(r["fact_id"] == fact_id for r in results)

    def test_import_memories_skips_duplicates(self, store):
        """Memory import avoids exact duplicate text/category rows."""
        store.store("User likes terminal apps", category="preference")
        count = store.import_memories([
            {"fact_text": "User likes terminal apps", "category": "preference"},
            {"fact_text": "User uses Windows", "category": "fact", "importance": 0.7},
        ])
        assert count == 1
        assert len(store.list_all()) == 2

    def test_suggest_merge_detects_duplicate_and_conflict(self, store):
        duplicate_id = store.store("User likes tea", category="preference")
        store.store("User prefers coffee", category="preference")

        duplicate = store.suggest_merge("User likes tea", category="preference")
        conflict = store.suggest_merge("User prefers green tea", category="preference")

        assert duplicate[0]["kind"] == "duplicate"
        assert duplicate[0]["fact_id"] == duplicate_id
        assert any(item["kind"] == "possible_conflict" for item in conflict)

    def test_list_all(self, store):
        """list_all returns all stored facts."""
        store.store("Fact one")
        store.store("Fact two")
        store.store("Fact three")
        all_facts = store.list_all()
        assert len(all_facts) == 3

    def test_get_recent(self, store):
        """get_recent returns the most recently created facts."""
        store.store("Old fact")
        store.store("Recent fact")
        recent = store.get_recent(limit=1)
        assert len(recent) == 1
        assert recent[0]["fact_text"] == "Recent fact"

    def test_search_empty_db(self, store):
        """Searching an empty database returns empty list."""
        results = store.search("anything")
        assert results == []

    def test_store_works_when_sentence_transformers_import_fails(self, tmp_path, monkeypatch):
        """Memory storage falls back instead of failing when optional embedding deps break."""
        module = types.ModuleType("sentence_transformers")
        monkeypatch.setitem(sys.modules, "sentence_transformers", module)
        embeddings._MODEL_CACHE.clear()
        fallback_store = MemoryStore(
            db_path=tmp_path / "fallback.db",
            embedding_backend="onnx",
        )
        try:
            fact_id = fallback_store.store("User is a JEE aspirant", category="fact")
            results = fallback_store.search("JEE")
        finally:
            fallback_store.close()

        assert fact_id > 0
        assert any("JEE aspirant" in result["fact_text"] for result in results)
