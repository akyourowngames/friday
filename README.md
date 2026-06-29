# Ares — Personal AI Assistant

A terminal-based personal AI assistant that remembers everything about you and helps with daily tasks. Think Jarvis from Iron Man, but in your terminal.

## Features

- **Natural language interaction** — just talk to it like a friend
- **Memory system** — remembers facts, preferences, and context about you with vector + FTS search
- **Memory control** — search, edit, and forget stored memories by ID
- **Task management** — create reminders and to-dos naturally
- **Runtime reminder engine** — checks due reminders while Ares is running and can send desktop notifications
- **Conversation persistence** — stores chat turns and session summaries in SQLite
- **Conversation compaction** — 4-phase history compression to fit long sessions in context
- **Automatic memory extraction** — LLM extracts facts/preferences from conversations automatically
- **Memory cleanup** — dedup, merge, and prune stale memories
- **Proactive context** — loads a user-owned soul file, profile, and project context every turn
- **Soul & Profile** — customizable personality definition and user identity with `@import` references
- **Project context** — automatically reads CLAUDE.md, AGENTS.md, README.md, pyproject.toml, package.json
- **ONNX embeddings** — uses Sentence Transformers' ONNX backend by default for faster memory embeddings
- **Streaming-first tool calls** — streams tokens immediately while detecting tool calls mid-stream
- **Web search summaries** — renders concise summaries plus numbered source cards
- **Tavily or ddgs search** — uses Tavily when configured, otherwise falls back to zero-key `ddgs`
- **URL fetching** — fetches and renders web page content
- **File access** — reads, searches, lists, globs, writes, edits, creates, deletes, and moves files — writes sandboxed to home directory
- **REPL sessions** — persistent Python and Shell subprocess REPLs with sentinel-based output framing
- **Image generation** — free AI image generation via Pollinations.ai
- **Image editing** — info, resize, convert, crop via Pillow
- **Export/import** — JSON backup and restore for memories, tasks, conversations, and non-secret config
- **Skills system** — portable SKILL.md playbooks with YAML frontmatter, 12 built-in skills across 5 categories
- **MCP client** — connect external MCP servers for extended tool capabilities
- **Google Workspace** — Gmail and Calendar integration via OAuth
- **Cron jobs** — scheduled agent sessions with natural language schedule parsing
- **Voice mode** — hands-free voice interaction with VAD, STT (faster-whisper), and TTS (Edge/Sarvam)
- **Beautiful terminal UI** — streaming markdown, thinking indicators, rich panels, tables, syntax highlighting
- **Desktop app** — Electron + React interface with streaming chat, sessions, tools, settings, terminal, and status
- **100% local data** — all your info stays on your machine
- **Free to run** — uses OpenCode Zen free models

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    User Interface                        │
│  ┌─────────────────┐  ┌──────────────────────────────┐  │
│  │    CLI (Rich)    │  │  Desktop (Electron + React)  │  │
│  │  prompt_toolkit  │  │  WebSocket ←→ ares/server.py │  │
│  └────────┬─────────┘  └──────────────┬───────────────┘  │
│           │                           │                   │
│           └───────────┬───────────────┘                   │
│                       │                                   │
│  ┌────────────────────▼────────────────────────────────┐  │
│  │                   Agent (agent.py)                   │  │
│  │  Central orchestrator — manages LLM, tools, memory,  │  │
│  │  conversation history, context, skills, MCP, cron    │  │
│  └──┬──────────┬──────────┬──────────┬─────────────────┘  │
│     │          │          │          │                     │
│     ▼          ▼          ▼          ▼                     │
│  ┌──────┐ ┌────────┐ ┌────────┐ ┌──────────┐             │
│  │ LLM  │ │ Tools  │ │Memory  │ │ Context  │             │
│  │client│ │Executor│ │ Store  │ │ Manager  │             │
│  └──────┘ └───┬────┘ └───┬────┘ └────┬─────┘             │
│               │          │           │                    │
│        ┌──────▼──┐ ┌─────▼─────┐ ┌───▼────────┐          │
│        │ Web     │ │ SQLite    │ │ Soul       │          │
│        │ Search  │ │ (memories,│ │ Profile    │          │
│        │ Files   │ │  convos,  │ │ ProjectCtx │          │
│        │ REPL    │ │  cron)    │ │ Compactor  │          │
│        │ Images  │ └───────────┘ │ Extractor  │          │
│        │ MCP     │              │ Cleaner    │          │
│        │ Google  │              └────────────┘          │
│        │ Cron    │                                       │
│        │ Export  │                                       │
│        └─────────┘                                       │
└─────────────────────────────────────────────────────────┘
```

### Data Flow

```
User Input (CLI / WebSocket / Voice)
    │
    ▼
Agent.run_stream(user_input, history)
    │
    ├─► context_blend.build_context_prompt()
    │       soul.md + profile.md + project context
    │     + relevant memories (vector + FTS)
    │     + session summaries
    │     + skills index
    │
    ├─► LLM.chat_stream(messages)
    │       streams tokens + detects tool calls mid-stream
    │
    ├─► (loop) if tool_calls:
    │       ToolExecutor.execute(tool_name, args)
    │         └─► local tool / MCP tool / Google tool
    │       feed results back → LLM continues streaming
    │
    └─► yield final response text
         │
         └─► post-session: memory extraction, compaction, cleanup
```

## Core Components

### Agent (`ares/agent.py`)
The central orchestrator. Holds all subsystems together: LLM client, tool executor, memory store, conversation store, MCP manager, soul/profile managers, skills manager, project context, and cron scheduler. Two main entry points:
- `run_stream()` — streaming agent loop with mid-stream tool call detection
- `run()` — non-streaming for background/cron tasks

### LLM Client (`ares/llm.py`)
Async OpenAI-compatible client with streaming support. Contains a `MODEL_REGISTRY` of ~50 known model IDs across free, Claude, GPT, Gemini, and Grok providers.

### CLI (`ares/cli.py`)
Interactive terminal built on `prompt_toolkit` (input with history, autocomplete, word completer) and `Rich` (panels, tables, markdown, live streaming, syntax highlighting). Handles all `/slash` commands, session management, and context display.

### WebSocket Server (`ares/server.py`)
Bridges the Electron desktop app to the Python backend. Manages sessions, chat streaming, context, memories, model switching, terminal forwarding, and status updates.

### Memory System
- **`memory.py`** — SQLite-backed long-term memory with `sqlite-vec` vector similarity search, FTS5 keyword search, and hybrid merge strategy. Facts have categories (preference, fact, belief, habit, relationship, note), importance, confidence scores, and access tracking.
- **`embeddings.py`** — Three backend abstraction: ONNX (default, fastest), Torch (fallback), Hash (last resort). Model cached in process.
- **`memory_extractor.py`** — LLM-powered automatic fact extraction from conversations.
- **`memory_cleaner.py`** — Deduplication, merging similar memories, pruning stale/low-importance facts.

### Context System
- **`context.py`** — `ProjectContext` scans current directory for CLAUDE.md, AGENTS.md, README.md, pyproject.toml, package.json and returns truncated excerpts.
- **`context_blend.py`** — Assembles soul + profile + project + memories + summaries into a priority-ordered string within a token budget. Adapts to model context windows (128k–1M tokens).
- **`context_manager.py`** — Orchestrates pre-send compaction and post-session processing.
- **`compactor.py`** — 4-phase compression: prune tool output → split head/middle/tail → LLM-summarize middle → reassemble.
- **`soul.py`** — Personality definition manager (`soul.md`).
- **`profile.py`** — User identity manager (`profile.md`) with `@path/to/file` import references.

### Tools Subsystem (`ares/tools/`)
~45 tools organized by domain, all with OpenAI function-calling JSON Schema definitions:
- **`web.py`** — Tavily/ddgs web search, URL content fetching (readability extraction)
- **`filesystem.py`** — Read-only: read, search, list, glob, checksum, tree, tail, head
- **`filesystem_write.py`** — Write/edit: create, delete, move, batch edit, find/replace, backup/undo, diff preview, templates — sandboxed to home directory
- **`repl.py`** — Persistent Python/Shell subprocess REPLs
- **`image_generate.py`** — AI image generation (Pollinations.ai)
- **`image_edit.py`** — Image info, resize, convert, crop (Pillow)
- **`exporter.py`** — JSON backup/restore of memories, conversations, config
- **`mcp_client.py`** — Model Context Protocol client: OAuth token management, tool discovery, execution
- **`google_mcp_bridge.py`** — Direct Google REST API for Gmail/Calendar
- **`definitions.py`** — All tool schemas
- **`executor.py`** — Dispatcher routing tool names to handlers
- **`tool_truncator.py`** — Truncates large tool outputs to save tokens
- **`renders.py`** — Rich console renderers for web search cards, file content, search results, directory trees

### Skills System (`ares/skills.py`)
Portable `SKILL.md` playbook pattern. Each skill has YAML frontmatter (name, description, category, version) + markdown instructions. Discovered across built-in (`ares/skills/`) and user (`~/.ares/skills/`) directories. Compact index loaded into system prompt; full instructions loaded on demand.

**12 built-in skills:**
| Category | Skills |
|---|---|
| Research | research-deep-dive, web-research |
| Coding | project-init, code-review, codebase-summary |
| Ares | weekly-review, memory-consolidator, export-backup |
| Utilities | image-batch-processor, system-info, backup-snapshot |
| Productivity | daily-standup, daily-planner |

### Cron / Scheduling (`ares/cron/`)
- **`store.py`** — SQLite storage for cron jobs and markdown execution logs
- **`scheduler.py`** — Background tick loop checking due jobs
- **`runner.py`** — Spawns fresh agent sessions per execution
- **`tools.py`** — Agent-callable tools: create, list, update, delete, run-now, get-logs
- **`schedule_utils.py`** — Natural language → cron expression parsing ("every day at 9am")

### Voice Mode (`ares/voice/`)
- **`agent.py`** — `ContinuousVoiceAgent`: always-listening with WebRTC VAD, utterance recording, faster-whisper transcription, agent pipeline, TTS response
- **`stt.py`** — Speech-to-text (faster-whisper)
- **`tts.py`** — Text-to-speech (Edge TTS, Sarvam AI)
- **`listener.py`** — Audio input stream
- **`player.py`** — Audio output with speed control

### Desktop App (`electron-app/`)
- **Main process** (`src/main/`) — Electron window, Python backend lifecycle (`python-manager.js`), PTY terminal (`terminal-manager.js` via node-pty), IPC handlers
- **Preload** (`src/main/preload.js`) — `contextBridge` exposing server URL, terminal, and restart APIs
- **Renderer** (`src/renderer/`) — React 19 + Vite + Zustand:
  - Chat: Composer, MessageList, streaming indicator, markdown rendering
  - Sidebar: session list with create/switch/delete
  - Settings: model selector, config panel
  - Tools: WebSearchCard, FileCard, MemoryCard, ToolCard
  - Terminal: xterm.js PTY terminal panel
  - Common: CodeBlock, StatusBar, ContextBar, TaskNotification

### Persistence

All data stored under `~/.ares/`:

| Path | Contents |
|---|---|
| `~/.ares/config.json` | User configuration |
| `~/.ares/data/ares.db` | SQLite DB: memories (vector + FTS), conversations, cron jobs |
| `~/.ares/data/soul.md` | Ares personality definition |
| `~/.ares/data/profile.md` | User profile |
| `~/.ares/skills/` | User-installed skills |
| `~/.ares_history` | Terminal prompt history |
| `~/.ares/data/mcp_tokens/` | OAuth tokens for MCP servers |
| `electron-app/node_modules/` | Desktop app dependencies (after `npm install`) |

## Quick Start

```bash
# Install
pip install -e .

# Run
python -m ares
```

### Desktop App

```bash
# Install Python package first
pip install -e .

# Install desktop dependencies
cd electron-app
npm install

# Run the Electron desktop app
npm run dev
```

For renderer-only development:

```bash
python -m ares --server
cd electron-app
npm run dev:vite
```

Open `http://127.0.0.1:5173` for the browser-hosted renderer. The Electron app and CLI share the same Ares backend, config, memories, tasks, conversations, `soul.md`, and `profile.md`.

### Voice Mode

```bash
pip install -e ".[voice]"
python -m ares --voice
```

### Server Mode

```bash
python -m ares --server                 # default port 8765
python -m ares --server --port 8766     # custom port
ares-server                              # console script alias
```

## First Run

On first run, Ares will:
1. Create `~/.ares/data/` for local storage
2. Download the embedding model (~90MB) for memory search
3. Show a welcome message with privacy info

## Usage

Just type naturally:
- "remember that I prefer dark mode"
- "what do you know about me?"
- "remind me to call the dentist tomorrow at 2pm"
- "what tasks do I have?"
- "show my active context"
- "search the web for Python 3.13 release notes"
- "use Tavily to search for today's AI news"
- "read pyproject.toml"
- "search for ddgs in this project"
- "list the docs directory"
- "generate an image of a sunset"
- "create a cron job to check the weather every morning"

### Slash Commands

| Command | Description |
|---------|-------------|
| `/help` | Show available commands |
| `/tasks` | List all pending tasks |
| `/tasks all` | List all tasks, including done/cancelled |
| `/tasks search QUERY` | Search tasks |
| `/tasks complete ID` | Mark a task done |
| `/tasks cancel ID` | Cancel a task |
| `/memory` | Show recent memories |
| `/memory search QUERY` | Search stored memories |
| `/memory edit ID NEW_TEXT` | Edit a memory and refresh its indexes |
| `/memory delete ID` | Delete a memory |
| `/forget ID` | Delete a memory by ID |
| `/model` | Show current/known models |
| `/model MODEL_NAME` | Switch model and save config |
| `/clear` | Clear terminal screen |
| `/export` | Export data to JSON |
| `/export PATH` | Export data to a specific JSON path |
| `/import PATH` | Import memories, tasks, and conversations from JSON |
| `/import PATH --config` | Import data and non-secret config |
| `/reset` | Reset conversation context |
| `/soul` | Show Ares' personality file |
| `/soul edit` | Open or create `soul.md` for editing |
| `/profile` | Show your profile file |
| `/profile edit` | Open or create `profile.md` for editing |
| `/context` | Show the active blended context |
| `/skills` | List available skills |
| `/skills search QUERY` | Search skills |
| `/skills load NAME` | Show a skill's full instructions |
| `/skills categories` | Show skill category counts |
| `/skill-name` | Load a skill directly by slash command |
| `/exit` | Exit Ares |

### Skills

Ares supports local reusable skills/playbooks using the portable `SKILL.md` pattern:

```text
~/.ares/skills/<category>/<skill-name>/SKILL.md
```

Each skill starts with YAML frontmatter (`name`, `description`, optional `category` and `version`) followed by markdown instructions. Ares loads only a compact name/description index into the system prompt and loads full instructions on demand with `load_skill` or `/skills load NAME`.

Built-in starter skills include `code-review`, `web-research`, `daily-planner`, `memory-consolidator`, `weekly-review`, `export-backup`, and `system-info`.

| Command | Description |
|---------|-------------|
| `/skills` | List available skills |
| `/skills search QUERY` | Search skill names, descriptions, categories, and bodies |
| `/skills load NAME` | Show a skill's full instructions |
| `/skills categories` | Show category counts |
| `/skill-name` | Load a skill directly by slash command |

### Tool Examples

Natural prompts that trigger local tools:

- "remember that I prefer dark mode"
- "forget memory 12"
- "update memory 12 to say I prefer coffee"
- "remind me to call the dentist tomorrow at 2pm"
- "mark task 4 done"
- "what is due in the next 24 hours?"
- "export my data"
- "search the web for today's AI news"
- "search Tavily for current Bitcoin price"
- "read README.md lines 1 through 80"
- "find files containing sqlite_vec"
- "list files in docs"
- "run `python --version` in a shell"
- "generate an image of a programming cat"
- "show me the disk usage"
- "create a cron job to summarize my week every Friday at 5pm"

## Configuration

Config is stored at `~/.ares/config.json`:

```json
{
  "model": "deepseek-v4-flash-free",
  "api_key": "",
  "api_base_url": "https://opencode.ai/zen/v1",
  "max_context_messages": 20,
  "max_memory_retrieval": 5,
  "data_dir": "~/.ares/data",
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "embedding_backend": "onnx",
  "embedding_provider": "CPUExecutionProvider",
  "embedding_file_name": "",
  "reminder_poll_seconds": 30,
  "enable_desktop_notifications": true,
  "session_summary_messages": 2,
  "web_search_provider": "auto",
  "tavily_api_key": "",
  "tavily_search_depth": "basic",
  "soul_path": "",
  "profile_path": "",
  "project_context_enabled": true,
  "context_token_budget": 2000,
  "project_context_max_files": 2,
  "skills_enabled": true,
  "skill_dirs": ["~/.ares/skills"],
  "skill_auto_suggest": true
}
```

`embedding_file_name` can be set when you want a specific ONNX file from a model repository, for example an optimized or quantized ONNX artifact. Leave it empty to let Sentence Transformers choose the default file.

If the Sentence Transformers/ONNX/Torch stack fails to import or load, Ares falls back to a deterministic local hash embedding backend so memory storage still works.

`web_search_provider` can be `auto`, `tavily`, or `ddgs`. In `auto` mode, Ares uses Tavily when `TAVILY_API_KEY` or `tavily_api_key` is configured, then falls back to `ddgs`. Set `tavily_search_depth` to `advanced` when you want deeper Tavily searches.

`soul_path` and `profile_path` can point to custom markdown files. Leave them empty to use `soul.md` and `profile.md` under `data_dir`. Project context scans the current directory for files like `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`, `package.json`, and `README.md`.

## Persistence

Stored across sessions:

- Memories, memory metadata, embeddings, and FTS search index in `~/.ares/data/ares.db`
- Tasks, due dates, reminder timestamps, and sent-reminder state in `~/.ares/data/ares.db`
- Conversation sessions, chat turns, and compact session summaries in `~/.ares/data/ares.db`
- Cron job definitions and execution logs in `~/.ares/data/ares.db`
- Config in `~/.ares/config.json`
- Ares personality in `~/.ares/data/soul.md` by default
- User profile in `~/.ares/data/profile.md` by default
- Terminal input history in `~/.ares_history`
- Desktop dependencies in `electron-app/node_modules/` after `npm install`

Not stored:

- API keys in JSON exports
- Tavily API keys in JSON exports
- A background reminder daemon after Ares exits
- Local chat models or Ollama integration

## Privacy

- All data (memories, tasks, conversations) stored locally in SQLite
- No telemetry, no analytics, no phone-home
- Free models may log data for improvement — switch to paid models for privacy
- Web search sends the search query to Tavily or external `ddgs` backends
- File read operations are unrestricted. File write operations are sandboxed to your home directory with protected paths (`~/.ares/`) blocked and destructive operations require explicit user confirmation
- Use `/model` to see available models

## Tech Stack

- Python 3.11+
- Rich (terminal rendering)
- prompt_toolkit (input with history/autocomplete)
- sentence-transformers ONNX backend (local embeddings)
- ONNX Runtime / Optimum ONNX (embedding acceleration)
- sqlite-vec (vector search)
- dateparser + tzlocal (natural date parsing)
- Plyer (optional desktop notifications)
- Tavily Search API (optional summarized web search)
- ddgs (zero-key web search fallback)
- ripgrep if installed, with Python regex fallback (file content search)
- httpx (async HTTP)
- pydantic (data models)
- websockets (desktop server protocol)
- faster-whisper (voice STT)
- Edge TTS / Sarvam AI (voice TTS)
- WebRTC VAD (voice activity detection)
- Electron + React + Zustand + Vite (desktop client)
- xterm.js + node-pty (desktop terminal)
- Pillow (image manipulation)
- MCP (Model Context Protocol)
- croniter (cron expression parsing)

## License

MIT
