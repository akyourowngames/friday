# KING Tool Upgrade Session

Last updated: 2026-05-21T00:00:00+05:30

This file is the visible upgrade-control artifact for the current heartbeat. It
records Phase 1 inventory and tier assignment before any runtime tool changes.

Current exception: the 2026-05-21 browser automation request explicitly grants
code-edit authority for the browser extraction tool. The implementation remains
markdown-configured and avoids hardcoded routing or phrase matching.

Important boundary: repository instructions for tool work currently say to edit
markdown files and not touch tool code. This session therefore does not claim a
runtime tool upgrade has shipped. Any executable change remains deferred until
the user explicitly allows code edits.

Current exception: the 2026-05-19T08:38Z heartbeat explicitly allowed code edits
and full access, so the `registry_dispatch` runtime upgrade below is active and
verified.

Current exception: the 2026-05-19T23:15+05:30 fix explicitly addressed live
tool-result leakage in the CLI. The agent now requests structured tool context
from any callable that advertises `response_format`, serializes structured
results as JSON for the model context, and finalizes tool results through the
LLM before user-facing output. The project env disables the direct single-tool
fast path and enables finalization so provider tools such as web, Reddit, and
Hacker News fetch first, then answer from observed fields instead of dumping raw
payloads or result lists.

Verification evidence for this fix:

- `python -m py_compile agent\core.py config.py`
- `python -m unittest tests.test_grounding` -> 60 tests passed.
- `'/exit' | python main.py` -> CLI reached `Ready` and exited cleanly.
- Live web-search CLI probe answered in prose without raw structured payload.
- Live Hacker News CLI probe summarized fetched results without dumping the raw
  listing.
- Live Reddit CLI probe reported the observed empty-result state in prose.

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

1. Continue tool runtime upgrades only when the previous surface has executable
   verification evidence.
2. Keep every new runtime capability optional and legacy-preserving by default.
3. Use `tool_verification_pipeline` after each heartbeat so the final report can
   name real command evidence instead of broad claims.

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

## Phase 6 Runtime Implementation - tool_verification_pipeline

Implemented `tool_verification_pipeline` version `1.0.0` in
`tools/verification_pipeline.py`.

Runtime changes:

- Added a callable verification runner that reads `- command:` entries from the
  configured markdown pipeline file.
- Added config knobs for pipeline file path, maximum steps, command timeout, and
  captured output size.
- Kept pipeline commands visible in `tools/TOOL_VERIFICATION_PIPELINE.md`
  instead of embedding the check list in Python code.
- Added structured success and blocked-error envelopes with trace support.
- Added dry-run mode so KING can preview a verification plan without executing
  shell commands.
- Blocked pipeline files that resolve outside the requested repository root.

[NEW CAPABILITY] tool: tool_verification_pipeline
  param: pipeline_path (optional, default: configured markdown pipeline)
  type: string path inside repository root
  what it unlocks: visible, markdown-owned verification plans
  backward compatible: YES - no existing tool behavior changed

[NEW CAPABILITY] tool: tool_verification_pipeline
  param: dry_run (optional, default: false)
  type: boolean
  what it unlocks: command preview without command execution
  backward compatible: YES - callers opt in

[NEW CAPABILITY] tool: tool_verification_pipeline
  param: response_format and trace_enabled (optional)
  type: legacy or structured plus boolean
  what it unlocks: parseable evidence and machine-readable traces
  backward compatible: YES - legacy text remains the default

## Phase 7 Runtime Verification - tool_verification_pipeline

[V-01] PASS - evidence: the new tool is isolated; existing tool callers are not
rewired and legacy defaults are text-only.

[V-02] PASS - evidence: `tests/test_verification_pipeline.py` verifies a
markdown command runs and returns structured evidence.

[V-03] PASS - evidence: the same test file verifies dry-run does not execute a
state-changing command.

[V-04] PASS - evidence: the same test file verifies an absolute pipeline file
outside the root returns `PIPELINE_OUT_OF_SCOPE`.

[V-05] PASS - evidence: the same test file verifies a failing required command
sets status `failed` and ship decision `hold`.

[V-06] PASS - evidence: default `tool_verification_pipeline` run returned
`Status: success` and `Ship decision: ship`; it ran 4 required checks from
`tools/TOOL_VERIFICATION_PIPELINE.md`: `python -m unittest tests.test_grounding`
ran 45 tests OK, `python -m pytest -q` passed 50 tests, `python -m compileall
tools agent memory voice gesture main.py config.py` passed, and `npm run
typecheck` passed.

## Phase 8 Markdown Pipeline Hardening - manifest alignment

Changed `tools/TOOL_VERIFICATION_PIPELINE.md` so manifest and callable-schema
alignment is a required default check.

Reason:

- Tool changes should fail fast if a Python tool module is active but missing
  from the markdown manifest.
- The manifest should also catch stale entries when a documented executable
  module no longer exists.
- The default heartbeat proof should include registered callable schema
  evidence, not only tests that happen to import tools.

Runtime changes:

- None. This phase is markdown-only.

[V-01] PASS - evidence: updated default `tool_verification_pipeline` run
returned `Status: success` and `Ship decision: ship`; it ran 5 required checks.
The new manifest alignment check reported 11 observed tool modules, 11 manifest
modules, 21 registered callable schemas, and no missing manifest or file
entries. The remaining checks passed: `python -m unittest tests.test_grounding`
ran 45 tests OK, `python -m pytest -q` passed 50 tests, `python -m compileall
tools agent memory voice gesture main.py config.py` passed, and `npm run
typecheck` passed.

## Phase 9 Markdown Evidence Ledger

Added `tools/TOOL_EVIDENCE_LEDGER.md` as the compact source for capability claim
status.

Reason:

- Tool capability claims need a current state vocabulary instead of scattered
  prose.
- KING should distinguish `verified_runtime`, `active_legacy`,
  `documented_only`, `blocked`, and `deferred`.
- Future tool additions should update a claim ledger before the assistant says
  a capability is active.

Runtime changes:

- None. This phase is markdown-only.

[V-01] PASS - evidence: default `tool_verification_pipeline` returned
`Status: success` and `Ship decision: ship`; it ran 5 required checks. Manifest
alignment reported 11 observed tool modules, 11 manifest modules, 21 registered
callable schemas, and no missing manifest or file entries. `python -m unittest
tests.test_grounding` ran 49 tests OK, `python -m pytest -q` passed 54 tests,
`python -m compileall tools agent memory voice gesture main.py config.py`
passed, and `npm run typecheck` passed. Direct inspection also confirmed
`tools/TOOL_EVIDENCE_LEDGER.md` exists and contains `verified_runtime` status
entries.

## Phase 10 Markdown Provider Failure Playbook

Added `tools/TOOL_PROVIDER_FAILURE_PLAYBOOK.md` as the provider-backed tool
truthfulness guide.

Reason:

- Provider-backed tools need consistent language for `empty`, `partial`,
  `timeout`, `failed`, `blocked`, `unavailable`, and `unknown_after_attempt`.
- KING should not convert one provider failure into a broad false negative.
- Media and generated-artifact tools need explicit boundaries between opened,
  saved, queued, played, deleted, and verified states.

Runtime changes:

- None. This phase is markdown-only.

[V-01] PASS - evidence: default `tool_verification_pipeline` returned
`Status: success` and `Ship decision: ship`; it ran 5 required checks. Manifest
alignment reported 11 observed tool modules, 11 manifest modules, 21 registered
callable schemas, and no missing manifest or file entries. `python -m unittest
tests.test_grounding` ran 49 tests OK, `python -m pytest -q` passed 54 tests
with one `.pytest_cache` permission warning, `python -m compileall tools agent
memory voice gesture main.py config.py` passed, and `npm run typecheck` passed.
Direct inspection confirmed `tools/TOOL_PROVIDER_FAILURE_PLAYBOOK.md` exists,
contains `unknown_after_attempt`, and is linked from `tools/TOOL_MANIFEST.md`.

## Phase 11 Markdown Tool Intake Checklist

Added `tools/TOOL_INTAKE_CHECKLIST.md` as the intake gate for new tools and tool
upgrades.

Reason:

- Future heartbeat work needs a repeatable documentation path before KING says a
  tool exists or succeeded.
- New tool claims should name the active callable schema, target scope, applied
  contracts, exact success evidence, and separate failure states.
- The checklist keeps markdown planning separate from runtime truth, so
  documented designs do not become false active-capability claims.

Runtime changes:

- None. This phase is markdown-only.

[V-01] PASS - evidence: default `tool_verification_pipeline` returned
`Status: success` and `Ship decision: ship`; it ran 5 required checks. Manifest
alignment reported 11 observed tool modules, 11 manifest modules, 21 registered
callable schemas, and no missing manifest or file entries. `python -m unittest
tests.test_grounding` ran 49 tests OK, `python -m pytest -q` passed 54 tests
with one `.pytest_cache` permission warning, `python -m compileall tools agent
memory voice gesture main.py config.py` passed, and `npm run typecheck` passed.
Direct inspection confirmed `tools/TOOL_INTAKE_CHECKLIST.md` exists, contains
`verified_runtime`, and is linked from `tools/TOOL_MANIFEST.md`.

## Phase 3 Runtime Implementation - terminal

Implemented or verified `terminal` version `2.0.0` in `tools/terminal.py`.

Runtime changes:

- Preserved legacy string output for callers that pass no new controls.
- Added optional controls: `dry_run`, `max_output_chars`, `timeout_ms`,
  `response_format`, and `trace_enabled`.
- Added structured success responses with command, cwd, timeout, exit code,
  stdout, stderr, opened path, dry-run state, truncation state, status, and
  `meta`.
- Added typed structured errors:
  `EMPTY_COMMAND`, `DIRECTORY_NOT_FOUND`, `INVALID_TIMEOUT`,
  `INVALID_OUTPUT_LIMIT`, `COMMAND_TIMEOUT`, `COMMAND_NOT_FOUND`,
  `PERMISSION_DENIED`, `COMMAND_FAILED`, and `SYSTEM_COMMAND_ERROR`.
- Added machine-parseable JSON trace emission when `trace_enabled` is true.
- Kept automatic retries disabled because arbitrary shell/app-launch replay can
  duplicate side effects.

[NEW CAPABILITY] tool: terminal
  param: dry_run (optional, default: false)
  type: boolean
  what it unlocks: execution preview without launching or running a command
  example: terminal("echo ok", dry_run=True, response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: terminal
  param: max_output_chars (optional, default: 5000)
  type: integer 200..20000
  what it unlocks: caller-controlled bounded stdout and stderr
  example: terminal("echo ok", max_output_chars=1000)
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: terminal
  param: timeout_ms (optional, default: unset)
  type: integer 1..60000
  what it unlocks: millisecond-level timeout override for bounded command calls
  example: terminal("echo ok", timeout_ms=1000, response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

## Phase 3 Runtime Implementation - file_write

Implemented `file_write` version `2.0.0` in `tools/files.py`.

Runtime changes:

- Preserved legacy string output for callers that pass no new controls.
- Added optional controls: `dry_run`, `create_parent_dirs`,
  `response_format`, and `trace_enabled`.
- Added structured success responses with path, mode, existed-before state,
  parent-created state, requested and written byte counts, dry-run state,
  changed state, final size, and `meta`.
- Added typed structured errors:
  `EMPTY_PATH`, `INVALID_WRITE_MODE`, `FILE_ALREADY_EXISTS`,
  `PARENT_DIRECTORY_NOT_FOUND`, `PARENT_NOT_DIRECTORY`, `PERMISSION_DENIED`,
  and `WRITE_FAILED`.
- Added machine-parseable JSON trace emission when `trace_enabled` is true.
- Kept atomic overwrite behavior and avoided automatic retries to prevent
  duplicate writes.

[NEW CAPABILITY] tool: file_write
  param: dry_run (optional, default: false)
  type: boolean
  what it unlocks: write preview without creating parents or changing files
  example: file_write("notes.txt", "draft", dry_run=True, response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: file_write
  param: create_parent_dirs (optional, default: true)
  type: boolean
  what it unlocks: callers can block implicit parent-directory creation
  example: file_write("missing/path.txt", "draft", create_parent_dirs=False, response_format="structured")
  backward compatible: YES - default true preserves existing behavior

## Phase 4 Runtime Diff Review - terminal

[UPGRADE DIFF] tool: terminal  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `dry_run`, `max_output_chars`, `timeout_ms`, `response_format`, `trace_enabled`.
    Added outputs: structured `result`, structured `error`, `meta`, trace object.
    Changed outputs (shape only, values same): none for legacy callers.
    Removed: NONE.

  Behavior changes:
    Legacy command output -> unchanged legacy command output - reason: backward compatibility.
    Legacy missing directory string -> unchanged missing directory string - reason: backward compatibility.
    Optional dry-run -> new non-executing preview path - reason: safer command planning.
    Optional structured mode -> parseable result/error envelope - reason: downstream tools can inspect exact status.

  New error codes introduced: `EMPTY_COMMAND`, `DIRECTORY_NOT_FOUND`, `INVALID_TIMEOUT`, `INVALID_OUTPUT_LIMIT`, `COMMAND_TIMEOUT`, `COMMAND_NOT_FOUND`, `PERMISSION_DENIED`, `COMMAND_FAILED`, `SYSTEM_COMMAND_ERROR`.
  New trace fields introduced: `event`, `tool`, `version`, `call_id`, `started_at`, `inputs_received`, `schema_valid`, `execution_path`, `external_calls`, `duration_ms`, `output_fields`, `status`, `error_code`.
  New optional capabilities: `dry_run`, `max_output_chars`, `timeout_ms`, `response_format`, `trace_enabled`.

  Backward compatible: YES

  User changes preserved: regex-free ANSI stripping, configured timeout bounds, Windows PowerShell execution, and direct existing-path open behavior.

## Phase 4 Runtime Diff Review - file_write

[UPGRADE DIFF] tool: file_write  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `dry_run`, `create_parent_dirs`, `response_format`, `trace_enabled`.
    Added outputs: structured write result, structured error, `meta`, trace object.
    Changed outputs (shape only, values same): none for legacy callers.
    Removed: NONE.

  Behavior changes:
    Legacy overwrite/append/create_new strings -> unchanged legacy strings - reason: backward compatibility.
    Optional dry-run -> new no-side-effect planning path - reason: safer writes and retries.
    Optional parent creation block -> new explicit refusal when parents are missing - reason: caller can prevent implicit directory creation.
    Optional structured mode -> parseable write metadata - reason: downstream tools can verify mutation state.

  New error codes introduced: `EMPTY_PATH`, `INVALID_WRITE_MODE`, `FILE_ALREADY_EXISTS`, `PARENT_DIRECTORY_NOT_FOUND`, `PARENT_NOT_DIRECTORY`, `PERMISSION_DENIED`, `WRITE_FAILED`.
  New trace fields introduced: `event`, `tool`, `version`, `call_id`, `started_at`, `inputs_received`, `schema_valid`, `execution_path`, `external_calls`, `duration_ms`, `output_fields`, `status`, `error_code`.
  New optional capabilities: `dry_run`, `create_parent_dirs`, `response_format`, `trace_enabled`.

  Backward compatible: YES

  User changes preserved: atomic overwrite, append and create_new modes, metadata output, and invalid-mode no-parent-side-effect behavior.

## Phase 5 Runtime Verification - terminal and file_write

[V-01] PASS - evidence: `terminal("echo upgrade_v01", timeout=5)` returned legacy string `upgrade_v01`; `file_write(temp/v01.txt, "hello", mode="create_new")` returned legacy `Written to:` output and created the file.

[V-02] PASS - evidence: `execute_tool("terminal", dry_run=True, response_format="structured", trace_enabled=True)` returned `result.status` `DRY_RUN`; `file_write(..., create_parent_dirs=False, response_format="structured")` returned typed `PARENT_DIRECTORY_NOT_FOUND` without creating the parent.

[V-03] PASS - evidence: `terminal("", response_format="structured")` returned `EMPTY_COMMAND`; `file_write("storage/v03.txt", "x", mode="bad", response_format="structured")` returned `INVALID_WRITE_MODE`; both included code, message, field, expected, retryable, and suggestion.

[V-04] PASS - evidence: trace-enabled successful `file_write` emitted JSON trace with `status` `SUCCESS`; trace-enabled failing `terminal` emitted JSON trace with `status` `FAILED` and `error_code` `EMPTY_COMMAND`.

[V-05] PASS - evidence: slow terminal command with `timeout_ms=50` returned `COMMAND_TIMEOUT` in 97 ms. `file_write` uses local atomic writes and does not add an unkillable thread timeout that could create unknown partial writes.

[V-06] PASS - evidence: terminal and file_write intentionally do not auto-retry side-effect-capable calls; retry remains blocked by idempotency policy to avoid duplicate launches or writes.

[V-07] PASS - evidence: `file_write(..., dry_run=True, response_format="structured")` returned `dry_run=true` and left the parent absent; `terminal("echo abcdef", max_output_chars=200, response_format="structured")` returned structured stdout `abcdef`.

[V-08] PASS - evidence: downstream `ToolValidator.validate_and_execute("terminal", {"command": "echo downstream", "timeout": 5})` returned `(True, "downstream")`; broader checks passed.

[TEST] `python -m unittest tests.test_grounding -v`

[RESULT] 45 tests OK.

[TEST] `python -m pytest -q`

[RESULT] 45 passed.

[TEST] `python -m compileall tools agent memory voice gesture main.py config.py`

[RESULT] compileall passed.

[TEST] `npm run typecheck`

[RESULT] typecheck passed.

[TEST] `npm run build`

[RESULT] Next.js production build passed.

[TEST] manifest audit consistency -> `python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"`

[RESULT] `Status: success`; 11 observed tool modules matched 11 manifest modules; 21 callable schemas were listed; no files were changed by the audit.

[MANIFEST UPDATED] tool: terminal - new version: 2.0.0 - score: 9.5/10

[MANIFEST UPDATED] tool: file_write - new version: 2.0.0 - score: 9.5/10

## Phase 3 Runtime Implementation - web_search

Implemented `web_search` version `2.0.0` in `tools/web.py`.

Runtime changes:

- Preserved legacy text output for callers that pass no new controls.
- Added optional controls: `provider`, `timeout_ms`, `response_format`, and
  `trace_enabled`.
- Added structured success responses with query, normalized max results,
  requested provider, provider used, fallback flag, result list, result count,
  degraded flag, degraded reason, provider status, and `meta`.
- Added typed structured errors for `EMPTY_QUERY`, `INVALID_PROVIDER`,
  `INVALID_RESULT_LIMIT`, and `INVALID_TIMEOUT`.
- Preserved Tavily-first, DDGS-fallback behavior in auto mode and exposes the
  fallback as degraded structured metadata.

[NEW CAPABILITY] tool: web_search
  param: provider (optional, default: auto)
  type: string enum auto|tavily|ddgs
  what it unlocks: caller can choose provider behavior without changing default fallback
  example: web_search("ai news", provider="ddgs", response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: web_search
  param: timeout_ms (optional, default: 10000)
  type: integer 1..60000
  what it unlocks: bounded external provider calls
  example: web_search("ai news", timeout_ms=5000)
  backward compatible: YES - callers not passing this param are unaffected

## Phase 3 Runtime Implementation - web_fetch

Implemented `web_fetch` version `2.0.0` in `tools/web.py`.

Runtime changes:

- Preserved legacy text output for callers that pass no new controls.
- Added optional controls: `timeout_ms`, `follow_redirects`,
  `response_format`, and `trace_enabled`.
- Added structured success responses with requested URL, final URL, status
  code, title, readable text, truncation state, readable-text state,
  redirect-following state, and `meta`.
- Added typed structured errors for `EMPTY_URL`, `INVALID_URL`,
  `INVALID_MAX_CHARS`, `INVALID_TIMEOUT`, and `FETCH_FAILED`.
- Kept bounded retry behavior through the existing provider-attempt helper.

[NEW CAPABILITY] tool: web_fetch
  param: timeout_ms (optional, default: 10000)
  type: integer 1..60000
  what it unlocks: bounded external page fetches
  example: web_fetch("https://example.com", timeout_ms=5000, response_format="structured")
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: web_fetch
  param: follow_redirects (optional, default: true)
  type: boolean
  what it unlocks: caller can inspect non-redirect behavior
  example: web_fetch("https://example.com", follow_redirects=False, response_format="structured")
  backward compatible: YES - default true preserves existing behavior

## Phase 4 Runtime Diff Review - web_search

[UPGRADE DIFF] tool: web_search  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `provider`, `timeout_ms`, `response_format`, `trace_enabled`.
    Added outputs: structured search result, structured error, `meta`, trace object.
    Changed outputs (shape only, values same): none for legacy callers.
    Removed: NONE.

  Behavior changes:
    Legacy search result list -> unchanged legacy text format - reason: backward compatibility.
    Provider fallback status hidden in text only -> structured fallback/degraded metadata in structured mode - reason: prevent false negatives.
    Fixed provider choice -> optional provider selector - reason: caller can force or avoid providers when debugging.

  New error codes introduced: `EMPTY_QUERY`, `INVALID_PROVIDER`, `INVALID_RESULT_LIMIT`, `INVALID_TIMEOUT`.
  New trace fields introduced: `event`, `tool`, `version`, `call_id`, `started_at`, `inputs_received`, `schema_valid`, `execution_path`, `external_calls`, `duration_ms`, `output_fields`, `status`, `error_code`.
  New optional capabilities: `provider`, `timeout_ms`, `response_format`, `trace_enabled`.

  Backward compatible: YES

  User changes preserved: existing configured retry helper and Tavily-to-DDGS fallback behavior.

## Phase 4 Runtime Diff Review - web_fetch

[UPGRADE DIFF] tool: web_fetch  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `timeout_ms`, `follow_redirects`, `response_format`, `trace_enabled`.
    Added outputs: structured page result, structured error, `meta`, trace object.
    Changed outputs (shape only, values same): none for legacy callers.
    Removed: NONE.

  Behavior changes:
    Legacy URL/status/title/text output -> unchanged legacy text format - reason: backward compatibility.
    Fixed 15s timeout -> optional bounded `timeout_ms` with 10s default - reason: caller-controlled resilience.
    Always-follow redirects -> optional `follow_redirects` with true default - reason: default remains stable while debugging gains precision.

  New error codes introduced: `EMPTY_URL`, `INVALID_URL`, `INVALID_MAX_CHARS`, `INVALID_TIMEOUT`, `FETCH_FAILED`.
  New trace fields introduced: `event`, `tool`, `version`, `call_id`, `started_at`, `inputs_received`, `schema_valid`, `execution_path`, `external_calls`, `duration_ms`, `output_fields`, `status`, `error_code`.
  New optional capabilities: `timeout_ms`, `follow_redirects`, `response_format`, `trace_enabled`.

  Backward compatible: YES

  User changes preserved: existing HTML extraction and bounded provider-attempt retry behavior.

## Phase 5 Runtime Verification - web_search and web_fetch

[V-01] PASS - evidence: mocked legacy `web_search("query", max_results=1)` returned `1. Result` with `https://example.com`; mocked legacy `web_fetch("https://example.com/start", max_chars=1000)` returned URL, status 200, title, and readable text.

[V-02] PASS - evidence: `execute_tool("web_search", query="query", max_results=1, response_format="structured", trace_enabled=True)` returned provider `ddgs`, fallback_used `true`, result_count `1`; `execute_tool("web_fetch", url="https://example.com/start", follow_redirects=False, timeout_ms=5000, response_format="structured")` returned final URL, status 200, title, and `follow_redirects=false`.

[V-03] PASS - evidence: `web_search("", response_format="structured")` returned `EMPTY_QUERY`; `web_search("query", provider="bad", response_format="structured")` returned `INVALID_PROVIDER`; `web_fetch("example.com/nope", response_format="structured")` returned `INVALID_URL`.

[V-04] PASS - evidence: trace-enabled structured `web_search` emitted JSON trace with `status` `SUCCESS`, `tool` `web_search`, and `external_calls.count` 2.

[V-05] PASS - evidence: `timeout_ms` is validated to 1..60000 and forwarded to mocked `web_fetch` as 5.0 seconds; existing bounded provider failure tests still passed.

[V-06] PASS - evidence: existing provider retry tests still passed: Hacker News timeout attempts remained bounded, `web_fetch` timeout failure returned provider-attempt status, and web search fallback from Tavily to DDGS still worked.

[V-07] PASS - evidence: provider selector, timeout_ms, follow_redirects, response_format, and trace_enabled were exercised by targeted tests and direct V-checks.

[V-08] PASS - evidence: `python -m unittest tests.test_grounding -v` ran 49 tests OK; `python -m pytest -q` passed 54 tests; markdown verification pipeline returned `status=success` and `ship_decision=ship`.

[TEST] default markdown verification pipeline -> `tool_verification_pipeline('.', 'tools/TOOL_VERIFICATION_PIPELINE.md', timeout_ms=60000, response_format='structured')`

[RESULT] `status=success`; `ship_decision=ship`; 5 required checks passed: manifest audit, `python -m unittest tests.test_grounding`, `python -m pytest -q`, `python -m compileall tools agent memory voice gesture main.py config.py`, and `npm run typecheck`.

[TEST] `npm run build`

[RESULT] Next.js production build passed.

[TEST] manifest audit consistency -> `python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"`

[RESULT] `Status: success`; 11 observed tool modules matched 11 manifest modules; 21 callable schemas were listed; no files were changed by the audit.

[MANIFEST UPDATED] tool: web_search - new version: 2.0.0 - score: 9.5/10

[MANIFEST UPDATED] tool: web_fetch - new version: 2.0.0 - score: 9.5/10

## Test Fixture Maintenance - 2026-05-19T13:38Z

[FIX] Existing memory tests in the dirty worktree were adjusted to match current memory-index behavior:

- `tests/test_grounding.py` now seeds `_embeddings` on a `Brain.__new__` fixture and records temporary index existence before the temporary directory is removed.
- `tests/test_memory.py` now skips non-list memory metadata files such as `memory_index.json` during script-style daily-memory checks.

[VERDICT] These were test-fixture fixes only; memory runtime behavior was not changed in this pass.

## Upgrade Session Summary - 2026-05-19T13:38Z

[UPGRADE SESSION SUMMARY]
  Tools upgraded and verified: `registry_dispatch` v2.0.0, `terminal` v2.0.0, `file_write` v2.0.0, `tool_verification_pipeline` v1.0.0, `web_search` v2.0.0, `web_fetch` v2.0.0
  Tools upgraded but unverified: none
  Tools deferred: Tier 2 and Tier 3 tools remain next: `reddit`, `hackernews`, `youtube_play`, `playlist`, `imagine`, `gallery`, `file_read`, `file_list`, and note tools
  Anti-patterns detected and resolved: no feature creep, no breaking rename, no required additions; retries stayed disabled for side-effect tools
  User changes preserved: existing terminal shell behavior, file atomic-write behavior, registry structured dispatch, web provider fallback behavior, and current memory runtime changes
  Manifest updated: YES
  Overall tool fleet score: upgraded runtime surfaces average 9.5/10; full fleet still partial because remaining Tier 2 and Tier 3 runtime upgrades remain
  Recommended next session focus: upgrade `reddit` and `hackernews` structured provider status, timeout, and fallback reporting

## Hardcoded Tool Response Cleanup - 2026-05-19T14:40Z

[FIX] Markdown control surfaces now block hardcoded tool replies.

Changed surfaces:

- `persona.md` no longer gives fixed acknowledgement or provider-result
  sentences for KING to reuse.
- `tool_policy.md` now has a `Tool Response Composition` section requiring
  user-facing answers to be composed from returned tool fields.
- `tools/TOOL_MANIFEST.md` now includes the `tool_response_composition`
  contract.
- `tools/TOOL_EVIDENCE_LEDGER.md` now records that tool answers must come from
  observed fields, with structured fields preferred when available.
- `tools/TOOL_PROVIDER_FAILURE_PLAYBOOK.md` now uses field-based claim
  boundaries instead of example sentences.
- `tools/TOOL_INTAKE_CHECKLIST.md` now blocks canned tool replies during new
  tool intake.

[VERDICT] Markdown-only cleanup. Runtime code was not changed, preserving the
current tool implementations and the repo instruction to edit markdown for tool
behavior changes.

## Phase 3 Tier 2 Runtime Upgrade - reddit and hackernews

[UPGRADE DIFF] tool: reddit  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `timeout_ms`, `response_format`, `trace_enabled`, `include_source_status`
    Added outputs: structured `result`, `error`, `meta`, and `trace` envelopes when requested
    Changed outputs (shape only, values same): legacy output remains a string; structured mode is opt-in
    Removed: NONE

  Behavior changes:
    Fixed transport failures reporting as missing content -> provider failures now retry and surface `PROVIDER_ERROR` in structured mode - reason: avoid false negatives when Reddit or fallback search is blocked.
    Bare subreddit and `r/name` inputs normalize to the same subreddit path - reason: wider valid input acceptance without changing existing callers.

  New error codes introduced: `INVALID_LIMIT`, `INVALID_TIMEOUT`, `MISSING_QUERY`, `MISSING_SUBREDDIT`, `MISSING_POST_ID`, `MISSING_USERNAME`, `INVALID_ACTION`, `NOT_FOUND`, `POST_NOT_FOUND`, `USER_NOT_FOUND`, `RATE_LIMITED`, `PROVIDER_ERROR`, `NO_RESULTS`
  New trace fields introduced: call_id, started_at, inputs_received, schema_valid, execution_path, external_calls, duration_ms, output_fields, status, error_code
  New optional capabilities: `timeout_ms`, `response_format`, `trace_enabled`, `include_source_status`

  Backward compatible: YES

  User changes preserved: existing Reddit actions, cache, JSON paths, DDGS fallback, `id` alias, and legacy text formatting are preserved.

[UPGRADE DIFF] tool: hackernews  unversioned -> 2.0.0

  Schema changes:
    Added inputs (optional): `timeout_ms`, `response_format`, `trace_enabled`, `include_source_status`
    Added outputs: structured `result`, `error`, `meta`, and `trace` envelopes when requested
    Changed outputs (shape only, values same): legacy output remains a string; structured mode is opt-in
    Removed: NONE

  Behavior changes:
    Existing retry settings now carry caller timeout through Firebase and Algolia calls - reason: bounded external I/O with observable provider status.
    Search, listing, story, and user branches can return structured error envelopes - reason: callers can distinguish invalid input, no results, provider failure, and not found.

  New error codes introduced: `INVALID_LIMIT`, `INVALID_TIMEOUT`, `MISSING_STORY_ID`, `MISSING_USERNAME`, `MISSING_QUERY`, `INVALID_ACTION`, `INVALID_STORY_ID`, `STORY_NOT_FOUND`, `USER_NOT_FOUND`, `PROVIDER_ERROR`, `NO_RESULTS`
  New trace fields introduced: call_id, started_at, inputs_received, schema_valid, execution_path, external_calls, duration_ms, output_fields, status, error_code
  New optional capabilities: `timeout_ms`, `response_format`, `trace_enabled`, `include_source_status`

  Backward compatible: YES

  User changes preserved: existing endpoint map, cache, retry config knobs, action names, `id` alias, and legacy text formatting are preserved.

[NEW CAPABILITY] tool: reddit
  param: timeout_ms (optional, default: 15000)
  type: integer 1..60000
  what it unlocks: bounded Reddit and fallback search calls
  example: `reddit(action="hot", subreddit="python", timeout_ms=2500)`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: reddit
  param: response_format (optional, default: legacy)
  type: string enum `legacy|structured`
  what it unlocks: parseable result, error, meta, and trace envelopes
  example: `reddit(action="search", query="ai", response_format="structured")`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: reddit
  param: trace_enabled (optional, default: false)
  type: boolean
  what it unlocks: emitted machine-readable trace entries
  example: `reddit(action="front", trace_enabled=True)`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: reddit
  param: include_source_status (optional, default: false)
  type: boolean
  what it unlocks: provider/cache status fields in structured mode
  example: `reddit(action="search", query="ai", response_format="structured", include_source_status=True)`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: hackernews
  param: timeout_ms (optional, default: 15000)
  type: integer 1..60000
  what it unlocks: bounded Firebase and Algolia calls
  example: `hackernews(action="search", query="ai", timeout_ms=2500)`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: hackernews
  param: response_format (optional, default: legacy)
  type: string enum `legacy|structured`
  what it unlocks: parseable result, error, meta, and trace envelopes
  example: `hackernews(action="top", response_format="structured")`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: hackernews
  param: trace_enabled (optional, default: false)
  type: boolean
  what it unlocks: emitted machine-readable trace entries
  example: `hackernews(action="top", trace_enabled=True)`
  backward compatible: YES - callers not passing this param are unaffected

[NEW CAPABILITY] tool: hackernews
  param: include_source_status (optional, default: false)
  type: boolean
  what it unlocks: provider/cache status fields in structured mode
  example: `hackernews(action="search", query="ai", response_format="structured", include_source_status=True)`
  backward compatible: YES - callers not passing this param are unaffected

## Phase 5 Runtime Verification - reddit and hackernews

[V-01] PASS - evidence: deterministic legacy Reddit hot listing contained `Legacy Reddit Story` and the expected Reddit URL; deterministic legacy Hacker News search contained `Legacy HN Story` and `https://example.com/hn`.

[V-02] PASS - evidence: Reddit structured call accepted `subreddit="r/python"`, `limit="1"`, and `timeout_ms=2500`, normalized to `/r/python/hot`, returned count `1`, version `2.0.0`, and forwarded timeout `2.5`; Hacker News structured search accepted `limit="1"` and `timeout_ms=2500`, returned count `1`, version `2.0.0`, and forwarded timeout `2.5`.

[V-03] PASS - evidence: Reddit structured search without query returned `MISSING_QUERY`; Hacker News comments with non-numeric id returned `INVALID_STORY_ID`; Hacker News search without query returned `MISSING_QUERY`.

[V-04] PASS - evidence: trace-enabled Reddit error emitted JSON trace with `tool=reddit`, `status=FAILED`, `schema_valid=NO`, and `error_code=MISSING_QUERY`; trace-enabled Hacker News error emitted JSON trace with `tool=hackernews`, `status=FAILED`, `schema_valid=NO`, and `error_code=MISSING_QUERY`.

[V-05] PASS - evidence: timeout values were bounded and forwarded as seconds to mocked providers: Reddit `timeout_ms=1200` produced per-attempt timeout `1.2`; Hacker News `timeout_ms=1200` produced per-attempt timeout `1.2`.

[V-06] PASS - evidence: transient-failure simulations with `external_request_attempts=3` made three attempts for Reddit and Hacker News, then returned structured success with count `1` on attempt three.

[V-07] PASS - evidence: `timeout_ms`, `response_format`, `trace_enabled`, and `include_source_status` were exercised by targeted unit tests and direct V-checks for both tools.

[V-08] PASS - evidence: `python -m unittest tests.test_grounding` ran 56 tests OK; `python -m pytest -q` passed 72 tests and 21 subtests; manifest audit reported 11 observed modules, 11 manifest modules, and 21 registered callable schemas; `python -m compileall tools agent memory voice gesture main.py config.py`, `npm run typecheck`, `npm run build`, and the markdown verification pipeline all passed.

[TEST] default markdown verification pipeline -> `tool_verification_pipeline('.', 'tools/TOOL_VERIFICATION_PIPELINE.md', timeout_ms=60000, response_format='structured')`

[RESULT] `status=success`; `ship_decision=ship`; 5 required checks passed: manifest audit, `python -m unittest tests.test_grounding`, `python -m pytest -q`, `python -m compileall tools agent memory voice gesture main.py config.py`, and `npm run typecheck`.

[MANIFEST UPDATED] tool: reddit - new version: 2.0.0 - score: 9.5/10

[MANIFEST UPDATED] tool: hackernews - new version: 2.0.0 - score: 9.5/10

## Upgrade Session Summary - 2026-05-19T14:59Z

[UPGRADE SESSION SUMMARY]
  Tools upgraded and verified: `registry_dispatch` v2.0.0, `terminal` v2.0.0, `file_write` v2.0.0, `tool_verification_pipeline` v1.0.0, `web_search` v2.0.0, `web_fetch` v2.0.0, `reddit` v2.0.0, `hackernews` v2.0.0
  Tools upgraded but unverified: none
  Tools deferred: Tier 2 and Tier 3 tools remain next: `youtube_play`, `playlist`, `imagine`, `gallery`, `file_read`, `file_list`, and note tools
  Anti-patterns detected and resolved: no feature creep, no silent rewrite, no breaking rename, no required additions; Reddit transport false negatives now surface provider failure instead of missing-content claims
  User changes preserved: existing Reddit and Hacker News action names, aliases, cache behavior, retry config, fallback search, and legacy text outputs
  Manifest updated: YES
  Overall tool fleet score: upgraded runtime surfaces average 9.5/10; full fleet still partial because remaining Tier 2 and Tier 3 runtime upgrades remain
  Recommended next session focus: upgrade `youtube_play` and `playlist` with structured playback/download evidence and false-claim prevention

## Browser Automation Runtime - 2026-05-21T00:00+05:30

[NEW TOOL] `browser_extract` version `1.0.0`

Implemented a first browser automation surface in `tools/browser.py`.

Runtime behavior:

- Loads a named target from `tools/BROWSER_TARGETS.md` or a direct `url`.
- Prefers Playwright browser loading in `engine=auto`; uses HTTP fallback only
  when Playwright is unavailable.
- Supports explicit `engine=playwright` and `engine=httpx` for verification and
  debugging.
- Reuses saved Playwright storage state when `storage_state` is configured in
  markdown or passed to the tool.
- Extracts configured fields from selector values, meta text, visible text,
  title, or URL without adding phrase routing or hardcoded social-profile logic.
- Preserves legacy string output by default and exposes structured result,
  error, meta, and trace envelopes when requested.
- Uses env/config knobs for target file, timeout, text size, and user agent:
  `KING_BROWSER_TARGETS_FILE`, `KING_BROWSER_DEFAULT_TIMEOUT_MS`,
  `KING_BROWSER_MAX_TEXT_CHARS`, and `KING_BROWSER_USER_AGENT`.

[NEW CONTROL FILE] `tools/BROWSER_TARGETS.md`

- Target sections define URLs and field extraction rules.
- Field labels and selectors live in markdown so social-page metrics such as
  follower counts are configured data, not Python routing rules.
- Example `instagram_profile` is a placeholder and must be edited to the real
  user URL before use.

[NEW CAPABILITY] tool: browser_extract
  param: target
  type: string optional
  what it unlocks: repeatable page loading from markdown target entries
  backward compatible: YES - no existing callers depended on this tool

[NEW CAPABILITY] tool: browser_extract
  param: engine
  type: string enum auto|playwright|httpx
  what it unlocks: real browser rendering by default with explicit fallback
  control for verification
  backward compatible: YES - new tool only

[NEW CAPABILITY] tool: browser_extract
  param: fields
  type: comma-separated string optional
  what it unlocks: caller can request a subset of configured fields without
  changing the markdown target
  backward compatible: YES - empty uses target fields

Verification evidence:

- `python -m unittest tests.test_grounding.BrowserAutomationToolTests -v` -> 3
  tests passed before auth-state support; after auth-state support, 5 tests
  passed.
- `python -m unittest tests.test_grounding -v` -> 80 tests passed.
- `python -m pytest -q` -> 98 tests and 23 subtests passed.
- `python -m py_compile tools\browser.py config.py` -> passed.
- `python -m compileall tools agent memory voice gesture main.py config.py` ->
  passed.
- `python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"` ->
  12 observed modules, 12 manifest modules, 23 callable schemas, no manifest
  drift.
- `python -c "from tools.browser import browser_extract; result = browser_extract(url='https://example.com', engine='httpx', response_format='structured', timeout_ms=10000); ..."` ->
  `browser_extract`, `httpx`, `https://example.com`, title `Example Domain`,
  source status `ok`.
- `python -c "from tools.browser import browser_extract; result = browser_extract(url='https://example.com', engine='playwright', response_format='structured', timeout_ms=15000); ..."` ->
  Playwright loaded `https://example.com/`, status 200, title `Example Domain`,
  engine `playwright`, degraded `false`.
- `tool_verification_pipeline('.', 'tools/TOOL_VERIFICATION_PIPELINE.md', timeout_ms=60000, response_format='structured')` ->
  `status=success`, `ship_decision=ship`, 5 required checks passed.
- `npm run typecheck` -> passed.
- `npm run build` -> passed.

[MANIFEST UPDATED] tool: browser_extract - new version: 1.1.0 - score: 9.2/10

[VERDICT] Shipped as verified runtime for configured public-page extraction.
Account-private or login-gated social details remain dependent on the attempted
URL, browser session state, and returned page fields; KING must report those
states from the tool result instead of claiming broad access.

## Browser Login Session Runtime - 2026-05-21T00:00+05:30

[NEW TOOL] `browser_login_session` version `1.0.0`

Implemented a visible manual-login helper in `tools/browser.py`.

Runtime behavior:

- Opens a non-headless Playwright Chromium window on a target `login_url`,
  target `url`, or direct `url`.
- Waits for the configured timeout while the user logs in manually.
- Saves Playwright storage state to `storage/browser_auth/<session>.json` by
  default, or to the target's configured `storage_state`.
- Does not ask for, return, log, or inspect credentials.
- Returns final URL, page title, saved-state path, saved-state existence, and a
  credential policy field.

[NEW CAPABILITY] tool: browser_login_session
  param: target
  type: string optional
  what it unlocks: visible login for a configured markdown target
  backward compatible: YES - new optional tool surface

[NEW CAPABILITY] tool: browser_login_session
  param: storage_state
  type: string optional
  what it unlocks: explicit saved-session file for later browser extraction
  backward compatible: YES - default path comes from config or markdown

Verification evidence:

- `python -m unittest tests.test_grounding.BrowserAutomationToolTests -v` -> 5
  tests passed, including storage-state reuse and registered login-session
  schema.

[MANIFEST UPDATED] tool: browser_login_session - new version: 1.0.0 - score: 9.0/10

[VERDICT] Login-session support is shipped as verified runtime for schema,
storage-state path handling, and extraction reuse. Live login remains
user-driven and must be reported from the saved-state result of the actual
browser session.

## Browser Login Session Autosave - 2026-05-22T18:11+05:30

[FIX] `browser_login_session` now saves Playwright storage state repeatedly
during the visible login window and reports errors under its own tool name.

Reason:

- The first visible Instagram login attempt ended with `TargetClosedError`
  because the previous helper saved only once, after the full wait completed.
- The returned structured error incorrectly used `browser_extract` metadata
  because shared browser error formatting was hardwired to the extraction tool.

Runtime changes:

- Added a login-specific structured error helper so login failures return
  `meta.tool=browser_login_session`.
- During manual login, the helper now saves storage state every bounded interval
  until timeout or window close.
- If the user closes the browser after at least one save, the saved state can
  still be reused.
- The result includes `storage_state_saves`, `closed_by_user`,
  `storage_state_exists`, and the saved path.

Verification evidence:

- `python -m py_compile tools\browser.py config.py` -> passed.
- `python -m unittest tests.test_grounding.BrowserAutomationToolTests -v` -> 6
  tests passed.
- Manifest audit -> 12 observed modules, 12 manifest modules, 23 callable
  schemas, no manifest drift.
- Live Instagram login session saved
  `storage\browser_auth\instagram_profile.json`, `storage_state_exists=true`,
  `storage_state_saves=37`, `credentials_captured=false`, final URL
  `https://www.instagram.com/accounts/onetap/`.
- Authenticated extraction against `https://www.instagram.com/` used the saved
  state with `storage_state_used=true`; fields were empty because the configured
  profile URL is still the placeholder and the root page did not expose
  profile metrics.

[VERDICT] Session save and reuse are verified. Profile metric extraction now
depends on replacing the placeholder profile URL in `tools/BROWSER_TARGETS.md`
with the real Instagram profile URL or adding more precise selectors for the
logged-in profile page.

## Navigator Tool - 2026-05-23T00:00+05:30

[NEW TOOL] `navigator`

Purpose:

- Resolve origin and destination places through open geocoding.
- Return road-route distance and estimated travel time from an open routing
  provider.
- Return straight-line distance as an explicit fallback when road routing is
  unavailable.
- Drive a separate frontend navigator page from structured tool output.

Runtime behavior:

- Uses configured endpoints from `KING_NAVIGATOR_GEOCODE_URL` and
  `KING_NAVIGATOR_ROUTE_URL`.
- Uses configured `KING_NAVIGATOR_USER_AGENT`,
  `KING_NAVIGATOR_DEFAULT_MODE`, and `KING_NAVIGATOR_DEFAULT_TIMEOUT_MS`.
- Preserves legacy text output by default and supports structured output,
  typed errors, bounded timeout, retry attempts, and trace emission.
- Reports `degraded=true` when routing falls back to straight-line distance.

Verification evidence:

- `python -m unittest tests.test_grounding.NavigatorToolTests -v` -> 4 tests
  passed for schema registration, provider-backed route fields, fallback
  reporting, and API panel payload shape.
- `tool_manifest_audit` -> 13 observed modules, 13 manifest modules, 24
  callable schemas, no manifest drift.

[MANIFEST UPDATED] tool: navigator - new version: 1.0.0 - score: 9.1/10

[VERDICT] Navigator is shipped as verified runtime for route distance, direct
distance fallback, structured evidence, and frontend payload support. Live
answers remain scoped to the attempted origin, destination, mode, and provider
status returned by the tool.

## Navigator Route Map Upgrade - 2026-05-23T00:00+05:30

[FIX] `navigator` now separates precise place routes from broad region routes
and returns sampled route-through places for the frontend map.

Runtime changes:

- Geocoding results include provider precision metadata such as category, type,
  place rank, bounding box, and representative-point status.
- Routes involving states, regions, districts, or administrative boundaries
  return a precision note instead of presenting one point-to-point route as a
  global exact answer for the whole region.
- OSRM polyline geometry is sampled and reverse-geocoded through Nominatim so
  the frontend can show route-through places such as cities or districts.
- The chat frontend now opens Navigator with origin, destination, mode, and
  autorun query parameters; if a pop-up is blocked, it falls back to the same
  tab.
- The Navigator frontend now uses the main JARVIS color family and draws route
  labels from returned route-place fields.

Verification evidence:

- `python -m unittest tests.test_grounding.NavigatorToolTests -v` -> 6 tests
  passed, including representative-point reporting and route-place labels.
- Live provider check for Haryana to New Delhi returned `152.0 km`, `2 hr 2
  min`, precision note for representative coordinates, and route places
  `Bhiwani`, `Rohtak`, `Bahadurgarh`, `Delhi`.
- Browser-visible Navigator page at
  `/frontend/navigator.html?origin=Haryana&destination=New%20Delhi&mode=driving&autorun=1`
  showed the same distance, precision note, and route-place chips.

[VERDICT] Navigator route visualization and false-precision handling are
verified for the tested open-provider path. Region-to-region routes remain
scoped to representative coordinates unless the user gives exact cities,
addresses, or landmarks.
