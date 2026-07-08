---
name: daily-standup
description: Compile a daily status — review recent conversations, active projects, calendar context if available, and store a memory snapshot. Use for "my daily standup", "what's on my plate", "daily summary", "status update".
category: productivity
version: 1.0.0
examples:
  - prompt: "Prepare my standup from yesterday's git work and notes."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Daily Standup

## Procedure

1. **Review memories** — Call `search_memory(query="")` with a broad query to surface recent context. Optionally search for project names the user mentions.

2. **Review conversations** — If the user has asked about specific sessions, use `search_memory` and session context to find related context.

3. **Compile report** — Present a clear summary:
   - ## Active Context (memories/projects relevant today)
   - ## Yesterday
   - ## Today
   - ## Blockers
   - ## Recent Progress
   - ## Recommended Actions (what to focus on)

4. **Extract evidence** — When available, use git logs, local notes, reminders, and conversation context to separate yesterday, today, and blocker items.

5. **Offer to snapshot** — Ask if they want to `store_memory` with today's status for future reference.

## Rules
- Keep it scannable — use bullet points, not paragraphs.
- Highlight urgent items (due < 24h or high priority).
