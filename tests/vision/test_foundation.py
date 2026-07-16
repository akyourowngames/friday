from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from ares.vision.capture import ImageCapture
from ares.vision.models import SceneSnapshot, VisionFrame, VisionSourceType
from ares.vision.ocr import PaddleOCRReader
from ares.vision.privacy import (
    VisionPermissionController,
    VisionPrivacyConfig,
    redact_sensitive_text,
)
from ares.vision.providers.ultralytics_provider import UltralyticsVisionDetector


def test_frame_keeps_pixels_out_of_durable_model_dump() -> None:
    image = Image.new("RGB", (12, 7), "white")

    frame = VisionFrame(source_id="upload", source_type=VisionSourceType.IMAGE, image=image)

    assert frame.image is image
    assert (frame.width, frame.height) == (12, 7)
    assert "image" not in frame.model_dump()
    assert "raw_frame" not in frame.model_dump()
    assert frame.persistent_dict()["source_id"] == "upload"
    with pytest.raises(ValueError, match="raw frame"):
        VisionFrame(source_id="upload", image=image, metadata={"image": image})


@pytest.mark.asyncio
async def test_image_capture_accepts_file_and_never_uses_path_as_frame_reference(tmp_path: Path) -> None:
    path = tmp_path / "desk.png"
    Image.new("RGB", (10, 6), "green").save(path)

    frame = await ImageCapture().capture(path, source_id="desk-upload")

    assert frame.source_id == "desk-upload"
    assert frame.image.size == (10, 6)
    assert frame.frame_reference is None
    assert str(path) not in frame.model_dump_json()


def test_permissions_fail_closed_and_memory_is_separate() -> None:
    controller = VisionPermissionController(
        VisionPrivacyConfig(retain_event_frames=True, frame_retention_seconds=60)
    )

    with pytest.raises(PermissionError, match="Observation permission"):
        controller.assert_observation_allowed("desk-camera", "camera")
    with pytest.raises(PermissionError, match="Memory permission"):
        controller.assert_memory_allowed("desk-camera")

    controller.grant_observation("desk-camera", "camera")
    controller.mark_source_active("desk-camera", "camera")
    assert controller.active_source_ids == ("desk-camera",)
    assert not controller.can_retain_frames("desk-camera")

    controller.grant_memory("desk-camera")
    assert controller.can_retain_frames("desk-camera")
    assert controller.approved_frame_reference("vision/events/cup.webp", source_id="desk-camera")
    assert controller.stop_all() == ("desk-camera",)
    assert not controller.is_source_active("desk-camera")


def test_persistence_preparation_redacts_sensitive_ocr_and_drops_unapproved_frame() -> None:
    controller = VisionPermissionController()
    snapshot = SceneSnapshot(
        source_id="screen",
        visible_text=["Email ana@example.com", "password: open-sesame"],
        frame_reference="vision/events/screen.webp",
    )

    prepared = controller.prepare_snapshot_for_storage(snapshot)

    assert prepared.frame_reference is None
    assert "ana@example.com" not in " ".join(prepared.visible_text)
    assert "open-sesame" not in " ".join(prepared.visible_text)
    assert "[redacted email]" in redact_sensitive_text("ana@example.com")


@pytest.mark.asyncio
async def test_lazy_ocr_and_detector_accept_injected_fakes_without_optional_packages() -> None:
    class FakeOCR:
        def ocr(self, image, cls=True):
            return [[[[0, 0], [1, 0]], ("Visible text", 0.99)]]

    class FakeBoxes:
        xyxy = [[1, 2, 8, 9]]
        conf = [0.91]
        cls = [0]

    class FakeResult:
        boxes = FakeBoxes()
        names = {0: "cup"}

    class FakeModel:
        def predict(self, image, **kwargs):
            return [FakeResult()]

    frame = VisionFrame(source_id="screen", source_type=VisionSourceType.SCREEN, image=Image.new("RGB", (10, 10)))
    ocr = PaddleOCRReader(reader=FakeOCR())
    detector = UltralyticsVisionDetector(model=FakeModel())

    assert await ocr.read(frame) == ["Visible text"]
    objects = await detector.detect(frame, prompts=["cup"])
    assert len(objects) == 1
    assert objects[0].label == "cup"
    assert objects[0].bounding_box == (1, 2, 8, 9)
