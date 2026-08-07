"""Tests for the auto-maintained user-model manager.

These exercise the pure merge/get_context logic without touching the broken
numpy-loaded import chain (agent.py etc.), so they run in this environment.
"""

import tempfile
from pathlib import Path

from ares.user_model import UserModelManager, UserModelReflector, _looks_secret


def _manager() -> UserModelManager:
    tmp = Path(tempfile.mkdtemp())
    mgr = UserModelManager(data_dir=tmp)
    mgr.ensure_exists()
    return mgr


def test_ensure_exists_creates_template():
    mgr = _manager()
    text = mgr.read()
    assert "## Facts" in text
    assert "auto-maintained" in text


def test_merge_facts_appends_unique():
    mgr = _manager()
    added = mgr.merge_facts([
        "Works as a software engineer in Lisbon, Portugal.",
        "Prefers concise terminal replies.",
    ])
    assert len(added) == 2
    text = mgr.read()
    assert "Works as a software engineer in Lisbon, Portugal." in text
    assert "Prefers concise terminal replies." in text


def test_merge_facts_dedupes_similar_and_ignores_blanks():
    mgr = _manager()
    mgr.merge_facts(["Prefers concise terminal replies."])
    # Same fact with different casing/whitespace must not be re-added.
    added = mgr.merge_facts([
        "prefers  concise   terminal   replies",
        "   ",
        "",
    ])
    assert added == []
    # A genuinely new fact is still appended.
    mgr.merge_facts(["Uses Windows 11."])
    assert "Uses Windows 11." in mgr.read()


def test_merge_facts_refuses_secrets():
    mgr = _manager()
    added = mgr.merge_facts([
        "API key is sk-1234567890abcdef1234567890abcdef",
        "Password: hunter2hunter2hunter2xy",
        "Likes dark mode.",
    ])
    assert "Likes dark mode." in mgr.read()
    text = mgr.read()
    assert "sk-1234567890abcdef1234567890abcdef" not in text
    assert "hunter2hunter2hunter2xy" not in text


def test_merge_facts_preserves_header_prose():
    mgr = _manager()
    mgr.write("# About You (auto-maintained)\n\nMy private notes about myself.\n\n## Facts\n- Pre-existing fact.\n")
    mgr.merge_facts(["New auto fact about the user."])
    text = mgr.read()
    assert "My private notes about myself." in text
    assert "Pre-existing fact." in text
    assert "New auto fact about the user." in text


def test_merge_facts_enforces_cap():
    mgr = _manager()
    mgr._max_facts = 3  # shrink cap for a fast test
    for i in range(5):
        mgr.merge_facts([f" Fact number {i}. "])
    text = mgr.read()
    # Only the 3 most recent facts survive; oldest are trimmed.
    assert "Fact number 4." in text
    assert "Fact number 3." in text
    assert "Fact number 2." in text
    assert "Fact number 0." not in text
    assert "Fact number 1." not in text
    assert text.count("\n- ") == 3


def test_get_context_within_budget():
    mgr = _manager()
    mgr.merge_facts([
        "A fairly long stable fact about the user that should be present.",
        "Another stable fact to give the context block some volume.",
    ])
    full = mgr.get_context(token_budget=400)
    assert "User Model" in full
    tiny = mgr.get_context(token_budget=4)
    assert tiny.startswith("## User Model")
    assert len(tiny) < len(full)


def test_reflector_parse_facts():
    # Plain JSON.
    assert UserModelReflector._parse_facts('{"facts": ["a", "b"]}') == ["a", "b"]
    # Fenced JSON.
    assert UserModelReflector._parse_facts("```json\n{\"facts\": [\"x\"]}\n```") == ["x"]
    # Bare object inside prose.
    assert UserModelReflector._parse_facts("sure {\"facts\": [\"y\"]} done") == ["y"]
    # Missing/empty -> no facts.
    assert UserModelReflector._parse_facts("not json") == []
    assert UserModelReflector._parse_facts('{"other": 1}') == []


def test_looks_secret_helper():
    assert _looks_secret("my api_key is abcdef1234567890")
    assert _looks_secret("token: abcdef1234567890abcdef12")
    assert not _looks_secret("Prefers dark mode in the editor")
