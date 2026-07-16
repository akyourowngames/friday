from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ares.watcher.upgrades import (
    WatcherPolicyError,
    evaluate_alert_policy,
    evaluate_condition_policy,
    evaluate_conditions,
    event_fingerprint,
    event_signature,
    health_projection,
    normalize_alert_policy,
    normalize_condition_policy,
    normalize_watcher_policy,
    project_watcher_event,
    suppression_reason,
    token_similarity,
    watcher_health,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def test_normalize_condition_policy_accepts_nested_and_or_and_legacy_default():
    policy = normalize_condition_policy(
        {
            "operator": "OR",
            "conditions": [
                {"type": "regex", "field": "body", "pattern": "sale", "flags": ["ignorecase"]},
                {
                    "operator": "AND",
                    "conditions": [
                        {"type": "changed", "field": "price"},
                        {"type": "threshold", "field": "price", "below": 90, "crossing": True},
                    ],
                },
            ],
        }
    )
    assert policy["operator"] == "any"
    assert policy["conditions"][0]["kind"] == "regex"
    assert policy["conditions"][1]["operator"] == "all"
    assert normalize_condition_policy(None)["conditions"][0] == {"kind": "changed", "expected": True}


def test_condition_evaluator_handles_changed_regex_threshold_similarity_and_or():
    policy = {
        "operator": "AND",
        "conditions": [
            {"type": "changed", "field": "price"},
                {"type": "regex", "field": "body", "pattern": r"limited\s+sale", "flags": "ignorecase"},
            {"type": "threshold", "field": "price", "below": 90, "crossing": True},
            {"type": "semantic", "field": "body", "old_field": "body", "max_similarity": 0.5},
        ],
    }
    evaluation = evaluate_condition_policy(
        policy,
        {
            "current": {"price": 80, "body": "Limited sale: console bundle"},
            "previous": {"price": 100, "body": "Weekly weather update"},
        },
    )
    assert evaluation.matched is True
    assert [child.kind for child in evaluation.children] == ["changed", "regex", "threshold", "similarity"]
    assert evaluation.children[-1].details["scores"]["score"] <= 0.5

    any_match = evaluate_condition_policy(
        {"operator": "OR", "conditions": [{"type": "regex", "pattern": "never"}, {"type": "threshold", "above": 70}]},
        {"new_value": 80, "old_value": 50},
    )
    assert any_match.matched is True


@pytest.mark.parametrize(
    "policy",
    [
        {"type": "regex", "pattern": "["},
        {"type": "threshold", "field": "price"},
        {"operator": "xor", "conditions": [{"type": "changed"}]},
        {"type": "semantic", "max_similarity": 1.2},
    ],
)
def test_condition_normalization_rejects_invalid_policies(policy):
    with pytest.raises(WatcherPolicyError):
        normalize_condition_policy(policy)


def test_token_similarity_is_local_and_deterministic():
    first = token_similarity("Ares checks price changes", "Ares checks price changes", "hybrid")
    second = token_similarity("Ares checks price changes", "Ares checks price changes", "hybrid")
    assert first == second
    assert first["score"] == 1.0
    assert token_similarity("alpha beta", "gamma delta", "jaccard")["score"] == 0.0


def test_alert_policy_normalization_and_each_suppression_reason():
    normalized = normalize_alert_policy(
        {
            "cooldown": 30,
            "dedupe_window": 60,
            "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "UTC"},
            "expires_after_seconds": 120,
            "false_positive_signatures": [{"field": "change_summary", "contains": "known noise"}],
        }
    )
    assert normalized["cooldown_seconds"] == 30
    assert normalized["quiet_hours"][0]["timezone"] == "UTC"

    event = {"monitor_id": "price", "event_type": "change", "new_value": "80", "change_summary": "Price changed", "created_at": NOW.isoformat()}
    cooldown = evaluate_alert_policy(normalized, event, history=[{**event, "created_at": (NOW - timedelta(seconds=10)).isoformat()}], now=NOW)
    assert {reason.code for reason in cooldown.reasons} >= {"cooldown", "duplicate"}

    quiet = evaluate_alert_policy({"quiet_hours": "22:00-07:00"}, event, now=NOW.replace(hour=23))
    assert quiet.deliver is False
    assert quiet.reasons[0].code == "quiet_hours"

    expired = evaluate_alert_policy({"expires_at": (NOW - timedelta(seconds=1)).isoformat()}, event, now=NOW)
    assert expired.deliver is False
    assert expired.reasons[0].code == "expired"

    false_positive = evaluate_alert_policy({"false_positive_signatures": ["*known noise*"]}, {**event, "change_summary": "Known noise from rotating banner"}, now=NOW)
    assert false_positive.deliver is False
    assert false_positive.reasons[0].code == "false_positive"


def test_alert_policy_scope_and_condition_suppression_are_deterministic():
    event = {"monitor_id": "one", "event_type": "price", "new_value": 90, "severity": "info", "created_at": NOW.isoformat()}
    other_monitor = {"monitor_id": "two", "event_type": "price", "new_value": 90, "created_at": (NOW - timedelta(seconds=1)).isoformat()}
    decision = evaluate_alert_policy(
        {"cooldown_seconds": 60, "suppress_if": {"type": "regex", "field": "event_type", "pattern": "^price$"}},
        event,
        history=[other_monitor],
        now=NOW,
    )
    assert [reason.code for reason in decision.reasons] == ["suppression_condition"]
    assert event_signature(event) == event_signature(dict(event))
    assert len(event_fingerprint(event)) == 64


def test_project_and_health_helpers_are_stable():
    event = {"id": "e1", "monitor_id": "m1", "event_type": "change", "severity": "warning", "change_summary": "Content changed", "created_at": (NOW - timedelta(seconds=20)).isoformat()}
    projection = project_watcher_event(event, now=NOW)
    assert projection["age_seconds"] == 20
    assert projection["summary"] == "Content changed"

    health = watcher_health(
        [
            {"status": "failed", "finished_at": (NOW - timedelta(seconds=5)).isoformat()},
            {"status": "failed", "finished_at": (NOW - timedelta(seconds=10)).isoformat()},
            {"status": "ok", "finished_at": (NOW - timedelta(seconds=15)).isoformat()},
        ],
        now=NOW,
    )
    assert health["status"] == "failed"
    assert health["consecutive_failures"] == 2
    assert health["success_rate"] == pytest.approx(1 / 3, abs=0.0001)


def test_scheduler_facing_policy_wrappers_are_json_friendly():
    normalized = normalize_watcher_policy(
        {
            "conditions": [{"type": "threshold", "field": "price", "below": 100}],
            "condition_operator": "AND",
            "alerts": {"dedupe_window_seconds": 20},
            "unrelated_legacy_key": True,
        }
    )
    assert normalized["operator"] == "all"
    assert normalized["conditions"][0]["kind"] == "threshold"

    matched = evaluate_conditions({"price": 120}, {"price": 95}, normalized["conditions"], normalized["operator"])
    assert matched["matched"] is True
    assert matched["evaluation"]["children"][0]["kind"] == "threshold"

    default_change = evaluate_conditions({"price": 120}, {"price": 95})
    assert default_change["matched"] is True

    event = {"monitor_id": "m", "event_type": "price", "new_value": 95, "created_at": NOW.isoformat()}
    reason = suppression_reason({"dedupe_window_seconds": 60}, [{**event, "created_at": (NOW - timedelta(seconds=3)).isoformat()}], event, NOW)
    assert reason is not None
    assert reason["code"] == "duplicate"

    projection = health_projection({"id": "m", "name": "Price", "enabled": True}, [{"status": "ok", "finished_at": NOW.isoformat()}], [event], now=NOW)
    assert projection["monitor_id"] == "m"
    assert projection["status"] == "healthy"
    assert projection["recent_events"] == 1
