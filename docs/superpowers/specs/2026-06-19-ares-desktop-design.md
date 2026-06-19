# Ares Desktop App — Design Spec

**Date:** 2026-06-19
**Status:** Draft
**Scope:** Electron frontend + Python WebSocket backend, Claude Desktop-style UI

---

## 1. Problem Statement

Ares v3 is a powerful terminal-based AI assistant with memory, tasks, soul/profile context, web search, and file tools. But the terminal has inherent limitations:

- No rich visual rendering (images, interactive tool cards, syntax-highlighted code blocks)
- No persistent conversation sidebar with search
- No system tray / background presence
- No drag-and-drop file attachments
- No notification badges for due tasks
- Steep learning curve for non-technical users

The goal: build a desktop app that feels like Claude Desktop — sleek, dark, minimal — while keeping the existing CLI as a first-class interface. Both share the same Python backend and data.

---

## 2. Research Findings

### Hermes Desktop (Nous Research)
- **Stack:** Electron + React + Vite frontend, Python CLI backend spawned as child process
- **Communication:** WebSocket at `/api/ws`, JSON events for chat/streaming/tool activity
- **UI Library:** assistant-ui for chat components (Thread, Composer, Markdown, Syntax Highlighting)
- **State:** nanostores for lightweight global state
- **Sidebar:** Virtualized session list, profile switcher
- **Theming:** CSS variables + Tailwind, dark/light mode
- **Packaging:** electron-builder, ships only Electron shell, installs Python on first launch
- **Key insight:** The desktop app is a thin GUI wrapper — the same Python agent core powers CLI, TUI, web dashboard, and desktop

### Claude Desktop (Anthropic)
- **Stack:** Electron-based (confirmed by community reverse engineering)
- **UI:** Clean dark theme, sidebar with conversation history, streaming responses
- **Tools:** Visual tool use cards with expand/collapse, code blocks with syntax highlighting
- **Pattern:** Minimal chrome, content-first design, generous whitespace

### assistant-ui (React Library)
- **Components:** Thread, Composer, Markdown rendering, Syntax Highlighting (Shiki), Model Selector, Attachment viewers, Mermaid diagrams, Diff Viewer, Context Display (token usage), Voice controls
- **Primitives:** Headless Radix-style building blocks for custom styling
- **Streaming:** Resumable streams, reconnection support
- **Tool UI:** Interactive tool call components with loading/result/interactive states
- **MCP support:** Model Context Protocol for external tool servers

### LobsterAI
- **Stack:** Electron + React 18 + Redux Toolkit + Tailwind CSS + TypeScript
- **Architecture:** Frontend (React/Redux) → Backend (Electron IPC/SQLite) → AI Engine (Claude SDK) → Execution (Node.js or Alpine VM sandbox)
- **Pattern:** Skill-loading system, IM gateway for remote control

### Key Patterns Across All
1. **Thin Electron shell** wrapping a Python/Node backend
2. **WebSocket** for real-time streaming (not REST polling)
3. **Markdown rendering** with syntax highlighting is table stakes
4. **Dark theme** with CSS variables for easy customization
5. **Sidebar** for conversation history with search
6. **Tool results** as expandable cards, not raw text dumps

---

## 3. Architecture Overview

### High-Level

```
┌─────────────────────────────────────────────┐
│              Electron App                    │
│  ┌─────────────┐  ┌──────────────────────┐  │
│  │  Main Process│  │   Renderer Process   │  │
│  │  (Node.js)  │  │   (React + Vite)     │  │
│  │             │  │                      │  │
│  │  - Window   │  │  - Chat UI           │  │
│  │  - Tray     │  │  - Sidebar           │  │
│  │  - IPC      │  │  - Settings          │  │
│  │  - Python   │  │  - Markdown          │  │
│  │    manager  │  │  - Tool cards        │  │
│  └──────┬──────┘  └──────────┬───────────┘  │
│         │    WebSocket        │              │
│         └────────┬────────────┘              │
└──────────────────┼──────────────────────────┘
                   │ ws://localhost:PORT
┌──────────────────┼──────────────────────────┐
│         Python WebSocket Server              │
│         (ares/server.py)                     │
│  ┌───────────────┴────────────────────────┐  │
│  │           Agent Core                    │  │
│  │  - Agent (run_stream)                   │  │
│  │  - MemoryStore, TaskStore               │  │
│  │  - SoulManager, ProfileManager          │  │
│  │  - LLMClient, ToolExecutor              │  │
│  └────────────────────────────────────────┘  │
└──────────────────────────────────────────────┘
```

### Communication Flow

```
User types message
  → Renderer sends JSON via WebSocket
  → Python server receives, calls Agent.run_stream()
  → Agent yields events: content, tool_call, tool_result
  → Server forwards each event as JSON over WebSocket
  → Renderer receives events, updates UI in real-time
```

### Coexistence Model

Both CLI and Desktop share:
- Same `~/.ares/data/ares.db` database
- Same `~/.ares/config.json` configuration
- Same `~/.ares/data/soul.md` and `profile.md`
- Same Python package (`ares`)
- Same Agent, Memory, Task, Soul, Profile classes

The Desktop app adds:
- `ares/server.py` — WebSocket server wrapping the Agent
- `electron-app/` — Electron + React frontend

The CLI continues to work independently. No conflicts.

---

## 4. Component 1: Python WebSocket Server

### Location
`ares/server.py`

### Purpose
Exposes the existing Agent over WebSocket so the Electron frontend can connect.

### Protocol

All messages are JSON. Server → Client events:

```json
// Streaming content token
{"type": "content", "text": "Here's what I found..."}

// Tool call started
{"type": "tool_start", "tool": "web_search", "args": {"query": "Python 3.13"}}

// Tool call result
{"type": "tool_result", "tool": "web_search", "content": "{...json...}"}

// Full response complete
{"type": "response_done", "content": "Full response text...", "tool_calls": [...]}

// Error
{"type": "error", "message": "Something went wrong"}

// Session info
{"type": "session_info", "session_id": 1, "model": "deepseek-v4-flash-free"}
```

Client → Server messages:

```json
// Send a chat message
{"type": "chat", "content": "What do you know about me?", "session_id": 1}

// Switch model
{"type": "set_model", "model": "mimo-v2.5-free"}

// Get context (for /context equivalent)
{"type": "get_context"}

// List sessions
{"type": "list_sessions"}

// Load a session
{"type": "load_session", "session_id": 5}

// Get recent memories
{"type": "get_memories", "limit": 10}

// Get pending tasks
{"type": "get_tasks"}
```

### Implementation

```python
# ares/server.py

import asyncio
import json
import logging
from pathlib import Path

from websockets.asyncio.server import serve

from ares.agent import Agent
from ares.config import load_config
from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.conversations import ConversationStore

logger = logging.getLogger("ares.server")


class AresServer:
    """WebSocket server wrapping the Ares Agent."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.config = load_config()
        self.memory_store = MemoryStore(self.config.data_dir)
        self.task_store = TaskStore(self.config.data_dir)
        self.conversation_store = ConversationStore(self.config.data_dir)
        self.agent: Agent | None = None

    async def start(self):
        """Initialize agent and start WebSocket server."""
        self.agent = Agent(
            memory_store=self.memory_store,
            task_store=self.task_store,
            conversation_store=self.conversation_store,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            config=self.config,
        )
        logger.info(f"Ares server starting on ws://{self.host}:{self.port}")
        async with serve(self.handle_connection, self.host, self.port):
            await asyncio.Future()  # run forever

    async def handle_connection(self, websocket):
        """Handle a single WebSocket connection."""
        session_id = None
        try:
            # Send session info
            await websocket.send(json.dumps({
                "type": "session_info",
                "model": self.config.model,
            }))

            async for raw in websocket:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "chat":
                    await self.handle_chat(websocket, msg)

                elif msg_type == "set_model":
                    self.config.model = msg["model"]
                    self.agent.set_model(msg["model"])
                    await websocket.send(json.dumps({
                        "type": "session_info",
                        "model": self.config.model,
                    }))

                elif msg_type == "get_context":
                    context = self.agent.get_context(
                        msg.get("query", "")
                    )
                    await websocket.send(json.dumps({
                        "type": "context",
                        "content": context,
                    }))

                elif msg_type == "list_sessions":
                    sessions = self.conversation_store.list_sessions()
                    await websocket.send(json.dumps({
                        "type": "sessions",
                        "sessions": sessions,
                    }))

                elif msg_type == "load_session":
                    session_id = msg["session_id"]
                    history = self.conversation_store.get_history(
                        session_id
                    )
                    await websocket.send(json.dumps({
                        "type": "session_history",
                        "session_id": session_id,
                        "messages": history,
                    }))

                elif msg_type == "get_memories":
                    limit = msg.get("limit", 10)
                    memories = self.memory_store.get_recent(limit)
                    await websocket.send(json.dumps({
                        "type": "memories",
                        "memories": memories,
                    }))

                elif msg_type == "get_tasks":
                    tasks = self.task_store.list_pending()
                    await websocket.send(json.dumps({
                        "type": "tasks",
                        "tasks": tasks,
                    }))

        except Exception as e:
            logger.error(f"Connection error: {e}")

    async def handle_chat(self, websocket, msg):
        """Process a chat message and stream the response."""
        content = msg.get("content", "")
        session_id = msg.get("session_id")
        conversation_history = []

        if session_id:
            conversation_history = self.conversation_store.get_history(
                session_id
            )

        try:
            async for event in self.agent.run_stream(
                content, conversation_history
            ):
                if isinstance(event, dict):
                    # Tool call or tool result
                    await websocket.send(json.dumps(event))
                else:
                    # Content text
                    await websocket.send(json.dumps({
                        "type": "content",
                        "text": event,
                    }))

            # Send completion signal
            await websocket.send(json.dumps({
                "type": "response_done",
            }))

        except Exception as e:
            await websocket.send(json.dumps({
                "type": "error",
                "message": str(e),
            }))


def run_server(host="127.0.0.1", port=8765):
    """Entry point for the WebSocket server."""
    server = AresServer(host, port)
    asyncio.run(server.start())
```

### Dependencies

Add to `pyproject.toml`:
```
"websockets>=14.0"
```

---

## 5. Component 2: Electron App Structure

### Location
`electron-app/`

### Directory Layout

```
electron-app/
├── package.json
├── vite.config.js
├── electron-builder.yml
├── src/
│   ├── main/                    # Electron main process
│   │   ├── index.js             # Window creation, Python process manager
│   │   ├── preload.js           # Context bridge for IPC
│   │   └── python-manager.js    # Spawns/monitors Python server
│   ├── renderer/                # React frontend
│   │   ├── index.html
│   │   ├── main.jsx             # React entry point
│   │   ├── App.jsx              # Root component
│   │   ├── components/
│   │   │   ├── Sidebar/
│   │   │   │   ├── Sidebar.jsx
│   │   │   │   ├── SessionList.jsx
│   │   │   │   └── SessionItem.jsx
│   │   │   ├── Chat/
│   │   │   │   ├── ChatArea.jsx
│   │   │   │   ├── MessageList.jsx
│   │   │   │   ├── Message.jsx
│   │   │   │   ├── Composer.jsx
│   │   │   │   └── StreamingIndicator.jsx
│   │   │   ├── Tools/
│   │   │   │   ├── ToolCard.jsx
│   │   │   │   ├── WebSearchCard.jsx
│   │   │   │   ├── FileCard.jsx
│   │   │   │   └── MemoryCard.jsx
│   │   │   ├── Settings/
│   │   │   │   ├── SettingsPanel.jsx
│   │   │   │   └── ModelSelector.jsx
│   │   │   └── common/
│   │   │       ├── MarkdownRenderer.jsx
│   │   │       ├── CodeBlock.jsx
│   │   │       ├── ThinkingIndicator.jsx
│   │   │       └── Avatar.jsx
│   │   ├── hooks/
│   │   │   ├── useWebSocket.js
│   │   │   ├── useMessages.js
│   │   │   └── useSettings.js
│   │   ├── stores/
│   │   │   ├── chatStore.js
│   │   │   ├── sessionStore.js
│   │   │   └── settingsStore.js
│   │   ├── styles/
│   │   │   ├── globals.css
│   │   │   ├── theme.css
│   │   │   └── components.css
│   │   └── lib/
│   │       ├── websocket.js
│   │       └── utils.js
│   └── assets/
│       ├── icon.png
│       └── tray-icon.png
```

### Package.json

```json
{
  "name": "ares-desktop",
  "version": "0.1.0",
  "description": "Ares — Your AI Assistant, now on desktop",
  "main": "src/main/index.js",
  "scripts": {
    "dev": "concurrently \"vite\" \"wait-on http://localhost:5173 && electron .\"",
    "build": "vite build && electron-builder",
    "build:win": "vite build && electron-builder --win",
    "build:mac": "vite build && electron-builder --mac",
    "build:linux": "vite build && electron-builder --linux"
  },
  "dependencies": {
    "react": "^19.0",
    "react-dom": "^19.0",
    "zustand": "^5.0",
    "react-markdown": "^10.0",
    "remark-gfm": "^4.0",
    "rehype-highlight": "^7.0",
    "react-syntax-highlighter": "^15.6",
    "lucide-react": "^0.500"
  },
  "devDependencies": {
    "electron": "^35.0",
    "vite": "^6.0",
    "@vitejs/plugin-react": "^4.0",
    "electron-builder": "^26.0",
    "concurrently": "^9.0",
    "wait-on": "^8.0"
  }
}
```

---

## 6. Component 3: Electron Main Process

### `src/main/index.js`

```javascript
const { app, BrowserWindow, ipcMain, Tray, Menu, nativeTheme } = require('electron');
const path = require('path');
const PythonManager = require('./python-manager');

let mainWindow;
let tray;
let pythonManager;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    titleBarStyle: 'hiddenInset',  // macOS frameless
    backgroundColor: '#0a0a0f',    // Dark background
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    icon: path.join(__dirname, '../../assets/icon.png'),
  });

  // In dev, load from Vite dev server
  if (process.env.NODE_ENV === 'development') {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools();
  } else {
    mainWindow.loadFile(path.join(__dirname, '../../dist/index.html'));
  }
}

async function startPythonServer() {
  pythonManager = new PythonManager();
  const port = await pythonManager.start();
  mainWindow.webContents.send('server-ready', { port });
}

app.whenReady().then(() => {
  createWindow();
  startPythonServer();

  // System tray
  tray = new Tray(path.join(__dirname, '../../assets/tray-icon.png'));
  tray.setToolTip('Ares');
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'Show', click: () => mainWindow.show() },
    { label: 'Quit', click: () => app.quit() },
  ]));
});

app.on('window-all-closed', () => {
  if (pythonManager) pythonManager.stop();
  app.quit();
});
```

### `src/main/python-manager.js`

```javascript
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

class PythonManager {
  constructor() {
    this.process = null;
    this.port = this._findPort();
  }

  _findPort() {
    // Find available port starting from 8765
    return 8765;
  }

  async start() {
    const pythonPath = process.env.ARES_PYTHON || 'python';
    const serverScript = path.join(
      __dirname, '..', '..', '..', 'ares', 'server.py'
    );

    this.process = spawn(pythonPath, ['-m', 'ares.server', '--port', this.port], {
      cwd: path.join(__dirname, '..', '..', '..'),
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    this.process.stdout.on('data', (data) => {
      const msg = data.toString();
      if (msg.includes('Ares server starting')) {
        // Server is ready
      }
    });

    this.process.stderr.on('data', (data) => {
      console.error(`Python stderr: ${data}`);
    });

    // Wait for server to be ready
    await this._waitForPort(this.port);
    return this.port;
  }

  _waitForPort(port, maxRetries = 30) {
    return new Promise((resolve, reject) => {
      let retries = 0;
      const check = () => {
        const socket = new net.Socket();
        socket.setTimeout(1000);
        socket.on('connect', () => {
          socket.destroy();
          resolve();
        });
        socket.on('error', () => {
          socket.destroy();
          if (++retries >= maxRetries) {
            reject(new Error('Python server failed to start'));
          } else {
            setTimeout(check, 500);
          }
        });
        socket.connect(port, '127.0.0.1');
      };
      check();
    });
  }

  stop() {
    if (this.process) {
      this.process.kill();
      this.process = null;
    }
  }
}

module.exports = PythonManager;
```

### `src/main/preload.js`

```javascript
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ares', {
  onServerReady: (cb) => ipcRenderer.on('server-ready', (_, data) => cb(data)),
  platform: process.platform,
});
```

---

## 7. Component 4: React Frontend — UI Design

### Theme: Claude Desktop Dark

Based on research of Claude Desktop and Hermes Desktop, the UI follows a minimal, content-first dark theme.

### Color System (CSS Variables)

```css
/* theme.css */
:root {
  /* Backgrounds */
  --bg-primary: #0a0a0f;
  --bg-secondary: #12121a;
  --bg-tertiary: #1a1a25;
  --bg-hover: #22222e;
  --bg-active: #2a2a38;

  /* Borders */
  --border-primary: #2a2a3a;
  --border-subtle: #1e1e2a;

  /* Text */
  --text-primary: #e8e8ed;
  --text-secondary: #9898a6;
  --text-tertiary: #6a6a78;
  --text-inverse: #0a0a0f;

  /* Accent */
  --accent-primary: #6366f1;
  --accent-hover: #818cf8;
  --accent-subtle: rgba(99, 102, 241, 0.12);

  /* Status */
  --success: #22c55e;
  --warning: #f59e0b;
  --error: #ef4444;

  /* Tool cards */
  --tool-bg: #14141e;
  --tool-border: #2a2a3a;
  --tool-header: #1e1e2a;

  /* Sidebar */
  --sidebar-width: 260px;
  --sidebar-bg: #0e0e16;

  /* Fonts */
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', monospace;

  /* Spacing */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;

  /* Shadows */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.4);
  --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.5);
}
```

### Layout

```
┌──────────────────────────────────────────────────────────┐
│ ■ ● ▲  Ares                                    ─  □  ✕  │  ← Title bar (hidden on macOS)
├────────────┬─────────────────────────────────────────────┤
│            │                                             │
│  Sessions  │           Chat Area                        │
│            │                                             │
│  ┌──────┐  │  ┌─────────────────────────────────────┐   │
│  │New   │  │  │                                     │   │
│  │Chat  │  │  │     Welcome back.                   │   │
│  └──────┘  │  │     What can I help you with?       │   │
│            │  │                                     │   │
│  Today     │  ├─────────────────────────────────────┤   │
│  ┌──────┐  │  │  👤 Remember that I prefer dark...  │   │
│  │Chat 1│  │  ├─────────────────────────────────────┤   │
│  │Chat 2│  │  │  🤖 I've noted that you prefer      │   │
│  │Chat 3│  │  │     dark mode. I'll remember        │   │
│            │  │     this across sessions.            │   │
│  Yesterday │  │                                     │   │
│  ┌──────┐  │  │  ┌─ 🔍 Web Search ───────────────┐ │   │
│  │Chat 4│  │  │  │ Query: Python 3.13 release     │ │   │
│  │Chat 5│  │  │  │ Found 5 results                │ │   │
│  └──────┘  │  │  └───────────────────────────────┘ │   │
│            │  │                                     │   │
│            │  ├─────────────────────────────────────┤   │
│            │  │  ┌─────────────────────────┐  📎  │   │
│            │  │  │ Ask Ares anything...    │  ⌘↵  │   │
│            │  │  └─────────────────────────┘       │   │
│            │  └─────────────────────────────────────┘   │
│            │                                             │
├────────────┴─────────────────────────────────────────────┤
│  🔋 deepseek-v4-flash-free          Memories: 12  Tasks: 3 │  ← Status bar
└──────────────────────────────────────────────────────────┘
```

### Component Details

#### Sidebar

- **New Chat button** — prominent, top of sidebar
- **Session list** — grouped by date (Today, Yesterday, Previous 7 Days, Older)
- **Session items** — show first message as title, hover for rename/delete
- **Search** — filter sessions by content
- **Width:** 260px, resizable via drag
- **Collapse:** toggle to icon-only mode (50px)

#### Chat Area

- **Messages** — alternating user (right-aligned, accent bg) and assistant (left-aligned, secondary bg)
- **Streaming** — tokens appear character by character with blinking cursor
- **Thinking indicator** — animated dots while agent processes
- **Auto-scroll** — follows new content, pauses on scroll up
- **Message actions** — copy, regenerate, edit (on hover)

#### Composer

- **Input** — auto-expanding textarea, max 200px height
- **Placeholder** — "Ask Ares anything..."
- **Send button** — appears when text is present
- **Keyboard** — Enter to send, Shift+Enter for newline, Cmd/Ctrl+Enter also sends
- **Attachment** — paperclip icon, drag-and-drop zone
- **Model indicator** — shows current model, click to switch

#### Tool Cards

Collapsible cards for each tool invocation:

```
┌─ 🔍 Web Search ──────────────────────────┐
│ ▼ Query: "Python 3.13 release notes"     │
│                                           │
│   Found 5 results:                        │
│   1. Python 3.13 Released — python.org    │
│   2. What's New in Python 3.13 — docs     │
│   ...                                     │
└───────────────────────────────────────────┘

┌─ 📁 Read File ───────────────────────────┐
│ ▶ README.md (42 lines)                    │
└───────────────────────────────────────────┘

┌─ 💾 Stored Memory ───────────────────────┐
│ ✅ Memory #12: User prefers dark mode     │
└───────────────────────────────────────────┘
```

- **Expand/collapse** — click header to toggle
- **Auto-expand** — tool results auto-expand, tool starts show loading
- **Color coding** — different accent colors per tool category

#### Markdown Rendering

- **react-markdown** with remark-gfm for GitHub Flavored Markdown
- **react-syntax-highlighter** (Shiki/Prism) for code blocks
- **Code blocks** — language label, copy button, line numbers optional
- **Inline code** — subtle background, monospace font
- **Lists, tables, blockquotes** — styled to match dark theme
- **Links** — accent color, open in external browser

#### Status Bar

- **Model indicator** — current model name, click to switch
- **Memory count** — total memories stored
- **Task count** — pending tasks
- **Connection status** — green dot = connected, red = disconnected

---

## 8. Component 5: WebSocket Client Hook

### `src/renderer/hooks/useWebSocket.js`

```javascript
import { useEffect, useRef, useCallback, useState } from 'react';
import { useChatStore } from '../stores/chatStore';

export function useWebSocket() {
  const wsRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [serverPort, setServerPort] = useState(null);

  useEffect(() => {
    // Listen for server-ready from main process
    window.ares?.onServerReady(({ port }) => {
      setServerPort(port);
    });
  }, []);

  useEffect(() => {
    if (!serverPort) return;

    const ws = new WebSocket(`ws://127.0.0.1:${serverPort}`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      // Reconnect after 2 seconds
      setTimeout(() => setServerPort(p => p), 2000);
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    };

    return () => ws.close();
  }, [serverPort]);

  const send = useCallback((msg) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  return { connected, send };
}

function handleMessage(msg) {
  const store = useChatStore.getState();

  switch (msg.type) {
    case 'content':
      store.appendStreamingContent(msg.text);
      break;
    case 'tool_start':
      store.addToolCall(msg.tool, msg.args);
      break;
    case 'tool_result':
      store.updateToolResult(msg.tool, msg.content);
      break;
    case 'response_done':
      store.finalizeResponse();
      break;
    case 'error':
      store.addError(msg.message);
      break;
    case 'session_info':
      store.setModel(msg.model);
      break;
    case 'memories':
      store.setMemories(msg.memories);
      break;
    case 'tasks':
      store.setTasks(msg.tasks);
      break;
  }
}
```

---

## 9. Component 6: State Management

### `src/renderer/stores/chatStore.js`

```javascript
import { create } from 'zustand';

export const useChatStore = create((set, get) => ({
  messages: [],
  streamingContent: '',
  streamingToolCalls: [],
  isStreaming: false,
  currentModel: 'deepseek-v4-flash-free',
  memories: [],
  tasks: [],

  // Actions
  addUserMessage: (content) => set((state) => ({
    messages: [...state.messages, {
      role: 'user',
      content,
      timestamp: Date.now(),
    }],
    isStreaming: true,
    streamingContent: '',
    streamingToolCalls: [],
  })),

  appendStreamingContent: (text) => set((state) => ({
    streamingContent: state.streamingContent + text,
  })),

  addToolCall: (tool, args) => set((state) => ({
    streamingToolCalls: [...state.streamingToolCalls, {
      tool,
      args,
      result: null,
      status: 'running',
    }],
  })),

  updateToolResult: (tool, content) => set((state) => ({
    streamingToolCalls: state.streamingToolCalls.map(tc =>
      tc.tool === tool && tc.status === 'running'
        ? { ...tc, result: content, status: 'done' }
        : tc
    ),
  })),

  finalizeResponse: () => set((state) => ({
    messages: [...state.messages, {
      role: 'assistant',
      content: state.streamingContent,
      toolCalls: state.streamingToolCalls,
      timestamp: Date.now(),
    }],
    streamingContent: '',
    streamingToolCalls: [],
    isStreaming: false,
  })),

  setModel: (model) => set({ currentModel: model }),
  setMemories: (memories) => set({ memories }),
  setTasks: (tasks) => set({ tasks }),
  addError: (message) => set((state) => ({
    messages: [...state.messages, {
      role: 'error',
      content: message,
      timestamp: Date.now(),
    }],
    isStreaming: false,
  })),

  clearMessages: () => set({
    messages: [],
    streamingContent: '',
    streamingToolCalls: [],
    isStreaming: false,
  }),
}));
```

---

## 10. Component 7: Entry Point Integration

### Modified `ares/__main__.py`

Add `--server` flag to start the WebSocket server instead of CLI:

```python
# ares/__main__.py additions

def main():
    import sys
    if "--server" in sys.argv:
        port = 8765
        for i, arg in enumerate(sys.argv):
            if arg == "--port" and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1])
        from ares.server import run_server
        run_server(port=port)
    else:
        # Existing CLI code
        from ares.cli import AresCLI
        app = AresCLI()
        _run_coro(app.run())
```

### Modified `pyproject.toml`

```toml
[project.scripts]
ares = "ares.__main__:main"
ares-server = "ares.server:run_server"
```

This means:
- `ares` or `python -m ares` → CLI (unchanged)
- `ares-server` or `python -m ares.server` → WebSocket server
- Desktop app spawns `ares-server` automatically

---

## 11. Component 8: Packaging & Distribution

### electron-builder.yml

```yaml
appId: com.ares.desktop
productName: Ares
directories:
  output: release
files:
  - dist/**/*
  - src/main/**/*
  - assets/**/*
extraResources:
  - from: ../
    to: backend/
    filter:
      - "**/*.py"
      - "**/pyproject.toml"
      - "!**/__pycache__/**"
      - "!**/tests/**"
win:
  target: nsis
  icon: assets/icon.ico
mac:
  target: dmg
  icon: assets/icon.icns
  category: public.app-category.productivity
linux:
  target: AppImage
  icon: assets/icon.png
  category: Utility
nsis:
  oneClick: false
  allowToChangeInstallationDirectory: true
```

### First-Launch Flow

1. User installs Ares Desktop (.exe / .dmg / .AppImage)
2. App starts, checks for Python 3.11+ in PATH
3. If found: creates virtual environment in `~/.ares/venv/`, installs `ares` package
4. If not found: shows instructions to install Python, offers download link
5. Starts WebSocket server from venv
6. Opens main window

---

## 12. Component 9: File Attachments

### Drag-and-Drop

- Drag files onto the chat area → file path sent to Agent
- Agent can use `read_file` tool to read the content
- Supported: text files, images (future: preview)

### Implementation

```javascript
// In ChatArea.jsx
const handleDrop = (e) => {
  e.preventDefault();
  const files = Array.from(e.dataTransfer.files);
  const paths = files.map(f => f.path);
  // Send attachment message
  send({
    type: 'chat',
    content: `Read these files: ${paths.join(', ')}`,
    attachments: paths,
  });
};
```

---

## 13. Component 10: Settings Panel

### Accessible via gear icon in sidebar bottom

Settings sections:
1. **Model** — dropdown of available models, API key input
2. **Appearance** — theme toggle (dark/light), font size
3. **Personality** — view/edit soul.md inline
4. **Profile** — view/edit profile.md inline
5. **Data** — export/import, memory count, task count
6. **About** — version, links, privacy info

Settings are synced with `~/.ares/config.json` via the WebSocket server.

---

## 14. Files to Create/Modify

| File | Action | What Changes |
|------|--------|-------------|
| `ares/server.py` | **Create** | WebSocket server wrapping Agent |
| `ares/__main__.py` | Modify | Add `--server` flag |
| `pyproject.toml` | Modify | Add `websockets` dep, `ares-server` script |
| `electron-app/` | **Create** | Entire Electron + React app |
| `electron-app/package.json` | **Create** | Dependencies and scripts |
| `electron-app/src/main/index.js` | **Create** | Electron main process |
| `electron-app/src/main/preload.js` | **Create** | Context bridge |
| `electron-app/src/main/python-manager.js` | **Create** | Python process manager |
| `electron-app/src/renderer/` | **Create** | React components (12+ files) |
| `electron-app/src/renderer/styles/` | **Create** | Theme CSS |
| `tests/test_server.py` | **Create** | WebSocket server tests |

**Total:** 1 new Python module, 2 modified Python files, ~20 new frontend files, 1 new test file.

---

## 15. Error Handling

| Scenario | Handling |
|----------|----------|
| Python not installed | Show install instructions, offer download link |
| Python server fails to start | Retry 3 times, then show error page with logs |
| WebSocket disconnects | Auto-reconnect after 2s, show "Reconnecting..." |
| Server crashes mid-response | Reconnect, show "Response interrupted" with retry button |
| Invalid JSON from server | Log error, ignore malformed message |
| File not found on drop | Show "File not found" toast |
| API key missing | Show settings panel with API key input |
| Rate limit hit | Show "Rate limited, retrying in X seconds" |
| Database locked | Show warning, suggest closing CLI if both running |

---

## 16. Testing Strategy

- **Unit tests:** WebSocket message parsing, state store actions, theme CSS variables
- **Integration:** Python server ↔ WebSocket client message round-trip
- **Manual:** Full chat flow, tool card rendering, streaming display, settings sync
- **Cross-platform:** Windows, macOS, Linux builds via electron-builder

---

## 17. Out of Scope (Phase 1)

- Voice input/output
- Image generation/display in chat
- Plugin/extension system
- Multi-window support
- Collaboration/sharing
- Auto-update mechanism
- Telemetry/analytics
- Mobile companion app

---

## 18. Success Criteria

- [ ] Desktop app launches and connects to Python WebSocket server
- [ ] Chat messages stream with real-time token display
- [ ] Tool calls render as expandable cards
- [ ] Markdown and code blocks render with syntax highlighting
- [ ] Sidebar shows conversation history
- [ ] Model switching works from UI
- [ ] Settings panel syncs with config.json
- [ ] Soul and profile can be viewed/edited from settings
- [ ] Dark theme matches Claude Desktop aesthetic
- [ ] CLI continues to work independently
- [ ] App packages for Windows (.exe), macOS (.dmg), Linux (.AppImage)
- [ ] First-launch Python detection and setup

---

## 19. References

- **Hermes Desktop:** Electron + React + WebSocket to Python CLI ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/desktop))
- **Claude Desktop:** Electron-based, dark minimal UI, streaming responses ([architecture analysis](https://github.com/AniketMan/claude-desktop-architecture))
- **assistant-ui:** React components for AI chat — Thread, Composer, Markdown, Syntax Highlighting ([site](https://www.assistant-ui.com/))
- **LobsterAI:** Electron + React 18 + Redux + Tailwind ([article](https://aibit.im/en/article/lobsterai-open-source-ai-assistant-built-on-electron))
- **Zustand:** Lightweight state management for React ([docs](https://zustand-demo.pmnd.rs/))
- **electron-builder:** Cross-platform packaging ([docs](https://www.electron.build/))
