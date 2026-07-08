---
name: memory-consolidator
description: Clean up Ares memories by finding duplicates, stale facts, contradictions, and better summaries.
category: ares
version: 1.0.0
examples:
  - prompt: "Review my Ares memories and suggest what to merge or delete."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Memory Consolidator

## Procedure
1. Review recent and relevant memories.
2. Merge duplicates into clearer facts.
3. Flag contradictions for user confirmation.
4. Preserve useful preferences and remove outdated notes only with consent.

## Aging Report
- Group memories by duplicate, conflict, stale, low-confidence, and healthy.
- For stale entries, include age, last accessed time, access count, and why it may no longer be useful.
- Recommend merge/delete/update actions, but do not delete without explicit consent.
- Keep one canonical memory per durable preference when possible.
