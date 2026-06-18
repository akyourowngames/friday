# Ares Capabilities, Persistence, and Upgrade Notes

This is the current practical map of what Ares can do after the streaming, file-tool, CLI UX, ONNX, and web-search upgrades.

## Core Model Capability

Ares is a terminal assistant wrapped around an OpenAI-compatible chat model endpoint. The model does the reasoning, planning, tool selection, and final response writing. Ares gives that model a local tool belt:

- long-term memory over SQLite + vector search
- proactive context from soul/profile/project files
- task and reminder management
- conversation history and session summaries
- current web search through Tavily or ddgs
- read-only local file access
- JSON export/import
- Rich terminal rendering for tool results

The model can answer normal questions without tools, but it should call tools whenever the answer depends on remembered user data, tasks, files, current events, or local state.

## Tool Surface

| Tool | What it does | Example prompt |
|------|--------------|----------------|
| `store_memory` | Saves a fact, preference, habit, relationship, belief, or note. | "remember that I prefer concise answers" |
| `search_memory` | Retrieves stored memories with vector search. | "what do you remember about my coding preferences?" |
| `update_memory` | Corrects a stored memory by ID. | "update memory 12 to say I use PowerShell" |
| `delete_memory` | Deletes a stored memory by ID. | "forget memory 12" |
| `create_task` | Creates tasks and reminders with natural dates. | "remind me to renew my domain next Friday at 9am" |
| `list_tasks` | Shows pending tasks. | "what tasks do I have?" |
| `search_tasks` | Finds tasks by text. | "find tasks about invoices" |
| `complete_task` | Marks a task done. | "mark task 4 done" |
| `cancel_task` | Cancels a task. | "cancel task 7" |
| `get_due_soon` | Shows tasks due soon. | "what is due in the next 24 hours?" |
| `export_data` | Exports local data and non-secret config to JSON. | "export my Ares data" |
| `web_search` | Searches current web results and returns a summary plus sources. | "search Tavily for Python 3.14 news" |
| `read_file` | Reads a local text file with line numbers. | "read README.md lines 1 to 80" |
| `search_files` | Searches files by content or name. | "find files containing sqlite_vec" |
| `list_directory` | Lists directory contents. | "list the docs directory" |

## Proactive Context

Ares now loads layered context every turn:

- `soul.md`: Ares' personality and communication style
- `profile.md`: user identity, preferences, projects, goals, and notes
- project files in the current directory, such as `CLAUDE.md`, `AGENTS.md`, `pyproject.toml`, `package.json`, and `README.md`
- recent session summaries
- relevant memories
- pending tasks

The files are user-owned. Ares creates templates on first run but does not auto-edit them.

Commands:

- `/soul`: show the personality file
- `/soul edit`: open or create the personality file
- `/profile`: show the user profile
- `/profile edit`: open or create the profile
- `/context`: show the full blended context currently being injected

## Web Search

The web search tool now returns structured JSON internally:

- `query`
- `provider`
- `summary`
- `answer`
- `results`
- `errors`

The CLI renders that as a readable web-search panel with a summary and numbered source cards. This fixes the old raw yellow text block shown in the screenshots.

Provider behavior:

- `auto`: use Tavily if a key exists, otherwise ddgs
- `tavily`: use Tavily only and report a clear error if no key is configured
- `ddgs`: use zero-key ddgs search

Tavily key priority:

1. `TAVILY_API_KEY` environment variable
2. `tavily_api_key` in `~/.ares/config.json`

Prompt examples:

- "search the web for current Bitcoin price"
- "use Tavily to summarize the latest OpenAI model news"
- "search ddgs for Python 3.13 release notes"
- "find current weather in Panipat and cite sources"

## File and Terminal UX

The CLI now routes named tool tokens:

```text
[tool:web_search:{...}]
[tool:read_file:...]
[tool:search_files:...]
[tool:list_directory:...]
```

Each renderer is specialized:

- `web_search`: summary and numbered source cards
- `read_file`: syntax-highlighted code with line numbers
- `search_files`: match table with content/name badges
- `list_directory`: tree view
- other tools: compact generic output

During tool execution, the live panel shows tool-specific statuses such as "Searching web...", "Reading file...", or "Listing directory...".

## ONNX Embeddings

Ares still uses Sentence Transformers, but it uses the Sentence Transformers ONNX backend by default:

```json
{
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "embedding_backend": "onnx",
  "embedding_provider": "CPUExecutionProvider",
  "embedding_file_name": ""
}
```

That means the model interface remains Sentence Transformers, while ONNX Runtime handles faster local inference for memory embeddings. This is a good default because it keeps the simple Sentence Transformers API and uses ONNX acceleration underneath.

Important implementation note: the in-process embedding cache now includes `embedding_file_name` and `export`. That matters if you switch from a default ONNX file to a quantized or optimized ONNX artifact in the same process.

Recommended ONNX settings:

- CPU-only machine: keep `CPUExecutionProvider`
- NVIDIA GPU with compatible runtime installed: later add provider support for `CUDAExecutionProvider`
- Quantized model artifact: set `embedding_file_name` to the specific ONNX file in the model repository

## What Persists Across Sessions

Stored locally in `~/.ares/data/ares.db`:

- memories
- memory categories, confidence, importance, timestamps, access counters
- memory embeddings for vector search
- task titles, descriptions, priorities, status, due dates
- reminder times and sent-reminder state
- conversation sessions
- chat turns
- compact conversation summaries

Stored in `~/.ares/config.json`:

- model
- API base URL
- context limits
- embedding backend settings
- reminder settings
- web search provider settings
- Tavily API key, if configured there
- proactive context settings and optional custom soul/profile paths

Stored as markdown files:

- Ares personality in `~/.ares/data/soul.md` by default
- user profile in `~/.ares/data/profile.md` by default

Stored in `~/.ares_history`:

- terminal prompt history

Not persisted:

- the transient live "thinking" display
- current in-memory model/tool execution state
- active background reminder loop after Ares exits
- Tavily or model API keys inside JSON exports
- web search results as first-class records, unless the assistant includes them in a saved conversation turn

## Upgrade Ideas

High value next upgrades:

- Add `/config` commands for changing Tavily, embedding, and provider settings without editing JSON.
- Add source-citation discipline to the final answer after web search, for example `[1]`, `[2]` references tied to rendered cards.
- Add a `web_fetch` or `read_url` tool so Ares can open selected result pages instead of relying only on snippets.
- Add safe write/edit tools behind explicit confirmation, starting with patch previews.
- Add a local `health` command that checks ONNX backend, sqlite-vec, Tavily key presence, ddgs availability, and model endpoint reachability.
- Add search-result caching with expiry so repeated current-info questions are faster and cheaper.
- Add CUDA/DirectML embedding provider detection for Windows machines with supported GPU runtimes.
- Add richer file search using ripgrep JSON output when `rg` is installed.
- Add a task/reminder dashboard command with overdue, today, upcoming, and completed sections.
- Add optional encrypted config secrets for API keys on disk.

My take: Tavily + ONNX + Rich renderers is the right direction. The next biggest quality jump would be a `web_fetch` tool plus stricter source citations, because search snippets alone are often enough for simple facts but not enough for deeper answers.
