# Context Management System — Design Spec

**Date:** 2026-06-20
**Status:** Draft
**Author:** Claude (brainstorming session)

---

## Overview

Replace Ares's reactive, naive context management with a proactive system that auto-compacts at 90% threshold, creates structured LLM summaries, extracts memories from conversations, cleans up memory duplicates, and truncates large tool outputs. Follows Hermes Agent's four-phase compression pattern and Claude Code's structured summary approach.

## Current State (Problems)

| Problem | Current Code | Issue |
|---------|-------------|-------|
| Reactive trimming | `_trim_history()` in server.py (lines 46-61) | Only fires on 400 errors, hardcoded 40-message limit |
| Naive summaries | `summarize_conversation()` in conversations.py (lines 133-157) | Concatenates first 5 user + 3 assistant messages, 1200 char cap |
| Unused summaries | `get_recent_summaries()` in conversations.py (lines 123-131) | Injected into context but agent ignores them |
| No memory cleanup | memory.py | 30+ facts accumulate forever, duplicates pile up |
| No tool truncation | agent.py line 240 | Full tool output goes into context |
| Naive token estimation | context_blend.py line 10 | `words * 1.3` is unreliable |
| Tiny context budget | AppConfig line 90 | `context_token_budget=2000` only covers context string |

## Design Goals

1. **Proactive compaction** — estimate tokens before each LLM call, auto-compact when history exceeds 90% of context window
2. **Structured LLM summaries** — replace naive text concatenation with LLM-generated structured summaries (goals, progress, decisions, files, next steps)
3. **Memory extraction** — after each conversation, extract new memories and store them
4. **Memory cleanup** — deduplicate similar memories, merge related facts, drop stale low-importance memories
5. **Tool output truncation** — auto-truncate large tool results before they enter context
6. **Iterative summaries** — subsequent compactions update the previous summary instead of starting from scratch

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Server / Agent (before each LLM call)              │
│  ┌─────────────────────────────────────────────┐    │
│  │  ContextManager.before_send(history)         │    │
│  │  1. TokenEstimator estimates history tokens  │    │
│  │  2. If > 90% → ContextCompactor.compact()   │    │
│  │  3. ToolTruncator.truncate old tool results  │    │
│  │  4. Return trimmed history                   │    │
│  └─────────────────────────────────────────────┘    │
│                                                     │
│  Server / Agent (after conversation ends)           │
│  ┌─────────────────────────────────────────────┐    │
│  │  ContextManager.after_session(history)       │    │
│  │  1. MemoryExtractor.extract_new_memories()   │    │
│  │  2. MemoryCleaner.dedup_and_merge()          │    │
│  │  3. ContextCompactor.summarize(history)      │    │
│  │  4. Store structured summary in DB           │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────┘
```

### Why Unified ContextManager

Follows Hermes pattern: single entry point handles all context lifecycle. The ContextManager owns the compactor, truncator, and memory cleaner. Server.py calls `ctx.before_send()` before LLM calls and `ctx.after_session()` when conversations end. One module, one responsibility.

### Data Flow — Before LLM Call

```
history = [system_msg, user_msg, assistant_msg, tool_msg, ...]
    │
    ▼
TokenEstimator.estimate_history(history)
    │ returns: estimated token count
    ▼
if tokens > context_limit * 0.9:
    ContextCompactor.compact(history)
        │ Phase 1: Prune old tool results (>200 chars) → stubs
        │ Phase 2: Split into head (2 msgs) + middle + tail (20 msgs)
        │ Phase 3: LLM summarizes middle → structured summary
        │ Phase 4: Reassemble: head + summary_msg + tail
    │ returns: compacted history
    ▼
ToolTruncator.truncate(history)
    │ Truncate any tool results > 500 chars → first/last 100 chars + "...[truncated]"
    │ returns: truncated history
    ▼
Return history to agent.py → build_messages() → LLM call
```

### Data Flow — After Session Ends

```
history = full conversation messages
    │
    ▼
MemoryExtractor.extract_new_memories(history)
    │ LLM extracts new facts/preferences from conversation
    │ Store in memory.py via MemoryStore.store()
    ▼
MemoryCleaner.dedup_and_merge(memory_store)
    │ Find similar memories (vector distance < 0.3)
    │ Merge if > 2 similar → keep merged version, delete originals
    │ Drop memories with importance < 0.2 and age > 30 days
    ▼
ContextCompactor.summarize(history)
    │ LLM creates structured summary:
    │   - Goals: what the user wanted
    │   - Progress: done / in-progress / blocked
    │   - Decisions: key choices made
    │   - Files: files modified or discussed
    │   - Next steps: what remains
    │ Store in conversations.summary
    ▼
ConversationStore.save()
```

---

## Components

### 1. TokenEstimator (Enhanced)

**File:** `ares/context_blend.py` (modify existing)

Replace naive `words * 1.3` with a more accurate estimator that handles different content types.

```python
class TokenEstimator:
    """Estimates token counts for messages and content types."""

    # Different estimation ratios for different content
    WORD_RATIO = 1.3  # English prose
    CODE_RATIO = 1.5  # Code (more tokens per word due to symbols)
    TOOL_OUTPUT_RATIO = 1.8  # Tool output (newlines, whitespace, special chars)

    def estimate_text(self, text: str, content_type: str = "text") -> int:
        """Estimate tokens in a text string."""
        if not text.strip():
            return 0
        ratio = {
            "text": self.WORD_RATIO,
            "code": self.CODE_RATIO,
            "tool_output": self.TOOL_OUTPUT_RATIO,
        }.get(content_type, self.WORD_RATIO)
        return max(1, int(len(text.split()) * ratio))

    def estimate_message(self, msg: dict) -> int:
        """Estimate tokens in a single message dict."""
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        content_type = "tool_output" if msg.get("role") == "tool" else "text"
        tokens = self.estimate_text(content, content_type)
        # Add overhead for role, tool_calls structure
        if msg.get("tool_calls"):
            tokens += len(msg["tool_calls"]) * 50  # ~50 tokens per tool call
        return tokens + 4  # role + content overhead

    def estimate_history(self, history: list[dict]) -> int:
        """Estimate total tokens in conversation history."""
        return sum(self.estimate_message(msg) for msg in history)

    def estimate_context_window(self, model: str) -> int:
        """Return the context window size for a model."""
        # Known context windows — extend as needed
        CONTEXT_WINDOWS = {
            "deepseek-v4-flash-free": 128_000,
            "deepseek-v4-flash": 128_000,
            "claude-sonnet-4-20250514": 200_000,
            "gpt-4o": 128_000,
            "gpt-4o-mini": 128_000,
            "gpt-4.1-nano-2025-04-14": 1_000_000,
        }
        return CONTEXT_WINDOWS.get(model, 128_000)  # default 128K
```

**Key change:** `estimate_history()` sums all messages including tool outputs. The 90% threshold uses `estimate_context_window()` to calculate the limit.

### 2. ToolTruncator

**File:** `ares/tool_truncator.py` (new)

Truncates large tool outputs before they enter context. Hermes pattern: replace verbose tool outputs (>200 chars) with a stub.

```python
class ToolTruncator:
    """Truncates large tool output messages in conversation history."""

    MAX_TOOL_OUTPUT_CHARS = 500  # chars before truncation
    TRUNCATED_HEAD = 150  # chars from start
    TRUNCATED_TAIL = 100  # chars from end
    STUB_TEMPLATE = (
        "{head}\n\n... [truncated — {original_len} chars total, "
        "{remaining} remaining] ...\n\n{tail}"
    )

    def truncate_history(self, history: list[dict]) -> list[dict]:
        """Truncate large tool outputs in old messages. Never truncate recent messages."""
        # Only truncate messages older than the last 6 messages
        protected = max(0, len(history) - 6)
        result = []
        for i, msg in enumerate(history):
            if i < protected and msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > self.MAX_TOOL_OUTPUT_CHARS:
                    msg = dict(msg)
                    msg["content"] = self._truncate_output(content)
                    msg["_truncated"] = True  # metadata flag for debugging
            result.append(msg)
        return result

    def _truncate_output(self, content: str) -> str:
        """Truncate a tool output, keeping head and tail."""
        original_len = len(content)
        head = content[:self.TRUNCATED_HEAD]
        tail = content[-self.TRUNCATED_TAIL:] if original_len > self.TRUNCATED_HEAD + self.TRUNCATED_TAIL else ""
        return self.STUB_TEMPLATE.format(
            head=head,
            tail=tail,
            original_len=original_len,
            remaining=original_len - self.TRUNCATED_HEAD - len(tail),
        )

    def truncate_single(self, content: str, role: str = "tool") -> str:
        """Truncate a single tool result before adding to history."""
        if role != "tool" or len(content) <= self.MAX_TOOL_OUTPUT_CHARS:
            return content
        return self._truncate_output(content)
```

### 3. ContextCompactor

**File:** `ares/compactor.py` (new)

Hermes-style four-phase compression. Uses the same LLM client for summarization.

```python
class ContextCompactor:
    """Four-phase context compression following Hermes Agent pattern."""

    COMPACT_THRESHOLD = 0.90  # Compact when history exceeds 90% of context window
    PROTECTED_TAIL_MESSAGES = 20  # Never compress the last 20 messages
    HEAD_MESSAGES = 2  # Keep first 2 messages (system prompt + first exchange)
    SUMMARY_RATIO = 0.20  # Summary budget = 20% of middle section tokens
    MIN_SUMMARY_TOKENS = 500
    MAX_SUMMARY_TOKENS = 3000

    def __init__(self, llm_client, config: AppConfig):
        self.llm = llm_client
        self.config = config
        self.estimator = TokenEstimator()

    def should_compact(self, history: list[dict]) -> bool:
        """Check if history exceeds 90% of context window."""
        tokens = self.estimator.estimate_history(history)
        window = self.estimator.estimate_context_window(self.config.model)
        threshold = int(window * self.COMPACT_THRESHOLD)
        return tokens > threshold

    def compact(self, history: list[dict]) -> list[dict]:
        """Execute the four-phase compression algorithm."""
        if len(history) < 4:
            return history  # Too short to compress

        # Phase 1: Prune old tool results
        history = self._phase1_prune_tool_results(history)

        # Phase 2: Determine head/middle/tail boundaries
        head, middle, tail = self._phase2_split(history)

        if not middle:
            return history  # Nothing to compress

        # Phase 3: Generate structured summary
        summary = self._phase3_summarize(middle)

        # Phase 4: Assemble compressed context
        return self._phase4_assemble(head, summary, tail)

    def _phase1_prune_tool_results(self, history: list[dict]) -> list[dict]:
        """Replace verbose tool outputs with short stubs."""
        protected = max(0, len(history) - self.PROTECTED_TAIL_MESSAGES)
        result = []
        for i, msg in enumerate(history):
            if i < protected and msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 200:
                    msg = dict(msg)
                    msg["content"] = content[:200] + f"\n... [tool output pruned, {len(content)} chars]"
            result.append(msg)
        return result

    def _phase2_split(self, history: list[dict]) -> tuple[list, list, list]:
        """Split history into head (protected start), middle (compressible), tail (protected end)."""
        head = history[:self.HEAD_MESSAGES]
        tail = history[-self.PROTECTED_TAIL_MESSAGES:] if len(history) > self.HEAD_MESSAGES + self.PROTECTED_TAIL_MESSAGES else []
        middle = history[self.HEAD_MESSAGES:-self.PROTECTED_TAIL_MESSAGES] if tail else history[self.HEAD_MESSAGES:]
        return head, middle, tail

    def _phase3_summarize(self, middle: list[dict]) -> str:
        """Generate structured summary using LLM."""
        # Build conversation text for the LLM
        conversation_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:500]}"
            for msg in middle
            if msg.get("content")
        )

        # Truncate if conversation text is too long
        tokens = self.estimator.estimate_text(conversation_text)
        max_input = min(tokens, self.estimator.estimate_context_window(self.config.model) - 5000)
        if tokens > max_input:
            conversation_text = self.estimator.truncate_to_tokens(conversation_text, max_input)

        summary_prompt = f"""Write a concise structured summary of this conversation segment.
Focus on what matters for continuing the work. Be specific, not vague.

Conversation:
{conversation_text}

Write your summary in this exact format:
## Goals
- What the user wanted to accomplish

## Progress
- What was completed (done)
- What's in progress
- What's blocked

## Decisions
- Key choices made and their rationale

## Files Modified
- List of files changed or discussed

## Next Steps
- What remains to be done

Summary:"""

        # Use the same LLM client for summarization
        # Synchronous call since we're in a blocking context
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in an async context — use a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run,
                        self._call_llm(summary_prompt)
                    ).result(timeout=30)
            else:
                result = loop.run_until_complete(self._call_llm(summary_prompt))
        except Exception:
            # Fallback: simple text summary if LLM fails
            return self._fallback_summary(middle)

        return result

    async def _call_llm(self, prompt: str) -> str:
        """Call the LLM for summarization."""
        messages = [{"role": "user", "content": prompt}]
        response = await self.llm.chat(messages, tools=[])
        return response.get("content", "") or self._fallback_summary([])

    def _fallback_summary(self, middle: list[dict]) -> str:
        """Simple fallback when LLM summarization fails."""
        topics = []
        for msg in middle:
            if msg.get("role") == "user" and msg.get("content"):
                topics.append(msg["content"][:100])
        return f"## Summary\nTopics discussed: {'; '.join(topics[:5])}"

    def _phase4_assemble(self, head: list, summary: str, tail: list) -> list[dict]:
        """Reassemble compressed context: head + summary message + tail."""
        summary_msg = {
            "role": "system",
            "content": f"[Previous conversation summary]\n{summary}",
            "_is_summary": True,  # metadata flag for tracking
        }
        return head + [summary_msg] + tail

    def summarize_for_session(self, history: list[dict]) -> str:
        """Create a structured session summary (called at session end)."""
        conversation_text = "\n".join(
            f"[{msg.get('role', 'unknown')}]: {msg.get('content', '')[:800]}"
            for msg in history
            if msg.get("content") and msg.get("role") in ("user", "assistant")
        )

        if not conversation_text.strip():
            return ""

        prompt = f"""Summarize this conversation session for future reference.
Be concise but capture the key details that would help in a future session.

Conversation:
{conversation_text}

Write a structured summary (max 500 words):
## What happened
Brief description of the conversation.

## Key decisions
- Decisions made and why

## Files/changes
- Files discussed or modified

## Open items
- Things left undone or to follow up on

Summary:"""

        try:
            import asyncio, concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    self._call_llm(prompt)
                ).result(timeout=30)
            return result
        except Exception:
            return self._fallback_summary(history)
```

### 4. MemoryExtractor

**File:** `ares/memory_extractor.py` (new)

Extracts new memories from conversations using LLM judgment.

```python
class MemoryExtractor:
    """Extracts new facts and preferences from conversations."""

    def __init__(self, llm_client, memory_store: MemoryStore):
        self.llm = llm_client
        self.memory_store = memory_store

    def extract_and_store(self, history: list[dict]) -> list[dict]:
        """Extract new memories from conversation and store them."""
        # Get recent user messages (last 10)
        user_messages = [
            msg for msg in history
            if msg.get("role") == "user" and msg.get("content")
        ][-10:]

        if not user_messages:
            return []

        conversation_text = "\n".join(
            f"User: {msg['content'][:500]}" for msg in user_messages
        )

        prompt = f"""Analyze this conversation and extract any NEW facts, preferences, or information about the user that should be remembered.

Only extract information that is:
1. A stated preference (e.g., "I prefer X over Y")
2. A fact about the user (e.g., "I work as a developer at Z")
3. A habit or routine (e.g., "I usually code at night")
4. A relationship (e.g., "my colleague John works on the API")

Do NOT extract:
- Temporary task details
- Information already commonly known
- Requests that don't contain personal information

For each extracted fact, respond with a JSON array:
[
  {{"fact_text": "...", "category": "preference|fact|habit|relationship", "importance": 0.0-1.0, "confidence": 0.0-1.0}}
]

If no new facts are found, respond with an empty array: []

Conversation:
{conversation_text}

New facts:"""

        try:
            import asyncio, concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(
                    asyncio.run,
                    self._call_llm(prompt)
                ).result(timeout=30)
            return self._parse_and_store(result)
        except Exception:
            return []

    async def _call_llm(self, prompt: str) -> str:
        messages = [{"role": "user", "content": prompt}]
        response = await self.llm.chat(messages, tools=[])
        return response.get("content", "") or "[]"

    def _parse_and_store(self, response: str) -> list[dict]:
        """Parse LLM response and store extracted memories."""
        import json, re
        # Extract JSON array from response
        match = re.search(r'\[.*\]', response, re.DOTALL)
        if not match:
            return []

        try:
            facts = json.loads(match.group())
        except json.JSONDecodeError:
            return []

        stored = []
        for fact in facts:
            if not fact.get("fact_text"):
                continue
            fact_id = self.memory_store.store(
                fact_text=fact["fact_text"],
                category=fact.get("category", "note"),
                confidence=float(fact.get("confidence", 0.8)),
                importance=float(fact.get("importance", 0.5)),
                source="conversation_extract",
            )
            stored.append({**fact, "fact_id": fact_id})

        return stored
```

### 5. MemoryCleaner

**File:** `ares/memory_cleaner.py` (new)

Deduplicates, merges, and prunes stale memories. No hard limit on total memories.

```python
class MemoryCleaner:
    """Cleans up memory store: dedup, merge, and prune stale facts."""

    DEDUP_SIMILARITY_THRESHOLD = 0.3  # vector distance below this = duplicate
    STALE_DAYS = 90  # memories older than 90 days with low importance
    LOW_IMPORTANCE_THRESHOLD = 0.2
    MIN_ACCESS_COUNT = 2  # memories accessed less than this are candidates

    def __init__(self, memory_store: MemoryStore, embedding_provider):
        self.memory_store = memory_store
        self.embedding_provider = embedding_provider

    def cleanup(self) -> dict:
        """Run full cleanup: dedup, merge, and prune. Returns stats."""
        stats = {"duplicates_merged": 0, "stale_pruned": 0, "total_before": 0, "total_after": 0}

        all_memories = self.memory_store.list_all()
        stats["total_before"] = len(all_memories)

        # Step 1: Dedup — find and merge near-duplicates
        stats["duplicates_merged"] = self._dedup_similar(all_memories)

        # Step 2: Prune — remove stale low-importance memories
        stats["stale_pruned"] = self._prune_stale()

        stats["total_after"] = len(self.memory_store.list_all())
        return stats

    def _dedup_similar(self, memories: list[dict]) -> int:
        """Find similar memories and merge them."""
        merged = 0
        seen = set()

        for i, mem_a in enumerate(memories):
            if mem_a["fact_id"] in seen:
                continue

            # Find all similar memories
            similar = self._find_similar(mem_a)
            if len(similar) < 2:
                continue

            # Keep the one with highest importance, merge text
            best = max(similar, key=lambda m: m.get("importance", 0.5))
            others = [m for m in similar if m["fact_id"] != best["fact_id"]]

            # Merge: combine unique info from others into best
            merged_text = best["fact_text"]
            for other in others:
                if other["fact_id"] not in seen:
                    merged_text += f" Also: {other['fact_text']}"
                    self.memory_store.delete(other["fact_id"])
                    seen.add(other["fact_id"])
                    merged += 1

            # Update best with merged text
            if merged_text != best["fact_text"]:
                self.memory_store.update(best["fact_id"], fact_text=merged_text)
            seen.add(best["fact_id"])

        return merged

    def _find_similar(self, memory: dict) -> list[dict]:
        """Find memories similar to the given one using vector search."""
        results = self.memory_store.search(memory["fact_text"], limit=10)
        # Filter by similarity threshold
        similar = []
        for r in results:
            if r.get("_score", 1.0) < self.DEDUP_SIMILARITY_THRESHOLD:
                similar.append(r)
        return similar

    def _prune_stale(self) -> int:
        """Remove old, low-importance, rarely-accessed memories."""
        from datetime import datetime, timedelta, timezone
        pruned = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.STALE_DAYS)

        all_memories = self.memory_store.list_all()
        for mem in all_memories:
            importance = mem.get("importance", 0.5)
            access_count = mem.get("access_count", 0)
            created_at = mem.get("created_at", "")

            if importance >= self.LOW_IMPORTANCE_THRESHOLD:
                continue  # Important enough to keep
            if access_count >= self.MIN_ACCESS_COUNT:
                continue  # Used recently enough

            # Check age
            try:
                created = datetime.fromisoformat(created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if created > cutoff:
                    continue  # Too new to prune
            except (ValueError, TypeError):
                continue

            self.memory_store.delete(mem["fact_id"])
            pruned += 1

        return pruned
```

### 6. ContextManager (Orchestrator)

**File:** `ares/context_manager.py` (new)

Single entry point that coordinates all context management concerns.

```python
class ContextManager:
    """Orchestrates all context management: compaction, truncation, memory, summaries."""

    def __init__(self, config: AppConfig, llm_client, memory_store: MemoryStore):
        self.config = config
        self.estimator = TokenEstimator()
        self.compactor = ContextCompactor(llm_client, config)
        self.truncator = ToolTruncator()
        self.memory_extractor = MemoryExtractor(llm_client, memory_store)
        self.memory_cleaner = MemoryCleaner(memory_store, memory_store.embedding_provider)

    def before_send(self, history: list[dict]) -> list[dict]:
        """Prepare history before LLM call: compact + truncate."""
        # Check if compaction needed
        if self.compactor.should_compact(history):
            history = self.compactor.compact(history)

        # Always truncate old tool outputs
        history = self.truncator.truncate_history(history)

        return history

    def after_session(self, history: list[dict]) -> dict:
        """Post-session processing: extract memories, clean up, summarize."""
        # 1. Extract new memories
        new_memories = self.memory_extractor.extract_and_store(history)

        # 2. Clean up memory store
        cleanup_stats = self.memory_cleaner.cleanup()

        # 3. Create structured session summary
        summary = self.compactor.summarize_for_session(history)

        return {
            "new_memories": new_memories,
            "cleanup_stats": cleanup_stats,
            "summary": summary,
        }
```

### 7. Integration Points

#### server.py Changes

Replace `_trim_history()` with ContextManager:

```python
# In AresServer.__init__():
self.context_manager = ContextManager(
    config=self.config,
    llm_client=self.agent.llm,
    memory_store=self.memory_store,
)

# In _handle_chat(), replace _trim_history(history, 20) with:
history = self.context_manager.before_send(history)

# In session end handling:
result = self.context_manager.after_session(history)
# Store summary via conversation_store.summarize_conversation()
```

#### agent.py Changes

No changes needed — server.py handles trimming before passing history to `build_messages()`.

#### conversations.py Changes

Replace `summarize_conversation()` with structured summary storage:

```python
# Add method to store structured summaries
def save_structured_summary(self, conversation_id: int, summary: str):
    """Save a structured LLM-generated summary."""
    self.conn.execute(
        """UPDATE conversations SET summary = ?, summarized_at = ? WHERE id = ?""",
        (summary, now_local_iso(), conversation_id),
    )
    self.conn.commit()
```

#### models.py Changes

Add context management config fields:

```python
class AppConfig(BaseModel):
    # ... existing fields ...
    context_compact_threshold: float = 0.90  # auto-compact at 90% of context window
    context_protected_tail: int = 20  # messages to never compress
    tool_output_max_chars: int = 500  # truncate tool outputs above this
    memory_dedup_threshold: float = 0.3  # vector distance for dedup
    memory_stale_days: int = 90  # prune memories older than this
    memory_extract_enabled: bool = True  # extract memories from conversations
    memory_cleanup_enabled: bool = True  # auto-cleanup after sessions
```

---

## Safety Model

- **No data loss:** Compacted messages are replaced with summaries, not deleted. Session summaries are stored in the DB.
- **Protected zones:** Last 20 messages and first 2 messages are never compressed.
- **Graceful degradation:** If LLM summarization fails, fallback to simple text summary.
- **Memory extraction is conservative:** Only extracts clear facts/preferences, not temporary task details.
- **No hard memory limit:** Memories accumulate forever, but cleanup runs after each session.

---

## Dependencies

No new external dependencies. All components use existing Ares infrastructure:
- LLM client (for summarization and extraction)
- MemoryStore (for memory operations)
- ConversationStore (for summary storage)
- TokenEstimator (enhanced in context_blend.py)

---

## Error Handling

| Scenario | Response |
|----------|----------|
| LLM summarization fails | Fallback to simple text summary (concatenation) |
| Memory extraction fails | Skip extraction, continue with cleanup |
| Memory dedup fails | Skip dedup, continue with pruning |
| Token estimation inaccurate | Conservative: compact earlier rather than later |
| History too short to compact (< 4 messages) | Skip compaction, return as-is |

---

## Out of Scope

- Multiple concurrent terminal sessions
- Cross-session memory search (memories already persist across sessions)
- Memory importance scoring via LLM (keep simple heuristic for now)
- Real-time streaming compression (only compress before/after, not during)
- Custom compression models (use same LLM as main agent)

---

## Sources

- [Hermes Agent Context Compression](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching/)
- [Claude Code Compaction API](https://platform.claude.com/docs/en/build-with-claude/compaction)
- [Context Compaction Deep Dive](https://codex.danielvaughan.com/2026/04/14/context-compaction-deep-dive-codex-cli-claude-code-opencode/)
- [Hermes Memory Architecture](https://vectorize.io/articles/hermes-agent-memory-explained)
- [Mem0 Memory Techniques](https://mem0.ai/blog/6-techniques-to-cut-ai-agent-memory-cost-beyond-basic-retrieval)
