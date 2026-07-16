from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from ares.models import AppConfig
from ares.server import AresServer
from ares.vision.models import (
    DetectedObject,
    SceneSnapshot,
    VisionFrame,
    VisionSourceType,
    VisionWatch,
    VisualEvent,
)
from ares.vision.service import VisionService


class RecordingLLM:
    def __init__(self) -> None:
        self.messages: list[list[dict]] = []
        self.fail = False

    async def chat(self, messages, tools=None):
        self.messages.append(messages)
        if self.fail:
            raise RuntimeError("text-only provider")
        prompt = messages[-1]["content"][0]["text"]
        if "semantic visual watch" in prompt:
            return {"content": '{"matched": true, "confidence": 0.91, "evidence": ["visible change"]}'}
        if "Assess the requested visible result" in prompt:
            return {"content": '{"status": "passed", "confidence": 0.92, "evidence": ["cup is visibly connected"], "missing_evidence": []}'}
        return {"content": "A cup is visible on the desk."}


class MemoryStub:
    def close(self) -> None:
        pass


class ConversationStub:
    def delete_empty_conversations(self) -> None:
        pass

    def close(self) -> None:
        pass


class SocketStub:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, payload: str) -> None:
        self.messages.append(json.loads(payload))


class FollowUpsStub:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def create(self, description: str, **kwargs):
        item = {"description": description, **kwargs}
        self.created.append(item)
        return item


class GoalsStub:
    def __init__(self) -> None:
        self.progress_mutations = 0

    def list_all(self, *, statuses=None, limit=50):
        assert statuses == ["active", "paused"]
        return [{
            "goal_id": 7,
            "title": "Complete chemistry homework",
            "description": "Work through the chemistry notebook",
            "next_action": "Review chemistry notes",
        }]

    def record_progress(self, *args, **kwargs):
        self.progress_mutations += 1
        raise AssertionError("vision must not mutate goal progress")


class AgentStub:
    def __init__(self, service: VisionService, llm: RecordingLLM) -> None:
        self.llm = llm
        self.tool_executor = type("ToolExecutor", (), {"vision_service": service})()

    def set_model(self, _model: str) -> None:
        pass


def build_server(tmp_path: Path) -> tuple[AresServer, VisionService, AgentStub, RecordingLLM]:
    service = VisionService(database_path=tmp_path / "vision.db")
    llm = RecordingLLM()
    agent = AgentStub(service, llm)
    server = AresServer(
        config=AppConfig(data_dir=str(tmp_path), enable_desktop_notifications=False),
        agent=agent,
        memory_store=MemoryStub(),
        conversation_store=ConversationStub(),
    )
    return server, service, agent, llm


def selected_frame() -> tuple[VisionFrame, SceneSnapshot]:
    frame = VisionFrame(
        source_id="desk",
        source_type=VisionSourceType.IMAGE,
        image=Image.new("RGB", (32, 24), "white"),
    )
    snapshot = SceneSnapshot(
        source_id="desk",
        objects=[DetectedObject(label="cup", confidence=0.94, bounding_box=(1, 1, 20, 20))],
    )
    return frame, snapshot


@pytest.mark.asyncio
async def test_server_wires_selected_frame_multimodal_callbacks_with_safe_fallback(tmp_path: Path) -> None:
    server, service, _agent, llm = build_server(tmp_path)
    frame, snapshot = selected_frame()
    watch = VisionWatch(source_id="desk", condition_text="tell me when the water looks close to overflowing")

    try:
        assert service.summary_callback is not None
        assert service.semantic_watch_callback is not None
        assert service.verifier.reasoner is not None

        summary = await service.summary_callback(frame, snapshot, "Describe the desk")
        semantic = await service.semantic_watch_callback(watch, snapshot, [], frame)
        verification = await service.verifier.verify(
            "the cup is connected", snapshot, frame=frame,
        )

        assert summary == "A cup is visible on the desk."
        assert semantic["matched"] is True
        assert verification.status.value == "passed"
        assert len(llm.messages) == 3
        for messages in llm.messages:
            image_url = messages[-1]["content"][1]["image_url"]["url"]
            assert image_url.startswith("data:image/jpeg;base64,")
            assert str(tmp_path) not in image_url

        llm.fail = True
        assert await service.summary_callback(frame, snapshot, "Describe the desk") == ""
        assert (await service.semantic_watch_callback(watch, snapshot, [], frame))["matched"] is False
        fallback = await service.verifier.verify("the cup is connected", snapshot, frame=frame)
        assert fallback.status.value == "uncertain"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_server_delivers_watch_notifications_and_queues_confirmation_only_goal_suggestions(tmp_path: Path) -> None:
    server, service, agent, _llm = build_server(tmp_path)
    socket = SocketStub()
    server._connected_websockets.append(socket)
    agent.goal_store = GoalsStub()
    agent.follow_up_store = FollowUpsStub()
    event = VisualEvent(
        event_type="verification_passed",
        source_id="desk-camera",
        subject="cup",
        description="Chemistry notebook evidence is visible.",
        confidence=0.92,
        previous_state={"expected_result": "complete chemistry notebook work"},
        frame_reference=str(tmp_path / "private-frame.webp"),
    )

    try:
        await service.notifier(event, VisionWatch(source_id="desk-camera", condition_text="cup moves"))
        follow_up = await service.goal_suggestion_callback(event)
        interrupted = await service.follow_up_callback(VisualEvent(
            event_type="source_error",
            source_id="desk-camera",
            description="Visual source interrupted.",
            confidence=1.0,
        ))

        assert follow_up is not None
        assert interrupted is not None
        assert len(agent.follow_up_store.created) == 2
        assert "Complete chemistry homework" in agent.follow_up_store.created[0]["description"]
        assert agent.goal_store.progress_mutations == 0
        notification = next(item for item in socket.messages if item["type"] == "vision_notification")
        assert notification["event"]["event_type"] == "verification_passed"
        assert "frame_reference" not in json.dumps(notification)
    finally:
        await service.shutdown()
