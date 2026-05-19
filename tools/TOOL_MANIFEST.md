# KING Tool Manifest

This manifest is the markdown control surface for KING tool behavior. It documents the active executable tools discovered from `tools/` and the markdown-level tool contracts that guide safe use without hardcoding routing logic in code.

## Active Executable Tools

- `datetime_tool.py` - time and date capability exposed by the Python tool registry.
- `files.py` - file read, write, and list capability with path and mode validation.
- `hackernews.py` - Hacker News retrieval capability.
- `image.py` - image generation or image-related capability.
- `manifest_audit.py` - read-only audit capability for comparing this manifest with observed tool modules and registered callable schemas.
- `notes.py` - note storage and retrieval capability.
- `reddit.py` - Reddit retrieval capability.
- `terminal.py` - shell and application launch capability with configured timeout bounds.
- `web.py` - web search or page retrieval capability.
- `youtube.py` - YouTube-related capability.

The runtime registry remains the source of truth for callable schemas. This manifest must not be used as a keyword table, intent shortcut, or replacement for the registry.

## Markdown Tool Contract: tool_readiness_audit

### Purpose

`tool_readiness_audit` is a session-start and pre-registration review procedure for checking whether a requested or proposed tool is ready to be trusted by KING.

It exists because tool changes can otherwise become unsafe in two ways: a capability can be documented without verification, or a failed runtime path can be misreported as a false negative.

### Inputs

- Requested capability or improvement.
- Current repository instructions.
- Current tool registry evidence.
- Current worktree state.
- Verification commands available in the project.

### Outputs

- Capability decision: `active`, `documented_only`, `blocked`, or `needs_code`.
- Evidence checked.
- Safety and grounding limits.
- Verification command results.
- Manifest update summary.

### Error Handling

- If the registry cannot be inspected, report `blocked` and name the inspection failure.
- If the capability needs code but the current instruction says to avoid code changes, report `needs_code` and do not pretend it is active.
- If verification fails, report `blocked` with the failing command and do not register the capability as active.
- If verification is partial, report `documented_only` with the missing checks.

### Verification Method

Before a tool is described as active, verify all available project checks that apply to the changed surface. For markdown-only changes, use repository tests and type checks when present, plus direct file inspection that proves the manifest and persona references exist.

### Chaining

- Use `file_list` or direct repository inspection to identify current tool files.
- Use `file_read` or direct file inspection to load this manifest and the persona.
- Use `terminal` or direct command execution for project tests and type checks.
- Report only evidence returned by those tools or commands.

### Known Limits

- This contract does not add a Python callable by itself.
- This contract cannot override registry schemas.
- This contract cannot make a failed or unavailable tool count as success.
- This contract is not an intent classifier and must not be expanded into keyword rules.

## Markdown Tool Contract: verification_gauntlet

### Purpose

`verification_gauntlet` is the standard evidence checklist for any KING tool, prompt, manifest, or runtime behavior change.

It exists because a change is not shipped until the project can prove what changed, what was checked, and what risk remains.

### Inputs

- Changed file list.
- Changed surface type: markdown contract, persona behavior, Python runtime, frontend, storage, or generated artifact.
- Available project commands.
- Expected user-visible behavior.
- Known constraints from the current repository instructions.

### Outputs

- Check plan matched to the changed surface.
- Commands or inspections executed.
- Pass, fail, blocked, or skipped status for each check.
- Exact warning or failure text when present.
- Final ship decision: `ship`, `hold`, or `document_only`.

### Required Checks

- For markdown contracts or persona behavior: inspect the changed markdown, prove the expected heading or reference exists, run the Python tests when present, and run frontend type checking when the frontend is present.
- For Python runtime behavior: run the targeted Python tests, then run the broader available Python test suite.
- For frontend behavior: run TypeScript type checking and the relevant build or browser verification available in the project.
- For storage or generated artifacts: prove the artifact path, size, and intended scope; do not treat generated runtime state as source code.

### Error Handling

- If a command cannot run because of permissions, report the permission boundary separately from code failure.
- If a command is unavailable, report `blocked` and name the missing command.
- If a check is not relevant to the changed surface, mark it `skipped` with the reason.
- If any required check fails, set the final ship decision to `hold`.

### Chaining

- Run `tool_readiness_audit` before using this contract to register a new capability.
- Use this contract after every manifest or persona update.
- Feed the resulting ship decision back into the session report so KING does not claim success without evidence.

### Known Limits

- This contract does not replace real tests.
- This contract does not allow network-only verification when local checks are available.
- This contract does not permit broad success claims from narrow evidence.

## Markdown Tool Contract: permission_risk_gate

### Purpose

`permission_risk_gate` is the pre-execution safety review for tool calls that can read private data, change files, launch apps, run commands, modify storage, access accounts, download media, or affect the operating system.

It exists so KING can be useful without pretending every available tool is equally safe or equally authorized.

### Inputs

- User request.
- Candidate tool name and callable schema from the active registry.
- Proposed arguments.
- Current working directory and intended target scope.
- Whether the operation is read-only, write, external network, account-bound, app-launching, or destructive.
- Whether a dry-run or preview mode is available.

### Outputs

- Risk decision: `allow`, `confirm`, `dry_run_first`, or `block`.
- Reason tied to the requested action and proposed scope.
- Required evidence before reporting success.
- Safe next step if the action is blocked or needs confirmation.

### Risk Decisions

- `allow` - read-only or low-impact action inside the requested scope.
- `confirm` - write, launch, account, network, or system state action where the user intent is present but impact should be acknowledged.
- `dry_run_first` - destructive, broad, recursive, bulk, or hard-to-undo action where a preview can reduce risk.
- `block` - action lacks user intent, exceeds available permission, targets secrets without need, or cannot be bounded safely.

### Error Handling

- If the proposed arguments do not match the active schema, return `block` and ask for corrected arguments or a different tool.
- If the target scope is ambiguous, return `confirm` or `dry_run_first`; do not infer a broad target.
- If the tool fails, times out, or is unavailable, report that runtime state as the result. Do not convert it into success or into a claim that the requested thing does not exist.
- If a destructive action has no dry-run path and no explicit user confirmation, return `block`.

### Chaining

- Use this contract before terminal, file write, app launch, download, account, or storage-changing operations.
- Use `verification_gauntlet` after a tool or manifest change that alters risk handling.
- Use `tool_readiness_audit` before documenting any new callable as active.

### Known Limits

- This contract does not grant permissions.
- This contract does not replace operating system safeguards.
- This contract does not authorize secret access unless the user request and tool scope require it.
- This contract must not be implemented as phrase matching or keyword routing.

## Markdown Tool Contract: structured_error_envelope

### Purpose

`structured_error_envelope` is the standard failure-reporting contract for KING tool calls and toolchain verification.

It exists so errors surface clearly without leaking raw stack traces, secrets, private tokens, or unsupported claims about the outside world.

### Inputs

- Tool or check name.
- Requested operation.
- Target scope that was actually attempted.
- Runtime state: unavailable, blocked, timed out, failed, empty, partial, or succeeded.
- Exit code or tool status when available.
- Safe stdout, stderr, warning, or result summary after secret review.

### Outputs

- Status: `success`, `partial`, `empty`, `blocked`, `timeout`, `failed`, or `unavailable`.
- Evidence summary tied only to the attempted scope.
- User-safe error detail.
- Retry guidance when retry is safe and bounded.
- Next grounded action when retry is not appropriate.

### Error Handling

- Never expose credentials, tokens, private keys, cookies, session values, or full raw traces.
- Never collapse `empty`, `blocked`, `timeout`, `failed`, or `unavailable` into the same answer.
- Never claim the requested thing does not exist when only one source, account, file, app, or command scope was checked.
- If a retry is recommended, keep it bounded by the tool's configured timeout and retry policy.
- If a failure could have changed state partially, report the partial state and require verification before attempting another write.

### Chaining

- Use this contract after `permission_risk_gate` for sensitive operations.
- Use this contract inside `verification_gauntlet` whenever a check fails, warns, or is skipped.
- Use this contract in final user reports so KING's answer matches the evidence instead of smoothing over runtime limits.

### Known Limits

- This contract does not hide real failures.
- This contract does not create retry support where the callable lacks it.
- This contract does not make narrow evidence broad.
- This contract must not become canned response text.

## Markdown Tool Contract: idempotent_write_guard

### Purpose

`idempotent_write_guard` is the standard pre-write and retry-safety contract for KING operations that create, modify, append, launch, download, enqueue, publish, or otherwise change state.

It exists so retries, repeated user confirmations, interrupted sessions, and follow-up requests do not silently duplicate side effects.

### Inputs

- User request and current-turn confirmation evidence.
- Candidate tool name and callable schema from the active registry.
- Proposed write target or state-changing target.
- Proposed action type: create, overwrite, append, delete, launch, download, enqueue, publish, or configure.
- Existing state evidence when available.
- Idempotency key or stable operation fingerprint when the callable supports one.
- Dry-run or preview result when available.

### Outputs

- Write decision: `safe_once`, `needs_existing_state`, `needs_idempotency_key`, `dry_run_first`, or `block`.
- The target scope that will change.
- The duplicate-risk assessment.
- Required verification after the write.
- Recovery note when the write may have partially completed.

### Decision Rules

- `safe_once` - the target scope is explicit, duplicate risk is low, and success can be verified.
- `needs_existing_state` - current state must be checked before deciding whether a write is new, duplicate, or partial.
- `needs_idempotency_key` - the callable supports repeated writes and should receive a stable operation identity before execution.
- `dry_run_first` - the operation is broad, destructive, external, or difficult to undo and a preview is available.
- `block` - the operation has unclear target scope, no user intent, no verification path, or unacceptable duplicate risk.

### Error Handling

- If a write times out, treat the outcome as unknown until the target state is checked.
- If a retry is attempted, first compare the intended target with observed state instead of repeating blindly.
- If a partial write is detected, report the completed and missing parts separately through `structured_error_envelope`.
- If no idempotency key support exists, use explicit target-state verification rather than inventing a hidden key.

### Chaining

- Run `permission_risk_gate` before this contract for sensitive writes.
- Use `structured_error_envelope` for unknown, partial, failed, or blocked write outcomes.
- Use `verification_gauntlet` after manifest changes and after tool changes that alter write behavior.

### Known Limits

- This contract does not add idempotency support to a callable by itself.
- This contract does not authorize writes outside the user's requested scope.
- This contract does not allow hidden persistent side effects.
- This contract must not be implemented with hardcoded operation names or phrase triggers.

## Markdown Tool Contract: tier1_upgrade_contract

### Purpose

`tier1_upgrade_contract` is the documented-only upgrade contract for the Tier 1
tool surfaces identified in the current KING upgrade session:
`registry_dispatch`, `terminal`, and `file_write`.

It exists so the highest-risk upgrades have a visible compatibility and
verification standard before executable code is changed.

### Status

- Current status: `documented_only`.
- Runtime callable schemas are unchanged until code edits are explicitly
  allowed and verification passes.
- Existing callers must keep legacy behavior unless they opt in to a new
  optional capability after implementation.

### Inputs

- Tool name under upgrade.
- Current callable schema from the active registry.
- Existing arguments that worked before the upgrade.
- Proposed optional arguments and their defaults.
- Current repository instructions.
- Verification evidence from the project test and typecheck commands.

### Outputs

- Compatibility decision: `preserved`, `at_risk`, or `blocked`.
- Optional capabilities planned for the tool.
- Error codes planned for structured mode.
- Trace fields planned for observable mode.
- Verification result: `documented_only`, `verified_runtime`, or `hold`.
- User-change preservation note.

### Tier 1 Contracts

#### registry_dispatch

- Compatibility rule: legacy dispatch keeps returning the same string shape
  unless the caller explicitly requests structured output.
- Runtime optional inputs: `response_format`, `trace_enabled`, and
  `timeout_ms`.
- Structured error codes: `TOOL_NOT_FOUND`, `UNKNOWN_PARAMETER`,
  `INVALID_TIMEOUT`, `TOOL_TYPE_ERROR`, `TOOL_EXECUTION_ERROR`, and
  `TOOL_TIMEOUT`.
- Trace requirement: trace only when enabled until runtime performance is
  measured.
- Runtime status: `verified_runtime`.

```json
{
  "name": "registry_dispatch",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T08:38:18Z",
  "purpose": "Dispatch registered tools with legacy string output by default and optional structured results, typed errors, traces, and bounded timeout handling.",
  "inputs": {
    "name": "string: registered tool name",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false",
    "timeout_ms": "integer 1..60000: optional, default unset",
    "kwargs": "object: registered tool arguments"
  },
  "outputs": {
    "legacy": "string: unchanged default output",
    "structured_success": "object: result plus meta",
    "structured_error": "object: error plus meta",
    "trace": "json object: emitted only when trace_enabled is true"
  },
  "new_capabilities": ["response_format", "trace_enabled", "timeout_ms"],
  "error_codes": ["TOOL_NOT_FOUND", "UNKNOWN_PARAMETER", "INVALID_TIMEOUT", "TOOL_TIMEOUT", "TOOL_TYPE_ERROR", "TOOL_EXECUTION_ERROR"],
  "has_trace": true,
  "timeout_ms": 60000,
  "retry": false,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T08:38:18Z",
  "score": 9.5
}
```

#### terminal

- Compatibility rule: existing `command`, `workdir`, and `timeout` behavior
  remains the default.
- Planned optional inputs: `dry_run`, `max_output_chars`, and
  `response_format`.
- Planned structured error codes: `DIRECTORY_NOT_FOUND`, `COMMAND_TIMEOUT`,
  `COMMAND_NOT_FOUND`, `PERMISSION_DENIED`, `SYSTEM_COMMAND_ERROR`, and
  `INVALID_TIMEOUT`.
- Trace requirement: record execution metadata and bounded output metadata, not
  file contents, secrets, or broad system claims.
- Runtime status: `documented_only`.

#### file_write

- Compatibility rule: existing `overwrite`, `append`, and `create_new` modes
  keep their legacy string outputs by default.
- Planned optional inputs: `dry_run`, `create_parent_dirs`, and
  `response_format`.
- Planned structured error codes: `INVALID_WRITE_MODE`, `FILE_ALREADY_EXISTS`,
  `PARENT_DIRECTORY_NOT_FOUND`, `PARENT_CREATE_FAILED`, `PERMISSION_DENIED`,
  and `WRITE_FAILED`.
- Trace requirement: record mutation metadata only; never record written
  content in traces.
- Runtime status: `documented_only`.

### Error Handling

- If code edits are not allowed, return `documented_only` and do not claim that
  the callable has the optional inputs.
- If the existing schema cannot be inspected, return `blocked`.
- If an optional input would become required, mark the upgrade `blocked`.
- If a field rename is proposed, mark the upgrade `at_risk` and require user
  confirmation before implementation.
- If V-01 cannot compare legacy behavior, mark the runtime upgrade `hold`.

### Verification Method

- For documented-only changes, inspect this manifest and the session artifact,
  then run available Python tests and frontend typecheck.
- For runtime implementation, run V-01 against the exact old inputs before
  checking new capabilities.
- If V-01 fails, do not update the runtime status beyond `hold`.
- Record verification evidence in `tools/TOOL_UPGRADE_SESSION.md`.

### Chaining

- Use `tool_readiness_audit` before turning this documented contract into code.
- Use `permission_risk_gate` before exercising `terminal` or `file_write` with
  side effects.
- Use `structured_error_envelope` for any failed or partial verification.
- Use `verification_gauntlet` before reporting a runtime upgrade as shipped.

### Known Limits

- This contract does not add optional inputs to the Python callables by itself.
- This contract does not prove runtime trace emission exists.
- This contract does not replace V-01.
- This contract must not be used as a router, keyword table, or shortcut around
  the active registry.

## Markdown Tool Contract: bounded_retry_timeout_policy

### Purpose

`bounded_retry_timeout_policy` is the standard contract for tool retries, timeouts, and circuit breaking.

It exists so KING can recover from transient failures without hanging, looping indefinitely, repeating unsafe writes, or hiding latency problems.

### Inputs

- Tool name and callable schema from the active registry.
- Requested operation and target scope.
- Configured timeout default and maximum when available.
- Caller-provided timeout when present.
- Retry safety decision from `idempotent_write_guard` for state-changing operations.
- Last observed status from `structured_error_envelope`.

### Outputs

- Retry decision: `do_not_retry`, `retry_once`, `retry_bounded`, or `circuit_break`.
- Effective timeout selected from configuration and caller input.
- Maximum attempt count.
- Backoff plan.
- Stop reason and final status.
- Verification evidence required before reporting success.

### Decision Rules

- Never retry a write, launch, publish, delete, download, append, or enqueue operation unless `idempotent_write_guard` has marked the retry safe.
- Retry only transient states such as timeout, temporary network failure, temporary service failure, or rate pressure.
- Do not retry schema errors, permission blocks, missing tools, invalid targets, or user-denied actions.
- Clamp caller-provided timeouts to the callable's configured minimum and maximum.
- Use a finite attempt budget; the default ceiling is three total attempts when the callable does not provide a stricter policy.
- Stop immediately when a retry would exceed the user's requested scope or current permission boundary.

### Error Handling

- If the timeout source cannot be inspected, use the callable's current behavior and report the policy as `documented_only`.
- If retries are exhausted, report the final state through `structured_error_envelope`.
- If a retry produces a partial result, stop and verify state before any further action.
- If latency exceeds the acceptable path for repeated use, feed the evidence back into `tool_readiness_audit` instead of adding a hidden shortcut.

### Chaining

- Use this contract after `permission_risk_gate` and before retrying any sensitive or state-changing operation.
- Use `idempotent_write_guard` for every retryable write decision.
- Use `verification_gauntlet` after runtime changes that alter timeout, retry, or circuit-break behavior.

### Known Limits

- This contract does not add runtime retry code by itself.
- This contract does not override configured tool timeout bounds.
- This contract does not permit infinite loops or background retries after the user-visible turn ends.
- This contract must not become a keyword-based latency shortcut.

## Maintenance Rules

- Update this manifest whenever a tool is added, removed, renamed, or given a new safety contract.
- Keep tool status tied to observed files and registry evidence.
- Do not add credentials, private paths, canned user-facing replies, or phrase triggers here.
- Prefer a `documented_only` status over claiming a capability is active without runtime proof.

## Evolution Log

- 2026-05-19T08:38Z - Upgraded `registry_dispatch` runtime to version 2.0.0 with legacy-preserving structured output, typed errors, optional traces, and bounded timeout handling.
- 2026-05-19T08:42Z - Added executable `tool_manifest_audit` in `manifest_audit.py`, imported it into the active toolchain, and documented its manifest entry.
- 2026-05-19T08:39Z - Added `bounded_retry_timeout_policy` as a markdown tool contract for finite retries, timeout clamping, and circuit-break decisions.
- 2026-05-19T08:30Z - Added `tier1_upgrade_contract` as a documented-only manifest contract for registry dispatch, terminal execution, and file writing upgrades.
- 2026-05-19T08:30Z - Added `idempotent_write_guard` as a markdown tool contract for duplicate-safe writes, retries, and partial-state recovery.
- 2026-05-19T08:26Z - Added `structured_error_envelope` as a markdown tool contract for safe, scoped, non-silent failure reporting.
- 2026-05-19T08:18Z - Added `permission_risk_gate` as a markdown tool contract for scoped allow, confirm, dry-run-first, and block decisions before sensitive tool execution.
- 2026-05-19T08:08Z - Added `verification_gauntlet` as a markdown tool contract so future toolchain changes have a reusable ship, hold, or document-only evidence path.
