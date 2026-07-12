"""Tests for SessionStore (per-session JSONL conversations)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares.sessions import SessionStore


@pytest.fixture
def session_store(tmp_path):
    return SessionStore(data_dir=tmp_path)


def test_write_and_read_message(session_store):
    session_store.write_message("sess-abc", "user", "Hello!")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 1
    assert entries[0]["type"] == "message"
    assert entries[0]["role"] == "user"
    assert entries[0]["content"] == "Hello!"
    assert entries[0]["session_id"] == "sess-abc"


def test_write_multiple_messages(session_store):
    session_store.write_message("sess-abc", "user", "Hi")
    session_store.write_message("sess-abc", "assistant", "Hello!")
    session_store.write_message("sess-abc", "user", "How are you?")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 3
    assert entries[0]["role"] == "user"
    assert entries[1]["role"] == "assistant"
    assert entries[2]["role"] == "user"


def test_write_tool_calls(session_store):
    tool_calls = [{"name": "web_search", "arguments": {"query": "test"}}]
    session_store.write_message("sess-abc", "assistant", "Let me search...", tool_calls=tool_calls)
    entries = session_store.read_session("sess-abc")
    assert entries[0]["tool_calls"] == tool_calls


def test_write_summary(session_store):
    session_store.write_message("sess-abc", "user", "Hello")
    session_store.write_summary("sess-abc", "User said hello")
    entries = session_store.read_session("sess-abc")
    assert len(entries) == 2
    assert entries[1]["type"] == "summary"
    assert entries[1]["content"] == "User said hello"


def test_get_previous_summary(session_store):
    session_store.write_summary("sess-first", "First session summary")
    session_store.write_message("sess-second", "user", "Hi")
    summary = session_store.get_previous_summary("sess-second")
    assert summary == "First session summary"


def test_get_previous_summary_none_when_no_sessions(session_store):
    summary = session_store.get_previous_summary("sess-first")
    assert summary is None


def test_list_sessions(session_store):
    session_store.write_message("sess-aaa", "user", "Hello")
    session_store.write_message("sess-bbb", "user", "World")
    sessions = session_store.list_sessions()
    assert len(sessions) == 2
    ids = [s["session_id"] for s in sessions]
    assert "sess-aaa" in ids
    assert "sess-bbb" in ids


def test_read_nonexistent_session(session_store):
    entries = session_store.read_session("sess-nonexistent")
    assert entries == []


def test_jsonl_file_is_valid(session_store):
    """Each line in the JSONL file should be valid JSON."""
    session_store.write_message("sess-test", "user", "Hello")
    session_store.write_message("sess-test", "assistant", "Hi there!")
    jsonl_path = session_store.sessions_dir / "sess-test.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text().strip().split("\n")
    for line in lines:
        obj = json.loads(line)
        assert "type" in obj
        assert "timestamp" in obj


def test_search_recall_reads_all_sessions_with_stable_provenance_and_neighbor_context(session_store):
    """A detail split across turns must be retrievable from historical JSONL."""
    session_store.write_message("sess-rohit", "user", "Rohit Verma is my cousin.")
    session_store.write_message("sess-rohit", "assistant", "His Instagram ID is @rohit_dev_42.")

    results = session_store.search_recall("Rohit Instagram", limit=5)

    assert results
    assert all(item["source"] == "session" for item in results)
    assert all(item["source_id"].startswith("session:sess-rohit:line:") for item in results)
    assert any(
        "@rohit_dev_42" in item["content"]
        or "@rohit_dev_42" in " ".join(item["context_after"] + item["context_before"])
        for item in results
    )
    assert any(set(item["matched_terms"]) == {"rohit", "instagram"} for item in results)


def test_search_recall_skips_malformed_historical_lines_without_losing_line_id(session_store):
    path = session_store.sessions_dir / "sess-legacy.jsonl"
    path.write_text(
        "{not valid json}\n"
        '{"type":"message","role":"user","content":"Nimbus retrieval marker","timestamp":"2026-07-10T10:00:00+00:00","session_id":"sess-legacy"}\n',
        encoding="utf-8",
    )

    results = session_store.search_recall("Nimbus", limit=3)

    assert len(results) == 1
    assert results[0]["source_id"] == "session:sess-legacy:line:2"
    assert results[0]["content"] == "Nimbus retrieval marker"
