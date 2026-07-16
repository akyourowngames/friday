"""Focused coverage for MCP timeout propagation through the agent boundary."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ares.agent import Agent, _trusted_local_execution_requested


@pytest.mark.asyncio
async def test_agent_outer_deadline_honors_finite_mcp_operation_timeout() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    agent = object.__new__(Agent)
    agent.config = SimpleNamespace(
        multi_agent=SimpleNamespace(
            tool_operation_timeout_seconds=0.01,
            tool_cancel_grace_seconds=0.01,
        )
    )
    agent.resource_coordinator = None
    agent.mcp_manager = SimpleNamespace(
        operation_timeout_for=lambda name, arguments: calls.append((name, arguments)) or 0.05
    )
    agent._authorize_tool = lambda _name, _args: None

    async def dispatch(_name, _args, _progress):
        await asyncio.sleep(0.025)
        return True, "completed"

    agent._dispatch_one_tool_async = dispatch
    result = await agent._execute_one_tool_async(
        4,
        {
            "function": {
                "name": "mcp__calendar__list_events",
                "arguments": json.dumps({"limit": 1}),
            }
        },
        None,
    )

    assert result == (4, True, "completed")
    assert calls == [("mcp__calendar__list_events", {"limit": 1})]


@pytest.mark.parametrize(
    ("user_input", "expected"),
    (
        ("Enable trusted local execution for all child tools.", True),
        ("Remove restrictions from Ares tools and MCPs.", True),
        ("Please inspect the available MCP tools.", False),
    ),
)
def test_trusted_local_profile_requires_an_explicit_owner_request(
    user_input: str,
    expected: bool,
) -> None:
    assert _trusted_local_execution_requested(user_input) is expected
