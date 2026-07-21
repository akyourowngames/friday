from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares.models import AppConfig, MultiAgentConfig
from ares.multi_agent import AgentOutput
from ares.multi_agent.runtime import MultiAgentRuntime
from ares.skills.discovery import SkillManager
from ares.tools.executor import ToolExecutor


class _Root:
    def __init__(self, tmp_path: Path) -> None:
        self.config = AppConfig(
            data_dir=str(tmp_path),
            multi_agent=MultiAgentConfig(max_tasks_per_run=4, max_parallel_agents=3),
        )


class _Store:
    pass


def _assert_envelope(payload: dict) -> None:
    assert set(payload) == {
        "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
        "next_actions", "provenance", "metrics", "undo_id",
    }


@pytest.mark.asyncio
async def test_parallel_delegation_upgrade_plans_conflicts_and_returns_structured_progress(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))

    async def executor(spec, task, context):
        return AgentOutput(content=f"{task.task_id} complete", summary="done")

    runtime.adapter = executor  # type: ignore[assignment]
    response = await runtime.execute_tool(
        "delegate_tasks_parallel",
        {
            "response_format": "structured",
            "plan": True,
            "tasks": [
                {"task_id": "inspect", "agent": "researcher", "prompt": "Inspect source", "resource_keys": ["repo:src"]},
                {"task_id": "verify", "agent": "researcher", "prompt": "Verify source", "resource_keys": ["repo:src"]},
            ],
            "budget": {"max_tasks": 2, "max_parallel": 2, "max_duration_seconds": 20},
            "evidence_contract": {"require_content": True},
        },
        session_id="upgrade-session",
    )
    payload = json.loads(response)
    _assert_envelope(payload)
    assert payload["ok"] is True
    assert payload["data"]["plan"]["dependencies"]["verify"] == ["inspect"]
    assert payload["data"]["progress"]["completed"] == ["inspect", "verify"]
    assert payload["metrics"]["task_count"] == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_delegation_evidence_contract_blocks_unverified_child_output(tmp_path: Path) -> None:
    runtime = MultiAgentRuntime(_Root(tmp_path))

    async def executor(spec, task, context):
        return AgentOutput(content="A claim without an evidence bundle", summary="done")

    runtime.adapter = executor  # type: ignore[assignment]
    response = await runtime.execute_tool(
        "delegate_task",
        {
            "agent": "researcher",
            "task": "Research an evidence-backed answer",
            "response_format": "structured",
            "evidence_contract": {"require_evidence": True, "require_content": True},
        },
        session_id="upgrade-session",
    )
    payload = json.loads(response)
    _assert_envelope(payload)
    assert payload["ok"] is False
    assert payload["data"]["run"]["results"][0]["status"] == "blocked"
    assert "acceptance contract" in payload["errors"][0]["message"]
    await runtime.close()


def test_skill_tools_rank_dependencies_and_preview_a_versioned_skill(tmp_path: Path) -> None:
    executor = ToolExecutor(_Store(), _Store())
    executor.skill_manager = SkillManager([tmp_path])
    executor.skill_manager.create_skill(
        "shared-base",
        "---\ndescription: Shared web evidence primitives.\ncategory: research\nversion: 1.0.0\n---\n\n# Base\n",
        category="research",
    )
    executor.skill_manager.create_skill(
        "web-evidence",
        "---\ndescription: Gather and verify web evidence.\ncategory: research\nversion: 2.0.0\ndependencies: [shared-base]\nprovides: [evidence, web]\n---\n\n# Evidence\n",
        category="research",
    )

    ranked = json.loads(executor.execute("list_skills", {
        "query": "web evidence",
        "task": "Gather web evidence",
        "response_format": "structured",
    }))
    _assert_envelope(ranked)
    assert [item["name"] for item in ranked["data"]["recommendations"]["selected"]] == ["shared-base", "web-evidence"]

    preview = json.loads(executor.execute("create_skill", {
        "name": "verified-workflow",
        "category": "research",
        "content": "# Workflow\n\n1. Verify the source.",
        "description": "A versioned verification workflow.",
        "version": "2.1.0",
        "required_tools": ["web_search", "fetch_url"],
        "preview": True,
        "response_format": "structured",
    }))
    _assert_envelope(preview)
    assert preview["status"] == "preview"
    assert "version: \"2.1.0\"" in preview["data"]["content"]
    assert executor.skill_manager.get_skill("verified-workflow") is None
