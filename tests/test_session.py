"""Tests for SessionManager."""
from ares.session import SessionManager


def test_session_id_format():
    sm = SessionManager()
    assert sm.session_id.startswith("sess-")
    assert len(sm.session_id) == 17  # "sess-" + 12 hex chars


def test_session_id_is_unique():
    sm1 = SessionManager()
    sm2 = SessionManager()
    assert sm1.session_id != sm2.session_id


def test_started_at_is_iso():
    sm = SessionManager()
    assert "T" in sm.started_at  # ISO format contains T


def test_get_id_returns_session_id():
    sm = SessionManager()
    assert sm.get_id() == sm.session_id


def test_build_context_includes_previous_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary="User discussed Python testing",
        token_budget=2000,
    )
    assert "Previous Session" in result
    assert "Python testing" in result


def test_build_context_omits_empty_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary="",
        token_budget=2000,
    )
    assert "Previous Session" not in result


def test_build_context_omits_none_summary():
    from ares.context_blend import build_context_prompt
    result = build_context_prompt(
        previous_session_summary=None,
        token_budget=2000,
    )
    assert "Previous Session" not in result
