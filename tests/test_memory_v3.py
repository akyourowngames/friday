"""Memory V3 integration coverage using production stores and real SQLite files."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ares.commitments import CommitmentStore
from ares.embeddings import EmbeddingProvider
from ares.goals import GoalStore
from ares.memory import MemoryStore
from ares.memory_lifecycle import MemoryLifecycleStore, MemoryPromotionService
from ares.memory_retrieval import MemoryRecallService
from ares.models import AppConfig, ReflectionConfig
from ares.profile import ProfileManager
from ares.reflection import ReflectionService, ReflectionStore
from ares.self_improvement import SelfImprovementStore


def _real_store(path):
    return MemoryStore(path, embedding_provider=EmbeddingProvider(backend="hash"))


def test_automatic_memory_lifecycle_has_no_policy_or_approval_gate(tmp_path):
    config = AppConfig().memory
    memory = _real_store(tmp_path / "memory-v3.db")
    lifecycle = MemoryLifecycleStore(memory, config)
    promotion = MemoryPromotionService(lifecycle, config.promotion)
    try:
        staged = lifecycle.stage_observation(
            "Delhi weather is rainy tonight",
            category="fact",
            confidence=0.1,
            importance=0.2,
            evidence="model-selected turn evidence",
            evidence_grounded=False,
            source_conversation_id="conversation-real-1",
            source_reflection_id="reflection-real-1",
        )
        decision = promotion.evaluate(int(staged["candidate"]["candidate_id"]))

        assert decision["action"] == "promoted"
        assert decision["explanation"]["automatic"] is True
        fact = memory.get(int(decision["fact_id"]))
        assert fact["fact_text"] == "Delhi weather is rainy tonight"
        assert fact["source_candidate_id"] == staged["candidate"]["candidate_id"]

        results = memory.search("rainy tonight", semantic=True)
        assert results[0]["fact_id"] == fact["fact_id"]
        assert memory.explain_last_retrieval()["selected_ids"] == [fact["fact_id"]]
        memory.flush_access_stats()
        refreshed_candidate = lifecycle.get_candidate(int(staged["candidate"]["candidate_id"]))
        assert refreshed_candidate["unique_query_count"] == 1
        assert refreshed_candidate["average_relevance"] > 0
    finally:
        memory.close()


def test_hermes_self_improvement_requires_review_before_retrieval(tmp_path):
    memory = _real_store(tmp_path / "learning.db")
    config = AppConfig().memory.self_improvement
    learning = SelfImprovementStore(memory.conn, config)
    try:
        row = learning.stage(
            title="Verify migrations against old databases",
            kind="workflow",
            summary="Open a pre-upgrade SQLite schema and exercise read, write, archive, and restore.",
            rationale="A schema-only unit test missed a live migration issue.",
            evidence="test it with real things",
            evidence_grounded=True,
            confidence=0.2,
            existing_skill=None,
            source_conversation_id="conversation-real-2",
            source_reflection_id="reflection-real-2",
        )

        assert row is not None
        assert row["status"] == "pending_approval"
        assert learning.search("test the SQLite migration", limit=3) == []
        assert learning.list(status="pending_approval")[0]["improvement_id"] == row["improvement_id"]

        reopened = SelfImprovementStore(memory.conn, config)
        assert reopened.get(row["improvement_id"])["status"] == "pending_approval"
        approved = reopened.approve(row["improvement_id"])
        assert approved["status"] == "active"
        results = learning.search("test the SQLite migration", limit=3)
        assert results[0]["improvement_id"] == row["improvement_id"]
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_real_reflection_flow_writes_memory_and_procedure_automatically(tmp_path):
    class CompletedTurnModel:
        async def chat(self, messages, tools=None):
            assert tools == []
            return {
                "content": json.dumps({
                    "new_memories": [{
                        "fact_text": "User wants real end-to-end validation",
                        "category": "preference",
                        "importance": 0.9,
                        "confidence": 0.3,
                        "evidence": "test it with real things",
                    }],
                    "skill_learnings": [{
                        "title": "Prefer real integration checks",
                        "kind": "workflow",
                        "summary": "Exercise production stores and migrations before reporting completion.",
                        "rationale": "The user explicitly asked for real validation.",
                        "evidence": "test it with real things",
                        "confidence": 0.3,
                    }],
                })
            }

    memory = _real_store(tmp_path / "reflection-real.db")
    goals = GoalStore(tmp_path / "reflection-real.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "reflection-real.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        memory_config=AppConfig().memory,
        llm_client=CompletedTurnModel(),
    )
    try:
        reflection_id = service.enqueue_turn(
            scope="conversation-real-3",
            user_text="Don't add guardrails or approvals; test it with real things.",
            assistant_text="Understood; I will validate the production flow.",
        )
        assert reflection_id is not None
        await service.close()

        run = service.store.get(reflection_id)
        assert run["status"] == "completed"
        assert memory.search("real end-to-end validation", semantic=False)
        pending = service.self_improvement_store.list(status="pending_approval")
        assert pending and pending[0]["status"] == "pending_approval"
        assert service.self_improvement_store.search("production validation migrations") == []
        service.self_improvement_store.approve(pending[0]["improvement_id"])
        learned = service.self_improvement_store.search("production validation migrations")
        assert learned and learned[0]["status"] == "active"
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_explicit_remember_and_user_correction_survive_empty_model_review(tmp_path):
    class EmptyReviewModel:
        async def chat(self, messages, tools=None):
            return {"content": "{}"}

    memory = _real_store(tmp_path / "empty-review.db")
    goals = GoalStore(tmp_path / "empty-review.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "empty-review.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5),
        memory_config=AppConfig().memory,
        llm_client=EmptyReviewModel(),
    )
    try:
        reflection_id = service.enqueue_turn(
            scope="conversation-real-fallback",
            user_text=(
                "Remember that automatic memory should remain enabled. "
                "Don't wait for approval when working on memory upgrades."
            ),
            assistant_text="Understood.",
        )
        await service.close()

        assert service.store.get(reflection_id)["status"] == "completed"
        assert memory.search("automatic memory enabled", semantic=False)
        learnings = service.self_improvement_store.list(status="pending_approval")
        assert learnings
        assert "Don't wait for approval" in learnings[0]["summary"]
    finally:
        memory.close()


def test_compaction_checkpoint_is_idempotent_in_real_sqlite(tmp_path):
    memory = _real_store(tmp_path / "checkpoint.db")
    store = ReflectionStore(memory.conn)
    try:
        first = store.enqueue_compaction(
            "conversation-real-4",
            "Remember the migration result",
            "The old database upgraded successfully",
            "same-checkpoint",
        )
        second = store.enqueue_compaction(
            "conversation-real-4",
            "Remember the migration result",
            "The old database upgraded successfully",
            "same-checkpoint",
        )

        assert first is not None
        assert second is None
        assert store.get(first)["job_type"] == "compaction"
        assert len(store.pending(scope="conversation-real-4")) == 1
    finally:
        memory.close()


def test_pre_v3_sqlite_schema_migrates_and_remains_reversible(tmp_path):
    path = tmp_path / "old-ares.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE facts_meta (
            fact_id INTEGER PRIMARY KEY,
            fact_text TEXT NOT NULL,
            category TEXT DEFAULT 'note',
            confidence REAL DEFAULT 1.0,
            created_at TEXT,
            last_accessed TEXT,
            access_count INTEGER DEFAULT 0,
            superseded_by INTEGER
        )"""
    )
    conn.execute(
        """INSERT INTO facts_meta
           (fact_id, fact_text, category, confidence, created_at, access_count)
           VALUES (1, 'Legacy user preference', 'preference', 0.8,
                   '2025-01-01T00:00:00+00:00', 0)"""
    )
    conn.commit()
    conn.close()

    memory = _real_store(path)
    try:
        columns = {
            row["name"] for row in memory.conn.execute("PRAGMA table_info(facts_meta)")
        }
        assert {"archived_at", "source_candidate_id", "revision", "tags_json"} <= columns
        assert memory.get(1)["fact_text"] == "Legacy user preference"
        assert memory.archive(1, reason="migration-test") is True
        assert memory.get(1)["archived_at"] is not None
        assert memory.restore(1) is True
        assert memory.get(1)["archived_at"] is None
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_real_hybrid_recall_rewrites_and_judges_supplied_ids(tmp_path):
    class RecallModel:
        async def chat(self, messages, tools=None):
            prompt = messages[0]["content"]
            if "retrieval-only question" in prompt:
                return {"content": json.dumps({
                    "query": "What decisions remain for the Ares memory project?"
                })}
            marker = "RETRIEVED_MEMORY_DATA="
            candidates = json.loads(prompt.split(marker, 1)[1])
            return {"content": json.dumps({
                "memory_ids": [candidates[0]["memory_id"]]
            })}

    memory = _real_store(tmp_path / "recall-real.db")
    config = AppConfig().memory.retrieval.model_copy(
        update={"foreground_model_calls_enabled": True}
    )
    try:
        relevant_id = memory.store(
            "The Ares memory project needs migration and compaction verification",
            category="project",
            importance=0.9,
        )
        memory.store("User likes masala chai", category="preference")
        recall = MemoryRecallService(memory, RecallModel(), config)

        result = await recall.prepare(
            "Can we continue that?",
            [{"role": "assistant", "content": "We were validating the Ares memory project."}],
            limit=3,
            scope="all",
            session_id=None,
            recent_sessions=3,
        )

        assert result.retrieval_query == "What decisions remain for the Ares memory project?"
        assert [item["fact_id"] for item in result.memories] == [relevant_id]
        assert result.diagnostics["selected_ids"] == [relevant_id]
        assert result.diagnostics["search"]["mode"] in {"hybrid", "vector", "fts"}
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_active_recall_timeout_fails_open_without_injecting_memory(tmp_path):
    class NeverReturns:
        async def chat(self, messages, tools=None):
            await __import__("asyncio").sleep(60)

    memory = _real_store(tmp_path / "recall-timeout.db")
    memory.store("Ares timeout test memory", category="project")
    config = SimpleNamespace(
        query_rewrite_enabled=False,
        active_judge_enabled=True,
        vector_weight=0.55,
        keyword_weight=0.30,
        metadata_weight=0.15,
        mmr_enabled=True,
        mmr_lambda=0.70,
        temporal_decay_enabled=True,
        max_candidates=10,
        max_injected=3,
        timeout_seconds=0.01,
        foreground_model_calls_enabled=True,
    )
    try:
        recall = MemoryRecallService(memory, NeverReturns(), config)
        result = await recall.prepare(
            "Tell me about the Ares timeout test",
            [],
            limit=3,
            scope="all",
            session_id=None,
            recent_sessions=3,
        )
        assert result.memories == []
        assert result.diagnostics["judge"]["fallback"] == "timeout"
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_foreground_recall_uses_zero_model_calls_and_local_rank(tmp_path):
    class MustNotRun:
        async def chat(self, messages, tools=None):
            raise AssertionError("foreground recall called a model")

    memory = _real_store(tmp_path / "fast-recall.db")
    config = AppConfig().memory.retrieval
    try:
        fact_id = memory.store(
            "Ares memory project migration compaction verification",
            category="project",
        )
        recall = MemoryRecallService(memory, MustNotRun(), config)
        started = __import__("time").perf_counter()
        result = await recall.prepare(
            "Can we continue that?",
            [{"role": "assistant", "content": "We were validating the Ares memory project."}],
            limit=3,
            scope="all",
            session_id=None,
            recent_sessions=3,
        )
        elapsed_ms = (__import__("time").perf_counter() - started) * 1_000

        assert [item["fact_id"] for item in result.memories] == [fact_id]
        assert result.diagnostics["foreground_model_calls"] == 0
        assert result.diagnostics["judge"]["fallback"] == "local-rank"
        assert elapsed_ms < 250
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_embedding_warmup_is_background_and_keeps_foreground_model_free(tmp_path):
    class MustNotRun:
        async def chat(self, messages, tools=None):
            raise AssertionError("embedding warmup called the chat model")

    memory = _real_store(tmp_path / "warm-recall.db")
    try:
        memory.store("Hermes outcome aware review", category="project")
        recall = MemoryRecallService(memory, MustNotRun(), AppConfig().memory.retrieval)
        recall.schedule_warmup()
        assert recall._warmup_task is not None
        await recall._warmup_task

        result = await recall.prepare(
            "outcome review",
            [],
            limit=3,
            scope="all",
            session_id=None,
            recent_sessions=3,
        )
        assert result.diagnostics["embedding_warmup"]["status"] == "ready"
        assert result.diagnostics["search"]["mode"] == "hybrid"
        assert result.diagnostics["foreground_model_calls"] == 0
        await recall.close()
    finally:
        memory.close()


@pytest.mark.asyncio
async def test_outcome_aware_review_receives_real_tool_result_and_stages_learning(tmp_path):
    class OutcomeModel:
        async def chat(self, messages, tools=None):
            prompt = messages[0]["content"]
            assert '"tool": "shell"' in prompt
            assert "12 passed" in prompt
            return {"content": json.dumps({
                "skill_learnings": [{
                    "title": "Verify focused tests",
                    "kind": "workflow",
                    "summary": "Run the focused suite before reporting completion.",
                    "rationale": "The actual command result confirmed the change.",
                    "evidence": "12 passed",
                    "confidence": 0.95,
                }],
            })}

    memory = _real_store(tmp_path / "outcome-review.db")
    goals = GoalStore(tmp_path / "outcome-review.db", connection=memory.conn)
    commitments = CommitmentStore(tmp_path / "outcome-review.db", connection=memory.conn)
    profile = ProfileManager(tmp_path)
    profile.ensure_exists()
    service = ReflectionService(
        memory_store=memory,
        goal_store=goals,
        commitment_store=commitments,
        profile_manager=profile,
        config=ReflectionConfig(timeout_seconds=5, idle_delay_seconds=0),
        memory_config=AppConfig().memory,
        llm_client=OutcomeModel(),
    )
    try:
        reflection_id = service.enqueue_turn(
            scope="outcome-real",
            user_text="Run the focused tests.",
            assistant_text="The focused tests passed.",
            outcome_summary=json.dumps({
                "tool_outcomes": [{"tool": "shell", "status": "completed", "result": "12 passed"}]
            }),
        )
        await service.close()

        run = service.store.get(reflection_id)
        extracted = json.loads(run["extracted_json"])
        outcomes = json.loads(run["outcomes_json"])
        assert run["outcome_summary"]
        assert extracted["outcome_reviews"][0]["status"] == "succeeded"
        assert any(item["kind"] == "outcome_review" for item in outcomes)
        pending = service.self_improvement_store.list(status="pending_approval")
        assert pending and pending[0]["title"] == "Verify focused tests"
    finally:
        memory.close()
