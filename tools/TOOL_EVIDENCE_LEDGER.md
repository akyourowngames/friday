# KING Tool Evidence Ledger

This ledger records the evidence standard KING must satisfy before it claims a
tool capability is active, upgraded, blocked, or only documented.

It is not a router, keyword table, prompt shortcut, or replacement for the
runtime registry. The active registry and real tool results remain the source of
truth for executable behavior.

## Status Vocabulary

- `verified_runtime` - executable behavior exists, is registered, and passed the
  current verification pipeline for its claimed capability.
- `active_legacy` - executable behavior exists and is registered, but newer
  structured or hardened behavior has not been proven for that tool.
- `documented_only` - markdown contract or design exists, but no callable schema
  or runtime behavior has been verified.
- `blocked` - verification could not run, failed, or proved the claimed
  capability unavailable.
- `deferred` - useful capability identified, but not implemented or verified in
  the current heartbeat.

## Claim Rules

- KING may claim `verified_runtime` only when the active callable schema and a
  real verification result both support the claim.
- KING may describe `documented_only` work as planned or specified, never as an
  available runtime capability.
- KING must report `blocked`, `failed`, `timeout`, `partial`, and `empty` as
  separate states when the evidence differs.
- KING must not turn one successful narrow check into a broad claim about every
  account, provider, folder, website, app, or file.
- KING must not say a tool cannot do something unless the selected callable
  schema or actual result proves that limit for the attempted scope.
- KING must not use hardcoded tool response text. User-facing tool answers must
  be composed from observed fields in the current tool result.

## Minimum Evidence By Surface

### Registry And Dispatch

- Callable schema is visible through the active registry.
- Legacy output remains compatible for existing callers.
- Structured mode, typed errors, trace output, and timeout behavior are tested
  when those capabilities are claimed.
- Downstream validation still executes the legacy path.
- User-facing reporting must prefer structured result and error fields over
  canned text when structured fields are available.

### Local Mutation Tools

- Dry-run or preview behavior is tested when available.
- Existing-state handling is tested before overwrite, append, delete, launch, or
  enqueue claims.
- Partial, timeout, and retry outcomes require state verification before any
  retry claim.
- Trace output must record metadata only, not user content.

### External Provider Tools

- Provider, fallback, timeout, no-result, and failure states must be distinct.
- Provider-backed tools should follow `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` before reporting broad absence, playback, generation, or provider availability.
- Results must cite the provider state returned by the tool, not inferred remote
  causes.
- Empty results from one provider must not become a broad "does not exist"
  answer.
- Network-only proof is not enough when local tests can cover the behavior.

### Markdown Contracts

- The changed markdown file must be inspected or exercised by a test or pipeline
  check.
- New tool claims should pass through `TOOL_INTAKE_CHECKLIST.md` before the
  status moves to `verified_runtime`.
- The manifest must name any new executable tool module.
- The verification pipeline must return `ship` before the contract is treated as
  current.
- Unsupported runtime fields in markdown stay `documented_only`.

### Generated Or Storage Artifacts

- Artifact path, size, scope, and intended ownership must be reported.
- Generated state is not source code unless the repo explicitly treats it that
  way.
- Runtime cache, memory, and build outputs should not be used to prove source
  behavior unless the check specifically targets those artifacts.

## Current Tool Fleet Snapshot

Last verified by the default markdown pipeline on 2026-05-23:

- Observed tool modules: 13.
- Manifest tool modules: 13.
- Registered callable schemas: 24.
- Missing from manifest: none.
- Missing from files: none.

## Current Runtime Status

- `tool_selection_execution_guard` - `verified_runtime` for preserving
  registry-selected tools when the user's words overlap the selected schema,
  keeping selected schema order aligned with router rank, and preventing
  unrelated follow-ups from being hijacked by local system-control correction
  handling.
- `registry_dispatch` - `verified_runtime`.
- `terminal` - `verified_runtime`.
- `file_write` - `verified_runtime`.
- `tool_manifest_audit` - `verified_runtime`.
- `tool_verification_pipeline` - `verified_runtime`.
- `browser_extract` - `verified_runtime` for markdown-configured URL, saved-session reuse, and field extraction with mocked page-load evidence; live provider results remain scoped to the attempted URL and engine state.
- `browser_login_session` - `verified_runtime` for visible manual-login schema, storage-state path handling, and credential-free session capture contract; live login success remains scoped to the saved storage-state result.
- `navigator` - `verified_runtime` for open geocoding, open route distance, representative-point warnings for broad regions, reverse-geocoded route-through places, straight-line fallback, structured result fields, typed errors, and frontend route payload support; live provider results remain scoped to the attempted origin, destination, provider status, route mode, and sampled route places.
- `system_control` - `verified_runtime` for CoreAudio-backed volume up/down
  with before/after verification, verified mute toggles, honest partial states
  for unverified brightness hardware-key fallbacks, and concrete system-action
  routing that does not repeat the previous volume action for a brightness
  request.
- `datetime_info` - `active_legacy`.
- `file_read` - `active_legacy`.
- `file_list` - `active_legacy`.
- `web_search` - `verified_runtime`.
- `web_fetch` - `verified_runtime`.
- `hackernews` - `active_legacy`.
- `reddit` - `active_legacy`.
- `youtube_play` - `active_legacy`.
- `playlist` - `active_legacy`.
- `imagine` - `active_legacy` with the transport limitation documented in
  `tool_policy.md`.
- `gallery` - `active_legacy`.
- Note tools - `active_legacy`.

## Escalation Rules

- If manifest audit fails, stop tool claims at `blocked` until alignment is
  restored.
- If tests pass but a live tool check fails, report the live failure as the
  current outcome for that request.
- If a capability requires code but the heartbeat is markdown-only, record it as
  `deferred` or `documented_only`.
- If the worktree contains unrelated user changes, preserve them and scope the
  ledger update to the current heartbeat.

## Related Control Files

- `TOOL_MANIFEST.md` - executable inventory and markdown contracts.
- `TOOL_VERIFICATION_PIPELINE.md` - default ship or hold checks.
- `TOOL_PROVIDER_FAILURE_PLAYBOOK.md` - provider-backed failure and partial-result reporting rules.
- `TOOL_INTAKE_CHECKLIST.md` - new tool documentation and verification gate.
- `TOOL_UPGRADE_SESSION.md` - implementation and verification history.
