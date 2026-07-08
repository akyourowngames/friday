---
name: project-init
description: Scaffold a new project — create dirs, init git, write boilerplate (README, .gitignore, license), run first commit. Use when the user says "start a new project" or "scaffold".
category: coding
version: 1.0.0
examples:
  - prompt: "Scaffold a small Python CLI project named notesync."
test_commands:
  - "python -m pytest tests/test_skills.py"
---

# Project Scaffold

## Procedure

1. **Ask** for project name, language/framework, and brief description if not provided.

2. **Create directory structure** — Use `run_command` to `mkdir -p` the project root and common subdirs (`src/`, `tests/`, `docs/`, `config/` etc.) based on the language/framework.

3. **Write boilerplate files:**
   - `README.md` with project name, description, setup instructions, and usage
   - `.gitignore` appropriate for the language/framework (Node, Python, Rust, Go, etc.)
   - `LICENSE` — ask which license if not specified (MIT, Apache 2.0, GPLv3)
   - Language-appropriate config if standard (e.g. `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`)

4. **Initialize git** — Run `git init` in the project root, then `git add -A && git commit -m "initial scaffold"`.

5. **Stack checklist** — Add stack-specific setup only when it matches the requested language/framework:
   - Python: `pyproject.toml`, package module, `tests/`, pytest smoke test.
   - Node/Vite: `package.json`, source entrypoint, lint/test script placeholders.
   - Static site: `index.html`, asset folder, README preview instructions.

6. **Post-create verification** — Run the cheapest appropriate verification command: import/smoke test for Python, `npm test` when a script exists, or file existence checks for static scaffolds.

7. **Report** — Tell the user what was created: path, structure summary, verification result, and next steps.

## Rules
- Do NOT install dependencies or run expensive build commands unless the user asked for that stack setup.
- Use `write_file` for all file creation, `run_command` for git/dir operations.
- Generate README content from the user's description — don't leave placeholders.
