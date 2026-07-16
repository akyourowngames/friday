from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ares.memory import MemoryStore
from ares.tools import ToolExecutor
from ares.vision.models import DetectedObject
from ares.vision.service import VisionService


class FixedDetector:
    async def detect(self, _frame, prompts=None):
        return [DetectedObject(label="cup", confidence=0.94, bounding_box=(1, 2, 12, 18))]


class FixedOCR:
    async def read(self, _frame):
        return ["Desk label"]


@pytest.mark.asyncio
async def test_tool_executor_dispatches_vision_tools_and_fails_closed_for_camera(
    tmp_path: Path,
    fake_embedding_provider,
) -> None:
    image_path = tmp_path / "desk.png"
    Image.new("RGB", (20, 16), "green").save(image_path)
    memory = MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    service = VisionService(
        database_path=tmp_path / "vision.db",
        memory_store=memory,
        detector=FixedDetector(),
        ocr=FixedOCR(),
    )
    executor = ToolExecutor(memory, vision_service=service)

    try:
        blocked = json.loads(await executor.execute_async("vision_observe", {
            "source": "camera",
            "source_id": "desk-camera",
        }))
        observed = json.loads(await executor.execute_async("vision_observe", {
            "source": "image",
            "source_id": "uploaded-desk",
            "image_path": str(image_path),
        }))
        watch = json.loads(await executor.execute_async("vision_watch", {
            "source_id": "uploaded-desk",
            "condition": "tell me when the cup moves",
        }))
        listed = json.loads(await executor.execute_async("vision_list_watches", {
            "source_id": "uploaded-desk",
        }))

        assert blocked["ok"] is False
        assert "Observation permission" in blocked["error"]
        assert observed["ok"] is True
        assert observed["objects"][0]["label"] == "cup"
        assert "frame_reference" not in json.dumps(observed)
        assert watch["ok"] is True
        assert watch["watch"]["source_id"] == "uploaded-desk"
        assert listed["ok"] is True
        assert [item["watch_id"] for item in listed["watches"]] == [watch["watch"]["watch_id"]]
    finally:
        executor.close()
        service.close()
        memory.close()
