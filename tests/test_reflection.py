import json

import pytest

from ares.commitments import CommitmentStore
from ares.context import ProjectContext
from ares.followups import FollowUpStore
from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import AppConfig, ReflectionConfig
from ares.profile import ProfileManager
from ares.reflection import ReflectionService
from ares.soul import SoulManager
from ares.user_context import build_user_context


class FakeReflectionLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def chat(self, _messages, tools=None):
        self.calls += 1
        assert tools == []
        return {"content": json.dumps(self.payload)}


@pytest.mark.asyncio
async def test_reflection_safely_applies_supported_state_changes(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = FakeReflectionLLM({
        "new_memories": [{
            "fact_text": "User prefers dark mode",
            "category": "preference",
            "importance": 0.8,
            "confidence": 0.95,
            "evidence": "I prefer dark mode",
        }],
        "updated_memories": [],
        "new_goals": [{
            "title": "Ship Ares reflection",
            "description": "Complete the reflection flow",
            "priority": "high",
            "milestones": [{"title": "Validate extraction"}, {"title": "Ship tests"}],
            "next_action": "Run the reflection tests",
            "confidence": 0.94,
            "evidence": "My goal is to ship Ares reflection",
        }],
        "goal_progress": [],
        "completed_goals": [],
        "profile_updates": [{
            "section": "Preferences",
            "key": "Theme",
            "value": "Dark",
            "confidence": 0.95,
            "evidence": "I prefer dark mode",
        }],
        "commitments": [{
            "description": "Send the test report tomorrow",
            "owner": "user",
            "status": "pending",
            "confidence": 0.92,
            "evidence": "I will send the test report tomorrow",
        }],
        "follow_up_opportunities": [],
    })
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )

    reflection_id = service.enqueue_turn(
        scope="conversation-7",
        user_text=(
            "I prefer dark mode. My goal is to ship Ares reflection. "
            "I will send the test report tomorrow."
        ),
        assistant_text="Understood.",
    )
    await service.close()

    assert llm.calls == 1
    assert service.store.get(reflection_id)["status"] == "completed"
    reflected_memory = memory.list_all()[0]
    assert reflected_memory["fact_text"] == "User prefers dark mode"
    assert reflected_memory["session_id"] is None
    assert reflected_memory["source_conversation_id"] == "conversation-7"
    assert reflected_memory["source_reflection_id"] == reflection_id
    goal = goals.search("reflection")[0]
    assert goal["next_action"] == "Run the reflection tests"
    assert [item["title"] for item in goal["milestones"]] == [
        "Validate extraction", "Ship tests",
    ]
    assert commitments.list_pending()[0]["description"] == "Send the test report tomorrow"
    assert "- Theme: Dark" in profile.read()
    memory.close()


@pytest.mark.asyncio
async def test_reflection_rejects_unsupported_evidence(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = FakeReflectionLLM({
        "new_memories": [{
            "fact_text": "User lives in Delhi",
            "category": "fact",
            "confidence": 0.99,
            "evidence": "I live in Delhi",
        }],
    })
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )

    reflection_id = service.enqueue_turn(
        scope="conversation-2",
        user_text="Tell me about Delhi.",
        assistant_text="You may live in Delhi.",
    )
    await service.close()

    assert memory.list_all() == []
    run = service.store.get(reflection_id)
    assert "unsupported_evidence" in run["outcomes_json"]
    memory.close()


@pytest.mark.asyncio
async def test_reflected_memory_remains_retrievable_across_unbounded_future_sessions(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    profile = ProfileManager(tmp_path)
    soul = SoulManager(tmp_path)
    profile.ensure_exists()
    soul.ensure_exists()
    llm = FakeReflectionLLM({
        "new_memories": [{
            "fact_text": "User's deployment codename is Helios",
            "category": "fact",
            "confidence": 0.98,
            "importance": 0.9,
            "evidence": "My deployment codename is Helios",
        }],
    })
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )
    reflection_id = service.enqueue_turn(
        scope="conversation-1",
        user_text="My deployment codename is Helios.",
        assistant_text="I will remember that.",
    )
    await service.close()

    for index in range(2, 52):
        memory.store(
            f"Conversation {index} temporary deployment note",
            session_id=f"conversation-{index}",
        )

    context = build_user_context(
        "What is my deployment codename?",
        config=AppConfig(
            data_dir=str(tmp_path),
            project_context_enabled=False,
            model="deepseek-v4-flash-free",
        ),
        soul_manager=soul,
        profile_manager=profile,
        project_context=ProjectContext(enabled=False),
        memory_store=memory,
        session_id="conversation-999",
        goal_store=goals,
        commitment_store=commitments,
        conversation_history=[],
    )

    assert "deployment codename is Helios" in context
    fact = next(item for item in memory.list_all() if "Helios" in item["fact_text"])
    assert fact["session_id"] is None
    assert fact["source_conversation_id"] == "conversation-1"
    assert fact["source_reflection_id"] == reflection_id
    memory.close()


@pytest.mark.asyncio
async def test_reflection_follow_up_queue_persists_lifecycle_and_resolution(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = FakeReflectionLLM({
        "follow_up_opportunities": [{
            "description": "Check whether the production migration completed",
            "confidence": 0.93,
            "cooldown_hours": 36,
            "evidence": "Check with me after the production migration",
        }],
    })
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5, follow_up_delay_hours=0),
        llm_client=llm,
    )
    reflection_id = service.enqueue_turn(
        scope="conversation-41",
        user_text="Check with me after the production migration.",
        assistant_text="I will follow up.",
    )
    await service.close()
    queued = service.follow_up_store.list_open()
    assert len(queued) == 1
    follow_up_id = queued[0]["follow_up_id"]
    memory.close()

    reopened = FollowUpStore(database)
    persisted = reopened.get(follow_up_id)
    assert persisted["status"] == "pending"
    assert persisted["confidence"] == pytest.approx(0.93)
    assert persisted["source_conversation_id"] == "conversation-41"
    assert persisted["source_reflection_id"] == reflection_id
    assert persisted["created_at"]
    assert persisted["eligible_at"]
    assert persisted["cooldown_hours"] == 36
    assert persisted["resolution"] == ""
    reopened.resolve(
        follow_up_id,
        status="resolved",
        resolution="The production migration completed successfully.",
    )
    reopened.close()

    final_store = FollowUpStore(database)
    resolved = final_store.get(follow_up_id)
    assert resolved["status"] == "resolved"
    assert resolved["resolution"] == "The production migration completed successfully."
    assert resolved["resolved_at"]
    final_store.close()
