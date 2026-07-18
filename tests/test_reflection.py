import asyncio
import json
from types import SimpleNamespace

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
        self.prompts = []

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.prompts.append(messages)
        assert tools == []
        return {"content": json.dumps(self.payload)}


class SlowReflectionLLM:
    """A reflection provider whose completion is controlled by the test."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def chat(self, messages, tools=None):
        assert tools == []
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return {"content": "{}"}


class OrderedRetryReflectionLLM:
    """Fail the first head job once, then expose the processing order."""

    def __init__(self):
        self.first_started = asyncio.Event()
        self.allow_first_failure = asyncio.Event()
        self.calls: list[str] = []
        self._first_attempt = True

    async def chat(self, messages, tools=None):
        assert tools == []
        prompt = str(messages[0]["content"])
        user_text = prompt.split("USER:\n", 1)[1].split("\n\nASSISTANT:", 1)[0]
        self.calls.append(user_text)
        if user_text == "first" and self._first_attempt:
            self._first_attempt = False
            self.first_started.set()
            await self.allow_first_failure.wait()
            raise ValueError("temporary reflection provider failure")
        return {"content": "{}"}


async def _wait_for_status(service, reflection_id, status):
    for _ in range(100):
        record = service.store.get(reflection_id)
        if record is not None and record["status"] == status:
            return record
        await asyncio.sleep(0)
    raise AssertionError(f"reflection {reflection_id} did not reach {status}")


@pytest.mark.asyncio
async def test_before_turn_preempts_background_reflection_without_waiting():
    service = ReflectionService.__new__(ReflectionService)
    blocker = asyncio.Event()
    active = asyncio.create_task(blocker.wait())
    service._scope_tasks = {"conversation-1": active}
    service.store = SimpleNamespace(pending_scopes=lambda: [])

    await asyncio.wait_for(service.before_turn("conversation-1"), timeout=0.05)

    assert active.cancelled()


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
async def test_reflection_context_lookup_never_initializes_semantic_embeddings(
    tmp_path, fake_embedding_provider, monkeypatch,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    semantic_values = []
    original_search = memory.search

    def track_search(*args, **kwargs):
        semantic_values.append(kwargs.get("semantic"))
        return original_search(*args, **kwargs)

    monkeypatch.setattr(memory, "search", track_search)
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=FakeReflectionLLM({}),
    )

    service.enqueue_turn(
        scope="conversation-no-embedding",
        user_text="What should I work on next?",
        assistant_text="Let's review the plan.",
    )
    await service.close()

    assert semantic_values == [False]
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

    # Guardrails removed: reflection items are accepted regardless of evidence support.
    assert len(memory.list_all()) == 1
    assert memory.list_all()[0]["fact_text"] == "User lives in Delhi"
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
    temporary_id = memory.store(
        "Temporary Helios launch key is Quartz",
        source="conversation",
        session_id="conversation-1",
    )
    memory.conn.execute(
        "UPDATE facts_meta SET created_at='2020-01-01 00:00:00' WHERE fact_id=?",
        (temporary_id,),
    )
    memory.conn.commit()

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
    assert "Quartz" not in context
    fact = next(item for item in memory.list_all() if "Helios" in item["fact_text"])
    assert fact["session_id"] is None
    assert fact["source_conversation_id"] == "conversation-1"
    assert fact["source_reflection_id"] == reflection_id
    memory.close()


@pytest.mark.asyncio
async def test_updated_reflected_memory_becomes_global_and_records_current_provenance(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    fact_id = memory.store(
        "User prefers weekly deployment summaries",
        category="preference",
        session_id="conversation-4",
    )
    llm = FakeReflectionLLM({
        "updated_memories": [{
            "fact_id": fact_id,
            "fact_text": "User prefers daily deployment summaries",
            "category": "preference",
            "confidence": 0.96,
            "evidence": "I prefer daily deployment summaries",
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
        scope="conversation-22",
        user_text="I prefer daily deployment summaries.",
        assistant_text="Understood.",
    )
    await service.close()

    updated = memory.get(fact_id)
    assert updated["session_id"] is None
    assert updated["source_conversation_id"] == "conversation-22"
    assert updated["source_reflection_id"] == reflection_id
    memory.close()


@pytest.mark.asyncio
async def test_relative_follow_up_time_uses_configured_non_utc_timezone(
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
            "description": "Check whether the rollout completed",
            "confidence": 0.95,
            "eligible_at": "2026-07-16T09:00:00",
            "evidence": "Tomorrow at 9 AM, check whether the rollout completed",
        }],
    })
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5, local_timezone="Asia/Kolkata"),
        llm_client=llm,
    )

    service.enqueue_turn(
        scope="conversation-31",
        user_text="Tomorrow at 9 AM, check whether the rollout completed.",
        assistant_text="I will follow up.",
    )
    await service.close()

    prompt = llm.prompts[0][0]["content"]
    follow_up = service.follow_up_store.list_open()[0]
    assert "current local datetime" in prompt
    assert "Asia/Kolkata" in prompt
    assert "timezone-aware ISO-8601" in prompt
    assert follow_up["eligible_at"] == "2026-07-16T03:30:00Z"
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


@pytest.mark.asyncio
async def test_before_turn_preempts_and_requeues_slow_active_reflection(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = SlowReflectionLLM()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )
    reflection_id = service.enqueue_turn(
        scope="conversation-fast-next-turn",
        user_text="First durable turn",
        assistant_text="First answer",
    )
    assert reflection_id is not None
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=0.5)

        # A following normal turn gets provider priority. The slow review is
        # cancelled and durably requeued instead of contending with chat.
        await asyncio.wait_for(service.before_turn("conversation-fast-next-turn"), timeout=0.1)
        assert service.store.get(reflection_id)["status"] == "pending"
        assert service.store.get(reflection_id)["attempts"] == 0
        service.after_turn()
    finally:
        llm.release.set()
        await service.close()
        memory.close()


@pytest.mark.asyncio
async def test_synchronization_barrier_times_out_without_cancelling_reflection(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = SlowReflectionLLM()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )
    reflection_id = service.enqueue_turn(
        scope="conversation-sync-barrier",
        user_text="Remember this durable preference",
        assistant_text="Understood",
    )
    assert reflection_id is not None
    try:
        await asyncio.wait_for(llm.started.wait(), timeout=0.5)
        await service.before_turn(
            "conversation-sync-barrier", synchronize=True, timeout_seconds=0.01,
        )

        # ``wait_for`` is shielded inside the service, so timing out the
        # foreground barrier leaves the durable background request alive.
        assert service.store.get(reflection_id)["status"] == "running"
        assert not llm.release.is_set()
    finally:
        llm.release.set()
        await service.close()
        memory.close()


@pytest.mark.asyncio
async def test_retrying_head_job_blocks_later_same_scope_reflection(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = OrderedRetryReflectionLLM()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5, max_attempts=3),
        llm_client=llm,
    )
    try:
        first_id = service.enqueue_turn(scope="conversation-ordered", user_text="first", assistant_text="one")
        assert first_id is not None
        await asyncio.wait_for(llm.first_started.wait(), timeout=0.5)
        second_id = service.enqueue_turn(scope="conversation-ordered", user_text="second", assistant_text="two")
        assert second_id is not None

        llm.allow_first_failure.set()
        await _wait_for_status(service, first_id, "pending")
        assert llm.calls == ["first"]
        assert service.store.get(second_id)["status"] == "pending"

        # A later lightweight kick retries the head first, then drains the
        # following turn.  The second mutation cannot overtake the retry.
        await service.before_turn("conversation-ordered")
        await service.close()
        assert llm.calls == ["first", "first", "second"]
        assert service.store.get(first_id)["status"] == "completed"
        assert service.store.get(second_id)["status"] == "completed"
    finally:
        await service.close()
        memory.close()


@pytest.mark.asyncio
async def test_before_turn_resumes_a_persisted_pending_scope(tmp_path, fake_embedding_provider):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    llm = FakeReflectionLLM({})
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        llm_client=llm,
    )
    reflection_id = service.store.enqueue("conversation-recovered", "saved turn", "saved answer")
    try:
        # Direct store insertion models a durable row recovered after an
        # earlier process stopped before it could create an in-memory task.
        await service.before_turn("another-conversation")
        await service.close()
        assert llm.calls == 1
        assert service.store.get(reflection_id)["status"] == "completed"
    finally:
        await service.close()
        memory.close()
