# KING New Tool Intake Checklist

This checklist is the minimum documentation and verification path before KING
should describe a new capability as available.

It is not executable routing logic. It must not become a keyword table, phrase
matcher, or shortcut around the active runtime registry.

## Intake Decision

Before adding or claiming a tool, record one of these outcomes:

- `use_existing_tool` - an active tool already covers the request.
- `documented_only` - a design or contract is useful, but no callable exists
  yet.
- `needs_code` - the capability requires runtime implementation.
- `blocked` - the capability is unsafe, unavailable, out of scope, or cannot be
  verified.
- `verified_runtime` - executable behavior exists, is registered, and has passed
  the required evidence path.

## Required Intake Questions

- What user action or information need does this capability satisfy?
- Which active callable schema proves the capability exists?
- What target scope is the tool allowed to inspect, mutate, launch, download,
  generate, or publish?
- Which existing tool contracts apply: readiness, permission risk, idempotent
  write, provider failure, bounded retry, structured error, or verification
  pipeline?
- What exact evidence will prove success?
- What outcome states must remain separate: empty, blocked, timeout, failed,
  partial, unavailable, or unknown after attempt?
- What user-owned data, credentials, local files, apps, accounts, or generated
  artifacts could be touched?

## Documentation Requirements

- Add or update `TOOL_MANIFEST.md` when an executable module is added, removed,
  renamed, or given a new contract.
- Add or update `TOOL_EVIDENCE_LEDGER.md` when a capability status changes.
- Add provider-backed behavior to `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` when the
  tool depends on a web, media, generation, download, browser, or app provider.
- Add implementation and verification notes to `TOOL_UPGRADE_SESSION.md`.
- Keep user-facing reporting grounded in actual callable schemas and verified
  outputs. Do not add canned tool replies, phrase triggers, or prewritten
  success/failure messages.

## Runtime Requirements

- Keep legacy behavior compatible unless the user explicitly accepts a breaking
  change.
- Make new controls optional by default.
- Prefer structured results for new evidence surfaces, while preserving legacy
  text where existing callers depend on it.
- Return typed failure states for schema errors, permission blocks, timeout,
  provider failure, no results, partial results, and unavailable dependencies.
- Avoid automatic retries for state-changing tools unless idempotency or target
  state verification proves retry safety.

## Verification Requirements

- Run the default markdown verification pipeline after intake docs or runtime
  changes.
- Add targeted tests for any new runtime behavior before claiming
  `verified_runtime`.
- Prove manifest alignment after adding or removing a module.
- Prove downstream validator behavior when a registry or schema-facing tool
  changes.
- Prove no side effect happened for dry-run or blocked paths.
- Record any warning that does not fail the pipeline, especially cache,
  permission, provider, or transport warnings.

## Claim Boundaries

- Do not claim a tool is active because it is planned in markdown.
- Do not claim a tool succeeded because a provider returned a partial result.
- Do not claim playback, download, file mutation, launch, delete, publish, or
  memory write without result evidence for that exact side effect.
- Do not claim a missing result is globally absent when only one scope,
  provider, file, account, or app was checked.
- Do not claim transport, credential, or account safety unless the runtime
  evidence proves it.

## Done State

A new tool or tool upgrade is ready to describe as `verified_runtime` only when:

- the active registry exposes the callable schema;
- the manifest and evidence ledger are updated;
- targeted tests prove the claimed new behavior;
- the default verification pipeline returns `ship`;
- remaining limits are documented without smoothing them over.
