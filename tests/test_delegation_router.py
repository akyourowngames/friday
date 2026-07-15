from __future__ import annotations

import pytest

from ares.delegation_router import (
    DelegationAvailability,
    DelegationFailureReason,
    DelegationMode,
    DelegationRouter,
    runtime_failure_decision,
)
from ares.tool_registry import RootToolRegistry, ToolCategory, categorize_tool_name
from ares.tools.definitions import get_tool_definitions
from ares.turn_policy import build_turn_execution_context


ROLES = ("planner", "researcher", "analyst", "builder", "reviewer", "synthesizer")


def availability(**overrides):
    values = {
        "enabled": True,
        "runtime_available": True,
        "available_roles": ROLES,
        "max_tasks_per_run": 8,
    }
    values.update(overrides)
    return DelegationAvailability(**values)


def test_explicit_separate_researchers_builds_bounded_dependency_plan() -> None:
    context = build_turn_execution_context(
        "Research FastAPI, Flask and Django in parallel using separate researchers, then synthesize a recommendation.",
        request_id="req-research",
    )
    decision = DelegationRouter().route(context, availability())

    assert decision.mode is DelegationMode.EXPLICIT
    assert decision.should_delegate
    assert decision.requested_parallelism
    assert decision.requested_roles == ("researcher",)
    researchers = [task for task in decision.plan if task.agent == "researcher"]
    synthesis = next(task for task in decision.plan if task.agent == "synthesizer")
    assert len(researchers) == 3
    assert set(synthesis.depends_on) == {task.task_id for task in researchers}
    assert len(decision.plan) <= 8


def test_plural_researcher_launch_with_real_topic_routes_before_meta_policy() -> None:
    context = build_turn_execution_context(
        "ok launch researchers to research how much corruption is there in world",
        request_id="req-corruption",
    )
    decision = DelegationRouter().route(context, availability())

    assert decision.mode is DelegationMode.EXPLICIT
    assert decision.should_delegate
    assert context.intent.value == "delegation"
    assert len(decision.plan) == 2
    assert {task.agent for task in decision.plan} == {"researcher"}
    assert all("corruption" in task.prompt.casefold() for task in decision.plan)


def test_with_multiple_agents_defaults_to_two_real_specialists() -> None:
    context = build_turn_execution_context(
        "Research on corruption in the world with multiple agents",
        request_id="req-telegram-corruption",
    )
    decision = DelegationRouter().route(context, availability())

    assert decision.mode is DelegationMode.EXPLICIT
    assert decision.should_delegate
    assert context.intent.value == "delegation"
    assert len(decision.plan) == 2
    assert all(task.agent == "researcher" for task in decision.plan)
    assert all("corruption" in task.prompt.casefold() for task in decision.plan)


@pytest.mark.parametrize(
    "message",
    (
        "launch agents multi agent no fluff",
        "oh okiee can you laucnh agents multi agent no fluff",
        "use multi-agent mode",
    ),
)
def test_vague_agent_launch_asks_for_assignment_without_starting_run(message: str) -> None:
    decision = DelegationRouter().route(
        build_turn_execution_context(message, request_id="req-vague"), availability()
    )

    assert decision.mode is DelegationMode.EXPLICIT
    assert not decision.should_delegate
    assert not decision.plan
    assert decision.failure_reason is DelegationFailureReason.MISSING_TASK
    assert "what you want" in decision.honest_failure_message.casefold()


def test_agent_meta_question_routes_to_manifest_introspection_not_new_agents() -> None:
    context = build_turn_execution_context(
        "How many agents did you use for the parallel search, and how did you launch them?",
        request_id="req-meta",
    )
    decision = DelegationRouter().route(context, availability())

    assert decision.mode is DelegationMode.META
    assert not decision.should_delegate
    assert not decision.plan
    assert "run records" in decision.reason


def test_explicit_agent_run_resume_routes_to_session_scoped_management() -> None:
    decision = DelegationRouter().route(
        build_turn_execution_context(
            "Resume agent run ma_abcdef1234567890", request_id="req-resume"
        ),
        availability(),
    )
    assert decision.mode is DelegationMode.META
    assert not decision.should_delegate


@pytest.mark.parametrize(
    "message",
    (
        "Did the researcher search the web?",
        "How many agents did you use for the parallel search?",
        "What tools did the agents use?",
        "How did you launch them?",
    ),
)
def test_exact_agent_meta_questions_never_trigger_a_new_run(message: str) -> None:
    context = build_turn_execution_context(message, request_id="req-exact-meta")
    decision = DelegationRouter().route(context, availability())

    assert decision.mode is DelegationMode.META
    assert not decision.should_delegate
    assert not decision.plan


def test_automatic_delegation_requires_meaningful_independent_workstreams() -> None:
    router = DelegationRouter()
    automatic = router.route(
        build_turn_execution_context(
            "Analyze the backend and frontend separately and then compare the findings.",
            request_id="req-auto",
        ),
        availability(),
    )
    simple = router.route(
        build_turn_execution_context("Explain this function", request_id="req-simple"),
        availability(),
    )

    assert automatic.mode is DelegationMode.AUTO
    assert automatic.should_delegate
    assert len(automatic.plan) == 2
    assert simple.mode is DelegationMode.NONE
    assert not simple.should_delegate


def test_explicit_delegation_fails_honestly_without_fallback() -> None:
    context = build_turn_execution_context(
        "Use separate researchers to compare these options", request_id="req-explicit"
    )
    router = DelegationRouter()

    disabled = router.route(context, availability(enabled=False))
    unavailable = router.route(context, availability(runtime_available=False))
    no_role = router.route(context, availability(available_roles=("analyst",)))

    assert disabled.failure_reason is DelegationFailureReason.DISABLED
    assert unavailable.failure_reason is DelegationFailureReason.RUNTIME_UNAVAILABLE
    assert no_role.failure_reason is DelegationFailureReason.NO_MATCHING_ROLE
    for decision in (disabled, unavailable, no_role):
        assert not decision.should_delegate
        assert not decision.plan
        assert decision.honest_failure_message
        assert "no agents ran" in decision.reason.casefold()


def test_requested_agent_count_over_limit_is_not_silently_trimmed() -> None:
    decision = DelegationRouter().route(
        build_turn_execution_context(
            "Use four agents to compare the framework options", request_id="req-limit"
        ),
        availability(max_tasks_per_run=3),
    )

    assert not decision.should_delegate
    assert decision.failure_reason is DelegationFailureReason.TASK_LIMIT_EXCEEDED
    assert not decision.plan


def test_runtime_errors_never_become_narrative_success() -> None:
    timeout = runtime_failure_decision(TimeoutError("provider timeout"))
    unauthorized = runtime_failure_decision(PermissionError("grant mismatch"))
    provider = runtime_failure_decision(RuntimeError("provider unavailable"))

    assert timeout.failure_reason is DelegationFailureReason.TIMEOUT
    assert unauthorized.failure_reason is DelegationFailureReason.AUTHORIZATION_FAILURE
    assert provider.failure_reason is DelegationFailureReason.PROVIDER_FAILURE
    assert all(not item.should_delegate and not item.plan for item in (timeout, unauthorized, provider))


def test_root_tool_registry_hides_workflows_during_explicit_delegation() -> None:
    schemas = get_tool_definitions()
    registry = RootToolRegistry(schemas)
    context = build_turn_execution_context(
        "Use multi-agent to research these frameworks", request_id="req-tools"
    )
    decision = DelegationRouter().route(context, availability())
    names = {
        schema["function"]["name"]
        for schema in registry.select_for_turn(context, delegation_decision=decision)
    }

    assert {
        "list_agents", "delegate_task", "delegate_tasks_parallel", "get_agent_run",
        "list_agent_runs", "get_latest_agent_run", "cancel_agent_run", "resume_agent_run",
    }.issubset(names)
    assert "create_task" not in names
    assert "run_task" not in names
    assert "web_search" not in names


def test_root_tool_registry_meta_selection_and_categories_are_deterministic() -> None:
    registry = RootToolRegistry(get_tool_definitions())
    context = build_turn_execution_context(
        "How many agents did you use for the parallel search?", request_id="req-meta-tools"
    )
    decision = DelegationRouter().route(context, availability())
    names = {
        schema["function"]["name"]
        for schema in registry.select_for_turn(context, delegation_decision=decision)
    }

    assert names == {"list_agents", "get_agent_run", "list_agent_runs", "get_latest_agent_run"}
    assert categorize_tool_name("create_task") is ToolCategory.WORKFLOWS
    assert categorize_tool_name("delegate_task") is ToolCategory.DELEGATION
    assert categorize_tool_name("mcp__playwright__browser_click") is ToolCategory.BROWSER
    assert categorize_tool_name("web_search") is ToolCategory.RESEARCH


def test_root_tool_registry_read_only_turn_hides_mutation_schemas() -> None:
    registry = RootToolRegistry(get_tool_definitions())
    context = build_turn_execution_context(
        "Read README.md and research the current behavior", request_id="req-read-tools"
    )
    names = {schema["function"]["name"] for schema in registry.select_for_turn(context)}

    assert {"read_file", "search_files", "web_search", "search_memory"}.issubset(names)
    assert not {"write_file", "run_command", "store_memory", "create_task", "run_task"} & names
