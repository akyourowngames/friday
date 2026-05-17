# KING Agent Instructions

## Golden Rule
- **NEVER hardcode anything.** Do not hardcode tool calls, tool names, responses, dates, paths, or any content meant to be dynamic. Everything must be read from config, file, or external source.
- This applies to AI personality (`persona.md`), tool schemas (registry), settings (`config.py`), and user data (storage). If it can change, it must not be hardcoded.

## Project Structure
- `main.py` - CLI entry point
- `config.py` - Settings (API key, model, debug flag)
- `persona.md` - **AI personality definition** (FRIDAY/JARVIS-style). Modify this to change how KING speaks and behaves — no code changes needed.
- `agent/` - Core agent logic
  - `core.py` - Agent loop (LLM → tool → repeat). Loads persona from `persona.md`.
  - `llm.py` - NVIDIA NIM client with streaming
  - `router.py` - Semantic tool selector (embeddings)
  - `validator.py` - Tool call validation
- `tools/` - Tool plugins (drop a file here to add)
  - `registry.py` - `@tool` decorator + registry
  - `web.py` - Web search, page fetch
  - `notes.py` - Save/read/list notes
  - `files.py` - Read/write/list files
- `storage/` - User data (notes, etc.)

## How to run
```powershell
$env:NVIDIA_API_KEY = "nvapi-..."
python main.py
```

## Adding a tool
Create a new file in `tools/` with:
```python
from tools.registry import tool

@tool(name="tool_name", description="What it does", examples=["example usage"])
def my_tool(param1: str, param2: int = 0) -> str:
    ...
```
Restart the app. The router auto-discovers it via semantic search.

## Commands
- `/debug` - Toggle verbose mode (shows tool selection, calls, results)
- `/tools` - List all registered tools
- `/model <name>` - Switch model (e.g., `/model meta/llama-3.3-70b-instruct`)
- `/new` - Start fresh conversation
- `/help` - Show commands
- `/exit` - Quit
