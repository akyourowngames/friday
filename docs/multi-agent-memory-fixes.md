# Multi-Agent, Memory & Skill Loading Fixes

**Date:** July 16, 2026  
**Issues Addressed:**
1. Multi-agent false negatives when users explicitly request agent mode
2. Memory flow between tools (context preservation on "continue")
3. Unnecessary skill loading
4. Powerful long-term memory recall

---

## Changes Made

### 1. Multi-Agent Detection Fix (`turn_policy.py`)

**Problem:** User requests like "Use multi agent to do this research on bjp" were not triggering the multi-agent system, returning "this request used regular tools, not specialist agents."

**Solution:** Added additional regex patterns to `_EXPLICIT_DELEGATION_PATTERNS`:
- `\buse\s+(?:the\s+)?multi[-\s]?agent\b` - matches "use multi agent"
- `\bwith\s+multi[-\s]?agent\b` - matches "with multi agent"
- `\bdo\s+(?:this\s+)?research\b` - matches "do this research"
- `\bresearch\s+(?:this|that|on|about)\b` - matches "research this"
- `\b(?:launch|run|use|start|spawn)\s+(?:the\s+)?agents?\b` - matches "launch agents"
- `\bwith\s+(?:multiple|several|two|three|four|five|\d+)\s+(?:agents?|researchers?)\b` - matches "with multiple agents"
- `\b(?:in\s+parallel|simultaneously)\b.*\bresearch\b` - matches "in parallel research"

**Result:** All test cases now correctly detect multi-agent requests.

### 2. Skill Loading Optimization (`skills.py`)

**Problem:** Skills were being loaded unnecessarily for simple conversation inputs like "hi", "ok", "continue", or "do you remember".

**Solution:** Added `_should_skip_skill_loading()` method to `SkillManager`:
- Skips loading for pure conversation patterns (hi, hello, thanks, ok, yes, no, bye)
- Skips loading for memory recall requests ("do you remember...")
- Skips loading for continuation-only inputs ("continue", "resume", "go on")
- Skips loading for very short inputs (< 3 words)

**Result:** Skill loading is now optimized, reducing unnecessary context overhead.

### 3. Memory Recall Enhancement (`memory.py`)

**Problem:** Long-term memory recall was not providing context for "do you remember" style queries.

**Solution:** Added `recall_context()` method to `MemoryStore`:
- Extracts entities from the query (quoted phrases, proper nouns, entity patterns)
- Searches for entities in long-term memory
- Performs semantic search for general context
- Returns formatted context for "do you remember" style queries

**Added helper method:**
- `_extract_recall_entities()` - Extracts entities to search in long-term memory

### 4. Context Flow Integration (`user_context.py`, `context_blend.py`)

**Problem:** Memory context was not flowing properly between conversation turns.

**Solution:** 
- Updated `build_user_context()` to call `memory_store.recall_context()` for deep context requests
- Updated `build_context_prompt()` to accept and include `memory_recall_context` parameter
- Memory recall context is now injected into the context building pipeline

**Result:** When users say "continue" or reference previous work, the agent now has access to relevant long-term memory context.

---

## Test Results

### Multi-Agent Detection
```
PASS: "Use multi agent to do this research on bjp" -> True
PASS: "Use multi-agent to research this topic" -> True
PASS: "do this research on AI" -> True
PASS: "research this for me" -> True
PASS: "launch agents for this task" -> True
PASS: "use multiple agents to investigate" -> True
PASS: "research bjp in parallel" -> True
```

### Skill Loading Optimization
```
SKIP: "hi"
SKIP: "ok"
SKIP: "thanks"
SKIP: "continue"
SKIP: "do you remember that file"
SKIP: "hello"
SKIP: "yes"
LOAD: "what is the status"
LOAD: "research bjp using multi agent"
LOAD: "open the browser and navigate to github"
```

### Memory Recall
```
Query: "do you remember Rohit email"
Result: ## Long-term Memory Context
- Rohit email is rohit@example.com
- We worked on the BJP research project last week

Query: "what file did we work on"
Result: ## Long-term Memory Context
- The file is called report.pdf and is in Documents folder
- We worked on the BJP research project last week
```

---

## Files Modified

1. `ares/turn_policy.py` - Added multi-agent detection patterns
2. `ares/skills.py` - Added skill loading optimization
3. `ares/memory.py` - Added memory recall functionality
4. `ares/user_context.py` - Integrated memory recall into context building
5. `ares/context_blend.py` - Added memory recall context parameter

## New Files

1. `ares/multi_agent_fixes.py` - Standalone fixes module (can be removed if not needed)
2. `docs/multi-agent-memory-fixes.md` - This documentation
