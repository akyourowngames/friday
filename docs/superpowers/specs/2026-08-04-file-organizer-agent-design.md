# File Organizer Agent — Design Spec

## What

A Node.js/TypeScript CLI agent that scans a directory, uses an LLM to decide how to organize files, and executes moves/renames/creates after user approval.

## Why

Replace manual file organization with AI-decided organization. The agent looks at file metadata and contents to decide where things belong, then shows you the plan before doing anything.

## Key Decisions

- **Direct tools, not MCP** — filesystem operations are TypeScript functions, not external MCP servers
- **AI-decided** — no hard-coded rules; the LLM decides categorization based on file info
- **CLI first** — desktop GUI comes later
- **Multi-provider LLM** — OpenAI-compatible API, starts with OpenCode free models
- **No restrictions** — user is the operator, no allowlists or blocked extensions
- **Approval flow** — agent shows plan, user approves/edits/rejects before execution

---

## Architecture

```
organizer/
├── src/
│   ├── agent/
│   │   ├── loop.ts          ← core agent loop (tool call → result → repeat)
│   │   └── types.ts         ← message, tool call, tool result types
│   ├── tools/
│   │   ├── definitions.ts   ← OpenAI function schemas
│   │   ├── filesystem.ts    ← list_dir, read_file, get_metadata
│   │   ├── organizer.ts     ← move_file, create_folder, rename_file
│   │   └── index.ts         ← registry: maps tool names → implementations
│   ├── llm/
│   │   ├── client.ts        ← OpenAI-compatible HTTP client
│   │   ├── providers.ts     ← provider configs (opencode, openai, etc.)
│   │   └── types.ts         ← chat completion types
│   ├── config.ts            ← settings, CLI flag parsing
│   └── index.ts             ← CLI entry point
├── package.json
└── tsconfig.json
```

---

## Core Flow

```
User runs CLI
    ↓
Scan target folder → build file list
    ↓
Send to LLM with tool definitions
    ↓
LLM decides: "move photo_2024.jpg to Pictures/Photos"
    ↓
Queue action (don't execute yet)
    ↓
LLM decides next action... until "done"
    ↓
Show plan: "Moved 12 files, created 3 folders"
    ↓
User approves → execute all actions
```

---

## Tools

### Read-only (execute immediately)

| Tool | Description | Returns |
|------|-------------|---------|
| `list_dir` | List directory contents | Files + dirs with metadata |
| `get_file_info` | Get detailed file info | Size, dates, extension, MIME type |
| `search_files` | Find files matching pattern | List of matching paths |

### Write (queue for approval)

| Tool | Description | Returns |
|------|-------------|---------|
| `move_file` | Move file from A to B | Success + new path |
| `rename_file` | Rename a file | Success + new path |
| `create_folder` | Create a directory | Success |
| `delete_file` | Delete a file (to trash) | Success |
| `done` | Signal completion | Summary of all queued actions |

### Tool Schema (what the LLM sees)

```json
{
  "name": "move_file",
  "description": "Move a file to a new location. The destination directory will be created if it doesn't exist.",
  "parameters": {
    "type": "object",
    "properties": {
      "source": {
        "type": "string",
        "description": "Full path of the file to move"
      },
      "destination": {
        "type": "string",
        "description": "Full path of the new location including filename"
      }
    },
    "required": ["source", "destination"]
  }
}
```

---

## LLM Client

Multi-provider, OpenAI-compatible format. Same pattern as Ares `llm.py`.

```ts
const PROVIDERS = {
  opencode: {
    baseUrl: "https://opencode.ai/zen/v1",
    envKey: "OPENCODE_API_KEY",
  },
  openai: {
    baseUrl: "https://api.openai.com/v1",
    envKey: "OPENAI_API_KEY",
  },
};

class LLMClient {
  constructor(provider: string, model: string) { ... }
  async chat(messages: Message[], tools: ToolDefinition[]): Promise<ChatResponse> { ... }
}
```

Uses standard `POST /chat/completions` with `tools` parameter. Provider routing is behind the scenes — agent just calls `llm.chat()`.

---

## System Prompt

```
You are a file organizer. You help users organize files into logical folders.

You have tools to:
- List directories and get file information
- Move files to new locations
- Create folders
- Rename files
- Delete files

When given a directory, analyze the files and organize them sensibly.
Group by: file type, project, date, or whatever makes sense for the content.

When you're finished organizing, call the "done" tool with a summary.
```

---

## CLI Usage

```bash
# Interactive — shows plan, waits for approval
node dist/index.js ~/Downloads

# Autopilot — agent decides and executes
node dist/index.js ~/Downloads --auto

# Dry run — show plan without executing
node dist/index.js ~/Downloads --dry-run

# Specify provider/model
node dist/index.js ~/Downloads --provider opencode --model deepseek-v4-flash-free
```

---

## Config

Minimal config at `~/.file-organizer/config.json`:

```json
{
  "provider": "opencode",
  "model": "deepseek-v4-flash-free"
}
```

Can also be set via CLI flags or environment variables:

```bash
export OPENCODE_API_KEY=xxx
node dist/index.js ~/Downloads --provider opencode
```

---

## Safety

**None.** User is the operator. No allowlists, no blocked extensions, no dry-run default. The agent works on whatever directory you point it at.

The only UX convenience is that the default mode shows the plan before executing — skip with `--auto`.

---

## Dependencies

```json
{
  "dependencies": {
    "chalk": "^5.0",
    "commander": "^12.0"
  },
  "devDependencies": {
    "typescript": "^5.5",
    "@types/node": "^22.0",
    "tsx": "^4.0"
  }
}
```

- **chalk** — colored terminal output
- **commander** — CLI argument parsing
- **typescript + tsx** — TypeScript compilation and dev runner

No heavy frameworks. Minimal dependencies.

---

## Implementation Order

1. **LLM client** — provider config, chat completions call
2. **Tool definitions** — JSON schemas for the LLM
3. **Tool implementations** — filesystem operations
4. **Agent loop** — connect LLM → tools → LLM cycle
5. **CLI** — argument parsing, output formatting
6. **Approval flow** — show plan, wait for user input
