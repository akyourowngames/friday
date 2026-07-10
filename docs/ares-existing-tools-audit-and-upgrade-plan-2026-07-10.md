# Ares Existing Tools Audit and Upgrade Plan

**Date:** 2026-07-10  
**Scope:** Existing Ares tools only  
**Product constraint:** Fix bugs and upgrade current behavior. Do not add new public tools or net-new product features. Preserve existing permissions and safety behavior.

## Straight answer

- Ares currently exposes **68 public tools**.
- This audit identifies **29 prioritized upgrade tracks** across those existing tools.
- It records **18 confirmed or directly code-backed defects**.
- **223 focused tests passed and 2 were skipped** in 73.44 seconds.
- The complete test suite did not finish inside the 120-second audit window, so this document does **not** claim that the entire repository is green.
- The most serious problems are false success/rollback reporting, lost command output, non-persistent Windows command state, incorrect MCP success reporting, incomplete web evidence, memory-search false negatives, and cron/config state races.

This is an implementation-ready plan. No production code was changed as part of this audit.

## What “closer to Iron Man JARVIS” means here

Ares does not need more tool names. It needs its existing tools to behave like one dependable system:

1. **Accurate perception** — web, files, phone, and tools return complete and correctly labeled evidence.
2. **Durable memory** — known user facts are found reliably and ranked according to confidence.
3. **Trustworthy action** — a tool never reports success when nothing happened or rollback when state was not restored.
4. **Explicit failure** — retries, timeouts, partial work, and external-tool errors remain visible.
5. **Self-recovery** — command sessions, scheduled jobs, and external integrations recover from crashes and stale state.
6. **Temporal awareness** — timezones and schedules remain exact.
7. **Long-running continuity** — terminal state, task state, and intermediate artifacts survive across multi-step work.

## Audit method

The findings came from:

- inventorying all schemas in `ares/tools/definitions.py`;
- tracing all 68 tools through `ares/tools/executor.py` into their implementations;
- inspecting current tests and the older upgrade workbook/implementation notes;
- reviewing error, timeout, cancellation, rollback, concurrency, and partial-I/O paths;
- running focused regression tests;
- writing targeted temporary repros for gaps not covered by the existing suite.

No finite audit proves that no other bugs exist. Findings are therefore labeled either **reproduced** or **code-backed** instead of claiming unsupported certainty.

## Confirmed defects and evidence

| ID | Severity | Tool/subsystem | Validation | Expected | Actual result | Evidence |
|---|---|---|---|---|---|---|
| BUG-001 | Critical | `batch_edit` | Reproduced | An edit with no matching text fails the transaction. | The batch reports completion while the file remains unchanged. | `ares/tools/filesystem_write.py:272` |
| BUG-002 | Critical | `batch_edit` | Reproduced | A failed batch after moving a directory restores the original directory. | The source remains missing and the destination remains present even though rollback is reported. | `ares/tools/filesystem_write.py:288` |
| BUG-003 | High | `web_search` | Reproduced | A provider publication date survives normalization. | The date is discarded and the source becomes `undated`. | `ares/tools/web.py:33` |
| BUG-004 | High | `web_search` | Reproduced | Only a real trusted hostname receives a primary/technical quality label. | `github.com.evil.example` is promoted to `primary-or-technical`. | `ares/tools/web.py:157` |
| BUG-005 | High | `get_current_datetime` | Reproduced | An invalid requested timezone returns an error. | `Mars/Olympus` silently returns a valid-looking UTC answer. | `ares/tools/datetime_tool.py:10` |
| BUG-006 | High | date parsing | Reproduced | A requested `America/New_York` result remains in that timezone. | The result is converted to the computer timezone (`+05:30` in the repro). | `ares/tools/dates.py:40` |
| BUG-007 | Medium | `crop_image` | Reproduced | A rectangle outside the image is rejected before saving. | It reaches Pillow as a zero-area image and returns `cannot write empty image`. | `ares/tools/image_edit.py:223` |
| BUG-008 | Critical | existing MCP tools | Reproduced | `CallToolResult.isError=true` is surfaced as an error. | The server failure text is returned as ordinary successful content. | `ares/tools/mcp_client.py:632` |
| BUG-009 | Critical | `run_code` | Reproduced | Output printed by a child process is returned to Ares. | A successful child `print(12345)` returns `Executed successfully (no output)`. | `ares/tools/repl.py:40`, `ares/tools/repl.py:180` |
| BUG-010 | Critical | `run_command` | Reproduced on Windows | Environment and working-directory state persist between calls. | A variable set in call one is missing in call two; the second call prints the literal `%ARES_PERSIST_CHECK%`. | `ares/tools/repl.py:128` |
| BUG-011 | High | `search_memory` | Reproduced | User text containing an unmatched quote can still find a literal memory. | With vector search disabled, an unbalanced quote causes FTS failure and zero results. | `ares/memory.py:276` |
| BUG-012 | Medium | memory bulk delete | Reproduced | Deleted count equals the number of database rows removed. | One existing ID plus one missing ID reports that two rows were deleted. | `ares/memory.py:396` |
| BUG-013 | Medium | `export_data` | Reproduced | Profile `MEMORIES` is labeled `memories`. | Data selection is case-insensitive, but the payload labels the export `full`. | `ares/tools/exporter.py:31` |
| BUG-014 | High | `fetch_url` | Code-backed | The 2 MB cap limits the network read. | `client.get` downloads the complete body before the response is sliced. | `ares/tools/web.py:517` |
| BUG-015 | High | MCP health | Code-backed | A failed health probe makes a server degraded or not ready. | The session remains registered, so readiness can remain `ready`. | `ares/tools/mcp_client.py:511` |
| BUG-016 | High | cron store | Code-backed | Concurrent updates are serialized. | Unlocked read-modify-write operations can overwrite one another. | `ares/cron/store.py:26` |
| BUG-017 | High | `phone_status` | Code-backed | Phone capabilities require current reachability. | Paired-but-unreachable KDE Connect can still report contacts/SMS as capable. | `ares/tools/kdeconnect_bridge.py:72`, `ares/tools/adb_bridge.py:121` |
| BUG-018 | Medium | `get_file_info` | Code-backed | A symlink is identified as a symlink. | The path is resolved before `is_symlink`, so it normally appears as the target type. | `ares/tools/filesystem.py:56`, `ares/tools/filesystem.py:522` |

## P0 — Fix trust and correctness first

### ARES-001 — `batch_edit`

**Weakness / bug**

- A no-match `edit` result is treated as success because error text is returned as an ordinary string.
- Directory snapshots only remember that a directory existed, not its contents.
- A moved or copied directory cannot be fully restored after a later operation fails.
- Backup files and index records created during a failed batch are not part of the rollback transaction.

**Upgrade / fix**

- Introduce an internal result object with `ok`, `changed`, `error`, and affected paths.
- Fail the transaction whenever an operation returns `ok=false`.
- Snapshot source and destination directory trees before mutation.
- Restore new, moved, overwritten, and deleted directories byte-for-byte.
- Include backup files and the backup index in the same transaction.

**Why important**

This is the foundation of autonomous multi-file work. False success or incomplete rollback can corrupt a repository while convincing the agent that everything is safe.

**JARVIS contribution**

Ares gains a dependable plan-act-verify-recover loop for complex workspace changes.

**Definition of done**

- A no-match edit fails the batch.
- A failure injected after a directory move restores both source and destination exactly.
- A failure injected after an overwrite restores the original bytes and metadata.
- No orphan files, directories, backup files, or index rows remain.

### ARES-008 — `web_search`

**Weakness / bug**

- Publication/update dates are discarded before freshness scoring.
- Host quality uses substring matching, which promotes lookalike domains.
- Local fetch errors in the synchronous path can disappear from the error list.
- Top-result pages are fetched sequentially, multiplying latency.

**Upgrade / fix**

- Preserve provider, publication date, update date, and result metadata.
- Match exact trusted domains and valid subdomains only.
- Record one explicit fetch outcome for every attempted result.
- Fetch the bounded top-result set concurrently while preserving result order.

**Why important**

Research labels currently look more certain than the retained evidence justifies.

**JARVIS contribution**

Ares becomes a current, source-aware, transparent research assistant instead of a link collector.

**Definition of done**

- Dated provider fixtures remain dated.
- `github.com.evil.example` is not promoted.
- Every failed page fetch appears in `errors`.
- Concurrency reduces latency without changing ordering or result caps.

### ARES-009 — `fetch_url`

**Weakness / bug**

- The complete response body is downloaded before the byte limit is applied.
- Character slicing is mixed with a byte constant.
- Some capped HTML/text responses are not marked truncated.
- On errors, the public wrapper discards status, final URL, and retryability.

**Upgrade / fix**

- Stream response chunks and stop after the configured byte cap.
- Track byte truncation separately from output-character truncation.
- Set `truncated=true` for every capped path.
- Preserve status, redirected URL, retryability, and content type on failure.

**Why important**

The current cap does not protect memory, and missing failure metadata prevents intelligent retries.

**JARVIS contribution**

Ares receives bounded web perception plus enough error context to recover automatically.

**Definition of done**

- A 10 MB local HTTP fixture reads at most the cap plus one chunk.
- The response reports truncation correctly.
- Redirect, 429, and 5xx fixtures retain status, final URL, and retryability.

### ARES-010 — `search_memory`

**Weakness / bug**

- Raw user text is sent directly to FTS syntax.
- Invalid quotes/operators are swallowed and can return no result.
- Session filtering occurs after a small global vector top-N query, so valid scoped memories may never become candidates.

**Upgrade / fix**

- Try the structured FTS query first, then a deterministic escaped/literal query.
- Apply session scope during candidate selection or continue retrieving until the requested scoped limit is filled.
- Record internally whether vector, FTS, fallback, or degraded retrieval produced the answer.

**Why important**

False-negative memory makes Ares contradict known facts and ask the user the same questions again.

**JARVIS contribution**

Ares gains reliable personal context at the moment it is needed.

**Definition of done**

- The query `"favorite` finds the matching stored memory when vector search is disabled.
- Scoped search returns the requested number of qualifying rows when they exist.
- Literal punctuation, quotes, hyphens, and FTS operators have regression coverage.

### ARES-012 — `export_data`

**Weakness / bug**

- Case variants select the right data but can write the wrong profile label.
- Only `api_key` and `tavily_api_key` are excluded.
- Arbitrary MCP dictionaries may contain nested tokens, OAuth secrets, or secret environment values.
- Timestamp-only default names can collide when two exports start in the same second.
- The JSON file is written directly rather than by temp-file replacement.

**Upgrade / fix**

- Normalize the profile once and reuse it for selection and labeling.
- Recursively redact credential-like keys while preserving config structure.
- Use a collision-safe suffix.
- Write, flush, fsync, validate, and atomically replace.

**Why important**

Incorrect labels damage restore confidence, and nested credentials can leak through a supposedly redacted backup.

**JARVIS contribution**

Ares becomes portable and recoverable across machines without losing the integrity of its operating context.

**Definition of done**

- All case variants produce the same label and content.
- Nested token/secret fixtures are redacted.
- Two exports in one second create distinct valid files.
- An injected write failure leaves no partial export.

### ARES-013 — `run_code`

**Weakness / bug**

- Python `StringIO` captures ordinary `print` calls but not child processes or file-descriptor-level writes.
- Those writes enter the control stream before the JSON response.
- The result reader collects that prefix and then discards it after receiving the JSON payload.

**Upgrade / fix**

- Separate the control protocol from OS stdout/stderr.
- Capture child-process and `sys.stdout.buffer` output.
- Bound large output without losing the end status.
- Restart the worker cleanly after timeout or protocol corruption.
- Preserve the existing pinned Python namespace.

**Why important**

Missing output creates false success and makes test/build verification impossible.

**JARVIS contribution**

Ares can execute, observe the real result, reason about it, and continue reliably.

**Definition of done**

- Ordinary print, child-process print, binary-buffer output, Unicode, large output, exception, and timeout fixtures all return correct results.
- Namespace persistence still works.
- Timeout recovery does not leak processes.

### ARES-014 — `run_command`

**Weakness / bug**

- Windows starts a `cmd.exe` session but does not send commands to it.
- Each command uses a separate `subprocess.run`, so environment variables and `cd` do not persist.
- The unused `cmd.exe` remains alive.
- A timeout kills the immediate process but may leave child processes running.
- Text decoding relies on ambient locale behavior.

**Upgrade / fix**

- Use one real long-lived PowerShell or cmd protocol on Windows.
- Preserve working directory and environment across calls.
- Capture Unicode deterministically.
- Terminate the complete process tree on timeout and reset.

**Why important**

Multi-step builds and diagnostics are nondeterministic when terminal state disappears between calls.

**JARVIS contribution**

Ares gains dependable workstation control instead of isolated one-shot commands.

**Definition of done**

- Set-then-read environment persists.
- Change-directory then query-directory persists.
- Reset clears the state.
- Timeout removes descendants.
- Unicode output and exit codes round-trip correctly.

### ARES-015 — `terminal_exec`

**Weakness / bug**

- It shares the broken Windows runtime with `run_command`.
- Visual terminal delivery is best-effort and invisible in the result.
- The user can therefore see a terminal state that differs from the captured execution state.

**Upgrade / fix**

- Move it onto the corrected persistent shell implementation.
- Keep execution semantics identical to `run_command`.
- Include a non-fatal display-delivery status.

**Why important**

Supervised work requires the visible command and captured result to agree.

**JARVIS contribution**

Ares can operate transparently while the user watches the same action.

**Definition of done**

- The same command returns the same execution result through both tools.
- Display success/failure is observable.
- Display callback failure never changes command success.

### ARES-016 — existing MCP tools (`mcp__*`)

**Weakness / bug**

- `CallToolResult.isError` is ignored.
- Health-probe failures can leave a session marked ready.
- A cancelled connection can return without a session or explicit server error.
- Servers connect sequentially, so one slow server delays all healthy servers.

**Upgrade / fix**

- Propagate the MCP error flag into the tool result.
- Mark failed probes degraded or disconnected.
- Treat cancelled initialization as an explicit failed connection.
- Connect and probe independent servers concurrently with per-server timeouts.

**Why important**

External actions are not usable if Ares cannot distinguish an integration failure from success.

**JARVIS contribution**

Ares can use a large ecosystem of tools without one unhealthy service corrupting the control loop.

**Definition of done**

- `isError=true` always returns an error state.
- Probe timeout changes readiness.
- Cancellation is recorded.
- A slow server does not delay healthy servers from becoming usable.

### ARES-021 — `create_cron_job` and `update_cron_job`

**Weakness / bug**

- Job storage is an unlocked JSON read-modify-write sequence.
- The scheduler, manual execution, CLI, or UI can overwrite another update.
- Atomic file replacement prevents partial JSON but does not prevent lost updates.

**Upgrade / fix**

- Add a cross-process file lock.
- Add a revision-aware mutation operation.
- Validate the complete updated job before commit.
- Retain atomic replacement for the final write.

**Why important**

Schedules and run state cannot silently disappear in a proactive assistant.

**JARVIS contribution**

Ares gains durable recurring plans and reliable background state.

**Definition of done**

- Parallel create/update/run-count stress tests preserve every committed mutation.
- The store always remains valid JSON.
- A stale revision retries or fails explicitly.

### ARES-022 — `run_cron_job_now`

**Weakness / bug**

- In an active event loop the tool creates an untracked task and immediately reports success.
- Invalid job IDs and setup failures can become unobserved task exceptions.
- Manual and scheduled runs can execute the same job concurrently.
- A failure after setting `state=running` can leave a stuck job.

**Upgrade / fix**

- Register every accepted run task.
- Atomically transition from scheduled to running.
- Reject or coalesce duplicate runs.
- Persist a terminal completed/failed state and log path even when initialization or cleanup fails.

**Why important**

Autonomous work needs one visible lifecycle, not ambiguous fire-and-forget execution.

**JARVIS contribution**

Ares becomes proactively useful while remaining observable and recoverable.

**Definition of done**

- Invalid ID fails synchronously.
- Duplicate trigger returns already-running.
- Every accepted run produces exactly one terminal state, log, and run-count increment.

### ARES-027 — `update_config`

**Weakness / bug**

- Empty paths, unknown fields, and wrong value types are written without validating the resulting `AppConfig`.
- A later load can reject the file and fall back to defaults.
- One bad surgical update can therefore appear to reset unrelated configuration.

**Upgrade / fix**

- Apply the patch to an in-memory copy.
- Validate the full `AppConfig`.
- Atomically commit only the valid result.
- Preserve the original file byte-for-byte on failure.
- Return field-level validation errors.

**Why important**

Configuration is the control plane for models, tools, phone integration, and automation.

**JARVIS contribution**

Ares can reconfigure itself without destabilizing the rest of its operating state.

**Definition of done**

- Empty path, unknown field, and wrong type do not modify the file.
- A valid nested update preserves every sibling field.
- The saved result reloads to an equivalent `AppConfig`.

## P1 — Improve precision, media integrity, phone awareness, and time

### ARES-002 — `edit_file`

- **Weakness:** Whitespace-normalized edits convert CRLF to LF. Backup index entries are written before the backup file exists.
- **Upgrade:** Preserve newline/trailing-newline state in every edit path. Copy and fsync the backup before atomically appending its index entry. Return explicit `changed` status.
- **Why important:** Prevents invisible formatting churn and invalid restore points.
- **JARVIS contribution:** Precise code editing without collateral changes.
- **Done when:** CRLF/LF fixtures remain stable and forced backup failure creates no index record.

### ARES-003 — `search_files`

- **Weakness:** In an active event loop the sync wrapper starts a thread and immediately joins it, blocking streaming. Results are deduplicated by file, hiding multiple line hits.
- **Upgrade:** Await `search_files_async` from async execution. Keep multiple ranked line hits within caps. Report matched files and matched lines separately.
- **Why important:** Blocking and dropped evidence increase diagnosis latency and false negatives.
- **JARVIS contribution:** Faster, more complete codebase perception.
- **Done when:** A heartbeat continues during a large search and multiple hits from one file remain visible.

### ARES-011 — `update_memory`

- **Weakness:** Ranking uses `confidence or 1.0`, so confidence `0.0` receives the same boost as `1.0`.
- **Upgrade:** Distinguish `None` from zero, preserve zero through storage/update/reload, and add deterministic ranking tests.
- **Why important:** Explicitly untrusted facts must not outrank verified facts.
- **JARVIS contribution:** Better judgment, not just retrieval.
- **Done when:** Otherwise-identical confidence-zero and confidence-one memories rank correctly.

### ARES-017 — `generate_image`

- **Weakness:** Filename identity hashes only the prompt. Different seed/model/size overwrites the previous result. Content-Type is trusted without decoding, and manifest failure makes a saved image look like failed generation.
- **Upgrade:** Key identity on all inputs or content checksum, validate decoded image bytes, save atomically, and report manifest failure as a warning while retaining the valid asset path.
- **Why important:** Prevents silent asset loss and retry loops.
- **JARVIS contribution:** Reproducible visual asset workflows.
- **Done when:** Different generation inputs produce distinct paths, corrupt bytes fail validation, and manifest failure does not hide the image.

### ARES-018 — `resize_image`

- **Weakness:** Zero/negative sizes fail late, overwrite is non-atomic, and animation collapses to one frame.
- **Upgrade:** Validate positive computed dimensions, preserve supported animation frames/timing, and save through a verified temp file plus atomic replace.
- **Why important:** A resize must not corrupt the source or silently destroy animation.
- **JARVIS contribution:** Dependable autonomous asset preparation.
- **Done when:** Invalid dimensions write nothing, failed save preserves the source checksum, and animated frame count/durations remain intact.

### ARES-019 — `convert_image`

- **Weakness:** Animated input becomes one frame, quality is not bounded to its documented range, extension can disagree with format, and overwrite is non-atomic.
- **Upgrade:** Preserve animation for supported targets, validate quality, validate/reconcile the extension, and atomically replace.
- **Why important:** Frame loss and ambiguous file types break previews and uploads.
- **JARVIS contribution:** Reliable multimodal workflow continuity.
- **Done when:** Animation fixtures retain frames, invalid quality is rejected, file signature matches the reported format, and failed overwrite preserves the original.

### ARES-020 — `crop_image`

- **Weakness:** Bounds are checked before clamping, allowing a zero-area crop to reach Pillow. Overwrite is non-atomic.
- **Upgrade:** Clamp first, reject an empty intersection with a domain-specific error, and save via temp-file replacement.
- **Why important:** Spatial operations need predictable coordinates and must protect the source.
- **JARVIS contribution:** Accurate visual manipulation with clear self-correction.
- **Done when:** Outside, edge, negative, and valid coordinate fixtures behave deterministically and no zero-area save is attempted.

### ARES-023 — `get_cron_job`

- **Weakness:** A crash can leave `state=running` forever. Missed-run simulation stops at 100 without stating that the result is capped.
- **Upgrade:** Use a running lease/heartbeat recoverable at startup. Add a `truncated`/lower-bound indication to schedule simulation.
- **Why important:** Stuck jobs and hidden backlog make automation appear healthy when it is not.
- **JARVIS contribution:** Self-recovering proactive execution.
- **Done when:** Expired leases recover deterministically and more than 100 missed runs are labeled as capped.

### ARES-024 — `phone_status`

- **Weakness:** Top-level status requires both KDE and ADB even when one capability family works. KDE can be `ok` while unreachable. Contacts/SMS ignore reachability. Auto-discovered device ID is cached indefinitely.
- **Upgrade:** Report capability-level live health, distinguish any-ready and fully-ready, require reachability for KDE actions, and invalidate discovery after config changes or failed actions.
- **Why important:** A false status makes Ares avoid working actions or attempt unavailable ones.
- **JARVIS contribution:** Accurate awareness of what the connected phone can do right now.
- **Done when:** KDE-only, ADB-only, unreachable, re-paired, and multi-device fixtures route correctly.

### ARES-025 — `phone_get_notifications` and `phone_search_contact`

- **Weakness:** Fixed one-line separators lose multiline or version-specific output. Limits are not consistently bounded. Timeout/Unicode decode failures can escape as exceptions.
- **Upgrade:** Add fixture-driven version parsers, clamp result limits, use explicit encoding error handling, and convert subprocess failures into the existing JSON error shape.
- **Why important:** Device context must be correctly attributed before Ares summarizes or acts.
- **JARVIS contribution:** Reliable ambient awareness of notifications and people.
- **Done when:** Unicode/multiline fixtures parse correctly and timeout/missing-device cases return bounded `ok=false` JSON.

### ARES-026 — `get_current_datetime`

- **Weakness:** Invalid timezones silently become UTC. Related parsing converts a requested zone into the machine zone.
- **Upgrade:** Return an explicit invalid-timezone error and preserve the requested timezone consistently.
- **Why important:** Time errors cascade into reminders, cron jobs, plans, and trust.
- **JARVIS contribution:** Precise temporal awareness.
- **Done when:** Invalid zones never masquerade as UTC and valid IANA zones round-trip exactly.

### ARES-029 — `copy_file`

- **Weakness:** Copy writes directly to the destination, so failure can leave a partial file. Success does not verify the copied bytes.
- **Upgrade:** Copy to a same-filesystem temp path, flush/fsync, preserve metadata, verify size/bytes, and atomically replace.
- **Why important:** Partial files can poison later edits, exports, or duplicate analysis.
- **JARVIS contribution:** Trustworthy workspace manipulation during unattended tasks.
- **Done when:** Injected mid-copy failure keeps the old destination intact and successful copies match the source.

## P2 — Remove scale and telemetry problems

### ARES-004 — `read_file`

- **Weakness:** Reads the complete file into memory before returning a small range.
- **Upgrade:** Stream the requested window, bound context scanning, and calculate totals without retaining all lines.
- **Why important:** Large logs or generated files can freeze local inspection.
- **JARVIS contribution:** Fast observation of large workspaces.
- **Done when:** A very large fixture returns 200 requested lines inside a fixed memory budget with correct line numbers and counts.

### ARES-005 — `get_file_info`

- **Weakness:** Resolution follows links before symlink classification.
- **Upgrade:** Use the lexical path plus `lstat`, then report target information separately.
- **Why important:** Linked worktrees/assets can be misidentified and edited incorrectly.
- **JARVIS contribution:** Better environmental awareness.
- **Done when:** File, directory, valid link, and broken link report correct identities on supported platforms.

### ARES-006 — `disk_usage`

- **Weakness:** Every displayed directory recursively rescans its subtree, approaching O(files × depth). File input can produce a misleading zero total.
- **Upgrade:** Walk once, aggregate sizes bottom-up, share ignore behavior, and handle file input explicitly.
- **Why important:** Routine storage diagnosis becomes slow on large repositories.
- **JARVIS contribution:** Fast workspace-size awareness.
- **Done when:** Instrumentation proves each file is stat'ed once and totals match a reference walk.

### ARES-007 — `find_duplicates`

- **Weakness:** Every same-size candidate is fully MD5-hashed, and ignore behavior differs from `search_files`.
- **Upgrade:** Compare size, partial samples, then full SHA-256 only for surviving candidates; share ignore handling.
- **Why important:** Avoids excessive disk reads while keeping exact duplicate results.
- **JARVIS contribution:** Efficient storage housekeeping.
- **Done when:** Results match the current reference while measured bytes read fall materially on non-duplicate datasets.

### ARES-028 — `write_file`

- **Weakness:** `len(content)` is labeled bytes even though it is a character count. Confirmation semantics live in the executor instead of one shared implementation path.
- **Upgrade:** Report encoded byte length and centralize overwrite/result behavior.
- **Why important:** CLI, agent, and direct calls should not diverge.
- **JARVIS contribution:** More precise and consistent file execution.
- **Done when:** UTF-8 fixtures report actual bytes and every entry point has the same overwrite contract.

## Full inventory of the 68 existing public tools

### Memory — 4

1. `store_memory`
2. `search_memory`
3. `update_memory`
4. `delete_memory`

### Skills — 3

5. `list_skills`
6. `load_skill`
7. `create_skill`

### Data portability — 1

8. `export_data`

### Web research — 2

9. `web_search`
10. `fetch_url`

### File discovery and file operations — 34

11. `read_file`
12. `search_files`
13. `list_directory`
14. `get_file_info`
15. `glob_pattern`
16. `write_file`
17. `edit_file`
18. `create_directory`
19. `delete_file`
20. `move_file`
21. `batch_edit`
22. `glob_apply`
23. `show_file_with_line_numbers`
24. `insert_line`
25. `replace_lines`
26. `delete_lines`
27. `preview_diff`
28. `backup_file`
29. `undo_last_edit`
30. `batch_file_ops`
31. `find_text`
32. `append_to_file`
33. `prepend_to_file`
34. `compare_files`
35. `create_file_from_template`
36. `safe_path_status`
37. `disk_usage`
38. `checksum`
39. `copy_file`
40. `find_duplicates`
41. `tail_file`
42. `head_file`
43. `count_lines`
44. `file_tree`

### Execution — 3

45. `run_code`
46. `run_command`
52. `terminal_exec`

### Media — 5

47. `generate_image`
48. `image_info`
49. `resize_image`
50. `convert_image`
51. `crop_image`

### Scheduled automation — 7

53. `create_cron_job`
54. `list_cron_jobs`
55. `get_cron_job`
56. `update_cron_job`
57. `delete_cron_job`
58. `run_cron_job_now`
59. `get_cron_logs`

### Phone control — 7

60. `phone_status`
61. `phone_get_notifications`
62. `phone_search_contact`
63. `phone_send_sms`
64. `phone_call_number`
65. `phone_launch_app`
66. `phone_open_url`

### Configuration and time — 2

67. `update_config`
68. `get_current_datetime`

Tools without a dedicated roadmap section were still inventoried. This pass did not confirm a critical standalone defect in each of them; many are covered by a shared subsystem fix, such as file transactions, phone health, cron storage, or the persistent execution runtime.

## Recommended implementation order

### Phase 0 — Turn findings into failing tests (2–3 days)

- Convert BUG-001 through BUG-018 into deterministic regression tests.
- Use local HTTP, fake MCP, temporary filesystem, fake phone CLI, and clock/timezone fixtures.
- Do not change production behavior until each confirmed bug has a red test.

**Exit:** Every defect has a failing test or a documented platform exception.

### Phase 1 — Trust the core (weeks 1–2)

Implement:

- ARES-001
- ARES-008 through ARES-016
- ARES-021 and ARES-022
- ARES-027

**Exit:** Zero false success in fault injection; all P0 tests and integration tests pass; no new public tool names.

### Phase 2 — Precision (weeks 3–4)

Implement:

- ARES-002 and ARES-003
- ARES-011 and ARES-015
- ARES-017 through ARES-020
- ARES-023 through ARES-026
- ARES-029

**Exit:** P1 acceptance tests pass on Windows and CI fixtures.

### Phase 3 — Scale (week 5)

Implement:

- ARES-004 through ARES-007
- ARES-028

**Exit:** Large-repository benchmarks meet budgets and match reference results.

### Phase 4 — System verification (week 6)

Run journeys that chain existing tools:

1. Search web → fetch evidence → save research note.
2. Search memory → update memory → recall corrected fact.
3. Search files → read context → edit → run tests → undo.
4. Batch-edit multiple files → inject failure → verify exact rollback.
5. Run code that launches a child process → capture complete output.
6. Persist terminal environment/cwd across several commands.
7. Create cron job → manual run → concurrent scheduler tick → verify one run and one log.
8. Check phone capability → read notifications/contacts with Unicode fixtures.
9. Generate → resize → crop → convert an image while preserving source integrity.
10. Export config/memory → validate redaction → import and compare.

**Exit criteria**

- Full test suite finishes and is green.
- At least 95% success across the ten journeys.
- Zero silent failures.
- Zero incomplete rollbacks.
- Zero orphaned processes or stuck cron jobs.
- Public tool count remains 68 unless a separate product decision explicitly changes it.

## Final recommendation

Do not add more tools yet. First make the existing 68 tools trustworthy under failure, timeout, concurrency, large input, Windows execution, malformed user input, and external integration errors.

The shortest path from current Ares to a JARVIS-like assistant is:

> **accurate evidence → durable state → truthful action result → recovery → verified continuation**

That sequence should be the release gate for every upgrade in this document.
