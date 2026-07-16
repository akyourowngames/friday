import json
from datetime import datetime, timezone

import pytest

from ares.cron.policy import cron_policy_block_reason, expand_variables
from ares.cron.store import CronConflictError, CronStore
from ares.cron.tools import CronToolHandlers


ENVELOPE_KEYS = {
    "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
    "next_actions", "provenance", "metrics", "undo_id",
}


def test_cron_preview_normalizes_natural_schedule_without_creating_job(tmp_path):
    store = CronStore(tmp_path)
    tools = CronToolHandlers(store)

    result = json.loads(tools.create_cron_job({
        "name": "Morning brief",
        "prompt": "Summarize ${topic}",
        "cron": "every weekday at 9am",
        "timezone": "UTC",
        "policy": {"variables": {"topic": "release health"}, "budget": {"max_iterations": 4}},
        "preview": True,
        "response_format": "structured",
    }))

    assert set(result) == ENVELOPE_KEYS
    assert result["status"] == "preview"
    assert result["data"]["cron"] == "0 9 * * 1-5"
    assert len(result["data"]["schedule_simulation"]["next_runs"]) == 5
    assert store.list_jobs() == []


def test_cron_dependency_graph_is_atomic_and_blocks_until_dependency_succeeds(tmp_path):
    store = CronStore(tmp_path)
    dependency = store.create_job("Build", "build", "0 * * * *")
    dependent = store.create_job(
        "Deploy", "deploy", "5 * * * *", policy={"dependencies": [dependency["id"]]},
    )

    with pytest.raises(CronConflictError, match="dependency incomplete"):
        store.claim_job(dependent["id"])

    store.update_job(dependency["id"], state="completed", last_status="completed")
    claimed = store.claim_job(dependent["id"])
    assert claimed["state"] == "running"

    with pytest.raises(ValueError, match="cycle"):
        store.update_job(dependency["id"], policy={"dependencies": [dependent["id"]]})
    assert store.get_job(dependency["id"])["policy"] == {}


def test_cron_run_caps_retries_and_failure_pause_are_enforced(tmp_path):
    store = CronStore(tmp_path)
    capped = store.create_job(
        "One shot", "run", "0 * * * *", policy={"run_caps": {"max_total": 1}},
    )
    claimed = store.claim_job(capped["id"])
    log = store.log_dir(capped["id"]) / "run.md"
    log.write_text("run", encoding="utf-8")
    store.complete_job(capped["id"], claimed["lease_id"], status="completed", log_path=log)
    with pytest.raises(CronConflictError, match="maximum total runs"):
        store.claim_job(capped["id"])

    retrying = store.create_job(
        "Retrying", "run", "0 * * * *",
        policy={
            "retry": {"max_attempts": 3, "base_seconds": 1, "multiplier": 2, "max_seconds": 10},
            "pause_after_failures": 2,
        },
    )
    first = store.claim_job(retrying["id"])
    after_first = store.complete_job(retrying["id"], first["lease_id"], status="failed", log_path=log)
    assert after_first["state"] == "scheduled"
    assert after_first["retry_count"] == 1
    assert after_first["enabled"] is True
    second = store.claim_job(retrying["id"])
    after_second = store.complete_job(retrying["id"], second["lease_id"], status="failed", log_path=log)
    assert after_second["enabled"] is False
    assert after_second["consecutive_failures"] == 2


def test_cron_windows_quiet_hours_daily_caps_and_variables_are_deterministic():
    now = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    base = {
        "id": "job", "timezone": "UTC", "run_count": 0, "run_history": [],
        "state": "scheduled",
    }
    assert cron_policy_block_reason({
        **base, "policy": {"window": {"start": "13:00", "end": "14:00", "days": [3]}},
    }, {}, now=now) == "outside execution window"
    assert cron_policy_block_reason({
        **base, "policy": {"quiet_hours": {"start": "11:00", "end": "13:00", "days": [3]}},
    }, {}, now=now) == "inside quiet hours"
    assert cron_policy_block_reason({
        **base, "run_history": ["2026-07-16T08:00:00Z"],
        "policy": {"run_caps": {"max_per_day": 1}},
    }, {}, now=now) == "maximum daily runs reached"
    assert expand_variables("Ship ${project} to ${region}", {
        "variables": {"project": "Ares", "region": "India"},
    }) == "Ship Ares to India"


def test_cron_logs_support_filters_metrics_and_cursor(tmp_path):
    store = CronStore(tmp_path)
    job = store.create_job("Logs", "run", "0 * * * *")
    directory = store.log_dir(job["id"])
    (directory / "2026-07-16T12-00-00Z.md").write_text(
        "# Cron Run: Logs\n**Job:** logs\n**Run:** 2026-07-16T12:00:00Z\n"
        "**Status:** failed\n**Duration:** 2.5s\n\n## Error\nProvider offline\n\n"
        "## Run Metadata\n- Retry attempt: 2\n",
        encoding="utf-8",
    )
    (directory / "2026-07-16T11-00-00Z.md").write_text(
        "# Cron Run: Logs\n**Job:** logs\n**Run:** 2026-07-16T11:00:00Z\n"
        "**Status:** completed\n**Duration:** 1.0s\n\n## Run Metadata\n- Retry attempt: 1\n",
        encoding="utf-8",
    )
    tools = CronToolHandlers(store)

    result = json.loads(tools.get_cron_logs({
        "job_id": job["id"], "status": "failed", "limit": 1,
        "include": ["content"], "response_format": "structured",
    }))

    assert set(result) == ENVELOPE_KEYS
    assert result["data"]["logs"][0]["failure_summary"] == "Provider offline"
    assert result["metrics"]["duration_seconds"] == 2.5
    assert result["metrics"]["retry_count"] == 1
