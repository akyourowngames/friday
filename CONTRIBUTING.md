# Contributing to KING / Friday

Thanks for wanting to improve Friday. This project is open source and welcomes
useful issues, docs, tests, tools, frontend polish, and runtime fixes.

## Good First Contributions

- improve README sections, screenshots, setup notes, or examples
- add tests for memory recall, tool routing, or frontend API behavior
- improve an existing tool's structured error output
- add provider fallback reporting without fake success claims
- polish the frontend chat, memory graph, navigator, or folder watcher pages
- document a reproducible bug with logs, commands, and expected behavior

## Contribution Rules

- Keep behavior grounded in real tool results.
- Do not add keyword routing shortcuts or canned success responses.
- Do not commit API keys, local secrets, personal memory files, or generated caches.
- Keep changes focused and explain the verification command you ran.
- Prefer markdown control surfaces for tool behavior when possible.

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install
Copy-Item .env.example .env
```

Fill in the provider keys you need in `.env`.

## Checks

Run the checks that match your change:

```powershell
python -m pytest -q
npm run typecheck
python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"
```

For tool/runtime changes, also inspect `tools/TOOL_VERIFICATION_PIPELINE.md`
and run the relevant focused tests.

## Pull Request Checklist

- The change is scoped to one clear goal.
- New behavior has tests or a manual verification note.
- Tool responses are based on returned fields, not invented prose.
- Documentation reflects any new public behavior.
- No secrets, local storage, pycache, or generated runtime artifacts are staged.

