"""Focused integration coverage for opt-in image batch and geometry upgrades."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.definitions import get_tool_definitions
from ares.tools.executor import ToolExecutor


def _envelope(payload: dict) -> None:
    assert set(payload) == {
        "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
        "next_actions", "provenance", "metrics", "undo_id",
    }


def _executor(tmp_path: Path) -> tuple[ToolExecutor, MemoryStore]:
    store = MemoryStore(db_path=tmp_path / "ares.db")
    executor = ToolExecutor(
        memory_store=store,
        config=AppConfig(data_dir=str(tmp_path / "ares-data")),
    )
    return executor, store


def test_resize_preset_padding_and_crop_modes_are_opt_in_and_verified(tmp_path: Path) -> None:
    executor, store = _executor(tmp_path)
    source = tmp_path / "wide.png"
    padded = tmp_path / "padded.png"
    cropped = tmp_path / "cropped.png"
    percent_cropped = tmp_path / "percent-cropped.png"
    Image.new("RGB", (240, 100), "navy").save(source)
    try:
        preview = json.loads(executor.execute("resize_image", {
            "path": str(source),
            "preset": "thumbnail",
            "pad": True,
            "preview": True,
            "response_format": "structured",
        }))
        _envelope(preview)
        assert preview["status"] == "preview"
        assert preview["data"]["plan"]["target"]["width"] == 160
        assert preview["data"]["plan"]["target"]["height"] == 160
        assert not padded.exists()

        resized = json.loads(executor.execute("resize_image", {
            "path": str(source),
            "preset": "thumbnail",
            "pad": True,
            "output": str(padded),
            "response_format": "structured",
        }))
        _envelope(resized)
        assert resized["ok"] is True
        assert resized["data"]["actual"]["width"] == 160
        assert resized["data"]["actual"]["height"] == 160

        center_crop = json.loads(executor.execute("crop_image", {
            "path": str(source),
            "mode": "aspect",
            "aspect_ratio": "1:1",
            "anchor": "center",
            "output": str(cropped),
            "response_format": "structured",
        }))
        _envelope(center_crop)
        assert center_crop["ok"] is True
        assert center_crop["data"]["actual"]["width"] == 100
        assert center_crop["data"]["actual"]["height"] == 100

        percentage_crop = json.loads(executor.execute("crop_image", {
            "path": str(source),
            "percent": {"left": 25, "top": 25, "right": 75, "bottom": 75},
            "output": str(percent_cropped),
            "response_format": "structured",
        }))
        _envelope(percentage_crop)
        assert percentage_crop["ok"] is True
        assert percentage_crop["data"]["actual"]["width"] == 120
        assert percentage_crop["data"]["actual"]["height"] == 50

        estimate = json.loads(executor.execute("resize_image", {
            "path": str(source), "width": 120, "estimate_only": True,
            "response_format": "structured",
        }))
        _envelope(estimate)
        assert estimate["status"] == "preview"
        assert estimate["data"]["plan"]["estimate"]["bytes"] > 0
    finally:
        executor.close()
        store.close()


def test_batch_transform_preview_requires_confirmation_and_writes_verified_outputs(tmp_path: Path) -> None:
    executor, store = _executor(tmp_path)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    output_dir = tmp_path / "processed"
    Image.new("RGB", (200, 100), "teal").save(first)
    Image.new("RGB", (100, 200), "orange").save(second)
    arguments = {
        "paths": [str(first), str(second)],
        "transform": {"resize": {"width": 50}},
        "output_dir": str(output_dir),
        "response_format": "structured",
    }
    try:
        preview = json.loads(executor.execute("batch_transform_images", arguments))
        _envelope(preview)
        assert preview["status"] == "preview"
        assert preview["data"]["plan"]["summary"] == {"total": 2, "valid": 2, "invalid": 0}
        assert not output_dir.exists()

        missing_confirmation = json.loads(executor.execute("batch_transform_images", {
            **arguments, "preview": False,
        }))
        _envelope(missing_confirmation)
        assert missing_confirmation["status"] == "preview"
        assert "confirm=true" in " ".join(missing_confirmation["warnings"])
        assert not output_dir.exists()

        completed = json.loads(executor.execute("batch_transform_images", {
            **arguments, "preview": False, "confirm": True,
        }))
        _envelope(completed)
        assert completed["ok"] is True
        assert completed["status"] == "completed"
        assert len(completed["artifacts"]) == 2
        with Image.open(output_dir / "first.png") as image:
            assert image.size == (50, 25)
        with Image.open(output_dir / "second.png") as image:
            assert image.size == (50, 100)
        with Image.open(first) as image:
            assert image.size == (200, 100)
    finally:
        executor.close()
        store.close()


def test_batch_schema_and_existing_output_protection(tmp_path: Path) -> None:
    executor, store = _executor(tmp_path)
    source = tmp_path / "source.png"
    output_dir = tmp_path / "processed"
    output_dir.mkdir()
    existing = output_dir / "source.png"
    Image.new("RGB", (100, 100), "red").save(source)
    Image.new("RGB", (1, 1), "black").save(existing)
    try:
        definitions = {item["function"]["name"]: item for item in get_tool_definitions()}
        assert "batch_transform_images" in definitions
        assert definitions["crop_image"]["function"]["parameters"]["required"] == ["path"]

        result = json.loads(executor.execute("batch_transform_images", {
            "paths": [str(source)],
            "transform": {"convert": {"format": "webp"}},
            "output_dir": str(output_dir),
        }))
        _envelope(result)
        assert result["status"] == "preview"
        assert result["data"]["plan"]["summary"] == {"total": 1, "valid": 1, "invalid": 0}

        blocked = json.loads(executor.execute("batch_transform_images", {
            "paths": [str(source)],
            "transform": {"resize": {"width": 50}},
            "output_dir": str(output_dir),
        }))
        _envelope(blocked)
        assert blocked["data"]["plan"]["summary"]["invalid"] == 1
        assert existing.stat().st_size > 0
    finally:
        executor.close()
        store.close()


def test_explicit_legacy_defaults_stay_on_the_original_string_contract(tmp_path: Path) -> None:
    executor, store = _executor(tmp_path)
    source = tmp_path / "legacy.png"
    Image.new("RGB", (80, 40), "purple").save(source)
    try:
        result = executor.execute("resize_image", {
            "path": str(source), "width": 40, "fit": "contain", "response_format": "legacy",
        })
        assert result.startswith("Resized 80×40 → 40×20")
        assert not result.lstrip().startswith("{")
    finally:
        executor.close()
        store.close()
