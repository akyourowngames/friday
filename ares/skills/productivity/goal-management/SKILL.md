---
name: goal-management
description: Capture, structure, decompose, connect, and review durable Ares goals with evidence-backed progress.
category: productivity
version: 1.0.0
examples:
  - prompt: "Track my goal to ship the watcher engine by July 25."
  - prompt: "Break my launch goal into milestones and connect the implementation task."
test_commands:
  - "python -m pytest tests/test_goals.py tests/test_skills.py"
---

# Goal Management

## Capture
1. Confirm that the user is naming a durable outcome, not a temporary task or brainstorm.
2. Write a concise outcome-oriented title and preserve the user's why in the description.
3. Record category, priority, and an ISO target date only when known.
4. Create the goal only after the user explicitly asks to track/save it.

## Structure
- Keep a goal flat when one independently completable outcome is clear.
- Use `decompose_goal` when the outcome contains distinct milestones or workstreams.
- Every child should be verifiable and independently completable.
- Multi-level trees are allowed, but avoid hierarchy that adds no decision value.

## Execute and prove
- Goals are what/why; Tasks are how. Use `create_task` for executable steps.
- Immediately call `link_goal_task` when a new durable task advances a known goal.
- Use `record_goal_progress` for timestamped check-ins, blockers, wins, and revised estimates.
- Use `sync_goal_progress` only on explicit request. Explain that derived progress uses child, task, and action evidence.
- Never mark completion from inference alone. Ask the user to confirm completion.

## Review
1. Call `list_goals` with `include_due=true`.
2. Lead with overdue/high-priority goals, then due-soon goals.
3. For each goal, distinguish manual progress from evidence-derived progress.
4. Ask for the smallest useful decision: continue, revise, pause, abandon, or complete.
5. Preserve history. Prefer pause/abandon over permanent deletion.
