# Ares Continuity & Autonomy Plan — People Memory, Action Ledger, Guardrailed Execution

**Date:** 2026-07-10
**Status:** Draft — plan only, no implementation yet
**Goal:** Give Ares two kinds of continuity it currently lacks — who your people are, and what Ares already did — and use that continuity as the foundation for letting Ares execute multi-step workflows (Playwright / Windows MCP) more independently, without weakening any existing confirm-gate safety rule.

---

## Why this is actually three problems, not one

You described two symptoms and one desired capability. They map to three separate architectural gaps:

| Symptom / ask | Root cause in current codebase |
|---|---|
| "It should remember my friends/relatives and reuse them in workflows" | `MemoryStore` (`facts_meta`) only stores freeform text facts. There is no structured "person" object to resolve a name against, so `phone_send_sms`, `gmail_send`, `calendar_create_event` all need a raw number/email typed out every time. |
| "It doesn't know about a file it made 5 days ago unless I mention it in this exact session" | `asset_manifest.py` logs image generation/edits to a flat JSONL, but nothing equivalent exists for `write_file`, `edit_file`, `delete_file`, `run_command`, `create_cron_job`, etc. Conversation summaries are prose blobs, not a queryable record — there's nothing concrete to search when you say "that file from 5 days ago." |
| "It should execute actions automatically using Playwright/Windows MCP" | Nothing in Ares today can carry a multi-step plan across turns or sessions. `TaskState` / `TASK_TRANSITIONS` already exist in `models.py`, but the `Task` model, `TaskStore`, and the `create_task`/`list_tasks`/`get_task_status` tools referenced in the skills/README were never built (this is already a tracked gap). Real automatic execution needs that durable backbone before it needs Playwright.

So this plan has four features that build on each other, plus one integration pass to wire them into context and prompts.

---

## Feature 1 — People & Relationships Store

A new, structured memory layer for people, separate from the generic freeform `facts_meta` table (same pattern as `MemoryStore`, but with real fields instead of one text blob).

**New module:** `ares/people.py` → `PeopleStore`, backed by a new `people_meta` table in the existing `ares.db`.

**Per-person fields:**
- canonical name
- aliases (e.g. "mom", "Priya", "Priya aunty" all resolve to the same person)
- relation (free text — mom, best friend, cousin, colleague, etc.)
- phone number / email (optional, either or both)
- important dates (birthday, anniversary — optional)
- notes (freeform)
- last-referenced timestamp, created-at, source (manual vs. Ares-suggested), confidence

**New tools** (mirrors the existing `store_memory`/`search_memory`/`update_memory`/`delete_memory` shape already in `tools/definitions.py`):
- `remember_person`
- `search_person`
- `update_person`
- `forget_person`

**Reuse in workflows** — the actual point of this feature: `phone_send_sms`, `phone_call_number`, `gmail_send`, `calendar_create_event` gain a resolution step. If you say "text mom," the executor first calls `search_person`, resolves the number, and only falls back to asking you if the name is ambiguous or unknown. This is what makes "reuse them in workflows and execution" real instead of just a memory feature nobody uses.

**Privacy rule (non-negotiable):** this is PII about *other people*, not you. It must never be silently inferred the way generic memory extraction works today. A person record is only created or updated when you explicitly say so ("remember my friend Rohan's number is...") or when Ares explicitly asks and you confirm — never auto-harvested from phone notifications, contacts sync, or SMS content (that boundary already exists for phone data and should extend here). It's also excluded from the default `full` export profile in `tools/exporter.py` unless a new `people` profile is explicitly requested.

---

## Feature 2 — Action & Artifact Ledger

This is the direct fix for "it should remember that file from 5 days ago."

**New module:** `ares/actions.py` → `ActionLedger`, new `actions_log` table in `ares.db`. This generalizes the pattern `asset_manifest.py` already uses for images, but to every consequential tool, and makes it queryable instead of a flat JSONL nobody reads back.

**Per-action fields:**
- action type (file_created, file_edited, file_deleted, file_moved, image_generated, image_edited, command_run, cron_job_created, export_created, calendar_event_created, email_sent, sms_sent, ...)
- target (path, URL, or identifier — never the sensitive content itself)
- one-line human summary
- originating tool name
- session id, timestamp, tags

**Hook point:** `ToolExecutor` — after every handler that currently just returns a result string for a consequential action (`write_file`, `edit_file`, `delete_file`, `move_file`, `batch_edit`, `generate_image`/`resize_image`/`convert_image`/`crop_image`, `run_command` when it clearly changed state, `create_cron_job`, `export_data`, `phone_send_sms`, `calendar_create_event`, `gmail_send`), also call `ActionLedger.record(...)`.

**New tool:** `search_actions(query, since, limit)` — "since" understands relative phrases like "5 days ago" for free, because `ares/tools/dates.py` already wraps `dateparser` for exactly this.

**Context integration:** `Agent.get_context()` gets a new bounded section, same pattern as memories today — always include the last N actions in one-line form, and run a keyword/temporal lookup when your message contains reference language ("that file," "the thing I made," "yesterday," "5 days ago," "remember when"). This is the actual fix — right now that kind of question has nothing real to search against once the session that made the file has ended.

**Privacy rule:** the ledger logs *what* happened, never sensitive content — an SMS action logs "sent SMS to Rohan," not the message body. It's a provenance record, not a second copy of private conversations.

---

## Feature 3 — Complete the Task/Workflow backbone (closes an existing gap)

Real automatic execution needs something durable to resume from — otherwise "autonomous" just means a longer single-turn tool loop that forgets everything the moment the conversation ends. This closes the gap already flagged in your own architecture notes: `TaskState`/`TASK_TRANSITIONS` exist in `models.py`, but `Task`, `TaskStore`, and the tools referenced in skills/README were never implemented.

**New module:** `ares/tasks.py` → `TaskStore`, same durable/revisioned pattern `ares/cron/store.py` already uses (atomic writes, lease-style state transitions), not a fresh design.

**Per-task fields:** goal, ordered plan steps, status (using the existing `TaskState` enum and transitions), created/updated timestamps, result summary, related person ids, related action ids, session id.

**New tools** (same shape as the existing cron tool family in `ares/cron/tools.py`): `create_task`, `list_tasks`, `get_task_status`, `update_task`, `cancel_task`.

This is boring on purpose — it's plumbing, not a feature you'll notice directly, but Feature 4 cannot be built safely without it.

---

## Feature 4 — Guardrailed Autonomous Execution ("Workflow Runner")

This is the "execute actions automatically using Playwright/Windows MCP" ask. It extends the Initiative Engine idea you were already considering — but that was scoped as research/draft-only. This plan promotes it to actual execution, under strict, explicit guardrails.

**New module:** `ares/autonomy/runner.py` — takes one `Task` from the `TaskStore` and executes its plan steps one at a time through the normal `Agent` + `ToolExecutor`, logging every step to the Action Ledger as it goes.

**Two-tier action classification** — this is the part that matters most, and it reuses the `confirm=true` pattern already everywhere in `tools/definitions.py` rather than inventing a new safety model:

- **Runs without a per-step prompt:** read-only and reversible actions — `read_file`, `search_files`, `web_search`, `fetch_url`, `phone_status`, `phone_get_notifications`, `generate_image`, list/search tools, and Playwright/Windows MCP navigation and observation steps.
- **Always stops and asks first, even mid-workflow:** anything sensitive or irreversible — `delete_file`, `phone_call_number`, `phone_send_sms`, `gmail_send`, `calendar_create_event` with external attendees, any purchase/payment flow, any destructive batch operation. The workflow pauses, surfaces one consolidated confirmation, and only continues after you approve. Being "autonomous" changes *who* is driving each step — it never changes *what* requires your sign-off.

Playwright/Windows MCP steps inside a workflow follow the same observe → act → verify loop already documented in your `computer-use` skill: a step isn't marked done until a fresh snapshot/read-back confirms the expected state actually happened, exactly like manual usage today.

Every workflow run's steps land in the Action Ledger tagged with the owning task id, so a later session can ask "what did that automated task from Tuesday actually do" and get a real answer.

---

## Feature 5 — Wire it all together

- `ares/agent.py` `get_context()`: add two more bounded context sections (people, recent/relevant actions) alongside soul/profile/project/memories, using the same token-budget scaling already in `context_blend.get_model_budgets()`.
- `ares/prompts.py`: add a "People & Relationships" section (when to use `remember_person`/`search_person`, and that third-party PII is only stored on explicit request) and an "Action History" section (when to use `search_actions`, and that "that file/thing from N days ago" should trigger a lookup before saying "I don't know").
- `ares/tools/renders.py`: renderers for `search_actions` and `search_person` results, matching the existing memory/skills table style in the CLI.

---

## Privacy & safety — carried over, not reinvented

- People Store is local-only, excluded from the default export profile, and never auto-populated from tool output (phone notifications/contacts stay governed by the existing `phone.store_notification_content` guardrail).
- Action Ledger stores provenance, not content — it can't become a second copy of anything private.
- Every existing `confirm=true` gate is exactly as strict inside an autonomous workflow as it is in manual chat. No exceptions for "the agent decided this was fine."

---

## Suggested build order

1. **Action Ledger** (Feature 2) — highest immediate value, fixes "remember that file from 5 days ago" directly, smallest surface area, raises no new safety questions.
2. **People Store** (Feature 1) — needed next because workflows/execution need someone to resolve "mom"/"Rohan" against.
3. **Task/Workflow backbone** (Feature 3) — closes the existing tracked debt; boring plumbing but required before real autonomy.
4. **Workflow Runner** (Feature 4) — the actual automatic-execution piece, built last since it depends on 1–3 and deserves the most safety review.
5. **Context/prompt wiring** (Feature 5) — rolled out incrementally alongside each feature above rather than as one big-bang change.

---

## Self-review

- Every feature maps to specific existing files/modules (`models.py`, `tools/definitions.py`, `tools/executor.py`, `agent.py`, `prompts.py`, `cron/*` as the structural template for both new stores) — nothing here is an abstract suggestion.
- The `confirm=true` pattern is preserved everywhere; autonomy never weakens it.
- Directly answers both original asks: reusable People memory for workflows, and durable cross-session Action memory for "that file from 5 days ago."
- **Open question for you before Feature 1 starts:** should a person record require explicit confirmation only on first creation, or every time it's updated too? Worth deciding up front since it changes the `remember_person`/`update_person` tool contracts.