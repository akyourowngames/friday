# KING: Local AI Assistant Runtime with Graph Memory and Tool Calling

KING is an open-source, local-first AI assistant runtime for developers who
want more than a chat box: semantic tool routing, graph-backed memory,
structured tool results, frontend control surfaces, and a verification pipeline
that proves behavior before the assistant claims it works.

It is built around a simple idea: an assistant should remember useful context,
use real tools when it has them, expose what it can do through inspectable
contracts, and avoid pretending that an action happened when no tool result
proved it.

## SEO Keywords

local AI assistant, open source Jarvis assistant, AI tool calling framework,
graph memory AI, semantic tool routing, local agent runtime, markdown driven
AI tools, personal AI assistant, NVIDIA NIM assistant, FastAPI AI assistant,
Next.js AI dashboard, Python AI agent, retrieval augmented memory, verified AI
tools, local automation assistant, open source AI agent

## Why Developers Star It

- **Graph memory by default**: durable facts become relation nodes and edges,
  with text projections kept for embeddings, display, and rollback.
- **Semantic tool routing**: tools are selected by embeddings and registry
  schemas, not fragile phrase tables.
- **Markdown-governed behavior**: tool policy, routing policy, memory rules,
  browser targets, system controls, and verification checks live in readable
  markdown files.
- **Structured tool results**: tools return typed success/error envelopes,
  provider status, trace metadata, and legacy text where older flows need it.
- **No fake success claims**: local actions, provider calls, and tool chains are
  grounded in returned fields instead of canned assistant replies.
- **Frontend included**: chat, memory graph, navigator, and tool-driven pages
  are available from the local web UI.
- **Verification is a feature**: a markdown-owned pipeline runs tests,
  compile checks, frontend type checks, and manifest audits.

## Open Source and Contributing

Friday/KING is open source and built for contributors. Pull requests, issues,
new tools, provider adapters, memory improvements, frontend surfaces, tests,
docs, and verification upgrades are welcome.

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks, and pull request
guidelines.

Good contribution paths:

- add a new tool with schema, typed errors, manifest evidence, and tests
- improve graph memory ranking, relation rules, or recall presentation
- add provider fallback logic without fake success claims
- polish the frontend pages for chat, memory, navigator, or folder watcher
- expand the verification pipeline with checks that produce useful evidence

Please keep changes grounded: no keyword routing shortcuts, no canned success
responses, no hidden verification claims, and no secrets in commits.

## Architecture

```mermaid
flowchart LR
    U["User input"] --> A["Agent core"]
    A --> R["Semantic router"]
    R --> S["Tool schemas"]
    A --> M["Graph memory recall"]
    A --> L["NVIDIA NIM LLM"]
    L --> C["Native or JSON tool call"]
    C --> V["Validator and grounding checks"]
    V --> T["Registered tools"]
    T --> E["Structured result envelopes"]
    E --> A
    A --> F["CLI or FastAPI stream"]
    F --> W["Frontend pages"]
```

## Core Systems

| System | What it does | Key files |
| --- | --- | --- |
| Agent runtime | Conversation loop, tool-call parsing, grounding, summaries | `agent/core.py`, `agent/router.py`, `agent/validator.py` |
| Tool registry | Discovers callable tools and JSON schemas | `tools/registry.py`, `tools/TOOL_MANIFEST.md` |
| Memory | Graph storage, vector recall, relation rules, profile context | `memory/brain.py`, `memory/MEMORY_UNIFIED_MODEL.md`, `memory/MEMORY_GRAPH_RELATIONS.md` |
| Frontend | Local web assistant, memory graph, navigator surface | `api_server.py`, `public/frontend/`, `src/app/` |
| Folder watcher | Local file intelligence service with API, index, dashboard, deploy templates | `folder_watcher/`, `deploy/folder_watcher/` |
| Telegram watcher | Separate Telegram bot endpoint service for natural allowed-zone file delivery, push notifications, and normal `python main.py` CLI bridging | `telegram_watcher/`, `telegram_watcher_service.py`, `tools/TELEGRAM_WATCHER_CONFIG.md` |
| Verification | Markdown-defined ship checks and evidence | `tools/TOOL_VERIFICATION_PIPELINE.md`, `tests/` |

## Obsidian Memory Vault

The repo includes an Obsidian-ready memory vault at `obsidian/King`. Open that
folder in Obsidian to browse KING memory as a graph. Start at `Home.md`, keep
`index.md` and `log.md` current, and follow `AGENTS.md` plus
`memory/MEMORY_OBSIDIAN_VAULT.md` when adding, updating, querying, or removing
memory. The vault is a markdown projection; runtime recall still belongs to the
structured memory tools. Runtime graph persistence also regenerates
`obsidian/King/Generated Memory Graph`, so remembered facts, graph nodes, graph
edges, updates, and removals show up in Obsidian automatically.

Normal `python main.py` starts the Telegram watcher endpoint service when
`tools/TELEGRAM_WATCHER_CONFIG.md` enables `main_cli_autostart` and Telegram
token/auth env values are present. CLI file-service requests go through that
separate service's `/cli/message` endpoint; ordinary broad chat stays with the
main KING agent.

## Tool Surface

KING ships with tools for:

- browser extraction and saved login sessions
- file read/write/list
- Hacker News and Reddit retrieval
- image generation and gallery management
- keyboard shortcuts and system controls
- memory recall, remember, forget, and assessment
- navigator routes and place details
- notes, terminal commands, datetime, web search, YouTube, and manifest audits
- folder watcher indexing, search, status, webhooks, and deployment

The registry remains the callable source of truth. Markdown files describe and
govern behavior; they are not keyword routing tables.

## Quickstart

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
npm install
Copy-Item .env.example .env
python main.py
```

Run the API and frontend:

```powershell
npm run api
npm run dev
```

Then open the local frontend served by Next.js or the static pages under
`public/frontend/`.

## Configuration

Copy `.env.example` to `.env`, then set provider keys and tuning knobs as
needed. The most important configuration areas are:

- `NVIDIA_API_KEY` and model settings for the LLM backend
- `KING_MEMORY_*` for graph memory, vector recall, and ranking
- `KING_TOOL_*` for routing, retries, grounding, and result bounds
- `KING_BROWSER_*`, `KING_SYSTEM_*`, and `KING_NAVIGATOR_*` for tool providers
- `TELEGRAM_BOT_TOKEN` and `KING_TELEGRAM_*` for the Telegram watcher daemon
- `KING_VERIFICATION_PIPELINE_*` for bounded ship checks

## Standalone Friday CLI

The repo also ships a direct local CLI entrypoint:

```powershell
python chat.py
```

This path uses:

- NVIDIA NIM streaming chat with `mistralai/ministral-14b-instruct-2512` by default
- last 20 chat messages in every model request
- append-only per-session JSONL transcripts under `storage/sessions/`
- SQLite message and fact storage at `storage/friday_assistant.sqlite3`
- LlamaIndex knowledge RAG over local MiniLM embeddings
- editable memory files: `memory/project.txt`, `memory/user.txt`, `memory/preferences.txt`, `memory/personal.txt`
- local knowledge files under `knowledge/`
- direct saved-memory context on every request, with optional agentic RAG query planning via `FRIDAY_AGENTIC_RAG_ENABLED=true`
- optional Sarvam Bulbul v3 voice output with the `priya` female speaker
- optional hold-Space voice input using Sarvam Saaras v3 STT

Useful commands:

```text
/memory
/memory rebuild
/memory search <query>
/db search <query>
/remember <project|user|preferences|personal> <fact>
/voice
/voice on
/voice off
/voice test
/voice speaker <name>
/voice input on
/voice input off
/session
/ping
```

For non-interactive checks:

```powershell
python chat.py --rebuild-memory
python chat.py --ping
python chat.py --voice-test
python chat.py --voice-roundtrip-test
python chat.py --transcribe-test storage/voice/some-file.wav
python chat.py --once "my name is Krish Verma"
```

Voice is controlled by `SARVAM_API_KEY` plus `FRIDAY_VOICE_*` settings. Generated WAV files are saved under `storage/voice/` and ignored by git.
Voice input is controlled by `FRIDAY_VOICE_INPUT_*` and `FRIDAY_STT_*` settings. In the interactive CLI, hold Space to record and release Space to transcribe and send the message.

## Verification

Use the repo-owned verification path before claiming a change is shipped:

```powershell
python -m pytest -q
npm run typecheck
python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"
```

For the full markdown-defined toolchain check, use the verification tool or run
the commands listed in `tools/TOOL_VERIFICATION_PIPELINE.md`.

## Reusable Code Gists

These public GitHub Gists extract the most useful code patterns from this repo:

- [JSON tool-call leak guard](https://gist.github.com/akyourowngames/a4ae05d4a11a0b36cf2be9f227f3d96b)
- [Semantic tool router](https://gist.github.com/akyourowngames/f6492d54f7f1f7302d5f1ed387f89a5a)
- [Structured tool result envelopes](https://gist.github.com/akyourowngames/5e1f59861ad965e0fe1a585ac9cbb022)
- [Graph memory unified ranker](https://gist.github.com/akyourowngames/64563ec381c7a435b13a79facd742a4e)

## Repository Map

```text
agent/                 core assistant loop, routing, validation, LLM client
memory/                graph memory, vector store, relation and recall docs
tools/                 registered tools plus markdown control surfaces
folder_watcher/        local file intelligence service
public/frontend/       static assistant, memory, navigator, and viewer pages
src/app/               Next.js app shell
tests/                 grounding, memory, tool, frontend-adjacent checks
deploy/folder_watcher/ service, Docker, launchd, nginx templates
```

## License

MIT. Use it, fork it, remix it, and send improvements back.

## Project Status

KING is an active open-source local assistant research/runtime repo. It is
strongest as a developer playground for agent memory, tool grounding, local
automation, and frontend-visible tool workflows.
