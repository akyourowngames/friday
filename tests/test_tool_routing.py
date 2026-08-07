"""Deep tests for Ares tool routing.

Routing has two modes:
  * ``llm``   — Full LLM routing: the answering model receives the live catalog
    and chooses the capability itself (no regex narrows the set).
  * ``regex`` — the deterministic intent/target classifier (fallback path).

The reported bug ("list files on my desktop" was served by the Windows MCP
``Snapshot`` UI-automation tool instead of native file tools) is fixed by
letting the model route from an unambiguous catalog + explicit prompt guidance,
with the regex classifier kept only as a fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ares.integrations.tool_registry import (
    RootToolRegistry,
    ToolCategory,
    categorize_tool_name,
    select_root_tools,
)
from ares.integrations.turn_policy import (
    TurnIntent,
    authorize_turn_tool,
    build_turn_execution_context,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

FILE_LISTING_REQUEST = (
    "hey can you list me files in my desktop and tell me which are new ones acc to date"
)


def _schema(name: str, description: str = "tool") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _registry() -> RootToolRegistry:
    return RootToolRegistry(
        [
            _schema("list_directory", "List the files of a folder on the local filesystem"),
            _schema("glob_pattern", "Find files on the local filesystem matching a glob"),
            _schema("get_file_info", "Get metadata about a file or folder on disk"),
            _schema("run_command", "Run a shell command"),
            _schema("mcp__windows__Snapshot", "Windows UI Automation snapshot of on-screen windows"),
            _schema("mcp__windows__Click", "Click a UI element"),
            _schema("mcp__playwright__browser_navigate", "Navigate a browser"),
            _schema("search_memory", "Search durable memory"),
        ]
    )


ALL_NAMES = {
    "list_directory",
    "glob_pattern",
    "get_file_info",
    "run_command",
    "mcp__windows__Snapshot",
    "mcp__windows__Click",
    "mcp__playwright__browser_navigate",
    "search_memory",
}


# --------------------------------------------------------------------------- #
# LLM routing mode (the default: model chooses from the live catalog)         #
# --------------------------------------------------------------------------- #


def test_llm_mode_passes_through_full_catalog_for_file_listing() -> None:
    reg = _registry()
    ctx = build_turn_execution_context(FILE_LISTING_REQUEST)
    names = {s["function"]["name"] for s in reg.select_for_turn(ctx, routing_mode="llm")}

    # Full LLM routing hands the model the entire live catalog: the native file
    # tools AND the Windows surface are both present (the model decides via the
    # prompt guidance, instead of a regex guessing the surface).
    assert names == ALL_NAMES
    assert {"list_directory", "glob_pattern", "get_file_info"} <= names


@pytest.mark.parametrize(
    "text",
    [
        "hey",
        FILE_LISTING_REQUEST,
        "open notepad and type a note",
        "search this website by clicking the search box",
        "remember blue is my favorite color",
    ],
)
def test_llm_mode_is_always_pass_through(text: str) -> None:
    reg = _registry()
    names = {
        s["function"]["name"]
        for s in reg.select_for_turn(build_turn_execution_context(text), routing_mode="llm")
    }
    assert names == ALL_NAMES


def test_llm_mode_keeps_native_file_tools_for_desktop_file_request() -> None:
    reg = _registry()
    names = {
        s["function"]["name"]
        for s in reg.select_for_turn(
            build_turn_execution_context("list me the new files on my desktop"),
            routing_mode="llm",
        )
    }
    # The native capabilities the model should pick are always on the menu.
    assert "list_directory" in names
    assert "glob_pattern" in names
    assert "get_file_info" in names


# --------------------------------------------------------------------------- #
# Regex fallback mode (unchanged deterministic behavior)                      #
# --------------------------------------------------------------------------- #


def test_regex_fallback_default_when_no_mode_passed() -> None:
    reg = _registry()
    names = {s["function"]["name"] for s in reg.select_for_turn(build_turn_execution_context("hey"))}
    assert names == set()


def test_regex_fallback_casual_sends_no_tools() -> None:
    reg = _registry()
    names = {
        s["function"]["name"]
        for s in reg.select_for_turn(build_turn_execution_context("hey"), routing_mode="regex")
    }
    assert names == set()


def test_regex_fallback_desktop_interaction_advertises_only_windows_surface() -> None:
    reg = _registry()
    names = {
        s["function"]["name"]
        for s in reg.select_for_turn(
            build_turn_execution_context("Open Notepad and type a note"), routing_mode="regex"
        )
    }
    assert names == {"mcp__windows__Snapshot", "mcp__windows__Click"}


def test_regex_fallback_browser_interaction_advertises_only_playwright() -> None:
    reg = _registry()
    names = {
        s["function"]["name"]
        for s in reg.select_for_turn(
            build_turn_execution_context("Search this website by clicking the search box"),
            routing_mode="regex",
        )
    }
    assert names == {"mcp__playwright__browser_navigate"}


def test_select_root_tools_respects_routing_mode() -> None:
    schemas = [_schema("list_directory", "x"), _schema("mcp__windows__Snapshot", "y")]
    llm = {
        s["function"]["name"]
        for s in select_root_tools(
            schemas, build_turn_execution_context(FILE_LISTING_REQUEST), routing_mode="llm"
        )
    }
    regex = {
        s["function"]["name"]
        for s in select_root_tools(
            schemas, build_turn_execution_context(FILE_LISTING_REQUEST), routing_mode="regex"
        )
    }
    assert llm == {"list_directory", "mcp__windows__Snapshot"}
    # regex: file-listing request falls through to MODEL_ROUTED -> full catalog
    assert regex == {"list_directory", "mcp__windows__Snapshot"}


# --------------------------------------------------------------------------- #
# The actual misfile: why it happened, and what now prevents it                #
# --------------------------------------------------------------------------- #


def test_file_listing_request_is_model_routed_under_regex() -> None:
    ctx = build_turn_execution_context(FILE_LISTING_REQUEST)
    # Root cause of the original bug: a plain file-listing request was classified
    # as MODEL_ROUTED, so the full catalog (incl. Windows Snapshot) was shown and
    # the model picked the wrong surface. The fix is model-side guidance, not a
    # regex that guesses "desktop" == GUI.
    assert ctx.intent is TurnIntent.MODEL_ROUTED


def test_prompt_forbids_windows_snapshot_for_file_work() -> None:
    prompt = (REPO_ROOT / "ares" / "prompts.py").read_text(encoding="utf-8")
    assert "mcp__windows__Snapshot" in prompt
    # The prompt must tell the model Snapshot is on-screen UI, not a file browser.
    assert ("not a file browser" in prompt) or ("never returns folder contents" in prompt)
    # ...and must name the native tools to use instead.
    assert "list_directory" in prompt
    assert "glob_pattern" in prompt


def test_native_file_tool_descriptions_reference_disk() -> None:
    definitions = (REPO_ROOT / "ares" / "tools" / "definitions.py").read_text(encoding="utf-8")
    # File tools must read as disk/filesystem operations so the model maps
    # "list files on my desktop" onto them rather than the Windows UI surface.
    assert "local filesystem" in definitions
    assert "on disk" in definitions


# --------------------------------------------------------------------------- #
# Cron registration: the scheduler must be reachable, not firewalled           #
# --------------------------------------------------------------------------- #
#
# Ares keeps its own god-tier cron engine (lease + heartbeat, missed-run
# catch-up, retry backoff, pause_after_failures, recover_expired_leases).  A
# natural-language "schedule X daily at 9" has no surface keyword, so it is
# classified MODEL_ROUTED.  The old bug: cron tools were never registered in
# categorize_tool_name -> fell to UNKNOWN_CONSEQUENTIAL -> blocked at the gate
# -> the model fell back to the Windows Task Scheduler.  These tests freeze the
# fix so the registration gap can never silently reopen.


CRON_TOOLS = (
    "create_cron_job", "list_cron_jobs", "get_cron_job", "update_cron_job",
    "delete_cron_job", "run_cron_job_now", "get_cron_logs",
)


@pytest.mark.parametrize("tool", CRON_TOOLS)
def test_cron_tools_are_a_registered_category(tool: str) -> None:
    # The scheduler must be a first-class capability, never UNKNOWN_CONSEQUENTIAL
    # (which the authorization gate blocks under MODEL_ROUTED turns).
    assert categorize_tool_name(tool) is ToolCategory.CRON


def test_cron_schedule_request_is_reachable_under_model_routed() -> None:
    prompt = "Every day at 9am, scan my Downloads and tell me about new large files"
    ctx = build_turn_execution_context(prompt)
    assert ctx.intent is TurnIntent.MODEL_ROUTED
    decision = authorize_turn_tool(ctx, "create_cron_job", {})
    assert decision.allowed, decision.reason
    assert decision.effect.value == "local_mutation"
