from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from ares.vision.events import make_visual_event
from ares.vision.models import DetectedObject
from ares.vision.service import VisionService
from ares.workspace.app import create_workspace_app


class FixedDetector:
    async def detect(self, _frame, prompts=None):
        return [DetectedObject(label="cup", confidence=0.94, bounding_box=(2, 3, 20, 30))]


class SensitiveOCR:
    async def read(self, _frame):
        return ["Email ana@example.com password: open-sesame"]


def _service(tmp_path: Path) -> VisionService:
    return VisionService(
        database_path=tmp_path / "vision.db",
        detector=FixedDetector(),
        ocr=SensitiveOCR(),
    )


def _image(path: Path) -> None:
    Image.new("RGB", (32, 24), "white").save(path)


def test_workspace_vision_routes_are_local_and_keep_frames_out_of_responses(tmp_path: Path) -> None:
    service = _service(tmp_path)
    image_path = tmp_path / "desk.png"
    _image(image_path)
    app = create_workspace_app(vision_service=service)

    try:
        with TestClient(app, client=("127.0.0.1", 40123)) as client:
            observed = client.post("/vision/observe", json={
                "source_id": "uploaded-desk",
                "source": "image",
                "image_path": str(image_path),
            })
            sources = client.get("/vision/sources")
            denied_camera = client.post("/vision/observe", json={
                "source_id": "desk-camera",
                "source": "camera",
            })

        assert observed.status_code == 200
        payload = observed.json()
        assert payload["ok"] is True
        assert payload["source_id"] == "uploaded-desk"
        assert payload["objects"] == [{
            "tracker_id": payload["objects"][0]["tracker_id"],
            "label": "cup",
            "confidence": 0.94,
            "bounding_box": [2, 3, 20, 30],
            "attributes": {},
        }]
        assert "frame_reference" not in json.dumps(payload)
        assert "image" not in payload
        assert sources.status_code == 200
        assert sources.json()["sources"][0]["source_id"] == "uploaded-desk"
        assert denied_camera.status_code == 403
        assert "Observation permission" in denied_camera.json()["detail"]

        persisted = service.store.latest_snapshot("uploaded-desk")
        assert persisted is not None
        assert persisted.frame_reference is None
        durable_text = " ".join(persisted.visible_text)
        assert "ana@example.com" not in durable_text
        assert "open-sesame" not in durable_text

        with TestClient(app, client=("192.0.2.9", 40123)) as remote_client:
            remote = remote_client.get("/vision/sources")
        assert remote.status_code == 403
    finally:
        service.close()


def test_vision_event_websocket_only_streams_privacy_prepared_event_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.create_source(source_id="screen", source_type="screen", grant_observe=True)
    app = create_workspace_app(vision_service=service)

    try:
        with TestClient(app, client=("127.0.0.1", 40123)) as client:
            with client.websocket_connect("/vision/stream/screen") as websocket:
                service._record_event(make_visual_event(
                    event_type="object_appeared",
                    source_id="screen",
                    subject="cup",
                    description="A cup appeared.",
                    confidence=0.9,
                    frame_reference=str(tmp_path / "would-be-retained-frame.webp"),
                ))
                payload = websocket.receive_json()

        assert payload["event_type"] == "object_appeared"
        assert payload["source_id"] == "screen"
        assert "frame_reference" not in payload
        assert "image" not in payload
        assert str(tmp_path) not in json.dumps(payload)
    finally:
        service.close()
