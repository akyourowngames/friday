"""ContextManager — single entry point for all context lifecycle management."""

from __future__ import annotations

from typing import Any

from ares.context_blend import TokenEstimator
from ares.compactor import ContextCompactor
from ares.tools.tool_truncator import ToolTruncator
from ares.memory_extractor import MemoryExtractor
from ares.memory_cleaner import MemoryCleaner


class ContextManager:
    """Orchestrates all context management: compaction, truncation, memory, summaries."""

    def __init__(
        self,
        config: Any,
        llm_client: Any,
        memory_store: Any,
        reflection_service: Any | None = None,
    ):
        self.config = config
        self.estimator = TokenEstimator()
        self.compactor = ContextCompactor(llm_client, config)
        self.truncator = ToolTruncator(max_chars=config.tool_output_max_chars)
        self.memory_extractor = MemoryExtractor(llm_client, memory_store, config=config)
        self.memory_cleaner = MemoryCleaner(
            memory_store, stale_days=config.memory_stale_days
        )
        self.memory_store = memory_store
        self.reflection_service = reflection_service

    def before_send(self, history: list[dict], *, scope: str | None = None) -> list[dict]:
        """Prepare history before LLM call: compact if needed, then truncate."""
        if self.compactor.should_compact(history):
            checkpoint = None
            if self.reflection_service is not None:
                checkpoint = lambda messages: self.reflection_service.enqueue_compaction(
                    scope=scope, messages=messages
                )
            history = self.compactor.compact(history, before_compaction=checkpoint)

        history = self.truncator.truncate_history(history)
        return history

    def after_session(self, history: list[dict]) -> dict:
        """Post-session processing: extract memories, clean up, summarize."""
        new_memories = []
        memory_config = getattr(self.config, "memory", None)
        capture_config = getattr(memory_config, "capture", None)
        legacy_enabled = bool(
            getattr(capture_config, "legacy_extractor_enabled", False)
            if capture_config is not None
            else getattr(self.config, "memory_extract_enabled", False)
        )
        if legacy_enabled:
            try:
                new_memories = self.memory_extractor.extract_and_store(history)
            except Exception:
                pass

        cleanup_stats = {}
        if self.config.memory_cleanup_enabled:
            try:
                cleanup_stats = self.memory_cleaner.cleanup()
            except Exception:
                pass

        summary = ""
        try:
            summary = self.compactor.summarize_for_session(history)
        except Exception:
            pass

        return {
            "new_memories": new_memories,
            "cleanup_stats": cleanup_stats,
            "summary": summary,
        }
