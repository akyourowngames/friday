from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ares.turn_policy import (
    ActionGrant,
    ActionGrantUseRegistry,
    TurnExecutionContext,
    TurnIntent,
    arguments_hash,
    authorize_turn_tool,
    build_turn_execution_context,
    classify_tool_effect,
    classify_turn_intent,
    issue_action_grant,
)


@pytest.mark.parametrize("text", ["hey", "hello!", "thanks", "yo", "okay"])
def test_casual_turns_are_conversation_only(text: str) -> None:
    assert classify_turn_intent(text) is TurnIntent.CONVERSATION


def test_turn_intent_distinguishes_memory_meta_delegation_and_browser_actions() -> None:
    assert classify_turn_intent("Remember blue as my favorite color") is TurnIntent.LOCAL_MUTATION
    assert classify_turn_intent("How many agents did you use for the parallel search?") is TurnIntent.READ_ONLY
    assert classify_turn_intent("Use separate researchers to compare these") is TurnIntent.DELEGATION
    assert classify_turn_intent("Search this website by clicking the search box") is TurnIntent.BROWSER_INTERACTION
    assert classify_turn_intent("Open Notepad and type a note") is TurnIntent.BROWSER_INTERACTION
    assert classify_turn_intent("Explain how web search works") is TurnIntent.READ_ONLY
    assert classify_turn_intent(
        "ok launch researchers to research how much corruption is there in world"
    ) is TurnIntent.DELEGATION
    assert classify_turn_intent(
        "Research on corruption in the world with multiple agents"
    ) is TurnIntent.DELEGATION


def test_conversation_turn_hard_denies_stale_action_calls() -> None:
    context = build_turn_execution_context("hey", request_id="req-greeting", session_id="session-new")

    for tool, arguments in (
        ("run_command", {"command": "start notepad"}),
        ("write_file", {"path": "old.txt", "content": "stale"}),
        ("mcp__windows__Type", {"text": "old workout"}),
        ("mcp__playwright__browser_navigate", {"url": "https://example.test"}),
        ("send_email", {"to": "x@example.test", "subject": "old"}),
        ("delegate_task", {"agent": "researcher", "task": "old task"}),
    ):
        decision = authorize_turn_tool(context, tool, arguments)
        assert not decision.allowed, (tool, decision)
        assert "conversation" in decision.reason


def test_unknown_local_tool_is_consequential_and_denied_until_registered() -> None:
    context = build_turn_execution_context("Read the README", request_id="req-unknown")
    assert classify_tool_effect("future_plugin_tool").value == "external_action"
    assert not authorize_turn_tool(context, "future_plugin_tool", {}).allowed


def test_read_only_and_agent_meta_turns_have_narrow_authority() -> None:
    read_context = build_turn_execution_context("Read README.md and explain it", request_id="req-read")
    assert authorize_turn_tool(read_context, "read_file", {"path": "README.md"}).allowed
    assert not authorize_turn_tool(read_context, "write_file", {"path": "README.md", "content": "x"}).allowed
    assert not authorize_turn_tool(read_context, "mcp__playwright__browser_click", {"ref": "e1"}).allowed

    meta = build_turn_execution_context(
        "How many agents did you use for the parallel search?", request_id="req-meta"
    )
    assert meta.explicit_targets == ("agents",)
    assert authorize_turn_tool(meta, "list_agent_runs", {}).allowed
    assert not authorize_turn_tool(meta, "web_search", {"query": "agents"}).allowed


def test_memory_mutation_and_delegation_do_not_cross_workflow_boundary() -> None:
    memory = build_turn_execution_context("Remember blue", request_id="req-memory")
    assert authorize_turn_tool(memory, "store_memory", {"content": "blue"}).allowed

    delegation = build_turn_execution_context(
        "Use multi-agent to research this", request_id="req-delegation"
    )
    assert authorize_turn_tool(
        delegation,
        "delegate_task",
        {"agent": "researcher", "task": "research this"},
    ).allowed
    workflow = authorize_turn_tool(
        delegation,
        "create_task",
        {"goal": "pretend agents ran", "plan": []},
    )
    assert not workflow.allowed
    assert "workflow_mutation" in workflow.reason


def test_explicit_agent_run_resume_is_a_current_turn_delegation_action() -> None:
    context = build_turn_execution_context(
        "Resume agent run ma_abcdef1234567890", request_id="req-resume", session_id="session-1"
    )
    assert context.intent is TurnIntent.DELEGATION
    assert authorize_turn_tool(
        context, "resume_agent_run", {"run_id": "ma_abcdef1234567890"}
    ).allowed


def test_ambiguous_continue_can_recall_but_cannot_replay_an_action() -> None:
    context = build_turn_execution_context("continue", request_id="req-continue")

    assert context.intent is TurnIntent.READ_ONLY
    assert "prior_task" in context.explicit_targets
    assert authorize_turn_tool(context, "search_actions", {"query": "recent task"}).allowed
    denied = authorize_turn_tool(context, "run_command", {"command": "old command"})
    assert not denied.allowed
    assert "resolve a specific prior task" in denied.reason


def test_action_grant_hash_is_canonical_and_consumed_exactly_once() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    arguments = {"to": "person@example.test", "body": "hello", "metadata": {"b": 2, "a": 1}}
    assert arguments_hash(arguments) == arguments_hash({
        "metadata": {"a": 1, "b": 2}, "body": "hello", "to": "person@example.test",
    })
    grant = issue_action_grant(
        request_id="req-confirm",
        session_id="session-1",
        tool_name="send_email",
        arguments=arguments,
        ttl_seconds=60,
        root_run_id="ma-1",
        child_run_id="agent-1",
        now=now,
    )
    context = TurnExecutionContext(
        request_id="req-confirm",
        session_id="session-1",
        user_input="confirm",
        intent=TurnIntent.CONFIRMATION_RESPONSE,
        confirmation_grants=(grant,),
        root_run_id="ma-1",
        child_run_id="agent-1",
    )
    uses = ActionGrantUseRegistry()

    first = authorize_turn_tool(
        context,
        "send_email",
        {"metadata": {"a": 1, "b": 2}, "body": "hello", "to": "person@example.test"},
        grant_uses=uses,
        now=now,
    )
    second = authorize_turn_tool(
        context, "send_email", arguments, grant_uses=uses, now=now
    )
    assert first.allowed and first.grant_consumed and first.grant_id == grant.grant_id
    assert not second.allowed
    assert "already been used" in second.reason


def test_action_grant_rejects_argument_scope_and_expiry_mismatches() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    arguments = {"number": "+10000000000", "message": "hello"}
    grant = issue_action_grant(
        request_id="req-sms", session_id="session-1", tool_name="phone_send_sms",
        arguments=arguments, root_run_id="ma-1", child_run_id="agent-1", now=now,
    )
    uses = ActionGrantUseRegistry()

    wrong_arguments = TurnExecutionContext(
        "req-sms", "session-1", "yes", TurnIntent.CONFIRMATION_RESPONSE,
        confirmation_grants=(grant,), root_run_id="ma-1", child_run_id="agent-1",
    )
    changed = authorize_turn_tool(
        wrong_arguments,
        "phone_send_sms",
        {"number": "+10000000000", "message": "changed"},
        grant_uses=uses,
        now=now,
    )
    assert not changed.allowed and "arguments" in changed.reason

    wrong_scope = TurnExecutionContext(
        "req-sms", "another-session", "yes", TurnIntent.CONFIRMATION_RESPONSE,
        confirmation_grants=(grant,), root_run_id="ma-1", child_run_id="agent-1",
    )
    scoped = authorize_turn_tool(
        wrong_scope, "phone_send_sms", arguments, grant_uses=uses, now=now
    )
    assert not scoped.allowed and "session" in scoped.reason

    expired = ActionGrant(
        grant_id="grant-expired",
        request_id="req-expired",
        session_id="session-1",
        tool_name="phone_send_sms",
        arguments_hash=arguments_hash(arguments),
        expires_at=now - timedelta(seconds=1),
    )
    expired_context = TurnExecutionContext(
        "req-expired", "session-1", "yes", TurnIntent.CONFIRMATION_RESPONSE,
        confirmation_grants=(expired,),
    )
    denied = authorize_turn_tool(
        expired_context, "phone_send_sms", arguments, grant_uses=uses, now=now
    )
    assert not denied.allowed and "expired" in denied.reason
