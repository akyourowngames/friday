---
name: code-review
description: Review code for bugs, security issues, maintainability, tests, and project conventions. Use when the user asks for a code review or PR check.
category: coding
version: 1.0.0
examples:
  - prompt: "Review this diff for bugs and missing tests."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Code Review

## Procedure
1. Inspect the relevant diff and surrounding code.
2. Identify correctness, security, performance, maintainability, and test gaps.
3. Prioritize findings by impact and include file/line references when possible.
4. Recommend concrete fixes and verification steps.

## Risk Checklist
- Correctness: edge cases, invariants, error handling, concurrency, data loss.
- Security: injection, path traversal, secrets, auth, unsafe command execution.
- Tests: identify existing coverage, missing regression tests, and the smallest meaningful test to add.
- Ownership: map each finding to the changed file/module and the nearest existing convention.
- Verification: include the command or inspection that would prove the fix.

## Verification
- Confirm tests or checks that were run.
- Do not invent behavior not visible in code or outputs.
