# Context Management System — Implementation Plan

**Date:** 2026-06-20
**Spec:** `docs/superpowers/specs/2026-06-20-context-management-design.md`
**Status:** Draft

---

## Overview

Replace Ares's reactive, naive context management with a proactive system following Hermes/Claude Code patterns. Four new modules, one modified module, integration across server.py and models.py.

---

## Task Breakdown

### Task 1: TokenEstimator Enhancement
**File:** `ares/context_blend.py`
**Depends:** None
**Effort:** Small

Enhance the existing `estimate_tokens()` function with content-type-aware estimation.

- Add `TokenEstimator` class with `estimate_text()`, `estimate_message()`, `estimate_history()`, `estimate_context_window()`
- Different ratios: prose (1.3), code (1.5), tool output (1.8)
- `estimate_message()` handles dict messages with role/content/tool_calls
- `estimate_history()` sums all messages
- `estimate_context_window()` maps model names to context sizes (deepseek=128K, claude=200K, gpt-4o=128K)
- Keep backward-compatible `estimate_tokens()` function (delegate to estimator)
- Add `truncate_to_tokens()` as method on estimator

**Verification:** Unit tests — estimate prose vs code vs tool output, verify context window mapping

---

### Task 2: ToolTruncator
**File:** `ares/tool_truncator.py` (new)
**Depends:** Task 1 (uses TokenEstimator)
**Effort:** Small

New module that truncates large tool outputs before they enter context.

- `ToolTruncator` class with `truncate_history()`, `truncate_single()`
- Configurable `MAX_TOOL_OUTPUT_CHARS` (default 500)
- Truncation preserves head (150 chars) + tail (100 chars) + truncation notice
- Only truncates messages older than last 6 (protected recent messages)
- Never truncates non-tool messages
- `_truncated` metadata flag for debugging

**Verification:** Unit tests — truncate long output, preserve short output, protect recent messages

---

### Task 3: ContextCompactor
**File:** `ares/compactor.py` (new)
**Depends:** Task 1 (uses TokenEstimator)
**Effort:** Large

Four-phase compression following Hermes Agent pattern.

- `ContextCompactor` class with `should_compact()`, `compact()`, `summarize_for_session()`
- Phase 1: `_phase1_prune_tool_results()` — replace verbose tool outputs (>200 chars) in middle section with stubs
- Phase 2: `_phase2_split()` — divide history into head (2 msgs), middle, tail (20 msgs)
- Phase 3: `_phase3_summarize()` — call LLM with structured prompt (goals, progress, decisions, files, next steps)
- Phase 4: `_phase4_assemble()` — reassemble head + summary message + tail
- `should_compact()` checks 90% of context window
- `summarize_for_session()` creates session-end summary
- Fallback: if LLM fails, use simple text concatenation
- Handle async LLM calls from sync context via ThreadPoolExecutor

**Verification:** Unit tests — should_compact threshold, phase boundaries, fallback on LLM failure. Integration test — compact a sample conversation.

---

### Task 4: MemoryExtractor
**File:** `ares/memory_extractor.py` (new)
**Depends:** None
**Effort:** Medium

Extracts new memories from conversations using LLM judgment.

- `MemoryExtractor` class with `extract_and_store()`
- Takes last 10 user messages from history
- LLM prompt extracts: facts, preferences, habits, relationships
- Only stores clear personal information, not temporary task details
- Parses JSON array response from LLM
- Stores via `MemoryStore.store()` with source="conversation_extract"
- Handles LLM failures gracefully (returns empty list)

**Verification:** Unit tests — parse extraction response, handle LLM failure, filter non-personal info

---

### Task 5: MemoryCleaner
**File:** `ares/memory_cleaner.py` (new)
**Depends:** None
**Effort:** Medium

Deduplicates, merges, and prunes stale memories.

- `MemoryCleaner` class with `cleanup()` returning stats
- `_dedup_similar()` — find memories with vector distance < 0.3, merge into best
- `_prune_stale()` — remove memories with importance < 0.2 and age > 90 days
- No hard limit on total memories
- Merge combines text from duplicates into best version
- Stats returned: duplicates_merged, stale_pruned, total_before, total_after

**Verification:** Unit tests — dedup similar memories, prune stale, preserve important, handle empty store

---

### Task 6: ContextManager (Orchestrator)
**File:** `ares/context_manager.py` (new)
**Depends:** Tasks 1-5
**Effort:** Small

Single entry point coordinating all context management.

- `ContextManager` class with `before_send()` and `after_session()`
- `before_send(history)` → compact if needed → truncate tool outputs → return history
- `after_session(history)` → extract memories → cleanup → summarize → return stats
- Wires together: TokenEstimator, ContextCompactor, ToolTruncator, MemoryExtractor, MemoryCleaner
- Takes config, llm_client, memory_store in constructor

**Verification:** Unit tests — before_send with short/long history, after_session end-to-end

---

### Task 7: AppConfig Extensions
**File:** `ares/models.py`
**Depends:** None
**Effort:** Small

Add context management configuration fields to AppConfig.

```python
context_compact_threshold: float = 0.90
context_protected_tail: int = 20
tool_output_max_chars: int = 500
memory_dedup_threshold: float = 0.3
memory_stale_days: int = 90
memory_extract_enabled: bool = True
memory_cleanup_enabled: bool = True
```

**Verification:** Verify defaults load correctly, existing configs unaffected

---

### Task 8: Server Integration
**File:** `ares/server.py`
**Depends:** Tasks 6, 7
**Effort:** Medium

Wire ContextManager into the WebSocket server.

- In `AresServer.__init__()`: create `ContextManager` instance
- In `_handle_chat()`: replace `_trim_history(history, 20)` with `self.context_manager.before_send(history)`
- Remove `_trim_history()` function (or deprecate)
- In session end handling: call `self.context_manager.after_session(history)`
- Store structured summary via `conversation_store`
- Keep existing progressive retry logic as safety net (Attempt 0 = 20 messages, Attempt 1 = ~13)

**Verification:** Integration test — send messages through server, verify compaction triggers, verify summaries stored

---

### Task 9: ConversationStore Enhancement
**File:** `ares/conversations.py`
**Depends:** Task 7
**Effort:** Small

Replace naive `summarize_conversation()` with structured summary support.

- Add `save_structured_summary(conversation_id, summary)` method
- Keep existing `summarize_conversation()` as fallback
- Update `get_recent_summaries()` to handle longer structured summaries (truncate if needed for context injection)

**Verification:** Unit tests — save/retrieve structured summaries, fallback to naive

---

### Task 10: Memory Store Enhancement
**File:** `ares/memory.py`
**Depends:** Task 5
**Effort:** Small

Add helper methods needed by MemoryCleaner.

- Add `bulk_delete(fact_ids: list[int])` for efficient batch deletion
- Add `find_similar_to(fact_id: int, limit: int)` that returns vector-similar memories
- Ensure `_rank_score()` works correctly with the new cleanup logic

**Verification:** Unit tests — bulk_delete, find_similar_to

---

### Task 11: Integration Tests
**File:** `tests/test_context_management.py` (new)
**Depends:** Tasks 1-10
**Effort:** Medium

End-to-end tests covering the full context management pipeline.

- Test: short history passes through unchanged
- Test: long history triggers compaction
- Test: tool outputs truncated in old messages
- Test: session end extracts memories and creates summary
- Test: memory cleanup deduplicates similar facts
- Test: fallback when LLM fails
- Test: context window estimation for different models

**Verification:** All tests pass

---

### Task 12: Smoke Tests
**File:** `tests/test_context_smoke.py` (new)
**Depends:** Task 11
**Effort:** Small

Quick smoke tests that verify the system works end-to-end.

- Test: ContextManager.before_send() returns valid history
- Test: ContextManager.after_session() returns stats
- Test: TokenEstimator estimates different content types
- Test: ToolTruncator preserves short outputs

**Verification:** All smoke tests pass

---

## Execution Order

```
Phase 1 (Independent):
  Task 1: TokenEstimator
  Task 7: AppConfig
  Task 10: Memory Store Enhancement

Phase 2 (Depends on Phase 1):
  Task 2: ToolTruncator (depends: Task 1)
  Task 3: ContextCompactor (depends: Task 1)
  Task 4: MemoryExtractor (depends: none, but logically next)
  Task 5: MemoryCleaner (depends: Task 10)

Phase 3 (Depends on Phase 2):
  Task 6: ContextManager (depends: Tasks 1-5)
  Task 9: ConversationStore Enhancement (depends: Task 7)

Phase 4 (Depends on Phase 3):
  Task 8: Server Integration (depends: Tasks 6, 7, 9)

Phase 5 (Verification):
  Task 11: Integration Tests (depends: all)
  Task 12: Smoke Tests (depends: Task 11)
```

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM summarization cost (extra API calls per session) | Medium | Low | Use cheaper model for summarization if available |
| Token estimation inaccuracy | Medium | Medium | Conservative threshold (90%) gives headroom |
| Memory extraction produces noise | Low | Low | Conservative extraction prompt, confidence thresholds |
| Compaction during active work loses context | Low | High | Protected tail (20 messages) prevents this |
| Async/sync mismatch in compactor | Medium | Medium | ThreadPoolExecutor wrapper with timeout |

---

## Testing Strategy

1. **Unit tests** for each component (Tasks 1-5, 7, 9, 10)
2. **Integration tests** for ContextManager orchestration (Task 11)
3. **Smoke tests** for end-to-end verification (Task 12)
4. **Manual testing:** Send 50+ messages through server, verify compaction triggers, verify summaries are structured, verify memory cleanup runs
