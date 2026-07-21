from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from ares.skills.actions import ActionLedger
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.export_security import decrypt_export_payload
from ares.tools.executor import ToolExecutor


def _envelope(payload: dict) -> None:
    assert set(payload) == {
        "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
        "next_actions", "provenance", "metrics", "undo_id",
    }


def _executor(tmp_path: Path) -> tuple[ToolExecutor, MemoryStore, ActionLedger]:
    database = tmp_path / "ares.db"
    memory = MemoryStore(db_path=database)
    actions = ActionLedger(db_path=database)
    executor = ToolExecutor(
        memory_store=memory,
        config=AppConfig(data_dir=str(tmp_path / "ares-data"), api_key="super-secret-key"),
        action_ledger=actions,
    )
    return executor, memory, actions


def test_structured_action_history_filters_timeline_and_chains(tmp_path: Path) -> None:
    executor, memory, actions = _executor(tmp_path)
    try:
        actions.record("image_generated", target="image.png", summary="Generated a local image", tool_name="generate_image", session_id="s1", task_id="task-a", tags=["image"])
        actions.record("image_edited", target="image.png", summary="Resized the image", tool_name="resize_image", session_id="s1", task_id="task-a", tags=["image", "edit"])
        payload = json.loads(executor.execute("search_actions", {
            "action_types": ["image_generated", "image_edited"],
            "tags": ["image"],
            "timeline_bucket": "task",
            "response_format": "structured",
        }))
        _envelope(payload)
        assert payload["ok"] is True
        assert payload["data"]["query"]["total"] == 2
        assert payload["data"]["timeline"]["buckets"][0]["key"] == "task:task-a"
        assert payload["data"]["chains"]["total"] == 1
    finally:
        executor.close()
        memory.close()
        actions.close()


def test_image_upgrade_previews_and_verifies_a_real_transform(tmp_path: Path) -> None:
    executor, memory, actions = _executor(tmp_path)
    source = tmp_path / "source.png"
    output = tmp_path / "resized.png"
    Image.new("RGB", (240, 120), "navy").save(source)
    try:
        preview = json.loads(executor.execute("generate_image", {
            "prompt": "A navy abstract landscape",
            "width": 400,
            "height": 400,
            "aspect_ratio": "16:9",
            "variations": 2,
            "style": ["cinematic"],
            "seed": 7,
            "preview": True,
            "response_format": "structured",
        }))
        _envelope(preview)
        assert preview["status"] == "preview"
        assert preview["data"]["manifest"]["target_size"] == {"width": 400, "height": 225}

        transformed = json.loads(executor.execute("resize_image", {
            "path": str(source),
            "width": 120,
            "height": 120,
            "fit": "contain",
            "output": str(output),
            "response_format": "structured",
        }))
        _envelope(transformed)
        assert transformed["ok"] is True
        assert transformed["data"]["actual"]["width"] == 120
        assert transformed["data"]["actual"]["height"] == 60
        assert output.is_file()
    finally:
        executor.close()
        memory.close()
        actions.close()


def test_advanced_export_previews_redaction_and_writes_a_verified_manifest(tmp_path: Path) -> None:
    executor, memory, actions = _executor(tmp_path)
    output = tmp_path / "export.json"
    try:
        preview = json.loads(executor.execute("export_data", {
            "path": str(output),
            "profile": "full",
            "preview": True,
            "response_format": "structured",
        }))
        _envelope(preview)
        assert preview["status"] == "preview"
        assert preview["data"]["full_manifest"]["redaction_count"] > 0

        completed = json.loads(executor.execute("export_data", {
            "path": str(output),
            "profile": "full",
            "response_format": "structured",
        }))
        _envelope(completed)
        assert completed["ok"] is True
        assert completed["data"]["write_result"]["verification"]["file"]["ok"] is True
        assert output.is_file()
        assert Path(completed["data"]["manifest_path"]).is_file()
    finally:
        executor.close()
        memory.close()
        actions.close()


def test_advanced_export_encrypts_without_echoing_password_or_plaintext(tmp_path: Path) -> None:
    executor, memory, actions = _executor(tmp_path)
    output = tmp_path / "export.encrypted.json"
    password = "correct horse battery staple"
    try:
        raw = executor.execute("export_data", {
            "path": str(output),
            "profile": "full",
            "include_categories": ["memories", "actions"],
            "encryption_password": password,
            "response_format": "structured",
        })
        completed = json.loads(raw)
        _envelope(completed)
        assert completed["ok"] is True
        assert completed["data"]["write_result"]["encrypted"] is True
        assert password not in raw
        assert output.is_file()
        envelope = json.loads(output.read_text(encoding="utf-8"))
        decrypted = decrypt_export_payload(envelope, password)
        assert "config" not in decrypted
        assert "memories" in decrypted
        assert decrypted["actions"] == []
    finally:
        executor.close()
        memory.close()
        actions.close()
