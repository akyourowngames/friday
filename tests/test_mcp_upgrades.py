"""Focused tests for opt-in MCP boundary upgrade helpers."""

from __future__ import annotations

import pytest

from ares.tools.mcp_upgrades import (
    MCPResponseCache,
    MCPUpgradeError,
    make_mcp_cache_key,
    merge_paginated_responses,
    normalize_pagination_policy,
    normalize_timeout_policy,
    pagination_page_arguments,
    parse_mcp_tool_name,
    prepare_mcp_call,
    project_mcp_error,
    project_mcp_health,
    project_mcp_readiness,
    project_pagination_page,
    split_mcp_arguments,
)


def test_mcp_tool_name_and_reserved_metadata_are_strictly_sanitized():
    parsed = parse_mcp_tool_name("mcp__calendar__list-events")
    assert parsed.server_name == "calendar"
    assert parsed.tool_name == "list-events"
    assert parsed.canonical_name == "mcp__calendar__list-events"

    prepared = prepare_mcp_call(
        "mcp__calendar__list-events",
        {
            "calendar_id": "primary",
            "__ares": {"timeout": 999, "pagination": {"max_pages": 2}},
            "_ares_cache_ttl": 15,
            "__ares_trace_id": "trace-123",
        },
        max_timeout_seconds=120,
    )

    assert prepared.arguments == {"calendar_id": "primary"}
    assert prepared.metadata == {
        "timeout_seconds": 999,
        "pagination": {"max_pages": 2},
        "cache_ttl_seconds": 15,
        "trace_id": "trace-123",
    }
    assert prepared.timeout.timeout_seconds == 120
    assert prepared.timeout.capped is True
    assert prepared.cache_ttl_seconds == 15
    assert prepared.pagination.enabled is True
    assert prepared.pagination.max_pages == 2

    with pytest.raises(MCPUpgradeError, match="Conflicting"):
        split_mcp_arguments(
            {"__ares_timeout": 5, "_ares_timeout_seconds": 10}
        )
    with pytest.raises(MCPUpgradeError, match="reserved"):
        split_mcp_arguments({"__aresunsafe": "must never reach an MCP"})
    with pytest.raises(MCPUpgradeError):
        parse_mcp_tool_name("mcp__bad server__tool")
    with pytest.raises(MCPUpgradeError):
        parse_mcp_tool_name("calendar__list-events")


def test_timeout_policy_defaults_caps_and_rejects_unsafe_values():
    default = normalize_timeout_policy(None, default_seconds=30, max_seconds=60)
    assert default.timeout_seconds == 30
    assert default.used_default is True

    capped = normalize_timeout_policy("90", default_seconds=30, max_seconds=60)
    assert capped.timeout_seconds == 60
    assert capped.capped is True

    with pytest.raises(MCPUpgradeError):
        normalize_timeout_policy(0)
    with pytest.raises(MCPUpgradeError):
        normalize_timeout_policy(True)


def test_pagination_projects_cursor_pages_and_merges_them_without_mutating_responses():
    policy = normalize_pagination_policy(
        {
            "mode": "cursor",
            "max_pages": 3,
            "cursor_param": "page_token",
            "initial_cursor": "first",
            "items_path": "payload.items",
            "next_cursor_path": "payload.next",
            "has_more_path": "payload.more",
            "merge": "pages",
        }
    )
    assert pagination_page_arguments(policy, 1) == {"page_token": "first"}

    first_response = {
        "payload": {"items": [{"id": 1}, {"id": 2}], "next": "second", "more": True}
    }
    first = project_pagination_page(first_response, policy, 1, cursor="first")
    assert first["next_arguments"] == {"page_token": "second"}
    assert first["has_more"] is True

    second = project_pagination_page(
        {"payload": {"items": [{"id": 3}], "next": None, "more": False}},
        policy,
        2,
        cursor="second",
    )
    merged = merge_paginated_responses([first, second], policy)
    assert [item["id"] for item in merged["items"]] == [1, 2, 3]
    assert merged["page_count"] == 2
    assert merged["has_more"] is False
    assert len(merged["pages"]) == 2
    assert first_response["payload"]["items"] == [{"id": 1}, {"id": 2}]


def test_mcp_response_cache_keys_are_stable_private_and_ttl_bound():
    left = make_mcp_cache_key(
        "mcp__search__find",
        {"query": "ares", "filters": {"kind": "docs"}, "__ares_timeout": 5},
    )
    right = make_mcp_cache_key(
        "mcp__search__find",
        {"filters": {"kind": "docs"}, "query": "ares", "_ares_timeout": 60},
    )
    assert left == right
    assert "ares" not in left
    assert "timeout" not in left

    cache = MCPResponseCache(clock=lambda: 100.0)
    response = {"items": ["one"]}
    assert cache.put(left, response, ttl_seconds=10) is True
    response["items"].append("mutated")
    hit = cache.lookup(left, now=105)
    assert hit.hit is True
    assert hit.value == {"items": ["one"]}
    hit.value["items"].append("also-mutated")
    assert cache.get(left, now=105) == {"items": ["one"]}
    assert cache.lookup(left, now=110).hit is False


def test_error_and_health_projections_are_redacted_and_actionable():
    error = (
        "HTTP 401 Authorization: Bearer secret-bearer "
        "token=secret-token https://example.test/mcp?api_key=secret-query"
    )
    projected = project_mcp_error(error, server_name="calendar", tool_name="list-events")
    assert projected["category"] == "authentication"
    assert projected["code"] == "mcp_authentication"
    diagnostic = projected["diagnostic"]
    assert "secret-bearer" not in diagnostic
    assert "secret-token" not in diagnostic
    assert "secret-query" not in diagnostic
    assert "[redacted]" in diagnostic

    ready = project_mcp_readiness(
        "calendar", configured=True, connected=True, tool_count=4, transport="http"
    )
    assert ready["status"] == "ready"
    assert ready["ready"] is True

    class Manager:
        servers = {"calendar": {"transport": "http"}, "broken": {"transport": "stdio"}}
        sessions = {"calendar": object()}
        schema_cache = {"calendar": [{}, {}], "broken": []}
        server_errors = {"broken": error}

    health = project_mcp_health(Manager())
    assert health["status"] == "degraded"
    assert health["metrics"] == {
        "configured": 2,
        "ready": 1,
        "degraded": 0,
        "offline": 1,
        "tools": 2,
    }
    broken = next(item for item in health["servers"] if item["name"] == "broken")
    assert broken["error"]["category"] == "authentication"
    assert "secret-query" not in broken["error"]["diagnostic"]
