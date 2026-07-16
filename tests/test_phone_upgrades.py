"""Focused coverage for privacy-preserving phone-tool upgrade helpers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ares.tools.phone_upgrades import (
    call_preflight,
    merge_contact_candidates,
    normalize_call_status,
    normalize_phone_number,
    prepare_notifications,
    preview_sms,
    rank_contact_candidates,
    resolve_contact_channel,
    sms_delivery_status,
    sms_segmentation,
    validate_post_call_note,
)


def test_notification_filtering_deduplication_grouping_and_metadata_privacy():
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    secret = "the private verification code is 889912"
    payload = prepare_notifications(
        [
            {"id": "mail-1", "app": "Mail", "title": "Urgent", "text": secret, "unread": True, "timestamp": now.isoformat()},
            {"id": "mail-2", "app": "Mail", "title": "Urgent", "text": secret, "unread": True, "timestamp": now.isoformat()},
            {"id": "chat-1", "app": "Chat", "title": "Asha", "text": "hello", "unread": False, "timestamp": (now - timedelta(minutes=2)).isoformat()},
            {"id": "old", "app": "Mail", "title": "Old", "text": "old", "timestamp": (now - timedelta(days=7)).isoformat()},
        ],
        applications="mail",
        keywords=["verification"],
        unread_only=True,
        since=(now - timedelta(hours=1)).isoformat(),
        group_by="application",
        now=now,
    )

    assert payload["metrics"]["inspected"] == 4
    assert payload["metrics"]["returned"] == 1
    assert payload["metrics"]["duplicates_collapsed"] == 1
    assert payload["groups"] == [{"group": "Mail", "count": 1, "highest_priority": "high", "notification_ids": ["mail-1"]}]
    encoded = json.dumps(payload)
    assert secret not in encoded
    assert payload["notifications"][0]["content"]["text"]["present"] is True
    assert "content_preview" not in payload["notifications"][0]

    full = prepare_notifications(
        [{"app": "Mail", "title": "Urgent", "text": secret, "conversation": "Asha"}],
        content_mode="full",
        group_by="conversation",
    )
    assert full["notifications"][0]["content_preview"]["text"] == secret
    assert full["groups"][0]["group"] == "Asha"


def test_contact_merge_ranking_masks_values_and_keeps_ambiguity():
    saved = [{
        "id": "person-rohan",
        "canonical_name": "Rohan Sharma",
        "aliases": ["Rohit"],
        "phone": "+91 98765 43210",
        "email": "rohan@example.com",
        "preferred_contact_method": "phone",
    }]
    device = [{
        "name": "Rohit Sharma",
        "numbers": ["+919876543210"],
        "emails": ["rohan@example.com"],
    }]
    safe_merged = merge_contact_candidates(device, saved)
    assert len(safe_merged) == 1
    assert set(safe_merged[0]["sources"]) == {"device", "saved_person"}
    assert "+919876543210" not in json.dumps(safe_merged)
    merged = merge_contact_candidates(device, saved, reveal_contact_values=True)

    ranked = rank_contact_candidates("rohit", device_contacts=device, saved_people=saved, action="sms")
    assert ranked["best_candidate_id"] == "person-rohan"
    candidate = ranked["candidates"][0]
    assert candidate["person_id"] == "person-rohan"
    assert candidate["preferred_channel"] == "phone"
    assert candidate["channels"][0]["value"].endswith("3210")
    assert "+919876543210" not in json.dumps(ranked)

    channel = resolve_contact_channel(merged[0], action="sms")
    assert channel["ok"] is True
    assert channel["value"].endswith("3210")
    assert "+919876543210" not in channel["value"]

    ambiguous = rank_contact_candidates(
        "sam", device_contacts=[{"name": "Sam One", "phone": "+15555550101"}, {"name": "Sam Two", "phone": "+15555550102"}]
    )
    assert ambiguous["requires_disambiguation"] is True
    assert ambiguous["best_candidate_id"] is None


def test_sms_preview_templates_segments_and_transport_only_retry():
    preview = preview_sms(
        "+1 (555) 555-0100",
        template="Hi {{name}}, your appointment is {time}.",
        variables={"name": "Asha", "time": "2pm"},
    )
    assert preview["ok"] is True
    assert preview["recipient"].endswith("0100")
    assert preview["message_included"] is False
    assert preview["message"]["characters"] > 0
    assert preview["confirmation_required"] is True
    assert sms_segmentation("a" * 161)["segments"] == 2
    assert sms_segmentation("🙂" * 36)["encoding"] == "UCS-2"
    assert sms_segmentation("🙂" * 36)["segments"] == 2

    missing = preview_sms("+15555550100", template="Hello {name}", variables={})
    assert missing["ok"] is False
    assert missing["missing_variables"] == ["name"]
    assert sms_delivery_status({"ok": False, "error": "network timeout"})["retry_allowed"] is True
    assert sms_delivery_status({"ok": False, "error": "invalid destination"})["retry_allowed"] is False


def test_call_preflight_status_and_post_call_note_validation_are_side_effect_free():
    status = {
        "ok": True,
        "any_ready": True,
        "devices": [{"id": "pixel", "reachable": True}],
    }
    waiting = call_preflight("+1 555 555 0100", phone_status=status, device_id="pixel", recipient="Asha")
    assert waiting["ready"] is True
    assert waiting["ok"] is False
    assert waiting["confirmation_required"] is True
    assert waiting["number"].endswith("0100")

    ready = call_preflight("+15555550100", phone_status=status, device_id="pixel", confirm=True)
    assert ready["ok"] is True
    assert ready["next_action"] == "place_call"
    offline = call_preflight("+15555550100", phone_status={"ok": False, "error": "offline"}, confirm=True)
    assert offline["ok"] is False
    assert offline["next_action"] == "restore_phone_connectivity"
    kde_only = call_preflight(
        "+15555550100",
        phone_status={"any_ready": True, "capability_matrix": {"calls": False}, "adb": {"devices": []}},
        confirm=True,
    )
    assert kde_only["ok"] is False
    legacy_status = call_preflight(
        "+15555550100",
        phone_status={"any_ready": True, "capability_matrix": {"calls": True}, "adb": {"connected": True, "devices": ["adb-1"]}},
        device_id="adb-1",
        confirm=True,
    )
    assert legacy_status["ok"] is True and legacy_status["selected_device_id"] == "adb-1"
    assert normalize_call_status({"status": "active", "call_id": "c1"})["status"] == "connected"
    assert normalize_call_status({"status": "ended"})["terminal"] is True

    valid = validate_post_call_note("Discussed project timeline.", person_id="person-1", call_id="c1")
    assert valid["ok"] is True and valid["requires_explicit_attachment"] is True
    assert valid["persisted"] is False
    assert validate_post_call_note("bad\x00note")["ok"] is False


def test_phone_normalization_requires_a_country_for_ambiguous_local_values():
    assert normalize_phone_number("00 44 20 7946 0958") == "+442079460958"
    assert normalize_phone_number("98765 43210", default_country_code="91") == "+919876543210"
    assert normalize_phone_number("98765 43210") == "9876543210"
    with pytest.raises(ValueError):
        normalize_phone_number("not a number")
