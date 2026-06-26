---
name: daily-standup
description: Compile a daily status — review pending tasks, due-soon items, recent conversations, active projects, and store a memory snapshot. Use for "my daily standup", "what's on my plate", "daily summary", "status update".
category: productivity
version: 1.0.0
---

# Daily Standup

## Procedure

1. **Review tasks** — Call:
   - `list_tasks` to see all pending items
   - `get_due_soon(hours=48)` for what's coming up
   - `search_tasks(query="", include_done=True)` limited to recently completed (check executed_at dates)

2. **Review memories** — Call `search_memory(query="")` with a broad query to surface recent context. Optionally search for project names the user mentions.

3. **Review conversations** — If the user has asked about specific sessions, use `search_tasks` and `search_memory` cross-reference to find related context.

4. **Compile report** — Present a clear summary:
   - ## Pending Tasks (grouped by priority)
   - ## Due Soon (next 48 hours)
   - ## Recently Completed
   - ## Active Context (memories/projects relevant today)
   - ## Recommended Actions (what to focus on)

5. **Offer to snapshot** — Ask if they want to `store_memory` with today's status for future reference.

## Rules
- Keep it scannable — use bullet points, not paragraphs.
- Highlight urgent items (due < 24h or high priority).
- If there are no pending tasks, say so — don't fabricate.
