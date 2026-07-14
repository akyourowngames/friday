# Native multi-agent mode

Ares uses a native manager/supervisor design: the existing user-facing `Agent` decides whether specialist work is useful, delegates bounded tasks through normal tool calls, receives structured results, and writes the final answer. It does not embed CrewAI, AutoGen, LangGraph, or another state machine beside Ares.

## When Ares delegates

Delegation is useful when independent research questions can overlap, web research and repository inspection can proceed separately, frontend and backend need different analysis, or an independent reviewer materially reduces implementation risk. It is intentionally discouraged for small edits, normal conversation, one factual lookup, overlapping mutations, or work that must share a single browser page.

Typical requests:

- “Research three approaches in parallel and compare them.”
- “Inspect the backend and frontend separately, then create an implementation plan.”
- “Have a builder implement this feature and a reviewer verify the changes.”
- “Analyze this bug using a code analyst, documentation researcher, and verifier.”

Ares does not delegate every request. The supervisor remains responsible for choosing the smallest useful team and for interpreting failures or blocked dependencies.

## Specialists

| Role | Default responsibility | Mutation |
|---|---|---:|
| `planner` | Decompose complex work, dependencies, success criteria | No |
| `researcher` | Web/docs/read-only MCP evidence and sources | No |
| `analyst` | Repository inspection, integration points, risks, affected tests | No |
| `builder` | Approved implementation and relevant verification | Yes |
| `reviewer` | Correctness, regression, security, architecture review | No |
| `synthesizer` | Resolve findings and produce compact structured results | No |

Definitions live in `ares.multi_agent.default_agent_specs()` and are copied into a per-runtime `AgentRegistry`. Add a specialist by creating an `AgentSpec` with a clear description, bounded instructions, explicit `allowed_tools`, timeout, iteration budget, and mutation/delegation flags, then register it. Prefer configuration overrides for local role tuning.

## Configuration

Existing config files load without migration because every setting has a conservative default:

```json
{
  "multi_agent": {
    "enabled": true,
    "max_parallel_agents": 3,
    "max_tasks_per_run": 8,
    "default_timeout_seconds": 120,
    "max_timeout_seconds": 600,
    "max_depth": 1,
    "allow_recursive_delegation": false,
    "require_review_for_mutations": true,
    "persist_runs": true,
    "retention_days": 30,
    "stream_progress": true,
    "role_overrides": {},
    "model_overrides_by_role": {}
  }
}
```

A role override may set `enabled`, `model`, `max_iterations`, `timeout_seconds`, `allowed_tools`, `can_mutate`, or `can_delegate`. Role model overrides can also be supplied through `model_overrides_by_role`. `/agents on` and `/agents off` persist the enabled flag through the normal shared Ares config.

## Execution model

`delegate_tasks_parallel` accepts task IDs, roles, prompts, dependency IDs, context, timeout, required status, and result format. The orchestrator validates missing dependencies and cycles, schedules every ready task in a deterministic wave, bounds active children with a semaphore, and returns results in the original request order even when completion order differs.

Each child is an adapter over the existing Ares `Agent`:

- new child run and session IDs plus root/parent IDs;
- separate messages, tool history, model client, timeout, and iteration budget;
- shared memory, MCP manager, skills, config, stores, action ledger, and browser lock;
- shared `ToolExecutor` so database engines, embedding infrastructure, REPLs, and watcher services are not duplicated;
- cancellation and errors normalized into structured results;
- file artifacts returned by reference rather than copied into run metadata.

The child never overwrites the root agent's `last_messages`, selected session, or conversation history.

## Tool visibility and safe concurrency

Tool security has two layers. `filter_tool_schemas()` applies exact or wildcard role allowlists before tool schemas reach the child model. `authorize_tool_call()` repeats authorization immediately before execution. Read-only agents cannot see or execute mutation, communication, shell, REPL, database-write, external-mutation, or delegation tools.

The runtime classifies calls as:

- `read_only`
- `filesystem_read`
- `filesystem_write`
- `browser_shared`
- `shell_shared`
- `repl_shared`
- `communication`
- `database_write`
- `external_mutation`
- `delegation`

Independent reads and writes to non-overlapping paths may run concurrently. Playwright and Windows desktop calls share the existing browser/desktop lock; overlapping file writes, persistent shell/REPL calls, communications, database writes, and consequential external mutations remain ordered. Tool results are assembled in the model's original call order and one failed call does not discard unrelated successes.

## Safety model

- Planner, researcher, analyst, reviewer, and synthesizer are read-only by default.
- Only an explicitly mutation-capable role sees write tools.
- A child cannot set `confirm=true`, `confirm_dangerous=true`, or related confirmation fields.
- Child shell commands that push, publish, delete, or perform unreviewed external mutation are denied.
- Communication tools are absent from default child allowlists.
- Recursive delegation is disabled, so specialists cannot create an accidental swarm.
- Normal Ares tool confirmation checks remain authoritative; delegation never bypasses them.
- When `require_review_for_mutations` is enabled, every builder task receives a dependent read-only reviewer unless the submitted graph already contains one.
- The root user-facing interaction owns any additional approval request and the final report.

## Persistence and progress

`~/.ares/data/multi_agent.db` stores bounded metadata: root/parent/child run IDs, session/task/role, prompt summary, dependencies, status, timestamps, duration, error/result summaries, bounded result content, artifact references, iterations, cancellation, and small metadata. It avoids storing entire private root prompts or large artifact bodies. Old runs are cleaned according to `retention_days`.

Stable events cover orchestration start/completion/cancellation, agent queue/start/progress/completion/failure/timeout/blocking, tool start/progress/completion, and synthesis start. The WebSocket sends these as `agent_event` objects; existing token and tool streams still deliver the normal final answer.

## Workspace and CLI

The existing Next.js chat displays a compact expandable run tree in the relevant conversation. It includes the root task, roles, dependencies, elapsed time, activity/current tool, terminal state, result, artifacts, synthesis state, and a root cancel control. With multi-agent mode disabled, the view is absent and chat behaves normally.

CLI commands:

```text
/agents
/agents status
/agents runs
/agents show RUN_ID
/agents cancel RUN_ID
/agents on
/agents off
```

Natural-language delegation uses the same runtime through `list_agents`, `delegate_task`, `delegate_tasks_parallel`, `get_agent_run`, and `cancel_agent_run`.

## Why native supervision

Ares already owns local sessions, memory, MCP lifecycle, skills, action provenance, confirmation-aware tools, watcher services, CLI/Telegram/voice/WebSocket delivery, and the shared Playwright surface. A second orchestration framework would duplicate those authorities and make cancellation, safety, and local persistence harder to reason about. The native supervisor is a small typed layer above the existing agent loop, so single-agent behavior remains the baseline and every specialist obeys the same Ares boundaries.
