# KING Tool Verification Pipeline

This markdown file is the default verification plan consumed by
`tool_verification_pipeline`.

The pipeline exists so KING can prove tool, prompt, manifest, runtime, and
frontend changes through visible checks instead of hidden success claims.

## Rules

- Keep commands bounded to the current repository.
- Use read-only checks unless the user explicitly requested a state-changing
  verification.
- Do not add phrase routing, regex routing, keyword routing, credentials, or
  canned user-facing replies here.
- Add a check only when its output provides useful evidence for a shipped
  change.
- If a required command fails, the pipeline verdict is `hold`.

## Runnable Checks

- command: `python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"`
  required: true
  reason: proves the markdown manifest, observed tool modules, and registered callable schemas stay aligned.
- command: `python -m unittest tests.test_grounding`
  required: true
  reason: validates tool grounding, registry dispatch, manifest audit, runtime
    tool upgrades, and no false-positive execution claims.
- command: `python -m pytest -q`
  required: true
  reason: runs the broader Python suite, including isolated verification
    pipeline coverage.
- command: `python -m compileall tools agent memory voice gesture main.py config.py`
  required: true
  reason: proves Python source compiles after tool or runtime edits.
- command: `npm run typecheck`
  required: true
  reason: proves the Next.js frontend type surface is still valid.

## Expected Report

`tool_verification_pipeline` must report the root, pipeline path, check count,
per-command status, exit code, bounded stdout and stderr, timeout status, and
final ship decision.

## Known Limits

- The tool runs commands listed here; it does not invent hidden checks.
- The pipeline does not replace targeted tests for a risky new tool.
- The pipeline cannot turn a failed or partial command into a success claim.
- The pipeline must not be used as an intent classifier.
