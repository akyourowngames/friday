# friday-ng

Next-generation AI coding harness. Single Node.js process, instant token
streaming, pluggable everything.

The same agent core that powers the interactive Pi-style TUI also drives
one-shot CLI prompts and pluggable programmatic integrations. The harness is
designed around a few key principles:

- **Harness-shaped architecture** — every long-lived object (`Agent`, `Tui`,
  `Session`, `Settings`, slash-command registry, tool registry) is composed
  from small typed interfaces. Hosts wire them together; nothing is
  hard-coded.
- **Streaming-first** — `Agent` consumes an async `StreamFn` and emits a typed
  `AgentEvent` stream. The CLI, the TUI, the session recorder, and any
  external host all subscribe to the same stream.
- **Pluggable everything** — providers, tools, slash commands, settings keys,
  and hooks are all extension points.

## What's in the harness

| Subsystem | File(s) | What it does |
|---|---|---|
| **Agent core** | `src/agent.ts`, `src/agent-loop.ts`, `src/event-stream.ts` | State machine + event stream. `Agent` drives the loop, manages messages, calls tools, supports abort. |
| **Tools** | `src/tools.ts`, `src/tools/shell.ts` | `read`, `write`, `edit`, `bash`, `glob`, `grep`, `websearch`. Each tool has a `TypeBox` schema, an `isReadOnly` flag, and a path-policy check. |
| **Sessions** | `src/sessions.ts` | JSONL-per-message sessions, with `createSession` / `loadSession` / `listSessions` / `deleteSession` / `recordMessage`. |
| **Compaction** | `src/compaction.ts` | Token-budget transformer that drops old tool results when the transcript is too long. Pluggable via `transformContext` on the agent. |
| **Settings** | `src/settings.ts` | Namespaced persisted settings (UI, model, harness). Hot-reload via `replaceConfig`. |
| **Slash commands** | `src/slash-commands.ts`, `src/commands/builtin.ts` | Registry of typed `SlashCommand`s + 14 built-ins (`/help`, `/model`, `/cost`, etc). |
| **Permissions** | `src/permissions.ts` | Allow/deny/ask policy with glob + regex pattern matching, per-tool and per-rule. |
| **Lifecycle hooks** | `src/hooks.ts` | `pre_tool_use`, `post_tool_use`, `pre_user_message`, `post_assistant_message`, `pre_model_call`, `post_model_call`, `turn_end` — each with veto/transform. |
| **Extension loader** | `src/extension-loader.ts` | Auto-discovers and runs `~/.friday-ng/extensions/*.js` modules that can register commands, tools, and hooks. |
| **Retry** | `src/retry.ts` | Wraps a `StreamFn` with exponential-backoff retry for transient errors (overloaded, 5xx, 429, network). |
| **TUI** | `src/tui.ts` | Raw-mode Pi-style REPL with input history, multiline (shift+enter), command palette, model picker, status line, scroll, and markdown-aware rendering. |
| **Console renderer** | `src/console-renderer.ts` | Token-by-token console rendering with optional color. |
| **Markdown** | `src/markdown.ts` | Tiny dependency-free markdown subset (code, bold, italic, links, lists). |
| **Providers** | `src/providers/*.ts` | OpenAI, Anthropic, Google, Ollama, Groq, OpenRouter, DeepSeek, Mistral, Together, Kilo, freecc, plus a `faux` mock. |
| **Config** | `src/config.ts` | Persisted `~/.friday-ng/config.json` with API keys, last model, recent sessions, last session id. |
| **Console setup** | `src/console-setup.ts` | Windows console UTF-8 + VT helpers (`--setup-utf8` / `--revert-utf8`). |

## Built-in slash commands

- `/help [cmd]` — list every command; or get detailed help for one.
- `/model [name]` — show current model; or pick a different one (with picker).
- `/models` — list available models for the current provider.
- `/provider [name]` — show current provider; or list available providers.
- `/tools` — list the tool names available in the current session.
- `/settings [key] [value]` — read or write a setting.
- `/reload` — re-read config from disk.
- `/clear` — wipe the in-memory chat history.
- `/compact` — force a compaction pass.
- `/cost` — show last-turn token usage and running totals.
- `/usage` — show cumulative token usage and limits.
- `/sessions` — list saved sessions.
- `/exit`, `/quit` — leave the TUI.

## CLI flags

```
  --provider <id>        Provider to use (default: openai, or saved)
  --model <name>         Model name (skip picker)
  --api-key <key>        API key (skip prompt, don't save)
  --list-providers       Print providers and exit
  --list-models          Print available models for the selected provider
  --no-config            Don't save API key or model
  --force-key            Re-prompt for API key even if saved
  --repl, -i             Interactive TUI
  --setup-utf8           Make UTF-8 + VT the Windows default
  --revert-utf8          Undo --setup-utf8
  --utf8-status          Print current console codepage + VT state
  --resume <id>          Resume a saved session by id
  --list-sessions        Print every saved session and exit
  --delete-session <id>  Delete a saved session and exit
  --help, -h             Show help
```

## Built-in tools

- `read(path, limit?, offset?)` — read a file with optional head/offset.
- `write(path, content)` — write a file (creates parents).
- `edit(path, old_string, new_string, replace_all?)` — surgical in-place edit.
- `bash(command, timeout?)` — run a shell command (with a 30s default timeout).
- `glob(pattern, path?)` — find files by pattern, returning sorted paths.
- `grep(pattern, path?, glob_filter?, max_results?)` — search file contents.
- `websearch(query, numResults?)` — DuckDuckGo HTML search.

All file/shell tools honour a `workingDir` and refuse to leave it unless the
host opts in. Read-only tools are tagged with `isReadOnly: true` so hosts can
auto-approve them.

## Quick start

```bash
npm install
npm run build
node dist/cli.js --list-providers
node dist/cli.js "What is 2+2?" --provider openai
node dist/cli.js -i --provider anthropic
node dist/cli.js -i --provider faux       # smoke test, no key needed
```

## Tests

```bash
npm test
```

The suite covers agent state machine, tool execution, sessions, compaction,
settings, slash-command registry, built-in commands, TUI rendering, markdown,
permissions, hooks, extensions, retry, and provider smoke tests against
Ollama (when available). 290+ tests, all green.

## Layout

```
src/
  agent.ts                Agent class (event stream, tool loop, abort)
  event-stream.ts         AgentEvent types + helpers
  agent-loop.ts           Pure agent state machine (testable without I/O)
  cli.ts                  Entry point: parses argv, picks provider, runs REPL
  tui.ts                  Pi-style raw-mode REPL
  interactive.ts          Model picker + provider setup
  config.ts               Persisted config + API key storage
  settings.ts             Persisted named settings (UI, model, harness)
  console-setup.ts        Windows console UTF-8 helpers
  console-renderer.ts     Token-by-token console rendering
  markdown.ts             Tiny dependency-free markdown subset
  sessions.ts             JSONL session storage
  compaction.ts           Token-budget transcript compaction
  slash-commands.ts       Slash command registry
  commands/builtin.ts     Built-in slash commands (14)
  permissions.ts          Allow/deny/ask policy with glob+regex patterns
  hooks.ts                Lifecycle hook registry
  extension-loader.ts     Auto-load ~/.friday-ng/extensions/*.js
  retry.ts                Stream-function retry with exponential backoff
  tools.ts                Built-in tool exports
  tools/shell.ts          bash, read, write, edit, glob, grep, websearch
  tools/path-safety.ts    Path-policy helpers
  types.ts                AgentMessage, Model, Tool, ToolResult, etc.
  providers/
    base.ts               ProviderMeta, StreamFn
    registry.ts           findProvider, listProviders
    openai.ts             OpenAI Chat Completions
    anthropic.ts          Anthropic Messages
    google.ts             Google Gemini
    ollama.ts             Ollama (OpenAI-compatible)
    openai-compat.ts      Groq, OpenRouter, DeepSeek, Mistral, Together, Kilo
    freecc.ts             freecc (Anthropic-compatible local proxy)
    faux.ts               Mock provider (no network) — for tests + smoke runs
tests/                    Vitest suite (unit + integration + provider smoke)
examples/extensions/      Example extension modules
```

## Writing an extension

An extension is a small `.js` file with a default export. Drop it into
`~/.friday-ng/extensions/` and friday-ng will discover and run it on startup.
The host exposes:

- `host.commands.register({ name, description, run })` — register a slash command
- `host.hooks.on("pre_tool_use", payload => ...)` — observe or veto a tool call
- `host.getSetting(key)`, `host.setSetting(key, value)` — read/write settings
- `host.log(msg)` — surface diagnostics to the user

See `examples/extensions/hello-world.js` and `examples/extensions/safe-rm.js`
for runnable demos.

## License

MIT — see `LICENSE`.
