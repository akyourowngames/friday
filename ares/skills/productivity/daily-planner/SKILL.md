---
name: daily-planner
description: Help plan a day with priorities, time blocks, constraints, reminders, and realistic next actions.
category: productivity
version: 1.0.0
examples:
  - prompt: "Plan my day around two meetings and a hard stop at 6pm."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Daily Planner

## Procedure
1. Identify deadlines, fixed events, energy constraints, and top priorities.
2. Propose a short prioritized plan with time blocks.
3. Offer calendar reminders or cron automations for commitments.
4. Keep the plan realistic and easy to revise.

## Capacity
- Estimate usable focus capacity after fixed events, breaks, commute, meals, and known energy constraints.
- Mark tasks as must, should, or could.
- Move overflow into a backlog instead of overfilling the day.
- When reminders are requested, confirm timezone and exact trigger time before creating them.
