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
