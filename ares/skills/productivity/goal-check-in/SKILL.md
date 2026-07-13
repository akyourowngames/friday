---
name: goal-check-in
description: Run a concise recurring goal review from durable goal state, due dates, linked work, and progress evidence.
category: productivity
version: 1.0.0
examples:
  - prompt: "Review my active and overdue goals and give me this week's next actions."
test_commands:
  - "python -m pytest tests/test_goals.py tests/test_skills.py"
---

# Goal Check-in

This playbook may be used as the prompt body of a user-created cron job. Never
create that recurring job unless the user explicitly asks for a schedule.

## Procedure
1. Call `list_goals` for active and paused goals with `include_due=true`.
2. For each overdue or due-soon goal, call `get_goal_status` to inspect its child tree, linked tasks, and recent timeline.
3. Report three compact groups: needs attention, moving, and waiting/paused.
4. Identify progress mode. Never present a manual percentage as task-verified evidence.
5. Recommend at most three next actions across all goals.
6. If the user confirms new progress, use `record_goal_progress`.
7. If the user wants evidence-derived progress, use `sync_goal_progress`.
8. Do not complete, abandon, delete, reschedule, or notify third parties without the user's explicit request.

## Output
- Goal and target date
- Current percentage and whether it is manual or derived
- Evidence or blocker from the latest timeline/task state
- One concrete next action
