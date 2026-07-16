"""Focused verification for advanced export planning and explicit writes."""

from __future__ import annotations

import json

import pytest

from ares.tools.advanced_export import (
    AdvancedExportError,
    plan_advanced_export,
    write_advanced_export,
)
from ares.tools.export_security import decrypt_export_payload
from ares.tools.media_export_upgrades import verify_export_manifest


def _payload() -> dict[str, object]:
    return {
        "version": 5,
        "exported_at": "2026-07-16T13:00:00Z",
        "config": {"theme": "dark", "api_key": "must-not-leak"},
        "memories": [
            {"fact_id": 1, "fact_text": "private detail", "created_at": "2026-07-15T08:00:00Z"},
            {"fact_id": 2, "fact_text": "older detail", "created_at": "2026-07-14T08:00:00Z"},
        ],
        "actions": [
            {"action_id": 1, "created_at": "2026-07-15T10:00:00Z", "summary": "saved"},
        ],
        "people": [{"person_id": 7, "created_at": "2026-07-15T11:00:00Z"}],
    }


def test_advanced_export_plan_is_side_effect_free_filtered_and_safe_to_render(tmp_path):
    output = tmp_path / "filtered.json"
    manifest = tmp_path / "filtered.manifest.json"

    plan = plan_advanced_export(
        _payload(),
        profile="full",
        include_categories=["memory", "actions"],
        since="2026-07-15T00:00:00Z",
        until="2026-07-15T23:59:59Z",
        output_path=output,
        manifest_path=manifest,
        encryption_password="never-render-me",
    )

    preview = plan.as_dict()
    assert not output.exists()
    assert not manifest.exists()
    assert preview["preview_only"] is True
    assert preview["encryption"] == {"enabled": True, "algorithm": "AES-256-GCM", "kdf": "scrypt"}
    assert preview["filters"]["effective_categories"] == ["actions", "memories"]
    assert preview["section_stats"]["memories"] == {"source": 2, "included": 1, "undated": 0}
    rendered = json.dumps(preview, sort_keys=True)
    assert "never-render-me" not in rendered
    assert "private detail" not in rendered
    assert "must-not-leak" not in rendered
    assert preview["verification"] == {"full_manifest": True, "write_manifest": True}


def test_advanced_export_encrypted_write_verifies_round_trip_and_plaintext_manifest(tmp_path):
    output = tmp_path / "backup.encrypted.json"
    manifest = tmp_path / "backup.manifest.json"
    password = "correct horse battery staple"
    plan = plan_advanced_export(
        _payload(),
        output_path=output,
        manifest_path=manifest,
        encryption_password=password,
    )

    result = write_advanced_export(plan, encryption_password=password)

    assert result["status"] == "completed"
    assert result["encrypted"] is True
    assert result["verification"]["round_trip"] is True
    assert result["verification"]["file"]["ok"] is True
    assert output.is_file() and manifest.is_file()
    rendered = json.dumps(result, sort_keys=True)
    assert password not in rendered
    assert "private detail" not in rendered
    envelope = json.loads(output.read_text(encoding="utf-8"))
    decrypted = decrypt_export_payload(envelope, password)
    plaintext_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert decrypted["config"]["api_key"] is None
    assert verify_export_manifest(decrypted, plaintext_manifest)["ok"] is True


def test_advanced_export_incremental_plain_write_has_a_manifest_for_the_delta(tmp_path):
    initial = plan_advanced_export(_payload(), redact=True)
    changed_payload = _payload()
    changed_payload["memories"] = [
        *changed_payload["memories"],  # type: ignore[operator]
        {"fact_id": 3, "fact_text": "new", "created_at": "2026-07-16T08:00:00Z"},
    ]
    output = tmp_path / "delta.json"
    manifest = tmp_path / "delta.manifest.json"
    delta = plan_advanced_export(
        changed_payload,
        previous_manifest=initial.as_dict()["full_manifest"],
        incremental=True,
        output_path=output,
        manifest_path=manifest,
    )

    result = write_advanced_export(delta)

    assert result["encrypted"] is False
    assert result["verification"]["round_trip"] is None
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["incremental"] is True
    assert set(written["sections"]) == {"memories"}
    write_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert verify_export_manifest(written, write_manifest)["ok"] is True


def test_advanced_export_requires_explicit_output_and_matching_encryption_mode(tmp_path):
    without_destination = plan_advanced_export(_payload())
    with pytest.raises(AdvancedExportError, match="output_path is required"):
        write_advanced_export(without_destination)

    encrypted = plan_advanced_export(
        _payload(),
        output_path=tmp_path / "secure.json",
        encryption_password="secret",
    )
    with pytest.raises(AdvancedExportError, match="encryption password is required"):
        write_advanced_export(encrypted)
    plain = plan_advanced_export(_payload(), output_path=tmp_path / "plain.json")
    with pytest.raises(AdvancedExportError, match="not encrypted"):
        write_advanced_export(plain, encryption_password="secret")


def test_advanced_export_rejects_same_output_and_manifest_paths(tmp_path):
    path = tmp_path / "same.json"
    with pytest.raises(AdvancedExportError, match="must be different"):
        plan_advanced_export(_payload(), output_path=path, manifest_path=path)
