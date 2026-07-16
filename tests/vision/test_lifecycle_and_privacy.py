"""Regression coverage for VisionService lifecycle and privacy boundaries."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from PIL import Image

from ares.models import VisionConfig
from ares.vision.models import VisionFrame, VisionSourceType
from ares.vision.service import VisionService
from ares.vision.verifier import VisionVerifier


class EmptyDetector:
    async def detect(self, _frame, prompts=None):
        return []


class EmptyOCR:
    async def read(self, _frame):
        return []


class RecordingCapture:
    """In-memory capture that records the privacy state visible during capture."""

    def __init__(self) -> None:
        self.service: VisionService | None = None
        self.start_calls = 0
        self.capture_calls = 0
        self.close_calls = 0
        self.active_samples: list[tuple[bool, bool, str | None]] = []
        self.captured = asyncio.Event()

    async def start(self) -> None:
        self.start_calls += 1

    async def capture(self, *, source_id: str, source_type: VisionSourceType) -> VisionFrame:
        self.capture_calls += 1
        assert self.service is not None
        permission = self.service.store.get_permission(source_id)
        source = self.service.store.get_source(source_id)
        self.active_samples.append((
            permission["active_indicator"],
            self.service.privacy.is_source_active(source_id),
            source.status if source is not None else None,
        ))
        self.captured.set()
        return VisionFrame(
            source_id=source_id,
            source_type=source_type,
            image=Image.new("RGB", (16, 12), "white"),
        )

    async def close(self) -> None:
        self.close_calls += 1


def make_service(tmp_path: Path, **kwargs) -> VisionService:
    return VisionService(
        database_path=tmp_path / "vision.db",
        detector=EmptyDetector(),
        ocr=EmptyOCR(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_one_shot_camera_observation_is_indicated_and_closes_capture(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    capture = RecordingCapture()
    capture.service = service
    service.create_source(
        source_id="desk-camera",
        source_type=VisionSourceType.CAMERA,
        grant_observe=True,
        capture=capture,
    )

    try:
        observation = await service.observe(
            source=VisionSourceType.CAMERA,
            source_id="desk-camera",
            include_ocr=False,
        )

        assert observation.snapshot.source_id == "desk-camera"
        assert capture.start_calls == 1
        assert capture.capture_calls == 1
        assert capture.active_samples == [(True, True, "active")]
        assert capture.close_calls == 1
        assert service.store.get_permission("desk-camera")["active_indicator"] is False
        assert service.privacy.is_source_active("desk-camera") is False
        assert service.store.get_source("desk-camera").status == "stopped"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_live_source_shutdown_closes_capture_and_clears_indicator(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    capture = RecordingCapture()
    capture.service = service
    service.create_source(
        source_id="live-camera",
        source_type=VisionSourceType.CAMERA,
        grant_observe=True,
        capture=capture,
    )

    await service.start_source("live-camera", check_interval_seconds=60)
    try:
        await asyncio.wait_for(capture.captured.wait(), timeout=2)
        assert service.store.get_permission("live-camera")["active_indicator"] is True
        assert service.privacy.is_source_active("live-camera") is True
    finally:
        await service.shutdown()

    assert capture.close_calls == 1
    assert service.privacy.is_source_active("live-camera") is False
    assert "live-camera" not in service._capture_tasks
    runtime = service._runtime.get("live-camera")
    assert runtime is None or runtime.latest_image is None


@pytest.mark.asyncio
async def test_live_verification_uses_one_current_frame_then_releases_transient_capture(tmp_path: Path) -> None:
    selected_frames: list[VisionFrame | None] = []

    async def reasoner(_expected, _snapshot, _reference, *, frame=None):
        selected_frames.append(frame)
        return {
            "status": "passed",
            "confidence": 0.93,
            "evidence": ["Current camera frame was evaluated."],
            "missing_evidence": [],
        }

    service = make_service(tmp_path, verifier=VisionVerifier(reasoner=reasoner))
    capture = RecordingCapture()
    capture.service = service
    service.create_source(
        source_id="verify-camera",
        source_type=VisionSourceType.CAMERA,
        grant_observe=True,
        capture=capture,
    )

    try:
        result = await service.verify(
            expected_result="the USB cable is connected",
            source_id="verify-camera",
            source=VisionSourceType.CAMERA,
        )

        assert result.status.value == "passed"
        assert selected_frames and selected_frames[0] is not None
        assert capture.active_samples == [(True, True, "active")]
        assert capture.close_calls == 1
        assert service.privacy.is_source_active("verify-camera") is False
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_sensitive_llm_summary_is_redacted_before_durable_storage(tmp_path: Path) -> None:
    async def sensitive_summary(_frame, _snapshot, _prompt):
        return "Password: open-sesame. Contact ana@example.com."

    service = make_service(tmp_path, summary_callback=sensitive_summary)
    try:
        await service.observe(
            frame=VisionFrame(
                source_id="uploaded-desk",
                source_type=VisionSourceType.IMAGE,
                image=Image.new("RGB", (16, 12), "white"),
            ),
            reasoning_prompt="Describe the desk.",
            include_ocr=False,
        )
        snapshot = service.store.latest_snapshot("uploaded-desk")

        assert snapshot is not None
        assert "open-sesame" not in (snapshot.summary or "")
        assert "ana@example.com" not in (snapshot.summary or "")
        assert "[redacted]" in (snapshot.summary or "").casefold()
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_request",
    [
        "Identify the person in this photo.",
        "What emotion is the person feeling?",
    ],
)
async def test_face_and_emotion_requests_are_rejected(tmp_path: Path, user_request: str) -> None:
    service = make_service(tmp_path)
    try:
        with pytest.raises(ValueError, match="does not perform face identification"):
            await service.verify(expected_result=user_request)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_snapshot_history_is_bounded_per_source(tmp_path: Path) -> None:
    service = make_service(tmp_path, config=VisionConfig(snapshot_history=2))
    try:
        for colour in ("red", "green", "blue", "yellow", "purple"):
            await service.observe(
                frame=VisionFrame(
                    source_id="desk",
                    source_type=VisionSourceType.IMAGE,
                    image=Image.new("RGB", (16, 12), colour),
                ),
                include_ocr=False,
            )

        snapshots = service.store.list_snapshots("desk", limit=10)
        assert len(snapshots) == 2
        assert all(snapshot.source_id == "desk" for snapshot in snapshots)
    finally:
        await service.shutdown()
