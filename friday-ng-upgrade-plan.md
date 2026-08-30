# friday-ng → Personal AI Harness: Full Upgrade Plan

Goal: turn friday-ng from "working agent loop with a rough TUI" into a
Claude-Code/Codex-grade personal harness — proper tool-call rendering,
a safety-gated execution path, and a `/init`-driven personalization layer
that makes it *your* assistant across every session.

Everything below references real files already in the repo. Nothing here
requires a rewrite — it's additive, in the same architecture you already
have (typed interfaces, pluggable everything).

---

## 0. Current-state audit (why this plan looks the way it does)

Things you built that are **good and should stay untouched**:
- `event-stream.ts` — clean async-iterable push model, no buffering. Keep it.
- `agent-loop.ts` — the turn/tool-call state machine is solid and already
  supports sequential/parallel execution, abort, steering, follow-ups.
- `tui.ts` — grapheme-aware wrapping, diff-based frame rendering, boxed
  messages. The rendering *primitives* are good; what's missing is using
  them for tool calls specifically.
- `permissions.ts`, `settings.ts` (`confirmToolCalls`) — fully designed,
  **currently wired to nothing**. This is free value sitting unused.

Things that are real gaps, not just polish:
- No confirmation/deny path before a tool executes, despite the policy
  engine existing (`permissions.ts` is dead code).
- `AgentTool.execute` has no channel for partial/streaming output — a
  `bash` call is invisible until it fully completes.
- No personalization layer at all — every session starts from zero
  context about who's talking to it.
- No checkpoint/undo for file edits.
- `ImageContent` is a real type used by every provider adapter, but no
  tool ever produces one.

---

## Phase 1 — Wire up what already exists (safety + settings)

**Why first:** zero new concepts, pure connection of code you already
wrote, and it closes the "agent can rm -rf without asking" gap before you
start relying on this daily.

### 1.1 Wire `beforeToolCall` → `permissions.ts`
Files: `src/cli.ts`, `src/tui.ts`

- Add a `confirm(prompt: string): Promise<boolean>` method to `Tui`
  (small modal overlay, same pattern as the existing `/model` selector —
  reuse `handleSelectorInput`'s input-capture approach but for a y/n).
- In `cli.ts`, pass a real `beforeToolCall` to `new Agent({...})`:
  ```ts
  import { DEFAULT_POLICY, decide } from "./permissions.ts";

  beforeToolCall: async ({ toolCall, args }) => {
    if (settings.get("confirmToolCalls") !== true) return undefined;
    const tool = cliTools.find((t) => t.name === toolCall.name)!;
    const { mode, reason } = decide(DEFAULT_POLICY, { tool, args });
    if (mode === "deny") return { block: true, reason };
    if (mode === "ask") {
      const ok = await tui.confirm(`Run ${toolCall.name}(${JSON.stringify(args)})?`);
      if (!ok) return { block: true, reason: "declined by user" };
    }
    return undefined;
  },
  ```
- Extend `DEFAULT_POLICY` with an explicit `bash: "ask"` and a rule
  denying the existing `SHELL_DANGEROUS_PATTERNS` regexes at the policy
  layer too (defense in depth — right now that check only lives inside
  `bashTool.execute`).

### 1.2 Surface `confirmToolCalls` in `/settings`
File: `src/commands/builtin.ts` — no code change needed, it already
reads/writes via `SettingsStore`; just confirm `/settings confirmToolCalls true`
round-trips (it will, once 1.1 reads it).

**Deliverable:** agent now asks before running anything not explicitly
allow-listed, with a policy config you already built.

---

## Phase 2 — Tool-call rendering overhaul

**Why second:** this is the visual transformation you asked for, and it's
independent of personalization — ship it and *feel* the difference
immediately.

### 2.1 Boxed tool calls (replace flat "● bash …" lines)
File: `src/tui.ts`

Add a box renderer parallel to the existing `renderBox()`:
```ts
function renderToolBox(
  name: string, argsLine: string, status: "running" | "done" | "error",
  body: string[], width: number,
): string[] {
  const color = status === "error" ? RED : status === "running" ? YELLOW : GREEN;
  const icon = status === "running" ? "●" : status === "error" ? "✗" : "✓";
  const maxInner = Math.max(10, width - 4);
  const lines = [`${color}╭─ ${icon} ${BOLD}${name}${RESET} ${DIM}${argsLine}${RESET}`.slice(0, width)];
  for (const b of body.slice(0, 12)) {
    lines.push(`${color}│${RESET} ${truncateToWidth(b, maxInner)}`);
  }
  if (body.length > 12) lines.push(`${color}│${RESET} ${DIM}… ${body.length - 12} more lines (Ctrl+O to expand)${RESET}`);
  lines.push(`${color}╰─${RESET}`);
  return lines;
}
```
Replace the current `tool_execution_start` / `tool_execution_end` handling
in `handleEvent()`: push a `running` box on start, mutate the same
history entry in place on end (same technique the code already uses for
replacing the "in-flight tool" line, just producing multiple lines
instead of one).

Add a `Ctrl+O` keybinding to toggle "expanded" mode per tool box (store
an `expanded: Set<string>` keyed by toolCallId in `Tui`, cap body at 12
lines when collapsed, full length when expanded).

### 2.2 Diffs for `edit` / `write`
Files: `src/tools/shell.ts`, `src/tui.ts`

- `editTool.execute`: put `{ oldText, newText, path }` into `details`
  instead of discarding them.
- `writeTool.execute`: if the file existed before, read the old content
  first and include it in `details` the same way (new-file writes just
  show all-green).
- Add a diff renderer:
  ```ts
  function renderDiffLines(oldText: string, newText: string, width: number): string[] {
    const oldLines = oldText.split("\n");
    const newLines = newText.split("\n");
    const out: string[] = [];
    for (const l of oldLines) if (!newLines.includes(l)) out.push(truncateToWidth(`${RED}- ${l}${RESET}`, width));
    for (const l of newLines) if (!oldLines.includes(l)) out.push(truncateToWidth(`${GREEN}+ ${l}${RESET}`, width));
    return out;
  }
  ```
  (Line-set diff, not a full Myers/LCS diff — good enough for chat display,
  upgrade later only if you notice bad hunks on reordered lines.)
- Feed this into `renderToolBox`'s `body` for `edit`/`write` results.

### 2.3 Streaming `bash` output (live, not just after exit)
Files: `src/types.ts`, `src/tools/shell.ts`, `src/agent-loop.ts`, `src/tui.ts`

This is the prerequisite that makes 2.1's `running` box actually show
something instead of just a spinner.

- `types.ts`: extend `AgentTool.execute` with an optional 4th param:
  ```ts
  execute: (
    toolCallId: string,
    params: Static<TParameters>,
    signal?: AbortSignal,
    onProgress?: (chunk: string) => void,
  ) => Promise<ToolResult>;
  ```
- `types.ts`: add a new `AgentEvent` variant:
  ```ts
  | { type: "tool_execution_progress"; toolCallId: string; chunk: string }
  ```
- `agent-loop.ts`: in `executePreparedToolCall`, pass an `onProgress`
  callback down that calls `emit({ type: "tool_execution_progress", ... })`.
- `tools/shell.ts`: `runShell` already has `child.stdout.on("data", ...)`
  — call the passed-in `onChunk` there in addition to buffering.
- `tui.ts`: on `tool_execution_progress`, append the chunk to the running
  box's body and re-render (coalesced via the existing `scheduleRender`).

### 2.4 Regex-level syntax highlighting for code blocks
File: `src/markdown.ts`

```ts
const KEYWORDS: Record<string, RegExp> = {
  ts: /\b(const|let|var|function|return|if|else|for|while|import|export|async|await|class|interface|type|extends|implements|new|this|typeof|as|from)\b/g,
  js: /\b(const|let|var|function|return|if|else|for|while|import|export|async|await|class|new|this|typeof)\b/g,
  py: /\b(def|return|if|elif|else|for|while|import|from|class|as|with|try|except|async|await|lambda)\b/g,
  bash: /\b(if|then|fi|for|do|done|echo|export|function)\b/g,
};

function highlightLine(line: string, lang: string): string {
  const kw = KEYWORDS[lang];
  let out = line;
  out = out.replace(/(["'`])(?:(?!\1).)*\1/g, (m) => `${FG_GREEN}${m}${RESET}${BG_DIM}`);
  out = out.replace(/\/\/.*$|#.*$/g, (m) => `${DIM}${m}${RESET}${BG_DIM}`);
  if (kw) out = out.replace(kw, (m) => `${FG_YELLOW}${m}${RESET}${BG_DIM}`);
  return out;
}
```
Call from `renderMarkdownColored` in the code-block branch instead of the
current flat per-language color.

### 2.5 Clickable links (OSC 8)
File: `src/markdown.ts` / `src/tui.ts`

```ts
function osc8Link(url: string, label = url): string {
  return `\x1b]8;;${url}\x1b\\${label}\x1b]8;;\x1b\\`;
}
```
Apply to `\bhttps?:\/\/\S+\b` matches inside `renderAssistantText`.

### 2.6 (Optional, low priority) Markdown tables
File: `src/markdown.ts` — detect a `---|---` separator row in
`renderMarkdown` and box-draw a table. Skip unless you notice the model
outputting tables often; effort/value ratio is worse than 2.1–2.3.

**Deliverable:** tool calls render as live, collapsible, colored boxes
with real diffs — the "holy crap this looks legit" moment.

---

## Phase 3 — Personalization layer (`/init` and beyond)

### 3.1 Global profile
New file: `src/profile.ts`
```ts
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

export function profileDir(): string {
  return process.env.FRIDAY_NG_CONFIG_DIR ?? path.join(os.homedir(), ".friday-ng");
}
export function profilePath(): string {
  return path.join(profileDir(), "PROFILE.md");
}
export async function loadProfile(): Promise<string | undefined> {
  try { return await fs.readFile(profilePath(), "utf8"); } catch { return undefined; }
}
```

### 3.2 `/init` slash command
File: `src/commands/builtin.ts`
```ts
export function makeInitCommand(deps: { hasProfile: () => Promise<boolean>; dir: string }): SlashCommand {
  return {
    name: "/init",
    description: "Set up or update your personal profile (name, preferences, current projects).",
    run: async (): Promise<SlashCommandResult> => {
      const exists = await deps.hasProfile();
      const prompt = exists
        ? `Read PROFILE.md (root: "${deps.dir}") with the read tool, summarize it for me, ask what's changed, then rewrite it with the write tool (same root) once I confirm.`
        : `Interview me one question at a time — name, role, how I like explanations (terse vs detailed), tone, current projects, anything else worth remembering. When done, write a concise Markdown PROFILE.md via the write tool with root "${deps.dir}" and path "PROFILE.md".`;
      return { submitFollowUp: prompt };
    },
  };
}
```
**Gotcha:** `writeTool`/`readTool` restrict to `root` (default `cwd`) via
`path-safety.ts`. Must pass `root: profileDir()` explicitly, or the
absolute home-dir path gets rejected by `isPathInside`.

### 3.3 Inject profile into every session
File: `src/cli.ts`
```ts
async function buildSystemPrompt(modelId: string): Promise<string> {
  const profile = await loadProfile();
  const projectFile = await loadProjectFile(); // see 3.4
  return (
    `You are friday-ng, a next-generation AI assistant with instant token streaming. ` +
    `Current model: ${modelId}. Be helpful, concise, and friendly.` +
    (profile ? `\n\n## About the user\n${profile}\n` : "") +
    (projectFile ? `\n\n## About this project\n${projectFile}\n` : "") +
    buildEnvironmentContext()
  );
}
```
Both call sites (`main()`, `runRepl()`) need `await` added since this
becomes async.

### 3.4 Project-level context (`AGENTS.md`)
Same loader pattern as 3.1 but `path.join(process.cwd(), "AGENTS.md")` —
global profile = who you are, project file = what *this* codebase is.
Lets you have per-repo context without polluting the global profile.

### 3.5 First-run nudge
File: `src/cli.ts` — in `runRepl()`, if `!(await loadProfile())`, append
a one-line system message via `tui.appendSystemLine("No profile yet — run /init to personalize friday-ng.")`.

### 3.6 `/profile` quick view/edit
File: `src/commands/builtin.ts` — thin command that just cats
`PROFILE.md` via `tui.appendSystemLine`, plus `/profile edit <text>`
for quick one-line appends without a full `/init` interview.

**Deliverable:** run `/init` once → friday-ng remembers who you are,
your preferences, and your current projects, forever, across every
session and every directory you run it from.

---

## Phase 4 — Harness capability upgrades

These are gaps found by reading the codebase, not aesthetic choices.
Do these after Phase 1–3 are stable; each is independent, do in any order.

### 4.1 Todo / plan tool
New tool + small `AgentState` extension. Model calls a `todoWrite` tool
with a list of `{ text, status: "pending"|"in_progress"|"done" }`; TUI
pins it as a persistent checklist box above the transcript (not part of
scrollback — always visible while active). This is most of why long
multi-step Claude Code / Codex sessions feel "under control."

### 4.2 Checkpoint / undo for file edits
File: new `src/checkpoints.ts` + hook into `beforeToolCall`.
Before any `write`/`edit` tool call, copy the target file to
`~/.friday-ng/checkpoints/<sessionId>/<n>-<basename>`. Add a `/undo`
slash command that restores the most recent checkpoint. Cheap insurance
against the model making a bad edit.

### 4.3 Multi-file / multi-hunk atomic edit
New tool `multiEditTool` in `tools/shell.ts`: takes
`edits: {path, oldText, newText}[]`, validates every `oldText` exists
exactly once in its file *before* writing anything, then applies all or
none. Prevents partial-patch corruption on multi-step refactors.

### 4.4 Image input support
Files: `src/types.ts` (`Model.input` already supports `"image"` in the
type, just not populated), `src/tools/shell.ts` (`readTool`).
Teach `readTool` to detect image extensions (`.png`, `.jpg`, `.webp`)
and return an `ImageContent` block (base64) instead of `fs.readFile(...,
"utf8")`. Set `input: ["text", "image"]` on models that support it
(Anthropic, Google, most OpenAI-compatible ones).

### 4.5 Defense-in-depth for dangerous shell patterns
Move `SHELL_DANGEROUS_PATTERNS` matching into `permissions.ts` as
policy rules too (currently only checked inside `bashTool.execute`) so
it applies even if a future tool wraps shell execution differently.

**Deliverable:** plan visibility, undo safety net, atomic multi-file
edits, and image understanding — the capability gaps that separate a
"chat with tool access" from an actual coding harness.

---

## Suggested build order

| Order | Item | Files touched | Effort |
|---|---|---|---|
| 1 | 1.1 permission gate | cli.ts, tui.ts, permissions.ts | S |
| 2 | 2.1 boxed tool calls | tui.ts | M |
| 3 | 2.2 diffs | shell.ts, tui.ts | S |
| 4 | 3.1–3.3 `/init` + profile injection | profile.ts (new), commands/builtin.ts, cli.ts | M |
| 5 | 2.3 streaming bash output | types.ts, shell.ts, agent-loop.ts, tui.ts | M |
| 6 | 2.4 syntax highlight, 2.5 links | markdown.ts | S |
| 7 | 3.4–3.6 project file, `/profile`, first-run nudge | cli.ts, commands/builtin.ts | S |
| 8 | 4.1 todo/plan tool | new tool + tui.ts | M |
| 9 | 4.2 checkpoints/undo | checkpoints.ts (new), cli.ts | M |
| 10 | 4.3 multi-edit, 4.4 images, 4.5 policy hardening | shell.ts, types.ts, permissions.ts | M |

S = one sitting, M = half a day, roughly. Items 1–4 alone will make it
feel like a different piece of software; everything after is compounding
polish and safety.
