import json
from datetime import datetime, timedelta, timezone

import pytest

from ares.skills.commitments import CommitmentStore
from ares.context.conversations import ConversationStore
from ares.skills.followups import FollowUpStore
from ares.skills.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import ProactiveConfig
from ares.profile import ProfileManager
from ares.skills.proactive import ProactiveService


class FakeInitiativeLLM:
    def __init__(
        self,
        *,
        decision: str = "send",
        confidence: float = 0.95,
        reason: str = "This is a timely and useful follow-up.",
        message: str | None = "A useful follow-up.",
    ):
        self.payload = {
            "decision": decision,
            "confidence": confidence,
            "reason": reason,
            "message": message,
        }
        self.calls = 0
        self.prompts = []

    async def chat(self, messages, tools=None):
        self.calls += 1
        self.prompts.append(messages)
        assert tools == []
        return {"content": json.dumps(self.payload)}


@pytest.mark.asyncio
async def test_inactive_goal_sends_one_specific_follow_up_then_cools_down(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    goal = goals.create(
        "Launch Ares",
        priority="high",
        next_action="Run the release checklist",
    )
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    old = (now - timedelta(days=4)).isoformat().replace("+00:00", "Z")
    memory.conn.execute(
        "UPDATE goals_meta SET last_activity_at=? WHERE goal_id=?",
        (old, goal["goal_id"]),
    )
    memory.conn.commit()
    delivered = []
    llm = FakeInitiativeLLM(
        message=(
            "A quick nudge on “Launch Ares”: the next action is "
            "Run the release checklist. Want help?"
        )
    )

    async def deliver(message, candidate):
        delivered.append((message, candidate["goal_id"]))
        return ["workspace"]

    service = ProactiveService(
        goal_store=goals,
        config=ProactiveConfig(
            inactive_goal_days=3,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=deliver,
        llm_client=llm,
        now_provider=lambda: now,
    )

    first = await service.tick()
    second = await service.tick()

    assert first.decision == "send"
    assert first.channels == ("workspace",)
    assert "Launch Ares" in first.message
    assert "Run the release checklist" in first.message
    assert second.decision == "no_action"
    assert len(delivered) == 1
    assert llm.calls == 1
    assert goals.get(goal["goal_id"])["last_reminder_at"] == "2026-07-15T12:00:00Z"
    assert service.store.recent()[0]["status"] == "sent"
    memory.close()


@pytest.mark.asyncio
async def test_proactive_engine_can_choose_no_action_for_low_confidence(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    goal = goals.create("Maybe learn Rust", confidence=0.4)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    memory.conn.execute(
        "UPDATE goals_meta SET last_activity_at=? WHERE goal_id=?",
        ((now - timedelta(days=5)).isoformat().replace("+00:00", "Z"), goal["goal_id"]),
    )
    memory.conn.commit()

    service = ProactiveService(
        goal_store=goals,
        config=ProactiveConfig(
            min_confidence=0.8,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=lambda *_args: ["workspace"],
        llm_client=FakeInitiativeLLM(),
        now_provider=lambda: now,
    )
    result = await service.tick()

    assert result.decision == "no_action"
    assert "confidence" in result.reason
    assert service.store.recent() == []
    memory.close()


def test_candidate_collection_covers_deadlines_blockers_commitments_and_reflection_followups(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    followups = FollowUpStore(connection=memory.conn)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    today = now.astimezone().date()
    goals.create(
        "Recover overdue release",
        target_date=(today - timedelta(days=1)).isoformat(),
    )
    goals.create(
        "Prepare upcoming release",
        target_date=(today + timedelta(days=2)).isoformat(),
    )
    goals.create(
        "Unblock production deploy",
        blockers=["Waiting for security approval"],
    )
    commitment = commitments.create("Send the launch summary", confidence=0.96)
    memory.conn.execute(
        "UPDATE commitments_meta SET last_activity_at=? WHERE commitment_id=?",
        ("2026-07-10T12:00:00Z", commitment["commitment_id"]),
    )
    memory.conn.commit()
    follow_up = followups.create(
        "Ask whether the migration is stable",
        confidence=0.94,
        source_conversation_id="conversation-8",
        source_reflection_id="reflection-8",
        eligible_at="2026-07-15T11:00:00Z",
    )
    service = ProactiveService(
        goal_store=goals,
        commitment_store=commitments,
        follow_up_store=followups,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=lambda *_args: ["workspace"],
        llm_client=FakeInitiativeLLM(),
        now_provider=lambda: now,
    )

    candidates = service.collect_candidates(now=now)
    types = {item.candidate_type for item in candidates}
    assert {
        "goal_overdue",
        "goal_due_soon",
        "goal_blocker",
        "commitment_pending",
        "reflection_follow_up",
    } <= types
    assert any(
        item.candidate_id == str(commitment["commitment_id"])
        for item in candidates
        if item.entity_type == "commitment"
    )
    assert any(
        item.candidate_id == follow_up["follow_up_id"]
        for item in candidates
        if item.entity_type == "follow_up"
    )
    memory.close()


def test_fresh_undated_commitment_waits_for_inactivity_threshold(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "commitment-inactivity.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    commitment = commitments.create("Send the fresh summary", confidence=0.96)
    memory.conn.execute(
        "UPDATE commitments_meta SET last_activity_at=? WHERE commitment_id=?",
        (now.isoformat().replace("+00:00", "Z"), commitment["commitment_id"]),
    )
    memory.conn.commit()
    service = ProactiveService(
        goal_store=goals,
        commitment_store=commitments,
        config=ProactiveConfig(
            inactive_commitment_days=3,
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=lambda *_args: ["workspace"],
        llm_client=FakeInitiativeLLM(),
        now_provider=lambda: now,
    )

    fresh = service.collect_candidates(now=now)
    memory.conn.execute(
        "UPDATE commitments_meta SET last_activity_at=? WHERE commitment_id=?",
        ((now - timedelta(days=3)).isoformat().replace("+00:00", "Z"), commitment["commitment_id"]),
    )
    memory.conn.commit()
    inactive = service.collect_candidates(now=now)

    assert all(item.entity_type != "commitment" for item in fresh)
    assert any(
        item.candidate_type == "commitment_pending"
        and item.candidate_id == str(commitment["commitment_id"])
        and "inactive for 3 days" in item.reason
        for item in inactive
    )
    memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("days_offset", "expected_type"),
    [(-2, "goal_overdue"), (2, "goal_due_soon")],
)
async def test_deadline_goal_reminders_are_delivered_and_audited(
    tmp_path, fake_embedding_provider, days_offset, expected_type,
):
    database = tmp_path / f"{expected_type}.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    target = (now.astimezone().date() + timedelta(days=days_offset)).isoformat()
    goal = goals.create(
        f"{expected_type} release",
        target_date=target,
        next_action="Review the deployment checklist",
        confidence=0.97,
    )
    delivered = []
    llm = FakeInitiativeLLM(message="The release deadline needs attention.")

    async def deliver(message, candidate):
        delivered.append((message, candidate))
        return ["workspace"]

    service = ProactiveService(
        goal_store=goals,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=deliver,
        llm_client=llm,
        now_provider=lambda: now,
    )
    result = await service.tick()

    assert result.decision == "send"
    assert result.candidate_type == expected_type
    assert delivered[0][1]["goal_id"] == goal["goal_id"]
    assert goals.get(goal["goal_id"])["last_reminder_at"] == "2026-07-15T12:00:00Z"
    event = service.store.recent()[0]
    assert event["status"] == "sent"
    assert event["candidate"]["candidate_type"] == expected_type
    assert event["model_decision"]["decision"] == "send"
    memory.close()


@pytest.mark.asyncio
async def test_pending_commitment_uses_bounded_full_initiative_context(
    tmp_path, fake_embedding_provider,
):
    database = tmp_path / "ares.db"
    memory = MemoryStore(database, embedding_provider=fake_embedding_provider)
    goals = GoalStore(database, connection=memory.conn)
    commitments = CommitmentStore(database, connection=memory.conn)
    conversations = ConversationStore(database)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    profile.write(profile.read().replace("- Name:", "- Name: Riya"))
    memory.store("User prefers concise launch briefs", category="preference")
    goals.create("Ship the product launch", next_action="Review the release plan")
    commitment = commitments.create(
        "Prepare the concise launch brief",
        owner="user",
        confidence=0.96,
    )
    previous = conversations.start_conversation()
    conversations.add_exchange(
        previous,
        "The product launch is the current priority.",
        "Understood.",
    )
    conversations.end_conversation(previous)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    memory.conn.execute(
        "UPDATE commitments_meta SET last_activity_at=? WHERE commitment_id=?",
        ((now - timedelta(days=4)).isoformat().replace("+00:00", "Z"), commitment["commitment_id"]),
    )
    memory.conn.commit()
    llm = FakeInitiativeLLM(message="Would you like help preparing the launch brief?")
    delivered = []

    async def deliver(message, candidate):
        delivered.append((message, candidate))
        return ["workspace"]

    service = ProactiveService(
        goal_store=goals,
        commitment_store=commitments,
        memory_store=memory,
        profile_manager=profile,
        conversation_store=conversations,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            initiative_context_token_budget=1_200,
        ),
        deliver=deliver,
        llm_client=llm,
        now_provider=lambda: now,
    )
    result = await service.tick()

    assert result.decision == "send"
    assert result.candidate_type == "commitment_pending"
    assert delivered[0][1]["commitment_id"] == commitment["commitment_id"]
    assert commitments.get(commitment["commitment_id"])["last_reminder_at"] == (
        "2026-07-15T12:00:00Z"
    )
    prompt = llm.prompts[0][0]["content"]
    assert "## Initiative Candidate" in prompt
    assert "concise launch briefs" in prompt
    assert "Riya" in prompt
    assert "The product launch is the current priority" in prompt
    assert "Ship the product launch" in prompt
    event_context = service.store.recent()[0]["initiative_context"]["context"]
    assert len(event_context) <= 1_200 * 4
    conversations.close()
    memory.close()


@pytest.mark.asyncio
async def test_llm_may_choose_no_action_and_decision_cooldown_avoids_rechecking(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    goal = goals.create("Consider a release retrospective", confidence=0.95)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    memory.conn.execute(
        "UPDATE goals_meta SET last_activity_at=? WHERE goal_id=?",
        ((now - timedelta(days=5)).isoformat().replace("+00:00", "Z"), goal["goal_id"]),
    )
    memory.conn.commit()
    llm = FakeInitiativeLLM(
        decision="no_action",
        confidence=0.91,
        reason="The recent conversation shows this can wait.",
        message=None,
    )
    delivered = []
    service = ProactiveService(
        goal_store=goals,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
        ),
        deliver=lambda *args: delivered.append(args),
        llm_client=llm,
        now_provider=lambda: now,
    )

    first = await service.tick()
    second = await service.tick()

    assert first.decision == "no_action"
    assert first.event_id
    assert second.decision == "no_action"
    assert llm.calls == 1
    assert delivered == []
    event = service.store.recent()[0]
    assert event["decision"] == "no_action"
    assert event["status"] == "evaluated"
    memory.close()


@pytest.mark.asyncio
async def test_quiet_hours_prevent_model_evaluation_and_delivery(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    commitments.create("Send the pending report", confidence=0.96)
    llm = FakeInitiativeLLM()
    delivered = []
    service = ProactiveService(
        goal_store=goals,
        commitment_store=commitments,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="23:59",
        ),
        deliver=lambda *args: delivered.append(args),
        llm_client=llm,
        now_provider=lambda: datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
    )

    result = await service.tick()

    assert result.decision == "no_action"
    assert result.reason == "quiet hours are active"
    assert llm.calls == 0
    assert delivered == []
    assert service.store.recent() == []
    memory.close()


@pytest.mark.asyncio
async def test_failed_delivery_is_audited_without_marking_or_immediate_retry(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    goal = goals.create("Publish the release", confidence=0.97)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    memory.conn.execute(
        "UPDATE goals_meta SET last_activity_at=? WHERE goal_id=?",
        ((now - timedelta(days=4)).isoformat().replace("+00:00", "Z"), goal["goal_id"]),
    )
    memory.conn.commit()
    llm = FakeInitiativeLLM(message="Would you like help publishing the release?")
    attempts = 0

    async def fail_delivery(_message, _candidate):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("notification transport unavailable")

    service = ProactiveService(
        goal_store=goals,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            failed_delivery_retry_hours=2,
        ),
        deliver=fail_delivery,
        llm_client=llm,
        now_provider=lambda: now,
    )

    first = await service.tick()
    second = await service.tick()

    assert first.decision == "no_action"
    assert first.reason == "follow-up delivery failed"
    assert second.decision == "no_action"
    assert attempts == 1
    assert llm.calls == 1
    assert goals.get(goal["goal_id"])["last_reminder_at"] is None
    event = service.store.recent()[0]
    assert event["status"] == "failed"
    assert "transport unavailable" in event["reason"]
    memory.close()


@pytest.mark.asyncio
async def test_reflection_follow_up_uses_its_notification_cooldown(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    followups = FollowUpStore(connection=memory.conn)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    follow_up = followups.create(
        "Check whether the migration finished",
        confidence=0.96,
        source_conversation_id="conversation-11",
        source_reflection_id="reflection-11",
        eligible_at="2026-07-15T11:00:00Z",
        cooldown_hours=36,
    )
    llm = FakeInitiativeLLM(message="Did the migration finish successfully?")
    delivered = []
    service = ProactiveService(
        goal_store=goals,
        follow_up_store=followups,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            max_messages_per_day=5,
        ),
        deliver=lambda message, candidate: delivered.append((message, candidate)) or ["workspace"],
        llm_client=llm,
        now_provider=lambda: now,
    )

    first = await service.tick()
    second = await service.tick()

    assert first.decision == "send"
    assert first.candidate_type == "reflection_follow_up"
    assert second.decision == "no_action"
    assert len(delivered) == 1
    assert llm.calls == 1
    persisted = followups.get(follow_up["follow_up_id"])
    assert persisted["status"] == "snoozed"
    assert persisted["last_delivered_at"] == "2026-07-15T12:00:00Z"
    assert persisted["eligible_at"] == "2026-07-17T00:00:00Z"
    memory.close()


@pytest.mark.asyncio
async def test_daily_message_cap_blocks_second_candidate_and_keeps_an_audit_record(
    tmp_path, fake_embedding_provider,
):
    memory = MemoryStore(tmp_path / "ares.db", embedding_provider=fake_embedding_provider)
    goals = GoalStore(tmp_path / "ares.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "ares.db", connection=memory.conn)
    commitments.create("Send the release notes", confidence=0.96)
    commitments.create("Publish the launch brief", confidence=0.96)
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    memory.conn.execute(
        "UPDATE commitments_meta SET last_activity_at=?",
        ((now - timedelta(days=4)).isoformat().replace("+00:00", "Z"),),
    )
    memory.conn.commit()
    llm = FakeInitiativeLLM(message="Would you like help with this commitment?")
    delivered = []
    service = ProactiveService(
        goal_store=goals,
        commitment_store=commitments,
        config=ProactiveConfig(
            quiet_hours_start="00:00",
            quiet_hours_end="00:00",
            max_messages_per_day=1,
        ),
        deliver=lambda message, candidate: delivered.append((message, candidate)) or ["workspace"],
        llm_client=llm,
        now_provider=lambda: now,
    )

    first = await service.tick()
    second = await service.tick()
    third = await service.tick()

    assert first.decision == "send"
    assert second.decision == "no_action"
    assert second.reason == "daily proactive message limit reached"
    assert third.decision == "no_action"
    assert len(delivered) == 1
    assert llm.calls == 2
    events = service.store.recent()
    assert [event["status"] for event in events] == ["blocked", "sent"]
    assert events[0]["reason"] == "daily proactive message limit reached"
    memory.close()
