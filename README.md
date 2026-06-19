# 🔥 Ares — Personal AI Assistant

A terminal-based personal AI assistant that remembers everything about you and helps with daily tasks. Think Jarvis from Iron Man, but in your terminal.

## Features

- **Natural language interaction** — just talk to it like a friend
- **Memory system** — remembers facts, preferences, and context about you
- **Memory control** — search, edit, and forget stored memories by ID
- **Task management** — create reminders and to-dos naturally
- **Runtime reminder engine** — checks due reminders while Ares is running and can send desktop notifications
- **Conversation persistence** — stores chat turns and session summaries in SQLite
- **Proactive context** — loads a user-owned soul file, profile, and project context every turn
- **ONNX embeddings** — uses Sentence Transformers' ONNX backend by default for faster memory embeddings
- **Streaming-first tool calls** — streams tokens immediately while detecting tool calls mid-stream
- **Web search summaries** — renders concise summaries plus numbered source cards
- **Tavily or ddgs search** — uses Tavily when configured, otherwise falls back to zero-key `ddgs`
- **Read-only file access** — reads files, searches file contents/names, and lists directories safely
- **Export/import** — JSON backup and restore for memories, tasks, conversations, and non-secret config
- **Beautiful terminal UI** — streaming markdown, thinking indicators, rich panels
- **Desktop app** — Electron + React interface with streaming chat, sessions, tools, settings, and status
- **100% local data** — all your info stays on your machine
- **Free to run** — uses OpenCode Zen free models

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

For renderer-only development, run:

```bash
python -m ares --server
cd electron-app
npm run dev:vite
```

Open `http://127.0.0.1:5173` for the browser-hosted renderer. The Electron app and CLI share the same Ares backend, config, memories, tasks, conversations, `soul.md`, and `profile.md`.

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
| `/exit` | Exit Ares |

### Server Mode

| Command | Description |
|---------|-------------|
| `python -m ares --server` | Start the desktop WebSocket server on `127.0.0.1:8765` |
| `python -m ares --server --port 8766` | Start the server on a custom port |
| `ares-server` | Console script for the same WebSocket server |

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
  "project_context_max_files": 2
}
```

`embedding_file_name` can be set when you want a specific ONNX file from a model repository,
for example an optimized or quantized ONNX artifact. Leave it empty to let Sentence Transformers
choose the default file.

If the Sentence Transformers/ONNX/Torch stack fails to import or load, Ares falls back to a
deterministic local hash embedding backend so memory storage still works.

`web_search_provider` can be `auto`, `tavily`, or `ddgs`. In `auto` mode, Ares uses Tavily
when `TAVILY_API_KEY` or `tavily_api_key` is configured, then falls back to `ddgs`.
Set `tavily_search_depth` to `advanced` when you want deeper Tavily searches.

`soul_path` and `profile_path` can point to custom markdown files. Leave them empty
to use `soul.md` and `profile.md` under `data_dir`. Project context scans the current
directory for files like `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`, `package.json`,
and `README.md`.

## Persistence

Stored across sessions:

- Memories, memory metadata, embeddings, and FTS search index in `~/.ares/data/ares.db`
- Tasks, due dates, reminder timestamps, and sent-reminder state in `~/.ares/data/ares.db`
- Conversation sessions, chat turns, and compact session summaries in `~/.ares/data/ares.db`
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
- File tools are read-only and restricted to your home directory
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
- Electron + React + Zustand + Vite (desktop client)

## License

MIT
