# KING: Local AI Assistant Runtime with Graph Memory and Tool Calling

KING is a local-first AI assistant runtime for developers who want more than a
chat box: semantic tool routing, graph-backed memory, structured tool results,
frontend control surfaces, and a verification pipeline that proves behavior
before the assistant claims it works.

It is built around a simple idea: an assistant should remember useful context,
use real tools when it has them, expose what it can do through inspectable
contracts, and avoid pretending that an action happened when no tool result
proved it.

## SEO Keywords

local AI assistant, open source Jarvis assistant, AI tool calling framework,
graph memory AI, semantic tool routing, local agent runtime, markdown driven
AI tools, personal AI assistant, NVIDIA NIM assistant, FastAPI AI assistant,
Next.js AI dashboard, Python AI agent, retrieval augmented memory, verified AI
tools, local automation assistant

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
| Verification | Markdown-defined ship checks and evidence | `tools/TOOL_VERIFICATION_PIPELINE.md`, `tests/` |

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
- `KING_VERIFICATION_PIPELINE_*` for bounded ship checks

## Verification

Use the repo-owned verification path before claiming a change is shipped:

```powershell
python -m pytest -q
npm run typecheck
python -c "import tools; from tools.manifest_audit import tool_manifest_audit; print(tool_manifest_audit('.', 300, True))"
```

For the full markdown-defined toolchain check, use the verification tool or run
the commands listed in `tools/TOOL_VERIFICATION_PIPELINE.md`.

## Developer Gists

This repo contains several reusable patterns worth lifting into other agent
projects:

- graph memory with relation repair and vector projection
- markdown as a governance layer for tools and verification
- structured tool envelopes with trace metadata
- provider fallback reporting without fake success claims
- frontend pages that visualize tools instead of hiding them behind chat

See `docs/DEV_GISTS.md` for copyable architecture notes.

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

## Project Status

KING is an active local assistant research/runtime repo. It is strongest as a
developer playground for agent memory, tool grounding, local automation, and
frontend-visible tool workflows.
