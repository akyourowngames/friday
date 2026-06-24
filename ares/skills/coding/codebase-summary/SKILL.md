---
name: codebase-summary
description: Analyze any codebase — tree view, language breakdown, file counts by type, largest files, recent changes. Use for "summarize this project", "explore this codebase", "what's in this repo".
category: coding
version: 1.0.0
---

# Codebase Summary

## Procedure

1. **Tree the structure** — Use `file_tree` or `list_directory` at the root to get the top-level layout. If the project is large, focus on `src/`, `lib/`, `app/`, `packages/`.

2. **Count by language** — Use `run_command` with language-appropriate counting:
   - `find . -name '*.py' | wc -l` and similar for `.js`, `.ts`, `.rs`, `.go`, `.jsx`, `.tsx`, `.rs`, `.java`, etc.
   - Include key config files: `package.json`, `Cargo.toml`, `pyproject.toml`, `go.mod`, etc.

3. **Find largest files** — `run_command`:
   - `find . -type f -not -path './.git/*' -exec wc -l {} + | sort -rn | head -15`

4. **Check recent git activity** if `.git` exists:
   - `git log --oneline -10` for recent commits
   - `git log --since="1 month ago" --format='%an' | sort | uniq -c | sort -rn` for contributor activity

5. **Write summary** — Use `write_file` to save a markdown summary at the project root:
   - ## Project Overview
   - ## Structure (tree)
   - ## Language Breakdown
   - ## Largest Files
   - ## Recent Activity (last 10 commits, contributors)

6. **Report** — Tell the user the file path and key stats (total files, main language, most active areas).

## Rules
- Do NOT read every source file — use tooling to count and size.
- Skip `.git/`, `node_modules/`, `__pycache__/`, `.venv/`, `dist/`, `build/`.
