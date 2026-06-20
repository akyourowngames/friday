# Interactive Terminal Panel — Design Spec

**Date:** 2026-06-20
**Status:** Draft
**Author:** Claude (brainstorming session)

---

## Overview

Add a persistent interactive terminal panel to the Ares desktop app — a real shell (bash/zsh/PowerShell) running inside the Electron window via node-pty + xterm.js. Ares can use it as a tool (agent-driven commands with visible output), and the user can type directly in it. A send-to-chat feature lets users select terminal output and reference it in chat with `@terminal:15` syntax.

## Current State

- Electron desktop app (React 19 + Vite + Zustand + WebSocket bridge)
- `run_command` tool executes one-shot commands (non-interactive, output captured to chat only)
- `run_code` tool executes Python code (same pattern)
- No terminal UI, no xterm.js, no PTY support
- Dark glassmorphism theme with split-panel-ready layout

## Design Goals

1. **Real interactive shell** — full PTY with colors, cursor movement, tab completion, interactive programs (vim, top, htop)
2. **Agent-driven commands** — Ares can send commands to the terminal, user sees output in real-time
3. **User-driven typing** — user can type directly in the terminal, like a real terminal
4. **Send-to-chat** — select text in terminal, Ctrl+Enter sends `@terminal:15` reference to chat
5. **Toggleable panel** — show/hide with Ctrl+` or button click
6. **Auto-routing** — Ares decides when to use terminal (interactive) vs run_command (one-shot)

---

## Architecture

Three layers, each with clear responsibilities:

```
┌─────────────────────────────────────────────────┐
│  Electron Renderer (React)                       │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │  Chat Panel   │  │  TerminalPanel (xterm.js)│ │
│  │  (left 60%)   │◄─│  (right 40%)            │ │
│  │               │  │  + TerminalStore (Zustand)│ │
│  └──────────────┘  └──────────┬───────────────┘ │
│                               │ IPC              │
├───────────────────────────────┼─────────────────┤
│  Electron Main Process        │                  │
│  ┌────────────────────────────▼───────────────┐  │
│  │  TerminalManager                            │  │
│  │  - node-pty spawns real shell               │  │
│  │  - manages PTY lifecycle                    │  │
│  │  - bridges IPC ↔ PTY                        │  │
│  └────────────────────────────┬───────────────┘  │
│                               │ PTY              │
├───────────────────────────────┼─────────────────┤
│  OS Shell (bash/zsh/cmd/PS)   │                  │
└───────────────────────────────┘
```

### Why node-pty in Electron Main (not Python backend)

Hermes Desktop uses the same pattern: PTY lives in the Electron main process, not the Python backend. Reasons:

1. **Zero latency** — IPC between main↔renderer is faster than Python→WebSocket→Electron (two hops)
2. **Real PTY** — node-pty creates a true pseudoterminal; programs think they're in a real terminal
3. **Standard pattern** — VS Code, Hyper, Hermes Desktop all use this architecture
4. **Python backend stays stateless** — terminal state lives in Electron, Python just routes commands when Ares needs to use it

### Data Flows

**User types in terminal:**
```
User keystroke → xterm.js.onData → IPC 'terminal:write' → node-pty.write → shell
shell output → node-pty.onData → IPC 'terminal:data' → xterm.js.write → screen
```

**Ares sends command to terminal:**
```
Ares calls terminal_exec tool → Python backend sends WebSocket 'terminal:exec' →
Electron renderer receives → IPC 'terminal:write' → node-pty.write → shell
output streams back through same path → xterm.js renders → user sees it
```

**User sends terminal text to chat:**
```
User selects text in xterm.js → Ctrl+Enter →
TerminalStore captures selection + line numbers →
Composer receives @terminal:15-20 reference chip →
User sends message → backend extracts terminal context
```

---

## Components

### 1. TerminalManager (Electron Main Process)

**File:** `electron-app/src/main/terminal-manager.js`

Manages the PTY process lifecycle. One active terminal session at a time.

```javascript
// TerminalManager class
// Properties:
//   - ptyProcess: IPty | null (the active PTY)
//   - sessionId: string (UUID)
//
// Methods:
//   - create(): spawns PTY, returns sessionId
//   - write(data: string): writes to PTY
//   - resize(cols: number, rows: number): resizes PTY
//   - kill(): kills PTY process
//   - isActive(): boolean
//
// Events emitted to renderer:
//   - 'terminal:data' — PTY output chunk
//   - 'terminal:exit' — PTY process exited (code, signal)
//   - 'terminal:create' — PTY created successfully
//
// IPC handlers registered:
//   - ares:terminal:create → create(), returns { sessionId }
//   - ares:terminal:write  → write(data)
//   - ares:terminal:resize → resize(cols, rows)
//   - ares:terminal:kill   → kill()
```

**Shell selection (cross-platform):**
- Windows: `powershell.exe` (or `cmd.exe` if user prefers — configurable later)
- macOS: `zsh` (default) or `bash`
- Linux: `bash` (default) or `zsh`

**PTY options (from node-pty docs):**
```javascript
pty.spawn(shell, [], {
  name: 'xterm-256color',
  cols: 80,
  rows: 24,
  cwd: process.env.HOME || process.env.USERPROFILE,
  env: { ...process.env, TERM: 'xterm-256color' },
})
```

**Key detail from Hermes Desktop research:** On Windows, native PTY requires WSL2 for POSIX compatibility. However, node-pty supports Windows via winpty/conpty, so we can use PowerShell/cmd directly without WSL2. node-pty's Windows backend uses ConPTY (Windows 10 1809+) which provides true PTY emulation.

### 2. Preload Script Extension

**File:** `electron-app/src/main/preload.js` (modify existing)

Add terminal API to `window.aresDesktop`:

```javascript
terminal: {
  create: () => ipcRenderer.invoke('ares:terminal:create'),
  write: (data) => ipcRenderer.send('ares:terminal:write', data),
  resize: (cols, rows) => ipcRenderer.send('ares:terminal:resize', cols, rows),
  kill: () => ipcRenderer.send('ares:terminal:kill'),
  onData: (cb) => ipcRenderer.on('terminal:data', (_, data) => cb(data)),
  onExit: (cb) => ipcRenderer.on('terminal:exit', (_, code) => cb(code)),
  onCreate: (cb) => ipcRenderer.on('terminal:create', (_, info) => cb(info)),
  removeAllListeners: () => {
    ipcRenderer.removeAllListeners('terminal:data');
    ipcRenderer.removeAllListeners('terminal:exit');
    ipcRenderer.removeAllListeners('terminal:create');
  },
}
```

### 3. TerminalStore (Zustand)

**File:** `electron-app/src/renderer/stores/terminalStore.js`

```javascript
// State:
//   isOpen: boolean        — panel visibility (default: false)
//   isConnected: boolean   — PTY alive (default: false)
//   sessionId: string|null — active session ID
//   lastSelection: { text: string, startLine: number, endLine: number } | null
//   commandHistory: string[] — recent commands for @terminal context
//
// Actions:
//   togglePanel()        — show/hide terminal panel
//   openPanel()          — show terminal, create PTY if needed
//   closePanel()         — hide terminal, kill PTY
//   createTerminal()     — request PTY from main process
//   writeToTerminal(data) — send input to PTY
//   resizeTerminal(cols, rows) — resize PTY
//   setSelection(text, startLine, endLine) — update last selection
//   sendSelectionToChat() — format selection as @terminal:N reference
//   addCommandToHistory(cmd) — track recent commands
```

### 4. TerminalPanel Component

**File:** `electron-app/src/renderer/components/TerminalPanel.jsx`

Renders xterm.js in the right panel. Key behaviors:

- **On mount:** Creates xterm.js `Terminal` instance, loads `FitAddon` + `WebLinksAddon`, requests PTY creation from main process
- **On PTY data:** `term.write(data)` — renders output in terminal
- **On user input:** `term.onData(data)` → IPC write to PTY
- **On resize:** `fitAddon.fit()` recalculates → sends new cols/rows to PTY
- **Selection tracking:** `term.onSelectionChange()` → captures selected text + line numbers → stores in TerminalStore
- **Keyboard shortcut:** Ctrl+Enter → calls `terminalStore.sendSelectionToChat()`
- **On unmount:** kills PTY, removes IPC listeners

**xterm.js setup:**
```javascript
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'

const term = new Terminal({
  theme: {
    background: '#0e0e12',      // matches Ares dark theme
    foreground: '#e0e0e0',
    cursor: '#6B5B95',           // matches Ares accent
    selectionBackground: '#6B5B9540',
    // ... full color palette
  },
  fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", monospace',
  fontSize: 14,
  cursorBlink: true,
  cursorStyle: 'bar',
  scrollback: 10000,
  allowProposedApi: true,
})
```

### 5. Send-to-Chat Feature

**How it works:**

1. User selects text in terminal (mouse drag or shift+arrow keys)
2. `term.onSelectionChange()` fires → TerminalStore captures:
   - `text`: the selected string
   - `startLine`: first visible line number (from buffer)
   - `endLine`: last visible line number
3. User presses Ctrl+Enter (configurable shortcut)
4. TerminalStore formats the reference: `@terminal:{startLine}-{endLine}`
5. ChatStore receives the reference and inserts it into the Composer as a chip/badge
6. User can add more text before sending
7. When message is sent, backend receives the `@terminal:N-M` reference
8. Backend extracts the relevant terminal history lines and includes them as context for Ares

**Composer chip UI:**
```
┌─────────────────────────────────────────┐
│ @terminal:15-20 ×  │ tell me about this │
└─────────────────────────────────────────┘
```

The chip is styled like the existing file/memory cards — a small pill with the terminal reference and an × to remove it.

**Backend handling in `server.py`:**
- New message type: `chat` with `terminal_refs` field
- When processing a message with `@terminal:N-M`, extract lines N-M from terminal history
- Include as context in the agent's message (like how file references work)

### 6. Layout — Split Panel

**File:** `electron-app/src/renderer/App.jsx` (modify)

```jsx
// Main layout:
<div className="app-layout">
  {isOpen ? (
    <>
      <div className="chat-panel" style={{ width: `${splitPos}%` }}>
        <Sidebar />
        <ChatArea />
      </div>
      <div className="split-divider" onMouseDown={startDrag} />
      <div className="terminal-panel" style={{ width: `${100 - splitPos}%` }}>
        <TerminalHeader onClose={closePanel} />
        <TerminalPanel />
      </div>
    </>
  ) : (
    <div className="chat-panel full-width">
      <Sidebar />
      <ChatArea />
    </div>
  )}
</div>
```

**Divider behavior:**
- Draggable divider between chat and terminal
- Default split: 60% chat / 40% terminal
- Min widths: chat 30%, terminal 20%
- Divider shows `⋮` handle on hover

**Toggle:** Ctrl+` (backtick) toggles terminal panel visibility

### 7. WebSocket Message Types

**File:** `ares/server.py` (modify)

New message types for agent-terminal communication:

```python
# Ares sends command to terminal
{
    "type": "terminal:exec",
    "command": "npm run dev",
    "wait": true  # optional: wait for command to complete
}

# Frontend responds with result
{
    "type": "terminal:exec_result",
    "output": "...",  # captured output
    "exit_code": 0
}

# Frontend streams terminal output to backend (for Ares monitoring)
{
    "type": "terminal:output",
    "data": "latest output chunk"
}
```

### 8. `terminal_exec` Tool

**File:** `ares/tools.py` (modify)

New tool definition:

```python
_tool(
    "terminal_exec",
    "Send a command to the interactive terminal and optionally wait for completion. Use for commands that need visible output or interactive shell features. For simple one-shot commands, prefer run_command.",
    {
        "command": {"type": "string", "description": "Shell command to execute in the terminal"},
        "wait": {"type": "boolean", "description": "Wait for command to complete (default true)"},
        "timeout": {"type": "integer", "description": "Max seconds to wait (default 30)"},
    },
    required=["command"],
)
```

**Handler:** Sends `terminal:exec` via WebSocket, waits for `terminal:exec_result` response. Returns output to Ares.

### 9. Auto-Routing Logic

Ares decides which tool to use based on command characteristics:

| Scenario | Tool Used | Why |
|----------|-----------|-----|
| Simple command (`echo hello`, `ls`) | `run_command` | One-shot, output in chat is fine |
| Command with output to analyze | `run_command` | Captured output easier for Ares to process |
| Interactive command (`npm init`, `git rebase -i`) | `terminal_exec` | Needs user interaction or visible progress |
| Long-running process (`npm start`, `docker compose up`) | `terminal_exec` | User needs to see live output |
| User says "show me" or "in the terminal" | `terminal_exec` | Explicit request for visibility |
| Command that might need user intervention | `terminal_exec` | User can type in terminal if needed |

This routing is handled by the LLM's tool selection — the tool descriptions guide it to the right choice. No hard-coded routing logic needed.

---

## Safety Model

- PTY runs with the user's permissions (same as opening a real terminal)
- No command restrictions or allowlists
- Terminal sessions persist until user closes the panel
- Command history kept in memory (last 10,000 lines) for @terminal references
- No confirmation prompts — user has full control

**Rationale:** Same as the power tools — Ares is a local personal assistant. The user can always close the terminal panel or kill the process.

---

## Dependencies

| Package | Used By | Installed? |
|---------|---------|-----------|
| `node-pty` | Electron main process | ❌ Needs install |
| `@xterm/xterm` | Renderer (terminal UI) | ❌ Needs install |
| `@xterm/addon-fit` | Renderer (auto-resize) | ❌ Needs install |
| `@xterm/addon-web-links` | Renderer (clickable links) | ❌ Needs install |

**Install (in electron-app/):**
```bash
npm install node-pty
npm install @xterm/xterm @xterm/addon-fit @xterm/addon-web-links
```

**Build consideration:** node-pty is a native module that must be rebuilt for Electron's Node.js ABI. Use `electron-rebuild` or configure in `electron-builder.yml`:
```yaml
npmRebuild: true
```

---

## Error Handling

| Scenario | Response |
|----------|----------|
| PTY spawn fails | Show error toast, terminal panel shows "Failed to start terminal" |
| PTY exits unexpectedly | Show exit code, offer restart button |
| node-pty not available | Disable terminal feature, show "Terminal not available" in panel |
| WebSocket disconnect while terminal open | Terminal continues working (PTY is local to Electron), reconnect syncs state |

---

## File Structure

```
electron-app/
├── src/
│   ├── main/
│   │   ├── index.js              # MODIFY — register terminal IPC handlers
│   │   ├── terminal-manager.js   # NEW — TerminalManager class
│   │   └── preload.js            # MODIFY — add terminal API
│   └── renderer/
│       ├── App.jsx               # MODIFY — split panel layout
│       ├── stores/
│       │   └── terminalStore.js  # NEW — terminal state management
│       ├── components/
│       │   ├── TerminalPanel.jsx  # NEW — xterm.js terminal component
│       │   ├── TerminalHeader.jsx # NEW — terminal panel header bar
│       │   └── Composer.jsx       # MODIFY — @terminal reference chips
│       └── styles/
│           └── components.css     # MODIFY — terminal panel styles
├── package.json                  # MODIFY — add dependencies

ares/
├── tools.py                      # MODIFY — add terminal_exec tool
└── server.py                     # MODIFY — add terminal WebSocket messages
```

---

## Testing Plan

1. **Unit tests for TerminalManager** — spawn, write, resize, kill, exit handling
2. **Unit tests for TerminalStore** — toggle, selection capture, send-to-chat formatting
3. **Component tests for TerminalPanel** — mount, render, input handling
4. **Integration tests** — end-to-end: spawn terminal → send command → capture output → send to chat
5. **Manual tests** — type in terminal, run interactive programs, verify colors/rendering
6. **Cross-platform** — verify on Windows (PowerShell), macOS (zsh), Linux (bash)

---

## Out of Scope

- Multiple terminal tabs/panes (single terminal only)
- Terminal session persistence across app restarts
- Terminal themes customization (hardcoded dark theme matching Ares)
- Split terminal panes
- Terminal search (find in output)
- Sudo password caching (user types password manually if needed)

---

## Sources

- [node-pty GitHub (Microsoft)](https://github.com/microsoft/node-pty)
- [Electron Forge + node-pty integration guide](https://thomasdeegan.medium.com/electron-forge-node-pty-9dd18d948956)
- [xterm.js documentation](https://xtermjs.org/)
- [Hermes Agent Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture/)
- [Hermes Desktop GitHub](https://github.com/fathah/hermes-desktop)
- [Hermes Agent Desktop Guide](https://www.digitalapplied.com/blog/hermes-agent-desktop-app-complete-guide-2026)
- [Hermes Agent terminal_tool.py](https://github.com/nousresearch/hermes-agent/blob/main/tools/terminal_tool.py)
- [FlyEnv xterm integration (DeepWiki)](https://deepwiki.com/xpf0000/FlyEnv/3.5-terminal-integration-with-xterm)
- [Stack Overflow: xterm.js + Electron + real command prompt](https://stackoverflow.com/questions/63390143/how-do-i-connect-xterm-jsin-electron-to-a-real-working-command-prompt)
- [ATerm AI-Powered Terminal](https://aterm.io/)
