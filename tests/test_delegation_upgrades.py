from __future__ import annotations

from pathlib import Path

import pytest

from ares.delegation.upgrades import (
    DelegationUpgradeError,
    enforce_delegation_budget,
    normalize_delegation_budget,
    normalize_delegation_checkpoint,
    plan_delegation_dag,
    project_delegation_progress,
    rank_skills_for_delegation,
    validate_evidence_artifact_contract,
)


def test_budget_normalization_caps_aliases_and_enforces_every_dimension() -> None:
    normalized = normalize_delegation_budget(
        {
            "max_tasks_per_run": 4,
            "max_parallel_agents": 12,
            "max_total_iterations": 999_999,
            "max_total_duration_seconds": 12.5,
        },
        hard_limits={"max_iterations": 50},
    )

    assert normalized.budget.max_tasks == 4
    assert normalized.budget.max_parallel == 4
    assert normalized.budget.max_iterations == 50
    assert normalized.budget.max_duration_seconds == 12.5
    assert "max_iterations" in normalized.capped
    assert "max_parallel" in normalized.capped

    denied = enforce_delegation_budget(
        normalized,
        usage={"task_count": 3, "iterations": 49},
        increment={"task_count": 2, "iterations": 2},
    )
    assert denied.allowed is False
    joined = " ".join(denied.violations)
    assert "task_count" in joined
    assert "iterations" in joined


@pytest.mark.parametrize("raw", [{"unknown": 1}, {"max_tasks": 1.5}, {"max_tasks": True}])
def test_budget_normalization_rejects_ambiguous_values(raw: dict[str, object]) -> None:
    with pytest.raises(DelegationUpgradeError):
        normalize_delegation_budget(raw)


def test_evidence_artifact_contract_validates_paths_mime_and_claims(tmp_path: Path) -> None:
    artifact = tmp_path / "report.md"
    artifact.write_text("result", encoding="utf-8")

    valid = validate_evidence_artifact_contract(
        {
            "summary": "Found a result",
            "artifacts": [{"path": str(artifact), "media_type": "text/markdown", "description": "report"}],
            "evidence": [{"claim": "The report was written", "artifact": str(artifact)}],
        },
        require_evidence=True,
        allowed_artifact_roots=[tmp_path],
        artifact_must_exist=True,
    )
    assert valid.valid is True
    assert valid.artifacts[0]["path"] == str(artifact.resolve())

    invalid = validate_evidence_artifact_contract(
        {"artifacts": [{"path": "../outside.txt", "media_type": "not-a-mime"}], "evidence": [{}]},
        require_evidence=True,
    )
    assert invalid.valid is False
    assert any("artifact[0]" in error for error in invalid.errors)
    assert any("evidence[0]" in error for error in invalid.errors)


def test_dag_plan_serializes_resource_conflicts_and_adds_synthesis() -> None:
    plan = plan_delegation_dag(
        [
            {"task_id": "research", "agent": "researcher", "prompt": "Research", "resource_keys": ["notes"]},
            {"task_id": "analysis", "agent": "analyst", "prompt": "Analyze", "resource_keys": ["notes"]},
            {"task_id": "review", "agent": "reviewer", "prompt": "Review"},
        ]
    )

    assert plan.dependencies["analysis"] == ("research",)
    assert plan.conflicts[0].resolution == "serialized"
    assert plan.synthesis_task is not None
    assert plan.synthesis_task["depends_on"] == ["analysis", "review"]
    assert plan.waves == (("research", "review"), ("analysis",), ("synthesize",))


def test_dag_plan_rejects_unknown_dependencies_and_cycles() -> None:
    with pytest.raises(DelegationUpgradeError, match="unknown task dependencies"):
        plan_delegation_dag([{"task_id": "one", "depends_on": ["missing"]}])
    with pytest.raises(DelegationUpgradeError, match="cycle"):
        plan_delegation_dag(
            [
                {"task_id": "one", "depends_on": ["two"]},
                {"task_id": "two", "depends_on": ["one"]},
            ]
        )


def test_progress_projection_merges_events_and_emits_resumable_checkpoint() -> None:
    plan = plan_delegation_dag(
        [
            {"task_id": "research"},
            {"task_id": "write", "depends_on": ["research"]},
        ],
        synthesize=False,
    )
    progress = project_delegation_progress(
        plan,
        checkpoint={"completed": ["research"]},
        events=[{"task_id": "write", "phase": "running"}],
    )

    assert progress.completed == ("research",)
    assert progress.running == ("write",)
    assert progress.ready == ()
    assert progress.current_wave == 1
    assert progress.checkpoint.completed == ("research",)
    assert progress.success_ratio == 0.5

    checkpoint = normalize_delegation_checkpoint(
        {"statuses": {"research": "complete", "write": "queued"}}, ["research", "write"]
    )
    assert checkpoint.completed == ("research",)


def test_skill_ranking_expands_dependencies_and_suppresses_overlaps() -> None:
    ranking = rank_skills_for_delegation(
        "web research current security news",
        [
            {
                "name": "web-research",
                "description": "Research current news and cite sources",
                "metadata": {"dependencies": ["source-citations"], "provides": ["research", "news"]},
            },
            {
                "name": "source-citations",
                "description": "Validate and format source citations",
                "metadata": {"provides": ["citations"]},
            },
            {
                "name": "news-research-copy",
                "description": "Research current news",
                "metadata": {"provides": ["research", "news"]},
            },
        ],
        limit=3,
    )

    assert [item.name for item in ranking.selected] == ["source-citations", "web-research"]
    assert ranking.suppressed["news-research-copy"].startswith("overlaps")
    assert ranking.selected[-1].score > 0
