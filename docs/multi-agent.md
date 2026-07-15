# Native multi-agent mode

Ares has one user-facing root agent and a native supervisor for bounded specialist work. The supervisor is part of Ares; it does not add CrewAI, AutoGen, LangGraph, or another authority layer. The root owns routing, approval, cancellation, synthesis, and the final answer.

For implementation details and invariants, see [Multi-agent architecture](multi-agent-architecture.md).

## Know what actually ran

Three kinds of work are deliberately kept separate:

| Mechanism | What executes | Agent count | Persistence |
|---|---|---:|---|
| Native agents | Independent specialist model loops with child run IDs, child sessions, role policy, results, and a root manifest | One per recorded child run | Multi-agent run store when enabled |
| Parallel tool calls | Several ordinary tools scheduled by one model loop | Zero | Normal tool/action history |
| Durable workflows | Ordered `create_task`/`run_task` records that can be resumed or inspected later | Zero | Task/workflow store |

A plan, a workflow task, a concurrent tool batch, or prose saying “three researchers” is not evidence that agents ran. Ares reports an agent count only from the current session’s `AgentExecutionManifest`. When native mode is disabled or a requested delegation cannot start, explicit requests fail honestly with zero claimed agents; they are not silently relabeled as workflows or parallel tool calls.

## Deterministic routing

Routing happens before the general root model sees its normal tool list. The router uses only the current user turn and live runtime availability.

- **Explicit delegation:** phrases such as “use three agents,” “separate researchers,” “multi-agent mode,” or “a builder and reviewer” must use native agents. Ares creates a bounded native plan or returns an explicit reason that no agents ran. It does not substitute `create_task`, browser work, or ordinary parallel tools.
- **Automatic delegation:** meaningful independent workstreams may be delegated when the deterministic router can identify at least two useful tasks—for example separate backend/frontend inspection or implementation followed by review. Small edits, one lookup, greetings, thanks, and overlapping stateful work stay single-agent.
- **Agent meta-questions:** “How many agents did you use?” and run-management questions query the latest manifest in the current session. They neither launch a new team nor load a browser skill/tool.
- **No delegation:** normal conversation and work without a useful independent split continue through the root agent.

Typical explicit requests:

- “Research FastAPI, Flask and Django in parallel using separate researchers, then synthesize a recommendation.”
- “Inspect the backend and frontend separately, then create an implementation plan.”
- “Have a builder implement this feature and a reviewer verify the changes.”
- “Use an analyst, documentation researcher, and verifier for this bug.”

### Current-turn authority

Every request gets an immutable `TurnExecutionContext` built from the current user message. Conversation history can explain the request but cannot authorize an action. A system-level current-turn guard is placed after history, and tool authorization is repeated immediately before execution.

A greeting such as `hey` is classified as conversation: it authorizes no tools, skills, workflow mutation, continuation, or delegation. `continue` or `resume` authorizes only bounded lookup of prior task/run state until the user explicitly names new work. A confirmation such as `yes` is not ambient permission; consequential execution requires an exact unexpired grant issued for the pending call.

## Specialist roles

| Role | Default responsibility | Default mutation authority |
|---|---|---:|
| `planner` | Dependencies, scope, and success criteria | No |
| `researcher` | Web/docs/read-only MCP evidence with sources | No |
| `analyst` | Repository structure, integration points, risks, and tests | No |
| `builder` | Approved implementation and relevant verification | Files/code/shell inside its assigned workspace |
| `reviewer` | Correctness, regressions, security, architecture, and missing tests | No |
| `synthesizer` | Evidence-preserving combination of specialist results | No |

Definitions come from `ares.multi_agent.default_agent_specs()`. Each `AgentSpec` has instructions, a visible tool allowlist, independent capability grants, model/iteration/timeout budgets, retry policy, and mutation/delegation flags. Local configuration can disable or narrow a role. Recursive delegation is off by default, so specialists do not create a swarm.

## Execution and child isolation

The supervisor validates duplicate IDs, missing dependencies, dependency cycles, roles, depth, and task limits before launch. Ready tasks run in deterministic waves and results return in the submitted task order even if completion order differs. A dependent task runs only after its dependencies reach a terminal state; failed dependencies block it unless partial dependencies were explicitly allowed.

Every native run has:

- one root run ID and parent session ID;
- a unique child run ID and unique child session ID for every specialist;
- root/parent/request IDs carried through events, grants, results, and persistence;
- separate model messages, tool history, iteration budget, timeout, and `LLMClient` per child;
- shared heavyweight local services such as stores, MCP lifecycle, tool executor, and resource coordinator without sharing child chat history.

Children run in `bounded_specialist` context mode. They receive the bounded assignment, explicitly passed shared constraints, task-specific context, dependency results, and run IDs. Root conversation history, root `last_messages`, global recent chat, profile/soul context, memories, and automatic skill context are not copied. Optional context categories are deny-by-default; the current implementation recognizes `project_files` only when the task explicitly allows it. Dependency output is treated as untrusted evidence and is included only for declared dependencies.

## Tool policy, capabilities, and action grants

Tool control has two independent gates:

1. `filter_tool_schemas()` hides tools outside the role allowlist or capability set before the child model call.
2. `authorize_tool_call()` checks the same boundaries again immediately before execution, including workspace paths and grants.

Capabilities are granular: `filesystem_read`, `filesystem_write`, `code_execution`, `shell_execution`, `browser_read`, `browser_interaction`, `database_read`, `database_write`, `communication`, `external_mutation`, and `delegation`. A tool being named in an allowlist is insufficient if the role lacks its required capability.

Children cannot manufacture `confirm=true`, `confirm_dangerous=true`, or equivalent fields. Browser interaction, communication, external mutation, and dangerous/indirect shell or REPL actions require a root-issued action grant. Each grant is:

- created only after explicit user confirmation;
- bound to one root run, child run, request ID, tool name, and canonical argument hash;
- expiring according to `action_grant_ttl_seconds`;
- single-use and revoked when the root run completes or is cancelled.

Changing an argument, tool, request, child, or root invalidates the grant. Children receive only the opaque grant ID and cannot create, extend, rewrite, or reuse it.

## Resource and worktree isolation

One root-owned `ResourceCoordinator` is shared across the root and every child. It provides path-aware read/write leases for files and serialized locks for stateful surfaces:

- overlapping file writes, or reads overlapping a write, serialize; disjoint known paths may overlap;
- unknown file scope is conservative and conflicts with writes;
- Playwright/desktop browser access shares one browser lock;
- persistent shell and Python/REPL calls serialize and also reserve unknown filesystem, database, communication, and external-mutation state;
- database writes, communications, external mutations, and delegation each have a serialized lane;
- `provider_max_concurrency` can bound concurrent MCP provider calls.

When a run contains multiple mutation-capable builders and `builder_worktree_isolation` is enabled, Ares attempts one detached Git worktree per builder. It uses a worktree only when Git is available, the repository is valid and clean, and the target is safe. Otherwise the builder receives the live repository and all mutation-capable children use one live-tree mutation slot, so conflicting builders serialize. The chosen workspace and fallback reason are recorded in run metadata.

Isolated worktree output is never silently merged by a child or reviewer. The adapter captures a binary Git patch artifact and a dependent reviewer may end with `APPROVE_PATCH`, but that marker only records a review verdict. By default the patch remains `held_for_root_approval`. Applying it requires the `auto_apply_builder_patches` opt-in plus an exact, single-use root-issued grant bound to the patch hash, repository, run, and child. A missing/rejected review, missing approval grant, dirty target tree, or apply conflict retains the artifact for manual application instead of modifying the repository.

Builders do not receive general shells or REPLs. Repository owners may configure named checks in `[tool.ares.agent_checks]` (for example `python -m pytest -q`, `python -m compileall -q ares tests`, `npm run lint`). `run_project_check` accepts only one of those pre-worktree snapshot names, runs it in the isolated worktree without a shell, and rejects pipes, redirects, nested interpreters, installers, publish commands, and direct network tools. Exit code and bounded output are recorded in the child manifest.

## Source-backed research

Researchers are instructed to prefer primary sources and preserve URLs, evidence, confidence, caveats, publication dates, and benchmark conditions. Structured results use claim objects with:

```json
{
  "claim": "A bounded claim",
  "source_urls": ["https://example.com/primary-source"],
  "evidence": ["Quoted or paraphrased support"],
  "confidence": 0.8,
  "caveats": ["Known limitation"],
  "publication_dates": ["2026-07-01"],
  "benchmark_conditions": ["Environment and workload, when numeric performance is claimed"]
}
```

The adapter parses researcher, synthesizer, and reviewer output and records `research_validation` in child metadata. Missing/invalid URLs, evidence-free exact figures, and performance figures without benchmark conditions are flagged. Synthesis must retain source URLs, conditions, uncertainty, and disagreement; helper policy caps synthesized confidence at the strongest underlying claim. Validation metadata is evidence for the root—it does not turn an invalid claim into a successful fact.

## Manifests, truthful counts, and persistence

`AgentExecutionManifest` is the authority for what ran. It contains:

- root run, request, and owning session IDs;
- root status, start/completion time, and duration;
- exact child count;
- ordered execution waves;
- for each child: run/task/role IDs, child and parent sessions, dependencies, status, timing, tools, artifacts, error, iterations, and bounded metadata;
- partial-result and builder-workspace metadata where applicable.

Only `len(manifest.child_runs)` is reported as the agent count. Parallel root tools remain zero agents. Agent meta-questions use the latest manifest for the selected session and answer from its recorded IDs, roles, waves, tools, and sources rather than from earlier assistant prose.

With `persist_runs=true`, `~/.ares/data/multi_agent.db` stores bounded root/child metadata, manifest JSON, launch plans, checkpoints, result summaries/content, artifacts by reference, and progress state. Large artifact bodies and entire private root prompts are not stored. Retention cleanup uses `retention_days`. A restarted runtime marks abandoned queued/running work `interrupted`; `resume_agent_run` reuses successful read-only child checkpoints and reruns only unfinished read-only work. It refuses unfinished mutation-capable work rather than guessing retry idempotence.

## Session ownership, events, and cancellation

Run reads and cancellation are ownership-checked. CLI uses the active Ares session; workspace numeric IDs normalize to `conversation-N`; Telegram uses `telegram-N`. `get_run`, `list_runs`, latest-run lookup, cancellation, events, and artifact discovery are filtered to that parent/child session relationship.

The WebSocket keeps the selected conversation per connection. A newly connected client receives no global run details until it selects a conversation. `agent_event`, `agent_runs`, cancellation, and artifacts are scoped to that selection. Telegram progress subscriptions are scoped to the originating chat, so concurrent chats do not see or cancel each other’s teams.

Cancellation targets the root task, propagates to unfinished children, records terminal `cancelled` states, emits cancellation events, and revokes action grants. A completed or foreign-session run cannot be cancelled through that surface.

## CLI operations

Commands are local and session-scoped:

```text
/agents
/agents status
/agents active
/agents roles
/agents runs [LIMIT]
/agents show RUN_ID
/agents cancel RUN_ID
/agents resume RUN_ID
/agents run REQUEST
/agents doctor
/agents smoke-test
/agents on
/agents off
```

- `/agents run REQUEST` forces a real native delegation using the default enabled research specialist. It uses the configured model/provider.
- `/agents doctor` is read-only and model-free. It reports enabled state, runtime initialization, active runs, SQLite health/path, role policy, delegation-schema visibility, resource locks, limits, and the active-run disable policy.
- `/agents smoke-test` launches two harmless real read-only specialists in one wave. It verifies the configured runtime but uses the configured model/provider and may incur provider usage.
- `/agents on` and `/agents off` persist `multi_agent.enabled` in the shared config. Turning delegation off does not disable normal Ares chat.

Telegram intentionally exposes inspection and cancellation but not remote enable/disable, forced runs, doctor, or provider-backed smoke tests:

```text
/agents status
/agents active
/agents roles
/agents runs [LIMIT]
/agents show RUN_ID
/agents cancel RUN_ID
/agents resume RUN_ID
/workers
```

Natural-language routing and the root tool plane use `list_agents`, `delegate_task`, `delegate_tasks_parallel`, `get_agent_run`, `list_agent_runs`, `get_latest_agent_run`, `cancel_agent_run`, and `resume_agent_run`. Resume reuses successful read-only child checkpoints and never automatically replays unfinished mutation-capable work.

## Configuration reference

All fields have defaults, so existing configuration files load without migration:

```json
{
  "multi_agent": {
    "enabled": true,
    "max_parallel_agents": 3,
    "max_tasks_per_run": 8,
    "default_timeout_seconds": 120.0,
    "max_timeout_seconds": 600.0,
    "max_total_duration_seconds": 900.0,
    "max_total_iterations": 80,
    "max_retries_per_task": 1,
    "retry_backoff_seconds": 0.5,
    "max_depth": 1,
    "allow_recursive_delegation": false,
    "require_review_for_mutations": true,
    "review_role": "reviewer",
    "persist_runs": true,
    "retention_days": 30,
    "stream_progress": true,
    "role_overrides": {},
    "model_overrides_by_role": {},
    "fallback_models_by_role": {},
    "partial_result_synthesis": true,
    "checkpoint_runs": true,
    "action_grant_ttl_seconds": 300.0,
    "provider_max_concurrency": 0,
    "builder_worktree_isolation": true,
    "builder_worktree_root": "~/.ares/agent-worktrees",
    "cancel_active_on_disable": false
  }
}
```

`provider_max_concurrency=0` means no additional provider semaphore. `max_parallel_agents` still bounds specialist loops. Per-task timeouts are capped by `max_timeout_seconds`; each run is also bounded by `max_total_duration_seconds` and shares `max_total_iterations` across its children.

A role override may set `enabled`, `model`, `max_iterations`, `timeout_seconds`, `allowed_tools`, `can_mutate`, `can_delegate`, `capabilities`, `retry_limit`, `retry_backoff_seconds`, and `fallback_models`:

```json
{
  "multi_agent": {
    "role_overrides": {
      "researcher": {
        "model": "provider/model-name",
        "allowed_tools": ["web_search", "fetch_url", "mcp__fetch__*"],
        "capabilities": ["browser_read"],
        "max_iterations": 6,
        "timeout_seconds": 180,
        "retry_limit": 1,
        "fallback_models": ["provider/fallback-model"]
      },
      "builder": {
        "enabled": false
      }
    }
  }
}
```

Do not use an allowlist or `can_mutate=true` as a substitute for the smallest required `capabilities` set.

### Live configuration lifecycle

Configuration reload builds a candidate role registry before swapping it into the runtime. New limits and roles apply to future runs; active runs keep their immutable launch snapshot and normally drain. Disabling rejects new delegation immediately. Set `cancel_active_on_disable=true` only when disabling should cancel active roots.

Changing `persist_runs` or the data directory while a run is active is rejected so a single manifest cannot split across stores. Wait for completion or cancel first. Provider-concurrency changes take effect after active runs drain. Retention cleanup runs when persistence initializes/reloads.

## Failure behavior

- Explicit delegation that is disabled, unavailable, over limit, missing a role, unauthorized, timed out, or rejected by the provider returns a concrete failure and does not claim success or pseudo-agents.
- Mutation tasks require the configured review role when `require_review_for_mutations=true`; launch fails if adding that review would exceed the task limit or the role is unavailable.
- Provider failures are retried only when the adapter can prove no tool started. Consequential work is never guessed safe to retry. Retry count/backoff and role fallback models are bounded by configuration.
- A failed dependency blocks downstream work unless the task explicitly allows partial dependencies. With `partial_result_synthesis=true`, a synthesizer can report successful evidence plus failures/caveats; the manifest marks partial results.
- Per-task and total timeouts become explicit `timed_out` states. Exceptions become `failed`; dependency suppression becomes `blocked`; user cancellation becomes `cancelled`.
- A failed agent never disappears from the manifest, and one success never changes another child’s terminal state.

## Offline acceptance harness

The deterministic harness exercises hardening-plan scenarios A–E with fake specialist executors and no model, browser, network, MCP, or paid API:

```bash
python -m ares.multi_agent_smoke
python -m pytest -q -p no:cacheprovider tests/test_multi_agent_smoke.py
```

It verifies:

- `hey` executes no tools/skills/continuation;
- three researcher children run in one real orchestration wave, synthesis follows, official source URLs survive, and no durable workflow substitutes for agents;
- meta-introspection reads the latest session manifest with exact counts/roles/waves and no browser;
- disabled mode reports zero agents honestly;
- two builders scheduled in the same wave serialize a conflicting temporary-file write and a dependent reviewer detects any lost update.

Broader validation from the repository root:

```bash
python -m compileall -q ares tests
python -m pytest -q
cd ares-workspace
npm ci
npm run lint
npm run typecheck
npm run build
```
