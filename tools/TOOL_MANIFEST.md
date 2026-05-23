# KING Tool Manifest

This manifest is the markdown control surface for KING tool behavior. It documents the active executable tools discovered from `tools/` and the markdown-level tool contracts that guide safe use without hardcoding routing logic in code.

## Active Executable Tools

- `browser.py` - browser or HTTP page loading with markdown-configured field extraction.
- `datetime_tool.py` - time and date capability exposed by the Python tool registry.
- `files.py` - file read, write, and list capability with path and mode validation.
- `hackernews.py` - Hacker News retrieval capability.
- `image.py` - image generation or image-related capability.
- `manifest_audit.py` - read-only audit capability for comparing this manifest with observed tool modules and registered callable schemas.
- `navigator.py` - open-provider route distance, straight-line distance, travel estimate, and place detail capability.
- `notes.py` - note storage and retrieval capability.
- `reddit.py` - Reddit retrieval capability.
- `terminal.py` - shell and application launch capability with configured timeout bounds.
- `verification_pipeline.py` - markdown-driven verification pipeline for bounded local checks and ship or hold evidence.
- `web.py` - web search or page retrieval capability.
- `youtube.py` - YouTube-related capability.

The runtime registry remains the source of truth for callable schemas. This manifest must not be used as a keyword table, intent shortcut, or replacement for the registry.

## Markdown Control Files

- `TOOL_MANIFEST.md` - active executable tool inventory and markdown contracts.
- `TOOL_VERIFICATION_PIPELINE.md` - default checks consumed by `tool_verification_pipeline`.
- `TOOL_EVIDENCE_LEDGER.md` - evidence standards and current capability claim status.
- `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` - provider, fallback, timeout, partial, and empty-result reporting rules.
- `TOOL_INTAKE_CHECKLIST.md` - required intake questions, documentation path, and verification gate before new tool claims.
- `TOOL_UPGRADE_SESSION.md` - heartbeat implementation and verification notes.
- `BROWSER_TARGETS.md` - editable browser target URLs, wait policy, and extraction fields consumed by `browser_extract`.

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

## Markdown Tool Contract: markdown_verification_pipeline

### Purpose

`markdown_verification_pipeline` is the executable verification control surface for KING toolchain work.

It exists so each tool, prompt, manifest, runtime, or frontend change can be checked by a visible markdown plan before KING reports a ship decision.

### Runtime Tool

- Callable name: `tool_verification_pipeline`.
- Runtime module: `verification_pipeline.py`.
- Pipeline file: configured by `KING_VERIFICATION_PIPELINE_FILE`, defaulting to `tools/TOOL_VERIFICATION_PIPELINE.md`.
- Runtime status: `verified_runtime`.

### Inputs

- Repository root.
- Markdown pipeline path.
- Maximum check count.
- Per-command timeout.
- Dry-run flag.
- Optional structured response and trace flags.

### Outputs

- Pipeline status: `success`, `failed`, `partial`, or `dry_run`.
- Ship decision: `ship` or `hold`.
- Root and pipeline path.
- Per-check command, required flag, status, exit code, duration, bounded stdout, bounded stderr, and truncation flag.
- Machine-readable trace when requested.

### Error Handling

- If the root is missing, report `ROOT_NOT_FOUND`.
- If the pipeline path resolves outside the root, report `PIPELINE_OUT_OF_SCOPE`.
- If the pipeline file is missing, report `PIPELINE_NOT_FOUND`.
- If the pipeline file is not UTF-8 markdown, report `PIPELINE_DECODE_FAILED`.
- If no `- command:` entries exist, report `NO_PIPELINE_COMMANDS`.
- If a required command fails or times out, set ship decision to `hold`.

### Chaining

- Use `tool_readiness_audit` before adding or changing tool capabilities.
- Use `verification_gauntlet` to decide which checks belong in the markdown pipeline.
- Keep manifest and callable-schema alignment as a required pipeline check after tool changes.
- Use `structured_error_envelope` to report blocked, failed, partial, timeout, or unavailable checks without converting them into success.
- Use `permission_risk_gate` before adding any state-changing command to the pipeline.

### Known Limits

- The tool runs commands listed in the markdown pipeline; it does not invent hidden checks.
- The tool does not make command output safe for secrets by itself; pipeline authors must avoid credential commands and broad private-data inspection.
- The tool is not a router, keyword table, or canned response generator.
- The tool cannot turn a failed command into a shipped runtime claim.

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

## Markdown Tool Contract: tool_response_composition

### Purpose

`tool_response_composition` removes hardcoded user-facing tool replies from KING's behavior contract.

It exists so KING answers from runtime evidence instead of prewritten success, failure, provider, launch, playback, file, or search phrases.

### Inputs

- User request.
- Selected callable schema.
- Tool result fields returned in the current turn.
- Runtime state such as success, empty, partial, blocked, timeout, failed, unavailable, or unknown after attempt.
- Relevant evidence contract from this manifest, the evidence ledger, or the provider failure playbook.

### Outputs

- A concise user-facing answer derived from observed fields only.
- A clear missing-evidence note when a requested claim cannot be proven.
- A bounded next step only when the result supports it.

### Decision Rules

- Compose from returned fields such as status, path, URL, title, provider, count, exit code, error code, changed state, truncation, fallback state, and returned text.
- Do not use canned acknowledgement strings, canned provider summaries, or fixed success and failure phrases.
- Do not use a tool name, manifest line, example, keyword, or prior response as proof that an action happened.
- Prefer structured fields over legacy prose when both exist.
- Keep tone separate from evidence. Style may be concise, but facts must come from runtime data.

### Error Handling

- If the tool output lacks the field needed to prove a claim, report that the evidence is missing instead of filling it with a default phrase.
- If the tool result is legacy text only, summarize the observed text and keep scope narrow.
- If the result is partial or degraded, report returned fields and missing fields separately.

### Chaining

- Use this contract after `structured_error_envelope` and `permission_risk_gate`.
- Use `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` for provider-backed tools before summarizing empty, partial, timeout, failed, or unavailable states.
- Use `TOOL_EVIDENCE_LEDGER.md` to decide whether a capability can be claimed as verified runtime or must be reported as legacy, documented-only, blocked, or deferred.

### Known Limits

- This contract does not change Python return strings by itself.
- This contract does not remove legacy tool output needed for backward compatibility.
- This contract does not permit hardcoded routing, phrase triggers, or keyword tables.

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
- Runtime optional inputs: `dry_run`, `max_output_chars`, `timeout_ms`,
  `response_format`, and `trace_enabled`.
- Structured error codes: `EMPTY_COMMAND`, `DIRECTORY_NOT_FOUND`,
  `INVALID_TIMEOUT`, `INVALID_OUTPUT_LIMIT`, `COMMAND_TIMEOUT`,
  `COMMAND_NOT_FOUND`, `PERMISSION_DENIED`, `COMMAND_FAILED`, and
  `SYSTEM_COMMAND_ERROR`.
- Trace requirement: record execution metadata and bounded output metadata, not
  file contents, secrets, or broad system claims.
- Runtime status: `verified_runtime`.

```json
{
  "name": "terminal",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T13:38:05Z",
  "purpose": "Run shell commands or open existing paths with legacy text output by default and optional dry-run, structured results, typed errors, traces, and bounded timeout handling.",
  "inputs": {
    "command": "string: shell command or launch request",
    "workdir": "string: optional working directory, default .",
    "timeout": "integer seconds: optional legacy timeout, default configured terminal timeout",
    "dry_run": "boolean: optional, default false",
    "max_output_chars": "integer 200..20000: optional, default 5000",
    "timeout_ms": "integer 1..60000: optional, default unset",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default output",
    "structured_success": "object: command result plus meta and trace",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["dry_run", "max_output_chars", "timeout_ms", "response_format", "trace_enabled"],
  "error_codes": ["EMPTY_COMMAND", "DIRECTORY_NOT_FOUND", "INVALID_TIMEOUT", "INVALID_OUTPUT_LIMIT", "COMMAND_TIMEOUT", "COMMAND_NOT_FOUND", "PERMISSION_DENIED", "COMMAND_FAILED", "SYSTEM_COMMAND_ERROR"],
  "has_trace": true,
  "timeout_ms": 60000,
  "retry": false,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T13:38:05Z",
  "score": 9.5
}
```

#### file_write

- Compatibility rule: existing `overwrite`, `append`, and `create_new` modes
  keep their legacy string outputs by default.
- Runtime optional inputs: `dry_run`, `create_parent_dirs`,
  `response_format`, and `trace_enabled`.
- Structured error codes: `EMPTY_PATH`, `INVALID_WRITE_MODE`,
  `FILE_ALREADY_EXISTS`, `PARENT_DIRECTORY_NOT_FOUND`,
  `PARENT_NOT_DIRECTORY`, `PERMISSION_DENIED`, and `WRITE_FAILED`.
- Trace requirement: record mutation metadata only; never record written
  content in traces.
- Runtime status: `verified_runtime`.

```json
{
  "name": "file_write",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T13:38:05Z",
  "purpose": "Create, overwrite, append, or create-new UTF-8 files with legacy text output by default and optional dry-run, parent-directory gating, structured results, typed errors, and traces.",
  "inputs": {
    "path": "string: target file path",
    "content": "string: UTF-8 text content",
    "mode": "string enum overwrite|append|create_new: optional, default overwrite",
    "dry_run": "boolean: optional, default false",
    "create_parent_dirs": "boolean: optional, default true",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default write confirmation or error",
    "structured_success": "object: write metadata plus meta and trace",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["dry_run", "create_parent_dirs", "response_format", "trace_enabled"],
  "error_codes": ["EMPTY_PATH", "INVALID_WRITE_MODE", "FILE_ALREADY_EXISTS", "PARENT_DIRECTORY_NOT_FOUND", "PARENT_NOT_DIRECTORY", "PERMISSION_DENIED", "WRITE_FAILED"],
  "has_trace": true,
  "timeout_ms": null,
  "retry": false,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T13:38:05Z",
  "score": 9.5
}
```

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

## Runtime Tool Entry: navigator

```json
{
  "name": "navigator",
  "version": "1.0.0",
  "upgrade_date": "2026-05-23T00:00:00+05:30",
  "purpose": "Resolve two user-supplied places with open geocoding, calculate route distance through an open routing provider, and return grounded travel details with a transparent straight-line fallback.",
  "inputs": {
    "origin": "string: non-empty starting place or address",
    "destination": "string: non-empty destination place or address",
    "mode": "string enum driving|walking|cycling: optional, default KING_NAVIGATOR_DEFAULT_MODE",
    "alternatives": "boolean: optional, default false",
    "timeout_ms": "integer 1..60000: optional, default KING_NAVIGATOR_DEFAULT_TIMEOUT_MS",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: route summary composed from observed place, distance, duration, provider, and fallback fields",
    "structured_success": "object: origin, destination, mode, provider sequence, route distance, straight-line distance, duration, degraded state, and narrative fields",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "providers": ["nominatim", "osrm", "haversine fallback"],
  "error_codes": ["EMPTY_ORIGIN", "EMPTY_DESTINATION", "INVALID_MODE", "INVALID_TIMEOUT", "PLACE_NOT_FOUND"],
  "has_trace": true,
  "timeout_ms": 12000,
  "retry": true,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-23T00:00:00+05:30",
  "score": 9.1
}
```

## Runtime Tool Entry: web_search

```json
{
  "name": "web_search",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T14:29:51Z",
  "purpose": "Search current web providers with legacy text output by default and optional provider selection, bounded timeout, structured results, degraded fallback reporting, typed errors, and traces.",
  "inputs": {
    "query": "string: non-empty search query",
    "max_results": "integer 1..10: optional, default 8",
    "provider": "string enum auto|tavily|ddgs: optional, default auto",
    "timeout_ms": "integer 1..60000: optional, default 10000",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default search result list or no-results message",
    "structured_success": "object: query, provider, fallback, result list, degraded status, provider status, and meta",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["provider", "timeout_ms", "response_format", "trace_enabled"],
  "error_codes": ["EMPTY_QUERY", "INVALID_PROVIDER", "INVALID_RESULT_LIMIT", "INVALID_TIMEOUT"],
  "has_trace": true,
  "timeout_ms": 10000,
  "retry": true,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T14:29:51Z",
  "score": 9.5
}
```

## Runtime Tool Entry: web_fetch

```json
{
  "name": "web_fetch",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T14:29:51Z",
  "purpose": "Fetch readable web pages with legacy text output by default and optional timeout, redirect control, structured page metadata, typed errors, and traces.",
  "inputs": {
    "url": "string: absolute http or https URL",
    "max_chars": "integer 500..8000: optional, default 4000",
    "timeout_ms": "integer 1..60000: optional, default 10000",
    "follow_redirects": "boolean: optional, default true",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default URL, status, optional title, and readable text",
    "structured_success": "object: requested URL, final URL, status code, title, text, truncation, readability, redirect flag, and meta",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["timeout_ms", "follow_redirects", "response_format", "trace_enabled"],
  "error_codes": ["EMPTY_URL", "INVALID_URL", "INVALID_MAX_CHARS", "INVALID_TIMEOUT", "FETCH_FAILED"],
  "has_trace": true,
  "timeout_ms": 10000,
  "retry": true,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T14:29:51Z",
  "score": 9.5
}
```

## Runtime Tool Entry: browser_extract

```json
{
  "name": "browser_extract",
  "version": "1.1.0",
  "upgrade_date": "2026-05-21T00:00:00+05:30",
  "purpose": "Load a configured website target or direct URL and extract markdown-configured details from browser-visible text, meta tags, title, URL, configured selectors, or a saved browser login session.",
  "inputs": {
    "target": "string: optional target name from tools/BROWSER_TARGETS.md",
    "url": "string: optional absolute http or https URL, overrides target URL",
    "fields": "string: optional comma-separated field names, empty uses target fields",
    "config_path": "string: optional markdown target file, default KING_BROWSER_TARGETS_FILE",
    "engine": "string enum auto|playwright|httpx: optional, default auto",
    "timeout_ms": "integer 1..60000: optional, default KING_BROWSER_DEFAULT_TIMEOUT_MS",
    "max_text_chars": "integer 500..50000: optional, default KING_BROWSER_MAX_TEXT_CHARS",
    "storage_state": "string: optional Playwright storage-state file to reuse a saved login session",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: target, URL, engine, title, and extracted field lines",
    "structured_success": "object: target, requested and final URL, status, engine, degraded state, storage-state use, title, configured fields, matched count, field evidence, text truncation, and meta",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["markdown targets", "playwright engine", "httpx fallback", "configured field extraction", "saved login session reuse", "response_format", "trace_enabled"],
  "error_codes": ["CONFIG_NOT_FOUND", "INVALID_CONFIG_PATH", "CONFIG_DECODE_FAILED", "INVALID_ENGINE", "INVALID_TIMEOUT", "INVALID_MAX_TEXT_CHARS", "TARGET_NOT_FOUND", "EMPTY_URL", "INVALID_URL", "BROWSER_DEPENDENCY_MISSING", "PAGE_TIMEOUT", "PAGE_LOAD_FAILED"],
  "has_trace": true,
  "timeout_ms": 15000,
  "retry": false,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-21T00:00:00+05:30",
  "score": 9.2
}
```

## Runtime Tool Entry: browser_login_session

```json
{
  "name": "browser_login_session",
  "version": "1.0.0",
  "upgrade_date": "2026-05-21T00:00:00+05:30",
  "purpose": "Open a visible Playwright browser for manual login and save storage state for later browser_extract calls without collecting credentials.",
  "inputs": {
    "target": "string: optional target name from tools/BROWSER_TARGETS.md",
    "url": "string: optional absolute login or page URL, overrides target login_url or URL",
    "session_name": "string: optional saved-session name, default target or URL host",
    "config_path": "string: optional markdown target file, default KING_BROWSER_TARGETS_FILE",
    "storage_state": "string: optional explicit storage-state output path",
    "timeout_ms": "integer 1..KING_BROWSER_LOGIN_TIMEOUT_MAX_MS: optional, default KING_BROWSER_LOGIN_TIMEOUT_MS",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: saved-state status, final URL, title, storage-state path, and credential policy",
    "structured_success": "object: target, login URL, final URL, title, session name, storage-state path, saved-state existence, timeout, and credential policy",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["visible manual login", "saved Playwright storage state", "target login_url", "credential-free session capture", "response_format", "trace_enabled"],
  "error_codes": ["BROWSER_DEPENDENCY_MISSING", "INVALID_TIMEOUT", "CONFIG_NOT_FOUND", "INVALID_CONFIG_PATH", "CONFIG_DECODE_FAILED", "TARGET_NOT_FOUND", "EMPTY_URL", "INVALID_URL", "LOGIN_SESSION_FAILED"],
  "has_trace": true,
  "timeout_ms": 180000,
  "retry": false,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-21T00:00:00+05:30",
  "score": 9.0
}
```

## Runtime Tool Entry: reddit

```json
{
  "name": "reddit",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T14:59:08Z",
  "purpose": "Browse Reddit listings, search, comments, and users with legacy text output by default and optional bounded timeout, structured provider status, fallback reporting, typed errors, and traces.",
  "inputs": {
    "action": "string enum front|hot|new|top|comments|search|user: optional, default front",
    "subreddit": "string: optional subreddit name, accepts bare name or r/name",
    "query": "string: search term, post id, or username depending on action",
    "limit": "integer 1..25: optional, default 10",
    "time": "string enum hour|day|week|month|year|all: optional, default week",
    "id": "string: optional alias for query",
    "sort": "string enum relevance|hot|top|new|comments: optional, default relevance",
    "timeout_ms": "integer 1..60000: optional, default 15000",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false",
    "include_source_status": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default Reddit listing, comment, user, fallback, or status text",
    "structured_success": "object: action, text, items, count, source, fallback, degraded status, cache state, provider status when requested, and meta",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["timeout_ms", "response_format", "trace_enabled", "include_source_status"],
  "error_codes": ["INVALID_LIMIT", "INVALID_TIMEOUT", "MISSING_QUERY", "MISSING_SUBREDDIT", "MISSING_POST_ID", "MISSING_USERNAME", "INVALID_ACTION", "NOT_FOUND", "POST_NOT_FOUND", "USER_NOT_FOUND", "RATE_LIMITED", "PROVIDER_ERROR", "NO_RESULTS"],
  "has_trace": true,
  "timeout_ms": 15000,
  "retry": true,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T14:59:08Z",
  "score": 9.5
}
```

## Runtime Tool Entry: hackernews

```json
{
  "name": "hackernews",
  "version": "2.0.0",
  "upgraded_from": "unversioned",
  "upgrade_date": "2026-05-19T14:59:08Z",
  "purpose": "Browse Hacker News listings, story details, comments, users, and search with legacy text output by default and optional bounded timeout, structured provider status, typed errors, retries, and traces.",
  "inputs": {
    "action": "string enum top|new|best|ask|show|comments|user|search: optional, default top",
    "limit": "integer 1..30: optional, default 10",
    "query": "string: story id, username, or search term depending on action",
    "id": "string: optional alias for query",
    "timeout_ms": "integer 1..60000: optional, default 15000",
    "response_format": "string enum legacy|structured: optional, default legacy",
    "trace_enabled": "boolean: optional, default false",
    "include_source_status": "boolean: optional, default false"
  },
  "outputs": {
    "legacy": "string: unchanged default story list, story detail, user, search, or status text",
    "structured_success": "object: action, text, items, count, endpoint/provider, degraded status, cache state, provider status when requested, and meta",
    "structured_error": "object: typed error plus meta and trace",
    "trace": "json object: emitted when trace_enabled is true"
  },
  "new_capabilities": ["timeout_ms", "response_format", "trace_enabled", "include_source_status"],
  "error_codes": ["INVALID_LIMIT", "INVALID_TIMEOUT", "MISSING_STORY_ID", "MISSING_USERNAME", "MISSING_QUERY", "INVALID_ACTION", "INVALID_STORY_ID", "STORY_NOT_FOUND", "USER_NOT_FOUND", "PROVIDER_ERROR", "NO_RESULTS"],
  "has_trace": true,
  "timeout_ms": 15000,
  "retry": true,
  "circuit_breaker": false,
  "backward_compatible": true,
  "verified": true,
  "verification_date": "2026-05-19T14:59:08Z",
  "score": 9.5
}
```

## Maintenance Rules

- Update this manifest whenever a tool is added, removed, renamed, or given a new safety contract.
- Update `TOOL_EVIDENCE_LEDGER.md` whenever a tool status changes between `documented_only`, `active_legacy`, `verified_runtime`, `blocked`, or `deferred`.
- Update `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` whenever a provider-backed tool gains a new observable provider state.
- Use `TOOL_INTAKE_CHECKLIST.md` before claiming a new tool or upgrade is active.
- Keep tool status tied to observed files and registry evidence.
- Do not add credentials, private paths, canned user-facing replies, or phrase triggers here.
- Prefer a `documented_only` status over claiming a capability is active without runtime proof.

## Evolution Log

- 2026-05-21T00:00+05:30 - Added `browser_login_session`, upgraded `browser_extract` to reuse saved storage state, and expanded `BROWSER_TARGETS.md` with `login_url` and `storage_state`.
- 2026-05-21T00:00+05:30 - Added `browser_extract` runtime, `BROWSER_TARGETS.md`, and config knobs for browser or HTTP page loading with markdown-configured extraction fields.
- 2026-05-19T23:15+05:30 - Fixed CLI tool-result leakage by forcing structured-capable tools into structured context and routing completed tool results through an LLM final-answer step before user-facing output.
- 2026-05-19T14:39Z - Added `TOOL_INTAKE_CHECKLIST.md` as the required documentation and verification gate before new tool claims.
- 2026-05-19T14:35Z - Added `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` so provider-backed tools report empty, partial, timeout, failed, blocked, unavailable, and unknown-after-attempt states without false negatives.
- 2026-05-19T14:30Z - Added `TOOL_EVIDENCE_LEDGER.md` as the claim-status source for verified, legacy, documented-only, blocked, and deferred tool capability reporting.
- 2026-05-19T14:27Z - Promoted manifest and callable-schema alignment into the default markdown verification pipeline.
- 2026-05-19T14:59Z - Upgraded `reddit` runtime to version 2.0.0 with legacy-preserving structured output, bounded timeout, provider fallback/degraded status, typed errors, retries, and traces.
- 2026-05-19T14:59Z - Upgraded `hackernews` runtime to version 2.0.0 with legacy-preserving structured output, bounded timeout, provider status, typed errors, retries, and traces.
- 2026-05-19T13:35Z - Added executable `tool_verification_pipeline`, the markdown pipeline file, config knobs, and isolated tests for bounded ship or hold verification.
- 2026-05-19T13:38Z - Upgraded `terminal` runtime to version 2.0.0 with legacy-preserving dry-run, structured output, typed errors, optional trace emission, bounded output, and millisecond timeout override.
- 2026-05-19T13:38Z - Upgraded `file_write` runtime to version 2.0.0 with legacy-preserving dry-run, parent-directory gating, structured write metadata, typed errors, and mutation-safe traces.
- 2026-05-19T14:29Z - Upgraded `web_search` runtime to version 2.0.0 with legacy-preserving provider selection, bounded timeout, structured provider results, fallback/degraded status, typed validation errors, and traces.
- 2026-05-19T14:29Z - Upgraded `web_fetch` runtime to version 2.0.0 with legacy-preserving timeout and redirect controls, structured page metadata, typed URL/fetch errors, and traces.
- 2026-05-19T08:38Z - Upgraded `registry_dispatch` runtime to version 2.0.0 with legacy-preserving structured output, typed errors, optional traces, and bounded timeout handling.
- 2026-05-19T08:42Z - Added executable `tool_manifest_audit` in `manifest_audit.py`, imported it into the active toolchain, and documented its manifest entry.
- 2026-05-19T08:39Z - Added `bounded_retry_timeout_policy` as a markdown tool contract for finite retries, timeout clamping, and circuit-break decisions.
- 2026-05-19T08:30Z - Added `tier1_upgrade_contract` as a documented-only manifest contract for registry dispatch, terminal execution, and file writing upgrades.
- 2026-05-19T08:30Z - Added `idempotent_write_guard` as a markdown tool contract for duplicate-safe writes, retries, and partial-state recovery.
- 2026-05-19T08:26Z - Added `structured_error_envelope` as a markdown tool contract for safe, scoped, non-silent failure reporting.
- 2026-05-19T08:18Z - Added `permission_risk_gate` as a markdown tool contract for scoped allow, confirm, dry-run-first, and block decisions before sensitive tool execution.
- 2026-05-19T08:08Z - Added `verification_gauntlet` as a markdown tool contract so future toolchain changes have a reusable ship, hold, or document-only evidence path.
