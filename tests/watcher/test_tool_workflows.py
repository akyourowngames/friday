import asyncio
import json

import pytest

from ares.watcher.fetchers.tool import ToolWorkflowFetcher


@pytest.mark.asyncio
async def test_tool_workflow_calls_existing_ares_tool_and_extracts_json_path():
    calls = []

    async def runner(name, arguments):
        calls.append((name, arguments))
        return json.dumps({"notifications": [{"title": "New DM", "body": "Hello"}]})

    fetcher = ToolWorkflowFetcher(runner)
    result = await fetcher.fetch("", {
        "tool_name": "phone_get_notifications",
        "arguments": {"limit": 5},
        "extract": {"json_path": "$.notifications[0].body"},
    })

    assert result.success is True
    assert result.content == "Hello"
    assert calls == [("phone_get_notifications", {"limit": 5})]
    assert result.metadata["source"] == "ares_tool_workflow"


@pytest.mark.asyncio
async def test_browser_workflow_uses_authenticated_playwright_navigation_and_snapshot():
    calls = []

    async def runner(name, arguments):
        calls.append((name, arguments))
        return "Inbox\nAlex: new message" if name.endswith("snapshot") else "Navigated"

    fetcher = ToolWorkflowFetcher(runner, browser=True)
    result = await fetcher.fetch("https://www.instagram.com/direct/inbox/", {})

    assert result.success is True
    assert result.content == "Inbox\nAlex: new message"
    assert calls == [
        ("mcp__playwright__browser_navigate", {"url": "https://www.instagram.com/direct/inbox/"}),
        ("mcp__playwright__browser_snapshot", {}),
    ]
    assert result.metadata["source"] == "ares_browser"


@pytest.mark.asyncio
async def test_browser_workflows_are_serialized_for_shared_playwright_session():
    active = 0
    peak = 0

    async def runner(name, _arguments):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "snapshot" if name.endswith("snapshot") else "navigated"

    fetcher = ToolWorkflowFetcher(runner, browser=True)
    first, second = await asyncio.gather(
        fetcher.fetch("https://example.com/one", {}),
        fetcher.fetch("https://example.com/two", {}),
    )
    assert first.success is second.success is True
    assert peak == 1


@pytest.mark.asyncio
async def test_tool_workflow_blocks_consequential_steps_without_two_opt_ins():
    called = False

    async def runner(_name, _arguments):
        nonlocal called
        called = True
        return "clicked"

    config = {"steps": [{"tool_name": "mcp__playwright__browser_click", "arguments": {"ref": "x"}}]}
    blocked = await ToolWorkflowFetcher(runner, global_allow_mutating=True).fetch("", config)
    assert blocked.success is False
    assert "change external state" in blocked.error
    assert called is False

    allowed = await ToolWorkflowFetcher(runner, global_allow_mutating=True).fetch(
        "", {**config, "allow_mutating_tools": True}
    )
    assert allowed.success is True
    assert called is True


@pytest.mark.asyncio
async def test_tool_workflow_supports_prior_step_placeholders():
    calls = []

    async def runner(name, arguments):
        calls.append((name, arguments))
        return "alpha" if len(calls) == 1 else arguments["query"]

    result = await ToolWorkflowFetcher(runner).fetch("", {
        "steps": [
            {"tool_name": "read_file", "arguments": {"path": "state.txt"}},
            {"tool_name": "search_memory", "arguments": {"query": "signal=${previous}"}},
        ]
    })
    assert result.success is True
    assert result.content == "signal=alpha"
