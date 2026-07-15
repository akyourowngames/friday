from datetime import datetime, timedelta, timezone

import pytest

from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.models import ProactiveConfig
from ares.proactive import ProactiveService


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
        now_provider=lambda: now,
    )
    result = await service.tick()

    assert result.decision == "no_action"
    assert "confidence" in result.reason
    assert service.store.recent() == []
    memory.close()
