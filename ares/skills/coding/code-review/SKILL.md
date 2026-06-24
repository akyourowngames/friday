---
name: code-review
description: Review code changes for correctness, security, performance, and style. Inspect diffs, surrounding code, and suggest concrete fixes. Use for "review this code", "check my PR", "review these changes".
category: coding
version: 1.0.0
---

# Code Review

## Procedure

1. **Understand scope** — If given files, read them with `read_file`. If given a description, use `search_files` or `glob_pattern` to find relevant files.

2. **Read the code** — For each file or section under review, `read_file` to get full context (not just the diff). Read surrounding functions/classes to understand the full picture.

3. **Check for issues across these dimensions:**
   - **Correctness** — Logic errors, race conditions, off-by-one, edge cases
   - **Security** — Injection, path traversal, secrets exposure, unsafe eval/exec
   - **Performance** — N+1 queries, unnecessary allocation, sync in async paths
   - **Maintainability** — Duplicated code, unclear naming, missing abstractions
   - **Style** — Deviations from language conventions, dead code, overly complex expressions

4. **Prioritize findings** — Categorize each as `HIGH` / `MEDIUM` / `LOW` impact. Only include LOW items if there are fewer than 3 HIGH/MEDIUM findings.

5. **Write review** — Present in this format:
   - ## Summary (1-2 sentences)
   - ## High Priority (with file:line references and concrete fix suggestions)
   - ## Medium (with recommendations)
   - ## Low / Nitpick (optional)
   - ## What's Good (positive feedback — don't skip this)

## Rules
- Always include file:line references for each finding.
- Suggest concrete fixes, not just "this is wrong".
- Include positive feedback too — don't be purely negative.
- If you can't find any real issues, say so — don't invent nitpicks.
