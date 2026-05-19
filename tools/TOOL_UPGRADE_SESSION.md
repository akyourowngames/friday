# KING Tool Upgrade Session

Last updated: 2026-05-19T08:38:18Z

This file is the visible upgrade-control artifact for the current heartbeat. It
records Phase 1 inventory and tier assignment before any runtime tool changes.

Important boundary: repository instructions for tool work currently say to edit
markdown files and not touch tool code. This session therefore does not claim a
runtime tool upgrade has shipped. Any executable change remains deferred until
the user explicitly allows code edits.

Current exception: the 2026-05-19T08:38Z heartbeat explicitly allowed code edits
and full access, so the `registry_dispatch` runtime upgrade below is active and
verified.

## Scope

Source read before this artifact was written:

- `tools/registry.py`
- `tools/datetime_tool.py`
- `tools/files.py`
- `tools/hackernews.py`
- `tools/image.py`
- `tools/notes.py`
- `tools/reddit.py`
- `tools/terminal.py`
- `tools/web.py`
- `tools/youtube.py`
- `tools/__init__.py`
- `tools/TOOL_MANIFEST.md`
- `tool_policy.md`
- `persona.md`
- `tests/test_grounding.py`
- `tests/test_memory.py`

Dirty worktree boundary: several tool code files were already modified before
this heartbeat. They are treated as user changes and were preserved exactly.

## Phase 1 Inventory And Tier Map

[TIER] tool: registry_dispatch -> Tier 1 - reason: every registered tool depends
on schema generation and dispatch.

[INVENTORY] tool: registry_dispatch
  Current version: unversioned
  Purpose: Registers Python functions as tools, exposes schemas, and dispatches a selected tool by name.
  Inputs: name: string optional; description: string optional; examples: array optional; param_descriptions: object optional; execute_tool name: string; execute_tool kwargs: object.
  Outputs: tool metadata objects; OpenAI-style function schemas; execute_tool string result or string error.
  Known issues: dispatch returns plain strings only; errors are not structured; no trace is emitted; version is absent; raw exception messages can leak implementation detail.
  User changes detected: no direct tracked delta shown for `tools/registry.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: normalize safe scalar coercions without accepting unknown fields silently.
    - Output layer: wrap new callers in result and meta while preserving legacy string return for existing dispatch paths.
    - Error layer: replace generic string failures with stable structured error objects behind an optional output mode.
    - Observability: add call id, schema-valid flag, selected tool, duration, output field count, and status.
    - Resilience: guard tool execution with typed boundaries and per-tool timeout support where possible.
    - New capabilities: optional structured response mode, optional trace sink, optional timeout override.
  Risk: HIGH - central dispatcher changes can affect every tool call.

[TIER] tool: terminal -> Tier 1 - reason: high-impact system execution and app-launch surface.

[INVENTORY] tool: terminal
  Current version: unversioned
  Purpose: Runs shell commands or opens existing paths and returns command output.
  Inputs: command: string required; workdir: string default `.`; timeout: integer default 30.
  Outputs: string command output, success message, or formatted error.
  Known issues: output is plain text; errors are not structured; high-risk actions rely on prompt policy rather than callable guardrails; trace is absent.
  User changes detected: `tools/terminal.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: bound timeout, validate workdir, and keep shell selection explicit.
    - Output layer: include exit code, stdout, stderr, opened path status, and meta for structured callers.
    - Error layer: classify timeout, missing command, missing directory, permission denied, and system error.
    - Observability: trace command class, workdir scope, duration, status, and whether an app/path launch branch ran.
    - Resilience: enforce timeout and avoid indefinite subprocess waits.
    - New capabilities: optional dry_run, optional max_output_chars, optional structured mode.
  Risk: HIGH - can mutate local system state and is broadly capable.

[TIER] tool: file_write -> Tier 1 - reason: local file mutation can overwrite user data.

[INVENTORY] tool: file_write
  Current version: unversioned
  Purpose: Creates, overwrites, or appends text content to a path.
  Inputs: path: string required; content: string required; mode: string default `overwrite`.
  Outputs: string write confirmation with resolved path and size, or string error.
  Known issues: overwrite is default; no structured error; no trace; no dry-run; no project-scope report; write confirmation lacks before/after metadata.
  User changes detected: `tools/files.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: accept safe mode aliases only through normalization, clamp content preview metadata, and validate parent path.
    - Output layer: include action, resolved path, bytes written, existed_before, and meta for structured callers.
    - Error layer: classify invalid mode, parent creation failure, permission denied, and encoding failure.
    - Observability: trace action, path scope, mode, duration, and status.
    - Resilience: make write failures atomic where possible and report partial state.
    - New capabilities: optional dry_run, optional create_parent_dirs, optional structured mode.
  Risk: HIGH - default overwrite behavior can destroy content if misused.

[TIER] tool: youtube_play -> Tier 2 - reason: frequent external and local playback path; failure should degrade cleanly.

[INVENTORY] tool: youtube_play
  Current version: unversioned
  Purpose: Searches YouTube, selects a likely best result, records it in the playlist, and attempts playback.
  Inputs: query: string required.
  Outputs: string playback status and selected video details, or string error.
  Known issues: subprocess and network failures collapse into empty results; no trace; no structured external-call status; background playback can fail after the tool returns.
  User changes detected: `tools/youtube.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: trim query and reject empty input with a typed error.
    - Output layer: include selected item fields, playlist action, playback attempt status, and meta.
    - Error layer: classify empty query, no search results, rank failure, playlist write failure, and playback launch failure.
    - Observability: trace search, ranking, playlist write, and playback attempt.
    - Resilience: explicit subprocess timeouts already exist; add retry only for transient external failures.
    - New capabilities: optional max_results, optional play_mode, optional structured mode.
  Risk: MEDIUM - it touches network, subprocesses, playlist storage, and browser/audio fallback.

[TIER] tool: playlist -> Tier 2 - reason: persistent user playlist mutations and playback management.

[INVENTORY] tool: playlist
  Current version: unversioned
  Purpose: Lists, searches, plays, favorites, removes, clears, and shuffles saved playlist tracks.
  Inputs: action: string required; query: string default empty.
  Outputs: string playlist listing, mutation confirmation, playback status, or string error.
  Known issues: destructive clear has no dry-run; no structured result; no trace; no explicit mutation count except clear.
  User changes detected: `tools/youtube.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: normalize action spelling conservatively and validate required query by action.
    - Output layer: include action, matched track, before_count, after_count, changed, and meta.
    - Error layer: classify unknown action, empty playlist, missing query, no match, and storage failure.
    - Observability: trace action branch, playlist path, item count, duration, and status.
    - Resilience: preserve playlist on failed mutation and report unchanged state.
    - New capabilities: optional dry_run for remove and clear, optional limit for list, optional structured mode.
  Risk: MEDIUM - persistent local user data and playback side effects.

[TIER] tool: web_search -> Tier 2 - reason: current-information search depends on external providers.

[INVENTORY] tool: web_search
  Current version: unversioned
  Purpose: Searches the web using Tavily when configured, otherwise DDGS.
  Inputs: query: string required; max_results: integer default 8.
  Outputs: string list of search results or `No results found`.
  Known issues: provider failures are silent; no structured provider status; no trace; no retry or timeout parameter exposed.
  User changes detected: `tools/web.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: reject empty query clearly and clamp result count.
    - Output layer: include provider, results considered, result list, degraded flag, and meta.
    - Error layer: classify empty query, provider unavailable, provider timeout, and no results.
    - Observability: trace provider branch, external calls, result count, duration, and status.
    - Resilience: explicit provider timeout, retry for transient provider failure, and fallback provider reporting.
    - New capabilities: optional timeout_ms, optional provider preference, optional structured mode.
  Risk: MEDIUM - external dependency failures can create false negatives.

[TIER] tool: web_fetch -> Tier 2 - reason: external page fetch and current-source grounding.

[INVENTORY] tool: web_fetch
  Current version: unversioned
  Purpose: Fetches a URL, extracts readable page text, and returns status, title, and text.
  Inputs: url: string required; max_chars: integer default 4000.
  Outputs: string URL, status, title if found, text, or generic fetch error.
  Known issues: fetch errors are generic; no structured status; no URL validation detail; no trace; no retry.
  User changes detected: `tools/web.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: validate URL scheme and clamp max_chars.
    - Output layer: include final_url, status_code, title, text, truncated, and meta.
    - Error layer: classify invalid URL, timeout, HTTP status, network failure, and parse failure.
    - Observability: trace URL host, redirect count if available, duration, and status without secrets.
    - Resilience: timeout_ms parameter and retry for transient network failure.
    - New capabilities: optional timeout_ms, optional follow_redirects, optional structured mode.
  Risk: MEDIUM - fetch failures currently hide useful recovery data.

[TIER] tool: reddit -> Tier 2 - reason: frequent external API plus fallback search path.

[INVENTORY] tool: reddit
  Current version: unversioned
  Purpose: Browses Reddit front page, subreddit listings, search, comments, and user pages.
  Inputs: action: string default `front`; subreddit: string default empty; query: string default empty; limit: integer default 10; time: string default `week`; id: string default empty; sort: string default `relevance`.
  Outputs: string listing, comments, user profile, fallback search results, or string error.
  Known issues: fallback and API failures are not structured; rate-limit status is text only; no trace; provider-specific status is hidden.
  User changes detected: `tools/reddit.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: normalize subreddit names without changing scope, clamp limit, and validate action-specific required fields.
    - Output layer: include action, source, result count, fallback_used, rate_limited, and meta.
    - Error layer: classify missing subreddit, missing query, rate limited, not found, provider error, and no results.
    - Observability: trace action branch, cache hit, external call count, fallback branch, duration, and status.
    - Resilience: keep fallback search as graceful degradation and expose the degraded reason.
    - New capabilities: optional timeout_ms, optional include_comments_limit, optional structured mode.
  Risk: MEDIUM - external failures can be mistaken for broad absence.

[TIER] tool: hackernews -> Tier 2 - reason: common external news source with item and search branches.

[INVENTORY] tool: hackernews
  Current version: unversioned
  Purpose: Retrieves Hacker News listings, story details, comments, users, and Algolia search results.
  Inputs: action: string default `top`; limit: integer default 10; query: string default empty; id: string default empty.
  Outputs: string story list, story detail, user summary, search results, or string error.
  Known issues: HTTP failures return `None` internally and become generic no-result text; no structured provider status; no trace; no retry.
  User changes detected: `tools/hackernews.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: normalize action, clamp limit, and validate id/query by action.
    - Output layer: include action, endpoint, item_count, cache_hit, and meta.
    - Error layer: classify missing query, invalid story id, not found, provider timeout, and provider error.
    - Observability: trace endpoint, cache hit, external calls, duration, result count, and status.
    - Resilience: expose timeout_ms and retry transient provider failures.
    - New capabilities: optional timeout_ms, optional include_text, optional structured mode.
  Risk: MEDIUM - external failure currently looks like no stories or unavailable search.

[TIER] tool: imagine -> Tier 2 - reason: external generation plus local file creation and app opening.

[INVENTORY] tool: imagine
  Current version: unversioned
  Purpose: Generates an image from a prompt, saves it locally, and attempts to open it.
  Inputs: prompt: string required; size: string default `1024x1024`; model: string default `pollinations`.
  Outputs: string saved path or string generation/save error.
  Known issues: timeouts are long and not caller-configurable; backend failures are plain text; no trace; fallback chain is hidden; opens file as a side effect.
  User changes detected: `tools/image.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: validate prompt length, size, and model with clear field-level errors.
    - Output layer: include path, model_used, fallback_used, opened, bytes_written, and meta.
    - Error layer: classify prompt too short, invalid size, invalid model, generation failed, save failed, and open failed.
    - Observability: trace backend path, external call count, duration, file path, and status.
    - Resilience: timeout_ms parameter, backend fallback reporting, and degraded open failure.
    - New capabilities: optional timeout_ms, optional open_after_save, optional structured mode.
  Risk: MEDIUM - network generation and local file side effects.

[TIER] tool: gallery -> Tier 2 - reason: manages generated local files and can delete images.

[INVENTORY] tool: gallery
  Current version: unversioned
  Purpose: Lists, searches, views, or removes previously generated images.
  Inputs: action: string required; query: string default empty.
  Outputs: string listing, path, delete confirmation, or string error.
  Known issues: remove is destructive without dry-run; fuzzy matching can select a close item without structured confidence; no trace.
  User changes detected: `tools/image.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: validate action and required query by action.
    - Output layer: include matched_path, action, count, removed, and meta.
    - Error layer: classify unknown action, missing query, no images, no match, and remove failure.
    - Observability: trace action branch, directory, files considered, duration, and status.
    - Resilience: dry-run before removal and unchanged-state reporting on failure.
    - New capabilities: optional dry_run for remove, optional limit for list, optional structured mode.
  Risk: MEDIUM - local deletion path needs stronger evidence.

[TIER] tool: file_read -> Tier 2 - reason: reads local files and may expose private content.

[INVENTORY] tool: file_read
  Current version: unversioned
  Purpose: Reads UTF-8 text file content with metadata and truncation.
  Inputs: path: string required; max_chars: integer default 3000.
  Outputs: string metadata plus content, binary notice, or string error.
  Known issues: no structured result; no trace; no explicit scope metadata; binary detection is extension and MIME based.
  User changes detected: `tools/files.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: clamp max_chars and validate resolved path.
    - Output layer: include resolved path, file type, size, modified time, truncated, and meta.
    - Error layer: classify not found, not file, binary, decode failure, and permission denied.
    - Observability: trace path scope, bytes read, truncation, duration, and status.
    - Resilience: fail closed on decode or permission errors without raw exception output.
    - New capabilities: optional structured mode, optional include_metadata_only.
  Risk: MEDIUM - local private content surface.

[TIER] tool: file_list -> Tier 2 - reason: local directory inspection can reveal private paths.

[INVENTORY] tool: file_list
  Current version: unversioned
  Purpose: Lists files and folders in a directory with optional hidden entries and a limit.
  Inputs: directory: string default `.`; include_hidden: boolean default false; limit: integer default 100.
  Outputs: string listing, empty message, or string error.
  Known issues: no structured item list; no trace; no resolved directory metadata; no explicit hidden count.
  User changes detected: `tools/files.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: clamp limit and validate resolved directory.
    - Output layer: include directory, items, item_count, hidden_excluded_count, truncated, and meta.
    - Error layer: classify directory not found, not directory, permission denied, and listing failure.
    - Observability: trace directory scope, count considered, hidden flag, duration, and status.
    - Resilience: fail closed on permission errors and report partial listing only when safe.
    - New capabilities: optional structured mode, optional include_metadata.
  Risk: MEDIUM - local filesystem visibility.

[TIER] tool: note_save -> Tier 2 - reason: persistent note mutation.

[INVENTORY] tool: note_save
  Current version: unversioned
  Purpose: Saves or overwrites a note title with content and optional tags.
  Inputs: title: string required; content: string required; tags: string default empty.
  Outputs: string saved or updated confirmation.
  Known issues: no structured result; no trace; migration can write during load; no dry-run.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: validate non-empty title and content without changing old accepted strings.
    - Output layer: include title, created, updated, changed, tag_count, and meta.
    - Error layer: classify empty title, storage read failure, migration failure, and storage write failure.
    - Observability: trace storage path, migration status, action, duration, and status.
    - Resilience: preserve old notes on failed write and report unchanged state.
    - New capabilities: optional structured mode, optional dry_run.
  Risk: MEDIUM - persistent user data mutation.

[TIER] tool: note_update -> Tier 2 - reason: persistent note mutation.

[INVENTORY] tool: note_update
  Current version: unversioned
  Purpose: Updates an existing note content and/or tags, with partial-title fallback.
  Inputs: title: string required; content: string default empty; tags: string default empty.
  Outputs: string update confirmation or string not-found error.
  Known issues: partial-title fallback has no confidence report; empty update still changes timestamp; no trace.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: require at least one changed field in new structured mode while preserving legacy behavior.
    - Output layer: include matched_title, changed_fields, updated timestamp, and meta.
    - Error layer: classify note not found, ambiguous match, storage read failure, and storage write failure.
    - Observability: trace match path, changed fields, duration, and status.
    - Resilience: avoid partial writes and report unchanged state on failure.
    - New capabilities: optional exact_match, optional structured mode.
  Risk: MEDIUM - persistent user data mutation.

[TIER] tool: note_delete -> Tier 2 - reason: persistent note deletion.

[INVENTORY] tool: note_delete
  Current version: unversioned
  Purpose: Deletes a note by exact or partial title match.
  Inputs: title: string required.
  Outputs: string delete confirmation, no-notes message, or not-found listing.
  Known issues: destructive partial match has no dry-run or confidence report; no trace.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: validate non-empty title and optionally require exact match.
    - Output layer: include matched_title, deleted, remaining_count, and meta.
    - Error layer: classify empty title, note not found, ambiguous match, and storage write failure.
    - Observability: trace match path, notes considered, duration, and status.
    - Resilience: optional dry-run and unchanged-state reporting on failure.
    - New capabilities: optional dry_run, optional exact_match, optional structured mode.
  Risk: MEDIUM - deletes persistent user data.

[TIER] tool: note_read -> Tier 3 - reason: read-only note retrieval.

[INVENTORY] tool: note_read
  Current version: unversioned
  Purpose: Reads a note by exact or partial title and shows metadata.
  Inputs: title: string required.
  Outputs: string note content plus tags and timestamps, no-notes message, or not-found listing.
  Known issues: partial match lacks confidence or ambiguity reporting; no structured result; no trace.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: validate non-empty title.
    - Output layer: include matched_title, content, tags, timestamps, and meta.
    - Error layer: classify empty title, note not found, ambiguous match, and storage read failure.
    - Observability: trace match path, notes considered, duration, and status.
    - Resilience: read failures should not trigger migration writes in read-only mode.
    - New capabilities: optional exact_match, optional structured mode.
  Risk: LOW - read-only, but may surface private note content.

[TIER] tool: note_list -> Tier 3 - reason: read-only note listing.

[INVENTORY] tool: note_list
  Current version: unversioned
  Purpose: Lists saved notes, optionally filtered by tag.
  Inputs: tag: string default empty.
  Outputs: string note titles with timestamps or previews, empty message, or no tag matches.
  Known issues: no structured list; no count metadata; no trace.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: normalize tag whitespace.
    - Output layer: include notes, count, filter tag, and meta.
    - Error layer: classify storage read failure and no matching tag.
    - Observability: trace tag filter, count considered, duration, and status.
    - Resilience: avoid mutating storage on list failures.
    - New capabilities: optional limit, optional structured mode.
  Risk: LOW - read-only note inventory.

[TIER] tool: note_search -> Tier 3 - reason: read-only note search.

[INVENTORY] tool: note_search
  Current version: unversioned
  Purpose: Searches note titles and content and returns matching previews.
  Inputs: keyword: string required.
  Outputs: string result list, empty message, or no-match message.
  Known issues: no structured matches; no count in no-result path beyond text; no trace.
  User changes detected: no direct tracked delta shown for `tools/notes.py`; preserved exactly.
  Upgrade candidates:
    - Input layer: validate non-empty search text.
    - Output layer: include matches, count, searched_fields, and meta.
    - Error layer: classify empty search text and storage read failure.
    - Observability: trace notes considered, matches, duration, and status.
    - Resilience: avoid raw exception output on bad storage.
    - New capabilities: optional limit, optional structured mode.
  Risk: LOW - read-only note content search.

[TIER] tool: datetime_info -> Tier 3 - reason: deterministic utility with limited failure surface.

[INVENTORY] tool: datetime_info
  Current version: unversioned
  Purpose: Returns current date and time for local or requested timezone.
  Inputs: timezone: string default `local`.
  Outputs: string formatted date/time, ambiguous timezone list, unknown timezone message, or string error.
  Known issues: no structured timezone resolution; generic error fallback; no trace; no machine-readable timestamp.
  User changes detected: `tools/datetime_tool.py` was already modified in the worktree; preserved exactly.
  Upgrade candidates:
    - Input layer: accept city names already; add safe normalization reporting.
    - Output layer: include resolved timezone, iso timestamp, display text, and meta.
    - Error layer: classify unknown timezone and ambiguous timezone.
    - Observability: trace resolution path, match count, duration, and status.
    - Resilience: graceful fallback if timezone database is unavailable.
    - New capabilities: optional output_format, optional structured mode.
  Risk: LOW - utility-only and already bounded.

## Phase 2 Tier 1 Upgrade Designs

These are executable-tool designs only. They are not marked implemented because
the current repo instruction says tool work should stay in markdown unless the
user explicitly allows code edits.

[UPGRADE DESIGN] tool: registry_dispatch
  Upgrade type: ADDITIVE

  Changes planned:
    Input layer:
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - preserves existing string callers while allowing structured callers to opt in.
      - Add optional `trace_enabled`: boolean, default false - lets callers request trace emission without forcing trace overhead into legacy paths.
      - Add optional `timeout_ms`: integer from 1 to 60000, default unset - gives the dispatcher a common timeout contract where a tool can honor it.
      - Keep unknown-parameter rejection - avoids silently accepting misspelled or hallucinated schema fields.
    Output layer:
      - Preserve the current legacy string return when `response_format` is `legacy` - V-01 can compare exact old outputs.
      - In structured mode, return `result.output`, `result.tool`, `result.arguments_used`, and `meta` with tool, version, duration_ms, attempt, and timestamp - makes dispatch output parseable without breaking old callers.
    Error layer:
      - Add stable structured error codes for structured mode: `TOOL_NOT_FOUND`, `UNKNOWN_PARAMETER`, `TOOL_TYPE_ERROR`, `TOOL_EXECUTION_ERROR`, and `TOOL_TIMEOUT` - callers can distinguish bad tool names, bad fields, bad types, runtime failures, and timeout failures.
      - Keep legacy string errors in legacy mode - avoids changing existing downstream text handling before tests prove the structured path.
    Observability:
      - Emit a machine-parseable trace when `trace_enabled` is true, including call_id, started_at, inputs_received, schema_valid, execution_path, external_calls, duration_ms, output_fields, status, and error_code - every dispatch can be audited without leaking secrets.
    Resilience:
      - Keep a typed boundary around each tool call - raw exceptions become classified errors in structured mode.
      - Treat timeout support as cooperative for existing tools until per-tool implementations are upgraded - avoids pretending central dispatch can interrupt every Python function safely.
    New capabilities (optional params only):
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable results - default: `legacy`
      - `trace_enabled`: boolean - unlocks structured trace emission - default: false
      - `timeout_ms`: integer 1..60000 - unlocks caller-bounded execution where supported - default: unset

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep the current string return contract.

  User changes preserved: `tools/registry.py` is not edited in this markdown-only pass.

[UPGRADE DESIGN] tool: terminal
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep required `command` and optional `workdir` and `timeout` unchanged - existing terminal calls remain valid.
      - Add optional `dry_run`: boolean, default false - lets high-risk callers preview the normalized command and working directory before execution.
      - Add optional `max_output_chars`: integer from 500 to 20000, default 5000 - lets callers control truncation without changing the current default.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output becomes opt-in only.
    Output layer:
      - Preserve current formatted string output in legacy mode - downstream chat wording and tests keep working.
      - In structured mode, return `result.exit_code`, `result.stdout`, `result.stderr`, `result.command`, `result.workdir`, `result.opened_path`, `result.timed_out`, and `meta` - callers can reason about success without parsing text.
    Error layer:
      - Add stable structured error codes for structured mode: `DIRECTORY_NOT_FOUND`, `COMMAND_TIMEOUT`, `COMMAND_NOT_FOUND`, `PERMISSION_DENIED`, `SYSTEM_COMMAND_ERROR`, and `INVALID_TIMEOUT` - recovery can be targeted.
      - Keep permission suggestions but move them into `error.suggestion` for structured mode - avoids mixing instructions into machine fields.
    Observability:
      - Emit a trace with command execution path, shell branch, workdir, duration, output field count, and status - terminal claims can be audited after the call.
      - Redact nothing by keyword; instead record only metadata and bounded command text supplied by the caller - avoids keyword routing while reducing secret exposure.
    Resilience:
      - Continue hard-enforcing configured timeout bounds - no command should hang indefinitely.
      - Treat dry-run as no side effect - gives destructive or broad operations a safe preview path.
    New capabilities (optional params only):
      - `dry_run`: boolean - previews command, shell, workdir, and risk metadata without execution - default: false
      - `max_output_chars`: integer 500..20000 - controls stdout and stderr truncation - default: 5000
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable command results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers keep the same `terminal(command, workdir='.', timeout=30)` behavior and same legacy string output.

  User changes preserved: existing ANSI stripping, Windows PowerShell shell selection, start-path normalization, and timeout bounds in `tools/terminal.py` stay intact.

[UPGRADE DESIGN] tool: file_write
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep required `path` and `content`, and optional `mode`, unchanged - existing write calls remain valid.
      - Add optional `dry_run`: boolean, default false - previews overwrite, append, or create_new effects without writing.
      - Add optional `create_parent_dirs`: boolean, default true - preserves existing parent-directory creation while allowing strict callers to opt out.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output is opt-in.
    Output layer:
      - Preserve current `Written to` and `Appended to` strings in legacy mode - existing tests and user-facing behavior stay stable.
      - In structured mode, return `result.action`, `result.path`, `result.mode`, `result.bytes_before`, `result.bytes_after`, `result.bytes_written`, `result.existed_before`, `result.changed`, `result.dry_run`, and `meta` - callers can verify what changed.
    Error layer:
      - Add stable structured error codes for structured mode: `INVALID_WRITE_MODE`, `FILE_ALREADY_EXISTS`, `PARENT_DIRECTORY_NOT_FOUND`, `PARENT_CREATE_FAILED`, `PERMISSION_DENIED`, and `WRITE_FAILED` - write failures become actionable.
      - Keep raw exception text out of structured caller-visible errors - raw details can go to internal trace only.
    Observability:
      - Emit a trace with normalized path, mode, dry_run flag, existed_before, duration, output_fields, status, and error_code - every file mutation attempt is auditable.
      - Do not include file content in traces - avoids writing private user text into operational logs.
    Resilience:
      - Use a same-directory temporary write and replace for overwrite in the future implementation where safe - reduces partial file risk.
      - Preserve append behavior unless dry_run is true - avoids changing relied-on append semantics.
    New capabilities (optional params only):
      - `dry_run`: boolean - previews file mutation impact without changing disk - default: false
      - `create_parent_dirs`: boolean - controls automatic parent directory creation - default: true
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable write results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep the same write modes, parent directory behavior, and legacy string output.

  User changes preserved: existing `_resolve`, `_metadata`, `_fmt`, binary detection, mode names, and file tool tests stay intact.

## Upgrade Anti-Pattern Review

- [ANTI-PATTERN: REQUIRED ADDITION] avoided: no new required runtime inputs were added.
- [ANTI-PATTERN: SILENT REWRITE] avoided: no executable tool code was edited.
- [ANTI-PATTERN: ASSUMED COMPAT] avoided: no backward-compatibility claim is made for unimplemented runtime upgrades.
- [ANTI-PATTERN: SPEC DRIFT] avoided: each candidate stays within the existing tool purpose.
- [ANTI-PATTERN: BREAKING RENAME] avoided: all planned capability names are optional additions and no existing field or function names are renamed.

## Phase 3 Markdown Manifest Implementation

Implemented a documented-only manifest contract named `tier1_upgrade_contract`
in `tools/TOOL_MANIFEST.md`.

Runtime status remains `documented_only` for:

- `registry_dispatch`
- `terminal`
- `file_write`

No executable schemas, Python callables, routing code, or storage behavior were
changed in this heartbeat.

## Phase 4 Markdown Diff Review

[UPGRADE DIFF] tool: registry_dispatch  unversioned -> documented-only contract

  Schema changes:
    Added inputs (optional): none at runtime; planned `response_format`, `trace_enabled`, `timeout_ms` documented only.
    Added outputs: none at runtime; planned structured `result` and `meta` documented only.
    Changed outputs (shape only, values same): none at runtime.
    Removed: NONE.

  Behavior changes:
    No runtime behavior changed -> reason: current repo instruction allows markdown tool work only.

  New error codes introduced: none at runtime; planned codes documented in manifest.
  New trace fields introduced: none at runtime; planned fields documented in manifest.
  New optional capabilities: none active; planned capabilities documented only.

  Backward compatible: YES

  User changes preserved: `tools/registry.py` was not edited.

[UPGRADE DIFF] tool: terminal  unversioned -> documented-only contract

  Schema changes:
    Added inputs (optional): none at runtime; planned `dry_run`, `max_output_chars`, `response_format` documented only.
    Added outputs: none at runtime; planned structured command result documented only.
    Changed outputs (shape only, values same): none at runtime.
    Removed: NONE.

  Behavior changes:
    No runtime behavior changed -> reason: current repo instruction allows markdown tool work only.

  New error codes introduced: none at runtime; planned codes documented in manifest.
  New trace fields introduced: none at runtime; planned fields documented in manifest.
  New optional capabilities: none active; planned capabilities documented only.

  Backward compatible: YES

  User changes preserved: existing `tools/terminal.py` user changes were not edited.

[UPGRADE DIFF] tool: file_write  unversioned -> documented-only contract

  Schema changes:
    Added inputs (optional): none at runtime; planned `dry_run`, `create_parent_dirs`, `response_format` documented only.
    Added outputs: none at runtime; planned structured write result documented only.
    Changed outputs (shape only, values same): none at runtime.
    Removed: NONE.

  Behavior changes:
    No runtime behavior changed -> reason: current repo instruction allows markdown tool work only.

  New error codes introduced: none at runtime; planned codes documented in manifest.
  New trace fields introduced: none at runtime; planned fields documented in manifest.
  New optional capabilities: none active; planned capabilities documented only.

  Backward compatible: YES

  User changes preserved: existing `tools/files.py` user changes were not edited.

## Phase 5 Markdown Verification

[V-01] PASS - evidence: no executable tool code or callable schema changed; legacy runtime behavior remains untouched by this heartbeat.

[V-02] SKIPPED - evidence: new input shapes are documented only and are not active runtime schema fields.

[V-03] SKIPPED - evidence: structured runtime errors are documented only and are not active callable outputs.

[V-04] SKIPPED - evidence: trace emission is documented only and no runtime trace path was added.

[V-05] SKIPPED - evidence: timeout changes are documented only and no runtime timeout implementation changed.

[V-06] SKIPPED - evidence: retry behavior is documented only and no runtime retry implementation changed.

[V-07] SKIPPED - evidence: optional capabilities are documented only and not active runtime parameters.

[V-08] PASS - evidence: downstream repository checks passed after the markdown contract update: `python -m unittest tests.test_grounding` ran 21 tests OK, and `npm run typecheck` passed.

[MANIFEST UPDATED] tool: tier1_upgrade_contract - new version: documented-only - score: not runtime-scored

## Phase 2 Tier 2 External Retrieval Upgrade Designs

These are executable-tool designs only. They are not marked implemented because
the current repo instruction says tool work should stay in markdown unless the
user explicitly allows code edits.

[UPGRADE DESIGN] tool: web_search
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep required `query` and optional `max_results` unchanged - existing search calls remain valid.
      - Add optional `timeout_ms`: integer from 1000 to 60000, default 10000 - gives callers bounded external search time.
      - Add optional `provider`: string enum `auto`, `tavily`, or `ddgs`, default `auto` - lets callers choose a provider without changing default fallback behavior.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output remains opt-in.
    Output layer:
      - Preserve current plain text result list in legacy mode - existing chat and tests keep working.
      - In structured mode, return `result.query`, `result.provider_used`, `result.results`, `result.count`, `result.degraded`, and `meta` - callers can distinguish no results from provider failure.
    Error layer:
      - Add stable structured error codes for structured mode: `EMPTY_QUERY`, `INVALID_PROVIDER`, `PROVIDER_TIMEOUT`, `PROVIDER_UNAVAILABLE`, and `NO_RESULTS` - current-information failures become actionable.
    Observability:
      - Trace provider branch, fallback branch, external call count, result count, duration, status, and error_code - false negatives can be audited.
    Resilience:
      - Keep provider fallback in `auto` mode and mark fallback as degraded instead of hiding it.
      - Retry only transient provider failures after timeout and circuit-breaker support exists in code.
    New capabilities (optional params only):
      - `timeout_ms`: integer 1000..60000 - bounds external search duration - default: 10000
      - `provider`: string enum `auto`, `tavily`, `ddgs` - selects provider behavior - default: `auto`
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable search results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep the same plain text output path.

  User changes preserved: existing `tools/web.py` user changes were not edited.

[UPGRADE DESIGN] tool: web_fetch
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep required `url` and optional `max_chars` unchanged - existing fetch calls remain valid.
      - Add optional `timeout_ms`: integer from 1000 to 60000, default 10000 - prevents indefinite page fetches.
      - Add optional `follow_redirects`: boolean, default true - preserves current redirect behavior while making it explicit.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output is opt-in.
    Output layer:
      - Preserve current URL, status, title, and text format in legacy mode.
      - In structured mode, return `result.requested_url`, `result.final_url`, `result.status_code`, `result.title`, `result.text`, `result.truncated`, `result.readable_text_found`, and `meta`.
    Error layer:
      - Add stable structured error codes for structured mode: `INVALID_URL`, `FETCH_TIMEOUT`, `HTTP_ERROR_STATUS`, `NETWORK_ERROR`, and `NO_READABLE_TEXT`.
    Observability:
      - Trace requested host, final URL host, redirect flag, status code, text length, duration, status, and error_code without storing page content in traces.
    Resilience:
      - Retry transient network failures only when the request method is read-only and timeout bounds are honored.
      - Treat empty readable text as an `empty` result, not a broad failure.
    New capabilities (optional params only):
      - `timeout_ms`: integer 1000..60000 - bounds page fetch duration - default: 10000
      - `follow_redirects`: boolean - controls redirect handling - default: true
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable page fetch results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep current redirect and text output behavior.

  User changes preserved: existing `tools/web.py` user changes were not edited.

[UPGRADE DESIGN] tool: reddit
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep current `action`, `subreddit`, `query`, `limit`, `time`, `id`, and `sort` inputs unchanged - existing Reddit calls remain valid.
      - Add optional `timeout_ms`: integer from 1000 to 60000, default 10000 - bounds Reddit API and fallback search calls.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output remains opt-in.
      - Add optional `include_source_status`: boolean, default false - lets callers request provider status without changing legacy text.
    Output layer:
      - Preserve current text listings and fallback text in legacy mode.
      - In structured mode, return `result.action`, `result.source`, `result.subreddit`, `result.query`, `result.items`, `result.count`, `result.fallback_used`, `result.rate_limited`, `result.degraded`, and `meta`.
    Error layer:
      - Add stable structured error codes for structured mode: `MISSING_SUBREDDIT`, `MISSING_QUERY`, `POST_NOT_FOUND`, `USER_NOT_FOUND`, `RATE_LIMITED`, `PROVIDER_ERROR`, and `NO_RESULTS`.
    Observability:
      - Trace action branch, cache hit, Reddit call count, fallback branch, result count, duration, status, and error_code.
    Resilience:
      - Preserve existing fallback search behavior, but expose fallback as degraded in structured mode.
      - Treat Reddit API block, rate limit, provider failure, and no results as separate outcomes.
    New capabilities (optional params only):
      - `timeout_ms`: integer 1000..60000 - bounds external Reddit and fallback calls - default: 10000
      - `include_source_status`: boolean - reports provider state in structured mode - default: false
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable Reddit results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep the same text output and fallback behavior.

  User changes preserved: existing `tools/reddit.py` user changes were not edited.

[UPGRADE DESIGN] tool: hackernews
  Upgrade type: HARDENING

  Changes planned:
    Input layer:
      - Keep current `action`, `limit`, `query`, and `id` inputs unchanged - existing Hacker News calls remain valid.
      - Add optional `timeout_ms`: integer from 1000 to 60000, default 10000 - bounds Firebase and Algolia calls.
      - Add optional `response_format`: string enum `legacy` or `structured`, default `legacy` - structured output remains opt-in.
      - Add optional `include_source_status`: boolean, default false - lets callers inspect provider state when needed.
    Output layer:
      - Preserve current story, user, comments, and search text output in legacy mode.
      - In structured mode, return `result.action`, `result.endpoint`, `result.query`, `result.items`, `result.count`, `result.cache_hit`, `result.source_status`, and `meta`.
    Error layer:
      - Add stable structured error codes for structured mode: `MISSING_QUERY`, `INVALID_STORY_ID`, `STORY_NOT_FOUND`, `USER_NOT_FOUND`, `PROVIDER_TIMEOUT`, `PROVIDER_ERROR`, and `NO_RESULTS`.
    Observability:
      - Trace action branch, endpoint, cache hit, external call count, result count, duration, status, and error_code.
    Resilience:
      - Keep cache usage visible in structured mode and retry only transient provider failures after code support exists.
      - Treat provider failure separately from empty listings so KING does not claim broad absence from a narrow failure.
    New capabilities (optional params only):
      - `timeout_ms`: integer 1000..60000 - bounds provider calls - default: 10000
      - `include_source_status`: boolean - reports provider state in structured mode - default: false
      - `response_format`: string enum `legacy` or `structured` - unlocks parseable Hacker News results - default: `legacy`

  Backward compatibility: PRESERVED
    -> Existing callers pass no new params and keep the same text output and cache behavior.

  User changes preserved: existing `tools/hackernews.py` user changes were not edited.

## Phase 2 Tier 2 Verification

[V-01] PASS - evidence: no executable tool code or callable schema changed; legacy runtime behavior remains untouched by this heartbeat.

[V-02] SKIPPED - evidence: new Tier 2 input shapes are documented only and are not active runtime schema fields.

[V-03] SKIPPED - evidence: structured runtime errors are documented only and are not active callable outputs.

[V-04] SKIPPED - evidence: trace emission is documented only and no runtime trace path was added.

[V-05] SKIPPED - evidence: timeout changes are documented only and no runtime timeout implementation changed.

[V-06] SKIPPED - evidence: retry behavior is documented only and no runtime retry implementation changed.

[V-07] SKIPPED - evidence: optional capabilities are documented only and not active runtime parameters.

[V-08] PASS - evidence: downstream repository checks passed after the Tier 2 external retrieval design update: `python -m unittest tests.test_grounding` ran 26 tests OK, and `npm run typecheck` passed.

## Next Heartbeat Work

1. Continue Tier 1 runtime upgrades with `terminal`, then `file_write`.
2. Keep every new runtime capability optional and legacy-preserving by default.
3. Do not mark any additional runtime tool as upgraded until V-01 passes against executable behavior.

## Phase 3 Runtime Implementation - registry_dispatch

Implemented `registry_dispatch` version `2.0.0` in `tools/registry.py`.

Runtime changes:

- Added optional dispatcher controls: `response_format`, `trace_enabled`, and
  `timeout_ms`.
- Preserved default legacy string output for callers that pass no new controls.
- Added structured success responses with `result` and `meta`.
- Added structured error responses with stable error codes:
  `TOOL_NOT_FOUND`, `UNKNOWN_PARAMETER`, `INVALID_TIMEOUT`, `TOOL_TIMEOUT`,
  `TOOL_TYPE_ERROR`, and `TOOL_EXECUTION_ERROR`.
- Added machine-parseable JSON trace emission when `trace_enabled` is true.
- Added bounded timeout handling for requested dispatcher calls.
- Kept retry disabled in the dispatcher because repeating arbitrary tool calls
  can duplicate side effects; retry belongs in side-effect-aware tool layers.

[NEW CAPABILITY] tool: registry_dispatch
  param: response_format (optional, default: legacy)
  type: string enum legacy|structured
  what it unlocks: parseable result/error envelopes with metadata
  example: execute_tool("datetime_info", response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: registry_dispatch
  param: trace_enabled (optional, default: false)
  type: boolean
  what it unlocks: machine-parseable per-call trace emission
  example: execute_tool("datetime_info", response_format="structured", trace_enabled=True)
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: registry_dispatch
  param: timeout_ms (optional, default: unset)
  type: integer 1..60000
  what it unlocks: bounded dispatcher wait time for requested calls
  example: execute_tool("slow_tool", timeout_ms=1000, response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

## Phase 4 Runtime Diff Review - registry_dispatch

[UPGRADE DIFF] tool: registry_dispatch  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `response_format`, `trace_enabled`, `timeout_ms`.
    Added outputs: structured `result`, structured `error`, `meta`, optional trace object.
    Changed outputs (shape only, values same): none for legacy callers.
    Removed: NONE.

  Behavior changes:
    Legacy success string -> unchanged legacy success string - reason: backward compatibility.
    Legacy unknown tool string -> unchanged legacy unknown tool string - reason: backward compatibility.
    Legacy unknown parameter string -> unchanged legacy unknown parameter string - reason: backward compatibility.
    Raw structured exceptions -> typed structured error without raw exception text - reason: no raw exceptions exposed to callers.

  New error codes introduced: `TOOL_NOT_FOUND`, `UNKNOWN_PARAMETER`, `INVALID_TIMEOUT`, `TOOL_TIMEOUT`, `TOOL_TYPE_ERROR`, `TOOL_EXECUTION_ERROR`.
  New trace fields introduced: `event`, `tool`, `version`, `call_id`, `started_at`, `inputs_received`, `schema_valid`, `execution_path`, `external_calls`, `duration_ms`, `output_fields`, `status`, `error_code`.
  New optional capabilities: `response_format`, `trace_enabled`, `timeout_ms`.

  Backward compatible: YES

  User changes preserved: no existing tool implementation files were reverted; only `tools/registry.py` and focused dispatcher tests were edited for this upgrade.

## Phase 5 Runtime Verification - registry_dispatch

[V-01] PASS - evidence: direct call `execute_tool("v_registry_echo", text="same")` returned exact legacy string `same`; legacy unknown parameter string remained `Error: 'registry_test_echo' received unknown parameter(s): extra. Accepted: text`.

[V-02] PASS - evidence: `execute_tool("v_registry_echo", text="structured", response_format="structured")` returned structured output with `result.output` equal to `structured`.

[V-03] PASS - evidence: intentional unknown parameter returned `UNKNOWN_PARAMETER`; intentional exception returned `TOOL_EXECUTION_ERROR`; raw text `raw-secret-detail` was not present in the structured response.

[V-04] PASS - evidence: trace-enabled success emitted JSON with `status` `SUCCESS`; trace-enabled failure emitted JSON with `status` `FAILED`.

[V-05] PASS - evidence: slow test tool with `timeout_ms=10` returned `TOOL_TIMEOUT` in 94 ms.

[V-06] PASS - evidence: dispatcher has no external dependency retry path by design; retry is intentionally disabled to avoid duplicate side effects from arbitrary tool replay.

[V-07] PASS - evidence: tests covered `response_format`, `trace_enabled`, and `timeout_ms`; direct V-check also exercised all three.

[V-08] PASS - evidence: downstream `ToolValidator.validate_and_execute("v_registry_echo", {"text": "downstream"})` returned `(True, "downstream")`; `python -m unittest tests.test_grounding` ran 32 tests OK; `python -m pytest -q` passed 35 tests; `python -m compileall tools agent memory voice gesture main.py config.py` passed; `npm run typecheck` passed.

[MANIFEST UPDATED] tool: registry_dispatch - new version: 2.0.0 - score: 9.5/10

## Security Audit Addendum - 2026-05-19T08:35Z

[SCAN] target: frontend dependency graph

[TEST] dependency advisory check -> `npm audit --audit-level=low`

[RESULT] initial audit failed with 2 moderate vulnerabilities through `next -> postcss@8.4.31`.

[FINDING: MEDIUM] vulnerable transitive dependency
  Evidence: npm advisory reported `postcss <8.5.10` with GHSA-qx2v-qp2m-jg93.
  Root cause: `next@16.2.6` resolved `postcss@8.4.31`.
  Fix: added a package override for `postcss@8.5.10` and refreshed the install.

[TEST] dependency advisory check after fix -> `npm audit --audit-level=low`

[RESULT] `found 0 vulnerabilities`

[TEST] resolved dependency proof -> `npm ls postcss`

[RESULT] `next@16.2.6` now resolves `postcss@8.5.10 overridden`.

[TEST] regression checks

[RESULT] `python -m pytest -q` passed 26 tests with one `.pytest_cache` permission warning; `npm run typecheck` passed; `python -m compileall agent tools memory voice gesture main.py config.py` passed.

[GAP] image generation still uses HTTP calls with TLS verification disabled in `tools/image.py`.

[VERDICT] dependency issue fixed and verified; TLS verification behavior remains a follow-up risk.

## Security Audit Addendum - 2026-05-19T08:46Z

[SCAN] target: network transport and audit scope controls

[TEST] fixed-string transport scan -> `rg -n -F "verify=False" tools agent memory voice gesture main.py config.py`

[RESULT] only `tools/image.py` still contains unverified HTTP calls; `tools/web.py` and `tools/hackernews.py` use verified `httpx` calls with provider status handling.

[FINDING: MEDIUM] image generation transport is unverified
  Evidence: `tools/image.py` passes `verify=False` to Pollinations, NVIDIA, poll-result, and image-url `httpx` calls.
  Root cause: the image tool disables TLS verification and suppresses the warning.
  Fix status: runtime code not changed in this pass because current tool-work instructions prefer markdown-only changes unless a verified code-level false negative requires code. `tool_policy.md` now requires KING not to call this transport verified or secure while the runtime remains unverified.

[TEST] manifest audit consistency -> `python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 200, True))"`

[RESULT] `Status: success`; 10 observed tool modules matched 10 manifest modules; 20 callable schemas were listed; no files were changed by the audit.

[GAP] `tool_manifest_audit` is read-only but accepts a caller-provided root, which can reveal path structure outside the repo if misused.

[FIX] `tool_policy.md` now requires manifest and tool audits to stay inside the current repository unless the user provides an exact alternate root.

[VERDICT] no runtime code changed; audit scope and unverified transport reporting are now guarded in the loaded markdown policy. Runtime TLS verification remains a follow-up code fix.
