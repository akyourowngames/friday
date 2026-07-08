# Existing Tools And Skills Upgrade Implementation

Source workbook: `docs/existing-tools-skills-upgrade-plan.xlsx` and `C:\Users\anime\Downloads\existing-tools-skills-upgrade-plan.xlsx`.

## Repository-Owned Upgrades

The Ares-owned rows from the workbook are implemented in the existing tool and skill surfaces, without introducing a new public tool count:

- `web_search` / `fetch_url`: Fetch MCP preference already existed; URL fetching now handles HTML/text/PDF extraction, final and canonical URLs, metadata, retryability, source-quality labels, and freshness labels.
- `mcp_client`: The MCP manager now has readiness reports, reconnect support, schema caching, per-server errors, and a health probe.
- `search_files` / `glob_pattern` / `file_tree`: File discovery now uses ignore-file awareness, ranked results, snippets, and ripgrep-first search.
- `read_file`: Code and markdown slices now include nearby imports, classes, functions, and headings.
- `write_file` / `edit_file` / line tools: Dry runs and edits show diffs, duplicate matches report line hints, CRLF is preserved in line edits, and backups are indexed as named restore points.
- `batch_edit` / `batch_file_ops` / `glob_apply`: Batch operations roll back touched files on partial failure.
- `preview_diff` / `backup_file` / `undo_last_edit`: Backups include labels, an index, and restore diffs.
- `run_command` / `terminal_exec`: Commands support profiles, structured summaries, and project command aliases from `pyproject.toml` and `package.json`.
- `run_code` / `PersistentREPL`: Runtime sessions support reset checkpoints and dependency fingerprints.
- `memory tools`: Store flow now blocks exact duplicates and exposes merge/conflict suggestions.
- `skills tools`: Skills now parse examples and test command metadata and expose lint messages.
- `cron tools`: Cron job details include next-run simulation and missed-run explanations.
- `phone_* tools`: Phone status includes permission preflight and a capability matrix.
- `image tools`: Generated and edited image assets record manifest rows with dimensions, format, checksum, and operation history.
- `export_data`: Export profiles and redaction previews are supported.
- Existing Ares skills named in the workbook now include the requested source matrices, evidence maps, risk checklists, architecture maps, stack-specific checklists, capacity estimation, standup extraction, and memory aging guidance.

## Non-Ares Rows

The workbook also contains Codex host tools and bundled/system skills such as `web`, `shell_command`, `apply_patch`, `image_gen`, browser/chrome/GitHub/OpenAI Docs/plugin skills, and skill-installer/creator rows. Per the Ares-local scope, those rows are treated as comparison/reference rows only. This implementation edits the Ares repository's existing local tools and `ares/skills/**/SKILL.md` files, and does not edit Codex host/plugin installations.

## Workbook Row Coverage

| Category | Existing row | Repo status |
|---|---|---|
| Local tool | `web_search` | Implemented in Ares. |
| Local tool | `fetch_url` | Implemented in Ares. |
| Local tool | `mcp_client` | Implemented in Ares. |
| Local tool | `search_files / glob_pattern / file_tree` | Implemented in Ares. |
| Local tool | `read_file` | Implemented in Ares. |
| Local tool | `write_file / edit_file / insert_line / replace_lines` | Implemented in Ares. |
| Local tool | `batch_edit / batch_file_ops / glob_apply` | Implemented in Ares. |
| Local tool | `preview_diff / backup_file / undo_last_edit` | Implemented in Ares. |
| Local tool | `run_command / terminal_exec` | Implemented in Ares. |
| Local tool | `run_code / PersistentREPL` | Implemented in Ares. |
| Local tool | `memory tools` | Implemented in Ares. |
| Local tool | `skills tools` | Implemented in Ares. |
| Local tool | `cron tools` | Implemented in Ares. |
| Local tool | `phone_* tools` | Implemented in Ares. |
| Local tool | `image tools` | Implemented in Ares. |
| Local tool | `export_data` | Implemented in Ares. |
| Codex tool | `web` | Non-Ares reference row; Ares analogue implemented in `web_search`. |
| Codex tool | `shell_command` | Non-Ares reference row; Ares analogue implemented in `run_command`. |
| Codex tool | `apply_patch` | Non-Ares reference row; Ares analogue implemented in file edit previews/backups. |
| Codex tool | `image_gen` | Non-Ares reference row; Ares analogue implemented in image asset manifests. |
| Skill | `browser:control-in-app-browser` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `chrome:control-chrome` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `github:github` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `github:gh-address-comments` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `github:gh-fix-ci` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `github:yeet` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `openai-docs` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `imagegen` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `plugin-creator` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `skill-creator` | Non-Ares reference row; not part of Ares-local skill edits. |
| Skill | `skill-installer` | Non-Ares reference row; not part of Ares-local skill edits. |
| Ares skill | `research/web-research` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `research/research-deep-dive` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `coding/code-review` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `coding/codebase-summary` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `coding/project-init` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `productivity/daily-planner` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `productivity/daily-standup` | Implemented in Ares skill metadata and instructions. |
| Ares skill | `ares/memory-consolidator` | Implemented in Ares skill metadata and instructions. |

## Verification

Focused verification:

```powershell
python -m pytest tests/test_web.py tests/test_filesystem.py tests/test_filesystem_write.py tests/test_mcp_client.py tests/test_image_edit.py tests/test_exporter.py tests/test_memory.py tests/test_skills.py tests/test_cron_schedule_utils.py tests/test_repl.py tests/test_shell_execution.py
python -m pytest tests/test_tools.py tests/test_power_tools_integration.py tests/test_repl_integration.py tests/test_tools_integration.py tests/test_phone_bridge.py tests/test_cron_tools.py
```
