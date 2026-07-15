import json

import pytest

from ares.commitments import CommitmentStore
from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import ReflectionConfig
from ares.profile import ProfileManager
from ares.reflection import ReflectionService


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
    assert memory.list_all()[0]["fact_text"] == "User prefers dark mode"
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
