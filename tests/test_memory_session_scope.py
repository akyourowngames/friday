"""Tests for session-scoped memory search."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ares.memory import MemoryStore


@pytest.fixture
def scoped_store(tmp_path):
    """Create a MemoryStore with a temporary database."""
    db_path = tmp_path / "test_memory.db"
    return MemoryStore(db_path=db_path)


def test_store_with_session_id(scoped_store):
    """Facts stored with a session_id should have it recorded."""
    fid = scoped_store.store("I like cats", session_id="sess-abc123")
    fact = scoped_store.get(fid)
    assert fact is not None
    assert fact["session_id"] == "sess-abc123"


def test_store_without_session_id_is_global(scoped_store):
    """Facts stored without session_id should be NULL (global)."""
    fid = scoped_store.store("Global fact")
    fact = scoped_store.get(fid)
    assert fact is not None
    assert fact["session_id"] is None


def test_search_scope_session_returns_current(scoped_store):
    """Session-scoped search should find facts from the current session."""
    scoped_store.store("Session fact", session_id="sess-current")
    scoped_store.store("Old fact", session_id="sess-old")

    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Session fact" in texts


def test_search_scope_session_includes_recent(scoped_store):
    """Session-scoped search should include recent sessions."""
    scoped_store.store("Current fact", session_id="sess-current")
    scoped_store.store("Recent fact", session_id="sess-recent")

    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Current fact" in texts
    assert "Recent fact" in texts


def test_search_scope_all_returns_everything(scoped_store):
    """Global search should return facts from all sessions."""
    scoped_store.store("Session fact", session_id="sess-current")
    scoped_store.store("Old fact", session_id="sess-old")

    results = scoped_store.search("fact", scope="all")
    texts = [r["fact_text"] for r in results]
    assert "Session fact" in texts
    assert "Old fact" in texts


def test_global_facts_always_searchable(scoped_store):
    """Facts without session_id should appear in both session and global search."""
    scoped_store.store("Global fact")
    scoped_store.store("Session fact", session_id="sess-current")

    results = scoped_store.search(
        "fact", scope="session", session_id="sess-current", recent_sessions=3
    )
    texts = [r["fact_text"] for r in results]
    assert "Global fact" in texts
    assert "Session fact" in texts


def test_session_id_migration(scoped_store):
    """Facts stored without session_id should have NULL session_id and be globally searchable."""
    # Store via the normal API without session_id (simulates pre-migration behavior)
    fid = scoped_store.store("Legacy fact")
    fact = scoped_store.get(fid)
    assert fact is not None
    assert fact["session_id"] is None

    # Should be findable in global search
    results = scoped_store.search("Legacy", scope="all")
    assert len(results) >= 1
    assert results[0]["fact_text"] == "Legacy fact"

    # Should also be findable in session-scoped search (global facts always included)
    results = scoped_store.search(
        "Legacy", scope="session", session_id="sess-other", recent_sessions=3
    )
    assert len(results) >= 1
    assert results[0]["fact_text"] == "Legacy fact"
