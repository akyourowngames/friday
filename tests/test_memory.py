"""Tests for the memory system with sqlite-vec and FTS5."""

import sys
import types

import pytest

from ares import embeddings
from ares.memory import MemoryStore


class CountingEmbeddingProvider:
    """Wrap the deterministic test embedder while recording query work."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.calls: list[str] = []

    def embed_bytes(self, text: str) -> bytes:
        self.calls.append(text)
        return self.delegate.embed_bytes(text)


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

    def test_pr28_reflected_rows_migrate_to_global_with_provenance(
        self, tmp_path, fake_embedding_provider,
    ):
        database = tmp_path / "legacy-reflection.db"
        original = MemoryStore(database, embedding_provider=fake_embedding_provider)
        fact_id = original.store(
            "User prefers focused regression tests",
            source="conversation_reflection",
            session_id="conversation-12",
        )
        original.close()

        migrated = MemoryStore(database, embedding_provider=fake_embedding_provider)
        fact = migrated.get(fact_id)

        assert fact["session_id"] is None
        assert fact["source_conversation_id"] == "conversation-12"
        assert fact["source_reflection_id"] is None
        migrated.close()

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

    def test_count(self, store):
        """count returns the total number of stored facts."""
        store.store("Fact one")
        store.store("Fact two")
        assert store.count() == 2

    def test_search_empty_db(self, store):
        """Searching an empty database returns empty list."""
        results = store.search("anything")
        assert results == []

    def test_trivial_greetings_and_confirmations_use_fts_without_embedding(
        self, tmp_path, fake_embedding_provider,
    ):
        provider = CountingEmbeddingProvider(fake_embedding_provider)
        memory = MemoryStore(tmp_path / "trivial.db", embedding_provider=provider)
        try:
            memory.store("hello saved memory")
            memory.store("thanks saved memory")
            if not memory.vector_enabled:
                pytest.skip("sqlite-vec is unavailable in this environment")

            stored_embedding_calls = len(provider.calls)
            greeting = memory.search("hello!")
            assert any("hello" in item["fact_text"] for item in greeting)
            assert len(provider.calls) == stored_embedding_calls
            assert memory.last_search_diagnostics["vector"] == "skipped-trivial"

            confirmation = memory.search("thanks")
            assert any("thanks" in item["fact_text"] for item in confirmation)
            assert len(provider.calls) == stored_embedding_calls

            # A caller can explicitly retain semantic behavior for an otherwise
            # trivial string when it has application-specific meaning.
            memory.search("hello!", semantic=True)
            assert len(provider.calls) == stored_embedding_calls + 1
        finally:
            memory.close()

    def test_query_embedding_cache_is_bounded_lru(self, tmp_path, fake_embedding_provider):
        provider = CountingEmbeddingProvider(fake_embedding_provider)
        memory = MemoryStore(
            tmp_path / "cache.db",
            embedding_provider=provider,
            query_embedding_cache_size=2,
        )
        try:
            memory.store("memory cache seed")
            if not memory.vector_enabled:
                pytest.skip("sqlite-vec is unavailable in this environment")

            stored_embedding_calls = len(provider.calls)
            for query in ("first lookup", "second lookup", "first lookup", "third lookup", "second lookup"):
                memory.search(query)

            # first and second populate the cache; first is reused; third
            # evicts second; second must be embedded again.
            assert provider.calls[stored_embedding_calls:] == [
                "first lookup", "second lookup", "third lookup", "second lookup",
            ]
        finally:
            memory.close()

    def test_search_defers_and_batches_access_stat_writes(self, store):
        fact_id = store.store("User prefers batched memory counters")

        assert store.get(fact_id)["access_count"] == 0
        assert store.search("batched memory counters")
        assert store.get(fact_id)["access_count"] == 0

        # Repeated retrieval of the same fact becomes one batched row update
        # with the correct cumulative access count.
        assert store.search("batched memory counters")
        assert store.flush_access_stats() == 1
        refreshed = store.get(fact_id)
        assert refreshed["access_count"] == 2
        assert refreshed["last_accessed"] is not None
        assert store.flush_access_stats() == 0

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
