# Contributing to Ares

Thanks for helping improve Ares. This project welcomes contributions from anyone, but all changes must go through pull requests and must be reviewed carefully before merge.

## Contribution Rules

| Rule | Requirement |
|---|---|
| Use pull requests | Open a PR for every change. Do not push directly to protected or shared branches. |
| Add tests every time | Every behavior change must include tests. Docs-only changes do not need runtime tests, but they must be proofread. |
| Keep PRs focused | One PR should solve one problem. Avoid mixing refactors, features, and formatting churn. |
| Review deeply | Check correctness, edge cases, security, privacy, performance, UX, and test coverage. |
| No surprise architecture changes | Do not change core architecture, data storage, tool contracts, or agent flow without opening an issue/discussion first. |
| Respect local-first design | Avoid sending user data to new external services unless the user explicitly configures that feature. |

## Getting Started

1. Fork the repository.
2. Create a feature branch.
3. Install development dependencies.
4. Make the smallest clear change.
5. Add or update tests.
6. Run the relevant checks.
7. Open a pull request with a clear explanation.

```bash
git clone https://github.com/<your-user>/friday.git
cd friday
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q
```

On macOS/Linux, activate the virtual environment with:

```bash
source .venv/bin/activate
```

## Pull Request Checklist

Before requesting review, make sure:

- [ ] The PR has one clear purpose.
- [ ] The code follows existing project patterns.
- [ ] Tests were added or updated.
- [ ] `python -m pytest -q` passes, or the PR explains why a narrower test run is appropriate.
- [ ] User-facing behavior is documented when needed.
- [ ] No secrets, tokens, local databases, generated caches, or personal files are committed.
- [ ] Architecture changes were discussed before implementation.

## Testing Expectations

| Change area | Expected tests |
|---|---|
| Agent loop or prompts | Agent, streaming, prompt, and regression tests. |
| Tools | Unit tests plus executor/integration tests when handlers change. |
| Skills | Skill parser, matcher, trigger, and prompt-injection tests. |
| CLI rendering | CLI tests that inspect rendered output. |
| Memory/context | Memory, context management, and integration tests. |
| Cron jobs | Store, scheduler, runner, and tool tests. |
| Phone/voice integrations | Unit tests with mocks; do not require real devices in CI. |
| Documentation | Link/command review and screenshots only when helpful. |

## Review Standard

Reviewers should look for:

- Real bugs and behavioral regressions.
- Missing or weak tests.
- Hidden data-loss, privacy, or shell-execution risks.
- Tool schema and handler mismatches.
- Prompt changes that create stale context, hallucinated success, or unwanted user-data storage.
- UX regressions in CLI output, tables, colors, wrapping, or desktop rendering.
- Unnecessary architecture churn.

## Architecture Change Policy

Open an issue or discussion before changing:

- The agent/tool-call loop.
- Memory schema or migration behavior.
- Tool definition contracts.
- Config file shape.
- Skill discovery or invocation semantics.
- Cron execution lifecycle.
- Phone, MCP, or shell execution safety boundaries.
- Desktop/server protocol contracts.

Small internal refactors are fine when they are local, tested, and do not alter public behavior.

## Commit and Branch Style

Use short, descriptive branch names:

```bash
fix/cli-table-wrapping
feat/skill-trigger-tests
docs/contributing-guide
```

Write commit messages in plain English:

```bash
Fix CLI table wrapping for narrow terminals
Add tests for silent skill auto-loading
Document pull request requirements
```

## Security and Privacy

Do not commit:

- API keys or tokens.
- `~/.ares` data.
- SQLite databases.
- OAuth token files.
- Phone identifiers or SMS/notification content.
- Private screenshots, transcripts, or local user files.

If you find a security issue, do not open a public exploit PR. Open a minimal private report to the maintainer with reproduction steps and impact.

## Maintainer Expectations

Maintainers should:

- Keep review feedback specific and actionable.
- Require tests before merge.
- Prefer small PRs over large rewrites.
- Reject architecture changes that were not discussed.
- Protect user privacy and local-first defaults.
