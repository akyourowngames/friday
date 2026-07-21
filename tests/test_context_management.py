"""Context management integration and smoke tests."""

import json

from ares.context.blend import TokenEstimator, estimate_tokens, format_memories, truncate_to_tokens
from ares.tool_truncator import ToolTruncator
from ares.context.compactor import ContextCompactor
from ares.memory.extractor import MemoryExtractor
from ares.memory.cleaner import MemoryCleaner
from ares.context.manager import ContextManager
from ares.models import AppConfig


# ── Helpers ────────────────────────────────────────────────────────────

class FakeLLM:
    def __init__(self, response="Summary of the conversation."):
        self._response = response

    async def chat(self, messages, tools=None):
        return {"content": self._response}


class FakeMemoryStore:
    def __init__(self):
        self._facts = []
        self._next_id = 1

    def store(self, fact_text, category="note", confidence=1.0, importance=0.5, source="conversation"):
        fid = self._next_id
        self._facts.append({"fact_id": fid, "fact_text": fact_text, "category": category,
                            "importance": importance, "confidence": confidence, "source": source,
                            "created_at": "2026-06-20T00:00:00", "access_count": 0})
        self._next_id += 1
        return fid

    def list_all(self):
        return list(self._facts)

    def search(self, query, limit=5):
        return self._facts[:limit]

    def delete(self, fact_id):
        self._facts = [f for f in self._facts if f["fact_id"] != fact_id]
        return True

    def update(self, fact_id, **kwargs):
        for f in self._facts:
            if f["fact_id"] == fact_id:
                f.update(kwargs)
                return True
        return False

    def get(self, fact_id):
        for f in self._facts:
            if f["fact_id"] == fact_id:
                return f
        return None


def _make_history(n_messages=30):
    """Create a synthetic conversation history."""
    history = [{"role": "system", "content": "You are Ares."}]
    for i in range(n_messages):
        history.append({"role": "user", "content": f"User message {i}"})
        history.append({"role": "assistant", "content": f"Assistant reply {i}"})
    return history


def _make_tool_history(n_tools=10, tool_size=1000):
    """Create history with large tool outputs."""
    history = [{"role": "system", "content": "You are Ares."}]
    for i in range(n_tools):
        history.append({"role": "user", "content": f"Do task {i}"})
        history.append({"role": "tool", "content": "x" * tool_size})
        history.append({"role": "assistant", "content": f"Done task {i}"})
    return history


# ── Task 1: TokenEstimator ────────────────────────────────────────────

class TestTokenEstimator:
    def test_estimate_text_prose(self):
        est = TokenEstimator()
        tokens = est.estimate_text("hello world foo bar", "text")
        assert tokens > 0
        assert tokens == int(4 * 1.3)

    def test_estimate_text_code(self):
        est = TokenEstimator()
        tokens = est.estimate_text("def foo(): pass", "code")
        assert tokens > 0

    def test_estimate_text_tool_output(self):
        est = TokenEstimator()
        tokens = est.estimate_text("output here", "tool_output")
        assert tokens > 0

    def test_estimate_text_empty(self):
        est = TokenEstimator()
        assert est.estimate_text("", "text") == 0
        assert est.estimate_text("   ", "text") == 0

    def test_estimate_message_basic(self):
        est = TokenEstimator()
        msg = {"role": "user", "content": "hello world"}
        tokens = est.estimate_message(msg)
        assert tokens > 4

    def test_estimate_message_with_tool_calls(self):
        est = TokenEstimator()
        msg = {"role": "assistant", "content": "ok", "tool_calls": [{"id": "1", "function": {"name": "run_command"}}]}
        tokens = est.estimate_message(msg)
        assert tokens > 50

    def test_estimate_history(self):
        est = TokenEstimator()
        history = _make_history(10)
        tokens = est.estimate_history(history)
        assert tokens > 100

    def test_estimate_context_window(self):
        est = TokenEstimator()
        assert est.estimate_context_window("deepseek-v4-flash-free") == 128_000
        assert est.estimate_context_window("claude-sonnet-4") == 200_000
        assert est.estimate_context_window("unknown-model") == 128_000

    def test_truncate_to_tokens(self):
        est = TokenEstimator()
        text = " ".join(["word"] * 1000)
        result = est.truncate_to_tokens(text, 100)
        assert len(result.split()) < 1000

    def test_backward_compat_estimate_tokens(self):
        tokens = estimate_tokens("hello world test")
        assert tokens > 0

    def test_backward_compat_truncate_to_tokens(self):
        text = " ".join(["word"] * 500)
        result = truncate_to_tokens(text, 50)
        assert "truncated" in result

    def test_format_memories_includes_evidence_warning(self):
        result = format_memories([
            {"fact_id": 1, "fact_text": "User likes tea", "category": "preference", "importance": 0.8}
        ])

        assert "runtime/tool evidence" in result
        assert "User likes tea" in result


# ── Task 2: ToolTruncator ─────────────────────────────────────────────

class TestToolTruncator:
    def test_short_output_preserved(self):
        truncator = ToolTruncator(max_chars=500)
        content = "short output"
        result = truncator.truncate_single(content, role="tool")
        assert result == content

    def test_long_output_truncated(self):
        truncator = ToolTruncator(max_chars=100)
        content = "x" * 500
        result = truncator.truncate_single(content, role="tool")
        assert "truncated" in result
        assert len(result) < 500

    def test_non_tool_role_not_truncated(self):
        truncator = ToolTruncator(max_chars=50)
        content = "x" * 200
        result = truncator.truncate_single(content, role="assistant")
        assert result == content

    def test_truncate_history_preserves_recent(self):
        truncator = ToolTruncator(max_chars=50)
        history = _make_tool_history(n_tools=5, tool_size=200)
        result = truncator.truncate_history(history)
        # Last 6 messages should be untouched
        for msg in result[-6:]:
            if msg.get("role") == "tool":
                assert "_truncated" not in msg

    def test_truncate_history_truncates_old(self):
        truncator = ToolTruncator(max_chars=50)
        history = _make_tool_history(n_tools=5, tool_size=200)
        result = truncator.truncate_history(history)
        # Older tool messages should be truncated
        truncated_count = sum(1 for m in result if m.get("_truncated"))
        assert truncated_count > 0


# ── Task 3: ContextCompactor ──────────────────────────────────────────

class TestContextCompactor:
    def test_should_compact_short_history(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        compactor = ContextCompactor(FakeLLM(), config)
        history = _make_history(3)
        assert compactor.should_compact(history) is False

    def test_should_not_compact_small_history(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        compactor = ContextCompactor(FakeLLM(), config)
        history = [{"role": "user", "content": "hi"}]
        assert compactor.should_compact(history) is False

    def test_fallback_summary(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        compactor = ContextCompactor(FakeLLM(), config)
        middle = [{"role": "user", "content": "test question"}]
        result = compactor._fallback_summary(middle)
        assert "Summary" in result

    def test_phase2_split(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        compactor = ContextCompactor(FakeLLM(), config)
        history = _make_history(30)
        head, middle, tail = compactor._phase2_split(history)
        assert len(head) == 2
        assert len(tail) == 20
        assert len(middle) == len(history) - 22

    def test_phase4_assemble(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        compactor = ContextCompactor(FakeLLM(), config)
        head = [{"role": "system", "content": "sys"}]
        tail = [{"role": "user", "content": "recent"}]
        summary = "Test summary"
        result = compactor._phase4_assemble(head, summary, tail)
        assert len(result) == 3
        assert result[1]["_is_summary"] is True
        assert "Test summary" in result[1]["content"]


# ── Task 4: MemoryExtractor ───────────────────────────────────────────

class TestMemoryExtractor:
    def test_extract_empty_history(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        result = extractor.extract_and_store([])
        assert result == []

    def test_parse_and_store_valid_json(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        response = '[{"fact_text": "User likes dark mode", "category": "preference", "importance": 0.8, "confidence": 0.9}]'
        result = extractor._parse_and_store(response)
        assert len(result) == 1
        assert result[0]["fact_text"] == "User likes dark mode"

    def test_parse_and_store_empty_array(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        result = extractor._parse_and_store("[]")
        assert result == []

    def test_parse_and_store_invalid_json(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        result = extractor._parse_and_store("not json at all")
        assert result == []

    def test_parse_and_store_missing_fact_text(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        response = '[{"category": "preference"}]'
        result = extractor._parse_and_store(response)
        assert result == []

    def test_parse_and_store_has_no_content_policy_gate(self):
        store = FakeMemoryStore()
        extractor = MemoryExtractor(FakeLLM(), store)
        response = json.dumps([
            {
                "fact_text": "User likes dark mode",
                "category": "preference",
                "importance": 0.8,
                "confidence": 0.9,
            },
            {
                "fact_text": "User said fuck you",
                "category": "note",
                "importance": 0.2,
                "confidence": 0.9,
            },
            {
                "fact_text": "Delhi weather is rainy tonight",
                "category": "fact",
                "importance": 0.3,
                "confidence": 0.9,
            },
        ])

        result = extractor._parse_and_store(response)

        assert [item["fact_text"] for item in result] == [
            "User likes dark mode",
            "User said fuck you",
            "Delhi weather is rainy tonight",
        ]


# ── Task 5: MemoryCleaner ─────────────────────────────────────────────

class TestMemoryCleaner:
    def test_cleanup_empty_store(self):
        store = FakeMemoryStore()
        cleaner = MemoryCleaner(store)
        stats = cleaner.cleanup()
        assert stats["total_before"] == 0
        assert stats["total_after"] == 0
        assert stats["duplicates_merged"] == 0
        assert stats["policy_pruned"] == 0
        assert stats["stale_pruned"] == 0

    def test_cleanup_does_not_apply_a_content_policy_gate(self):
        store = FakeMemoryStore()
        store._facts.extend([
            {
                "fact_id": 1, "fact_text": "Delhi weather is rainy tonight", "category": "fact",
                "importance": 0.3, "confidence": 0.9, "source": "test",
                "created_at": "2026-01-01T00:00:00+00:00", "access_count": 0,
            },
            {
                "fact_id": 2, "fact_text": "User likes dark mode", "category": "preference",
                "importance": 0.8, "confidence": 0.9, "source": "test",
                "created_at": "2026-01-01T00:00:00+00:00", "access_count": 0,
            },
        ])

        cleaner = MemoryCleaner(store, stale_days=10_000)
        stats = cleaner.cleanup()

        assert stats["policy_pruned"] == 0
        assert [fact["fact_text"] for fact in store._facts] == [
            "Delhi weather is rainy tonight",
            "User likes dark mode",
        ]

    def test_prune_stale_keeps_important(self):
        store = FakeMemoryStore()
        store._facts.append({
            "fact_id": 1, "fact_text": "important", "category": "fact",
            "importance": 0.8, "confidence": 1.0, "source": "test",
            "created_at": "2025-01-01T00:00:00+00:00", "access_count": 0,
        })
        cleaner = MemoryCleaner(store, stale_days=30)
        stats = cleaner.cleanup()
        assert stats["stale_pruned"] == 0
        assert len(store._facts) == 1

    def test_prune_stale_removes_old_low_importance(self):
        store = FakeMemoryStore()
        store._facts.append({
            "fact_id": 1, "fact_text": "stale fact", "category": "note",
            "importance": 0.1, "confidence": 0.5, "source": "test",
            "created_at": "2025-01-01T00:00:00+00:00", "access_count": 0,
        })
        cleaner = MemoryCleaner(store, stale_days=30)
        stats = cleaner.cleanup()
        assert stats["stale_pruned"] == 1
        assert len(store._facts) == 0


# ── Task 6: ContextManager ────────────────────────────────────────────

class TestContextManager:
    def test_before_send_short_history(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        store = FakeMemoryStore()
        mgr = ContextManager(config, FakeLLM(), store)
        history = _make_history(5)
        result = mgr.before_send(history)
        assert len(result) == len(history)

    def test_after_session_returns_stats(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        store = FakeMemoryStore()
        mgr = ContextManager(config, FakeLLM(), store)
        history = _make_history(5)
        result = mgr.after_session(history)
        assert "new_memories" in result
        assert "cleanup_stats" in result
        assert "summary" in result

    def test_before_send_with_tool_outputs(self):
        config = AppConfig(model="deepseek-v4-flash-free")
        store = FakeMemoryStore()
        mgr = ContextManager(config, FakeLLM(), store)
        history = _make_tool_history(n_tools=5, tool_size=2000)
        result = mgr.before_send(history)
        # Old tool outputs should be truncated
        truncated = sum(1 for m in result if m.get("_truncated"))
        assert truncated > 0


# ── Task 12: Smoke Tests ──────────────────────────────────────────────

class TestSmoke:
    def test_token_estimator_smoke(self):
        est = TokenEstimator()
        assert est.estimate_text("hello world") > 0
        assert est.estimate_context_window("deepseek-v4-flash-free") == 128_000

    def test_tool_truncator_smoke(self):
        t = ToolTruncator(max_chars=100)
        assert t.truncate_single("short") == "short"
        assert "truncated" in t.truncate_single("x" * 500, role="tool")

    def test_context_compactor_smoke(self):
        config = AppConfig()
        compactor = ContextCompactor(FakeLLM(), config)
        assert compactor.should_compact([]) is False
        assert compactor._fallback_summary([]) != ""

    def test_memory_extractor_smoke(self):
        store = FakeMemoryStore()
        ext = MemoryExtractor(FakeLLM(), store)
        assert ext.extract_and_store([]) == []
        assert ext._parse_and_store("[]") == []

    def test_memory_cleaner_smoke(self):
        store = FakeMemoryStore()
        cleaner = MemoryCleaner(store)
        stats = cleaner.cleanup()
        assert stats["total_before"] == 0

    def test_context_manager_smoke(self):
        config = AppConfig()
        store = FakeMemoryStore()
        mgr = ContextManager(config, FakeLLM(), store)
        history = _make_history(5)
        result = mgr.before_send(history)
        assert isinstance(result, list)
        session_result = mgr.after_session(history)
        assert "summary" in session_result

    def test_appconfig_has_new_fields(self):
        config = AppConfig()
        assert config.context_compact_threshold == 0.90
        assert config.context_protected_tail == 20
        assert config.tool_output_max_chars == 500
        assert config.memory_dedup_threshold == 0.3
        assert config.memory_stale_days == 90
        assert config.memory_extract_enabled is True
        assert config.memory_cleanup_enabled is True
