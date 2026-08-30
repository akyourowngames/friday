# Taste

## Workflow / tooling
- Also uses Cline as an AI coding tool and keeps its session transcripts; asks the assistant to locate and read the latest Cline session and continue its work (expects cross-tool continuity). Cline sessions live in `C:\Users\anime\AppData\Roaming\Code\User\globalStorage\saoudrizwan.claude-dev\tasks` and may belong to unrelated projects, so check task metadata before assuming relevance. Confidence: 0.6
- Develops on Windows: PowerShell is the shell, home dir is `C:\Users\anime`, and active projects live under Desktop (e.g. TypeScript repo `friday-ng`). Confidence: 0.8
- Wants to ship `friday-ng` as a public npm package so end users can `npm install -g friday-ng` and run it from anywhere by typing `friday-ng`; expects the `bin` field to expose the CLI entrypoint. Confirmed package name `friday-ng` (unscoped), MIT license, version 0.3.0. Confidence: 0.9
- Prefers the assistant to handle publish-readiness prep itself (bump version, write LICENSE, add `.npmrc`, dry-run pack) but NEVER to run `npm login` or touch credentials — the user drives auth in a separate shell and signals when done. Confidence: 0.85

## Communication
- Sends terse, casual, lowercase requests (typos common, e.g. "contieu", "hanress", "di for and for repsonses"); expects the assistant to infer missing specifics (like where session files live) instead of asking for clarification. Resumes in-progress work with single-word prompts like "continue". Confidence: 0.7
- Wants **properly formatted responses with headings, bullet points, and code blocks** — explicitly does NOT want raw text dumps (e.g. dumping raw `bash` JSON tool calls or file lists without structure). Confidence: 0.85

## Git / publishing
- GitHub username is `akyourowngames` — expects the assistant to push local changes to that GitHub account when asked ("push all local changes to github as akyourowngames"). Confidence: 0.8