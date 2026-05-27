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
- `folder_watcher` - `verified_runtime` for read-only HTTP bridge actions,
  markdown-owned client target parsing, structured service/auth/validation
  errors, mocked endpoint mapping, main API `POST /folder-watcher` JSON bridge,
  and semantic router selection without keyword routing.
- `telegram_watcher_service` - `verified_runtime` as a standalone Telegram
  daemon for markdown-configured natural file delivery, authorized-user checks,
  allowed-zone enforcement, blocked-file policy, Folder Watcher integration,
  local scan fallback, pick lists, lockdown, and push notifications. It is not
  a registry callable tool and does not change core routing.
- `composio` - `verified_runtime` for markdown-limited external app gateway
  status, session creation payloads, auth-link gating, approved tool execution
  routing, structured missing-key/provider/policy errors, and mocked Composio
  HTTP behavior; live provider success remains scoped to a configured
  `COMPOSIO_API_KEY` and connected Composio account.
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

## 2026-05-25 Tool Fleet Repair Snapshot

- `registry_dispatch` remains `verified_runtime`; it now also covers tool
  schemas with a callable argument named `name`.
- `reddit` is `verified_runtime` for live front-page retrieval, live query
  search, oversized limit clamping, and `front` plus query normalization to
  search.
- `hackernews` is `verified_runtime` for live top-story retrieval and
  markdown-owned action alias normalization from `fetch` to `top`.
- `imagine` is `verified_runtime` for live Pollinations image generation with a
  four-character prompt and viewer-disabled save proof.
- `file_list` is `verified_runtime` for markdown-owned natural path alias
  resolution through `FILE_PATH_ALIASES.md`.
- `keyboard_press` is `verified_runtime` for sending explicit key combinations;
  visible desktop or window state remains unverified unless a future result
  field proves it.
- Registry-wide smoke covered all 32 registered tools with 32 pass, 0 fail.
- Latest ship checks: `python -m pytest -q` passed 210 tests and 24 subtests;
  `python tests\live_gauntlet.py --full-live` passed 7 checks; the markdown
  verification pipeline returned `ship` with 6 passed checks, 0 failed, and 0
  timed out.

## 2026-05-26 Folder Watcher KING Bridge Snapshot

- Added registered executable module `tools/folder_watcher.py` and markdown
  client config `tools/FOLDER_WATCHER_CLIENT.md`.
- Manifest audit observed 18 tool modules, 18 manifest modules, 34 registered
  callable schemas, and no manifest/file mismatches.
- Focused bridge checks passed: `python -m unittest tests.test_folder_watcher_tool`.
- Router and registry checks passed: `python -m unittest tests.test_tools_fleet`.
- Source watcher service checks passed: `python -m unittest tests.test_folder_watcher`.
- Repository suite passed: `python -m pytest -q` reported 232 passed tests and
  33 passed subtests.
- The markdown verification pipeline returned `ship` with 6 passed checks, 0
  failed checks, and 0 timeouts.
- Follow-up CLI and routing repair added markdown-owned semantic arbitration so
  natural current-folder, file-count, image/media, Python-file, and size
  questions prefer `folder_watcher` over raw `file_list`; raw directory-entry
  requests still select `file_list`.
- `python main.py --api http://127.0.0.1:8011 /folder-stats` returned live
  folder watcher rollups through the lightweight CLI API client.
- Follow-up repository suite passed: `python -m pytest -q` reported 247 passed
  tests and 35 passed subtests.
- Registry timeout verification was hardened for low-end runtime jitter while
  preserving the structured `TOOL_TIMEOUT` assertion.
- Follow-up markdown verification pipeline returned `ship` with 6 passed checks,
  0 failed checks, and 0 timeouts.

## 2026-05-26 Composio Gateway Snapshot

- Added registered executable module `tools/composio.py` and markdown policy
  `tools/COMPOSIO_GATEWAY.md`.
- Added environment knobs for `COMPOSIO_API_KEY`, `KING_COMPOSIO_USER_ID`,
  `KING_COMPOSIO_SESSION_ID`, `KING_COMPOSIO_BASE_URL`, and policy path.
- Added focused tests in `tests/test_composio_tool.py` covering registration,
  local status, missing API key, markdown-limited session creation, disabled
  tool rejection, confirmation-required write risk, approved execution routing,
  auth-link auto-session creation, and invalid JSON arguments.
- Manifest audit observed 19 tool modules, 19 manifest modules, 35 registered
  callable schemas, and no manifest/file mismatches.
- Focused checks passed: `python -m py_compile tools\composio.py`,
  `python -m unittest tests.test_composio_tool`, and
  `python -m unittest tests.test_tools_fleet`.
- Repository checks passed: `python -m unittest tests.test_grounding`,
  `python -m pytest -q` with 245 passed tests and 35 subtests, compileall, and
  `npm run typecheck`.
- The markdown verification pipeline returned `ship` with 6 passed checks, 0
  failed checks, and 0 timeouts when run with a 420000 ms per-command timeout.
- Registry dispatch proof returned `composio` status successfully with
  `api_key_present: false`, enabled toolkit `github`, and three enabled GitHub
  read tools from markdown policy.
- Follow-up management UI added `/composio/status`, `/composio/policy`,
  `/composio/action`, and `/composio/policy/tool` API routes plus
  `public/frontend/composio.html`, `composio.css`, and `composio.js`.
- Live Composio status observed `api_key_present: true`, user id
  `krish-local`, enabled toolkit `github`, and the same three markdown-approved
  GitHub read tools.
- Live session creation succeeded through both registry dispatch and
  `POST /composio/action`, returning Composio tool-router session ids and MCP
  URLs scoped to the approved GitHub tools.
- Browser verification loaded `http://127.0.0.1:3000/frontend/composio.html`,
  observed the Ready state and approved tool list, clicked Session, and clicked
  Schema successfully.
- Follow-up checks passed: `python -m unittest tests.test_composio_tool
  tests.test_composio_api`, `node --check public\frontend\composio.js`,
  `python -m unittest tests.test_tools_fleet`, `python -m pytest -q` with 253
  passed tests and 35 subtests, compileall, `npm run typecheck`, and the
  markdown verification pipeline with 6 passed checks, 0 failed, 0 timed out,
  `ship`.
- Follow-up endpoint and natural-call repair added markdown-owned `Argument
  Defaults` for approved GitHub repo tools, local Git remote context in
  `composio` status and `/composio/policy`, execution-time default application,
  compact schema inputs for `/composio/action` schema calls, and schema-aware UI
  feedback for missing inputs.
- Direct dispatch proof executed `GITHUB_LIST_REPOSITORY_ISSUES` with `{}` and
  returned provider success after applying `owner: akyourowngames` and
  `repo: friday`; provider error was `None`.
- Focused follow-up checks passed: `python -m unittest tests.test_composio_tool
  tests.test_composio_api`, `node --check public\frontend\composio.js`, and
  `python -m compileall tools\composio.py api_server.py`.
- Browser verification loaded the Composio page, selected
  `GITHUB_LIST_REPOSITORY_ISSUES`, observed schema-rendered `owner` and `repo`
  fields prefilled from local repo defaults, clicked Run, and received provider
  output with `error: null`.
- Final follow-up checks passed: `python -m pytest -q` with 272 passed tests and
  35 subtests, `npm run typecheck`, `python -m unittest tests.test_tools_fleet`,
  compileall for the ship pipeline scope, and the markdown verification pipeline
  with 6 passed checks, 0 failed, 0 timed out, `ship`.
- Follow-up gateway expansion added direct Composio catalog/session endpoints,
  bulk markdown policy install, compact catalog items, and markdown-gated
  semantic slug resolution for imprecise requests such as `get_repo_details`.
- Direct dispatch proof executed `tool_slug=get_repo_details`; the gateway
  resolved it to `GITHUB_GET_A_REPOSITORY`, applied local owner/repo defaults,
  and returned provider output with `error: None`.
- CLI-route proof found the model may pass filler arguments such as
  `{"owner":"owner","repo":"repo"}`; the gateway now repairs only
  markdown-listed placeholder values from local repo defaults before execution.
- Real CLI proof for `use composio to get repo details for this repo` returned
  live details for `akyourowngames/friday` after semantic slug recovery and
  placeholder argument repair.
- Catalog proof executed `action=tools`, `toolkit=github`, `query=issues`, and
  returned 5 compact items with provider `total_items: 72`.
- Seamless auth/install follow-up adds `install_tools` as a local markdown
  policy write action, `connect` as an alias-preserving Composio tool-router
  auth link action, and `auth_status` to compact session toolkit connection
  metadata instead of assuming auth succeeded.
- Composio frontend catalog search now renders returned tools with one-click
  allow controls and an allow-shown bulk path backed by `/composio/policy/tools`.
- Live auth-status probe returned GitHub as connected from Composio's
  `connected_account` session metadata, and live connect probe returned a
  provider redirect URL with the requested alias preserved.
- Focused follow-up checks passed: `python -m unittest tests.test_composio_tool
  tests.test_composio_api`, `python -m py_compile tools\composio.py
  api_server.py`, API probes for `/composio/tools`, `/composio/toolkits`, and
  bulk `/composio/policy/tools`, plus the final markdown verification pipeline.
- Final seamless-auth checks passed: `python -m pytest -q` with 288 passed tests
  and 35 subtests, `npm run typecheck`, `python -m unittest
  tests.test_tools_fleet`, `node --check public\frontend\composio.js`,
  compileall, browser catalog-card verification, and the markdown verification
  pipeline with 6 passed checks, 0 failed, 0 timed out, `ship`.
- Starter pack follow-up installed exact Composio slugs for Gmail, Google
  Calendar, Google Drive, Google Docs, Google Sheets, Google Tasks, Slack, and
  Notion through the local `install_tools` gateway action. Read tools are
  enabled directly; send, create, append, update, and post actions are marked
  `write` and remain behind `confirm=true`.
- Verification passed for the starter pack: live `/composio/status` reported
  the new enabled toolkits, direct schema probes resolved Gmail, Calendar,
  Drive, Slack, and Notion slugs, write-risk probes returned
  `CONFIRMATION_REQUIRED`, focused Composio tests passed 29 tests, fleet tests
  passed 17 tests, `python -m pytest -q` passed 290 tests and 35 subtests, and
  the markdown verification pipeline returned `ship` with 6 passed checks.

## 2026-05-27 Telegram Watcher Service Snapshot

- Added standalone package `telegram_watcher/`, entrypoint
  `telegram_watcher_service.py`, and markdown control surface
  `tools/TELEGRAM_WATCHER_CONFIG.md`.
- The service uses markdown-configured action semantics for natural Telegram
  text, optional slash command aliases, per-chat numeric pick lists, and no
  `agent/core.py` routing changes.
- Security controls are config-driven: token source, authorized numeric user-id
  source, allowed zones, blocked suffixes, blocked name fragments, blocked path
  parts, rate limits, lockdown PIN source, state path, and append-only session
  log path.
- File intelligence goes through Folder Watcher HTTP first and degrades to a
  bounded local scan of allowed zones only when Folder Watcher is unavailable.
- Focused checks passed: `python -m unittest tests.test_telegram_watcher -v`
  reported 8 tests covering config load, silent unauthorized ignore, natural
  send, multi-match pick lists, blocked-file policy, local fallback, push
  notification mode, lockdown, and unlock.
- Service status probe passed: `python telegram_watcher_service.py status`
  loaded the markdown config, resolved desktop/downloads/documents/drop zones,
  and correctly reported `token_present: false` and
  `authorized_ids_configured: false` in this checkout rather than claiming a
  live bot without credentials.
- Manifest audit passed with 19 observed tool modules, 19 manifest modules, 35
  registered callable schemas, and no manifest/file mismatches.
- Repository checks passed: `python -m pytest -q` reported 288 passed tests and
  35 passed subtests; `npm run typecheck` passed.
- The markdown verification pipeline returned `ship` with 6 passed checks, 0
  failed checks, and 0 timeouts after adding `telegram_watcher` to the compile
  gate.

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
