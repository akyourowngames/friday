"""Focused coverage for opt-in media, action-history, and export helpers."""

from __future__ import annotations

import json

import pytest

from ares.tools.media_export_upgrades import (
    UpgradeValidationError,
    build_action_chains,
    build_action_timeline,
    build_image_variation_manifest,
    filter_export_payload,
    plan_export,
    plan_image_batch_transform,
    plan_image_transform,
    project_action_history,
    query_action_history,
    summarize_action_history,
    validate_image_metadata,
    validate_transform_result,
    verify_export_file,
    verify_export_manifest,
)


def test_image_variation_manifest_records_reproducible_style_aspect_negative_and_fallbacks():
    first = build_image_variation_manifest(
        "A observatory above the clouds",
        width=1200,
        height=1000,
        model="flux-pro",
        seed=42,
        variations=3,
        style=["Cinematic", "cinematic", "noir"],
        aspect_ratio="16:9",
        negative_prompt="logos, text",
        fallbacks=[{"strategy": "retry", "model": "flux"}, "stable-image"],
    )
    second = build_image_variation_manifest(
        "A observatory above the clouds",
        width=1200,
        height=1000,
        model="flux-pro",
        seed=42,
        variations=3,
        style=["Cinematic", "cinematic", "noir"],
        aspect_ratio="16:9",
        negative_prompt="logos, text",
        fallbacks=[{"strategy": "retry", "model": "flux"}, "stable-image"],
    )

    assert first["target_size"] == {"width": 1200, "height": 675}
    assert first["aspect_ratio"]["label"] == "16:9"
    assert first["style"] == ["Cinematic", "noir"]
    assert first["negative_prompt"] == "logos, text"
    assert first["fallback_policy"][1]["model"] == "stable-image"
    assert [item["seed"] for item in first["variants"]] == [item["seed"] for item in second["variants"]]
    assert len({item["seed"] for item in first["variants"]}) == 3
    assert first["seed"]["strategy"] == "derived-per-variation"


def test_image_transform_plan_batch_geometry_and_result_validation(tmp_path):
    plan = plan_image_transform(
        {"path": "source.jpg", "width": 800, "height": 600, "format": "jpeg", "bytes": 12},
        resize={"width": 400, "height": 400},
        crop={"left": 100, "top": 20, "right": 390, "bottom": 280},
        convert={"format": "webp", "quality": 82},
        output=tmp_path / "result.webp",
    )

    assert plan["target"]["width"] == 290
    assert plan["target"]["height"] == 260
    assert plan["target"]["format"] == "WEBP"
    assert validate_transform_result(plan, {"width": 290, "height": 260, "format": "webp"})["ok"] is True
    mismatch = validate_transform_result(plan, {"width": 290, "height": 261, "format": "webp"})
    assert mismatch["ok"] is False and "height" in mismatch["checks"]

    batch = plan_image_batch_transform(
        [
            {"path": "first/hero.jpg", "width": 100, "height": 100, "format": "jpeg"},
            {"path": "second/hero.jpg", "width": 100, "height": 100, "format": "jpeg"},
            {"path": "bad.jpg", "width": 0, "height": 100, "format": "jpeg"},
        ],
        {"resize": {"width": 50}},
        output_dir=tmp_path,
    )
    assert batch["summary"] == {"total": 3, "valid": 2, "invalid": 1}
    valid_outputs = [item["plan"]["target"]["output"] for item in batch["items"] if item["ok"]]
    assert valid_outputs[0].endswith("hero.jpg")
    assert valid_outputs[1].endswith("hero-2.jpg")


def test_image_transform_rejects_impossible_animation_conversion_and_metadata_limits():
    with pytest.raises(UpgradeValidationError, match="cannot preserve animation"):
        plan_image_transform(
            {"width": 100, "height": 100, "format": "gif", "frame_count": 2},
            convert={"format": "jpeg"},
        )
    validation = validate_image_metadata(
        {"width": 1600, "height": 900, "format": "png", "bytes": 1000},
        {"aspect_ratio": "16:9", "format": ["PNG", "WEBP"], "max_bytes": 500},
    )
    assert validation["ok"] is False
    assert validation["checks"]["aspect_ratio"] is True
    assert validation["checks"]["max_bytes"] is False


def _actions():
    return [
        {
            "action_id": 1,
            "action_type": "file_written",
            "target": "notes/plan.md",
            "summary": "Saved project plan",
            "tool_name": "write_file",
            "task_id": "goal-1",
            "session_id": "session-a",
            "tags": ["project", "write"],
            "created_at": "2026-07-15T10:00:00Z",
        },
        {
            "action_id": 2,
            "action_type": "image_generated",
            "target": "images/hero.png",
            "summary": "Saved hero image",
            "tool_name": "generate_image",
            "task_id": "goal-1",
            "session_id": "session-a",
            "tags": ["project", "media"],
            "created_at": "2026-07-15T10:05:00Z",
        },
        {
            "action_id": 3,
            "action_type": "export_created",
            "target": "backup.json",
            "summary": "Saved local export",
            "tool_name": "export_data",
            "task_id": "goal-2",
            "session_id": "session-b",
            "tags": ["backup"],
            "created_at": "2026-07-16T12:00:00Z",
        },
    ]


def test_rich_action_queries_timeline_chains_and_summary_are_consistent():
    filtered = query_action_history(
        _actions(),
        query="saved",
        tags=["project"],
        action_types=["file_written", "image_generated"],
        limit=1,
    )
    assert filtered["total"] == 2
    assert filtered["next_cursor"] == "1"
    assert filtered["items"][0]["action_id"] == 2

    timeline = build_action_timeline(_actions(), bucket="day")
    assert [bucket["count"] for bucket in timeline["buckets"]] == [2, 1]
    chains = build_action_chains(_actions(), max_gap_seconds=600)
    assert chains["total"] == 2
    assert next(chain for chain in chains["chains"] if chain["owner"] == "task:goal-1")["count"] == 2
    summary = summarize_action_history(_actions())
    assert summary["action_types"]["image_generated"] == 1
    projection = project_action_history(_actions(), filters={"tags": "project", "limit": 10})
    assert projection["summary"]["total"] == 2
    assert projection["timeline"]["total"] == 2


def test_export_plan_redacts_secrets_checksums_incremental_and_file_verification(tmp_path):
    original = {
        "config": {"api_key": "secret", "theme": "dark"},
        "memories": [{"id": 1, "text": "hello"}],
        "metadata": {"embedded": "sk_abcdefghi"},
    }
    first = plan_export(original, profile="full", redact=True)
    assert first["full_payload"]["config"]["api_key"] is None
    assert first["full_payload"]["metadata"]["embedded"] is None
    assert {item["path"] for item in first["manifest"]["redactions"]} == {"config.api_key", "metadata.embedded"}
    assert verify_export_manifest(first["full_payload"], first["manifest"])["ok"] is True

    changed = {
        "config": {"api_key": "secret", "theme": "dark"},
        "memories": [{"id": 1, "text": "hello"}, {"id": 2, "text": "new"}],
        "metadata": {"embedded": "sk_abcdefghi"},
    }
    incremental = plan_export(changed, previous_manifest=first["manifest"], incremental=True)
    assert incremental["manifest"]["incremental"]["changed_sections"] == ["memories"]
    assert incremental["write_payload"]["sections"] == {"memories": incremental["full_payload"]["memories"]}

    path = tmp_path / "export.json"
    path.write_text(json.dumps(first["full_payload"]), encoding="utf-8")
    assert verify_export_file(path, first["manifest"])["ok"] is True
    mutated = dict(first["full_payload"])
    mutated["config"] = dict(mutated["config"], theme="light")
    assert verify_export_manifest(mutated, first["manifest"])["ok"] is False


def test_export_filter_selects_categories_and_excludes_undated_rows_in_date_range():
    payload = {
        "version": 5,
        "exported_at": "2026-07-16T12:00:00Z",
        "config": {"theme": "dark"},
        "memories": [
            {"fact_id": 1, "created_at": "2026-07-15T08:00:00Z"},
            {"fact_id": 2, "created_at": "2026-07-14T08:00:00Z"},
            {"fact_id": 3, "fact_text": "undated"},
        ],
        "actions": [
            {"action_id": 1, "created_at": "2026-07-15T09:00:00Z"},
            {"action_id": 2, "created_at": "2026-07-16T09:00:00Z"},
        ],
        "people": [{"person_id": 3, "created_at": "2026-07-15T10:00:00Z"}],
        "conversations": [{"conversation_id": "c1", "created_at": "2026-07-15T10:00:00Z"}],
        "conversation_messages": [{"message_id": "m1", "created_at": "2026-07-15T10:01:00Z"}],
    }

    filtered = filter_export_payload(
        payload,
        include_categories=["memory", "actions"],
        since="2026-07-15T00:00:00Z",
        until="2026-07-15T23:59:59Z",
    )

    assert set(filtered["payload"]) == {"version", "exported_at", "memories", "actions", "export_filter"}
    assert [row["fact_id"] for row in filtered["payload"]["memories"]] == [1]
    assert [row["action_id"] for row in filtered["payload"]["actions"]] == [1]
    assert filtered["section_stats"]["memories"] == {"source": 3, "included": 1, "undated": 1}
    assert "memories: omitted 1 undated record(s)" in filtered["warnings"][0]

    with pytest.raises(UpgradeValidationError, match="both included and excluded"):
        filter_export_payload(payload, include_categories=["actions"], exclude_categories=["action"])
