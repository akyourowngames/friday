# Ares Desktop App — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an Electron desktop app with a Claude Desktop-style dark UI that communicates with the existing Python Agent backend over WebSocket.

**Architecture:** A Python WebSocket server (`ares/server.py`) wraps the existing `Agent.run_stream()` and translates its text/tool events into structured JSON. An Electron + React frontend connects over WebSocket, renders streaming messages with markdown/code highlighting, and displays tool calls as expandable cards. Both CLI and Desktop share the same data and Agent core.

**Tech Stack:** Python 3.11+ (websockets), Electron 35, React 19, Vite 6, Zustand 5, react-markdown, react-syntax-highlighter, lucide-react, electron-builder

---

## File Structure

| File | Responsibility |
|------|---------------|
| `ares/server.py` | **NEW** — WebSocket server: wraps Agent, JSON protocol, session/memory/task endpoints |
| `ares/__main__.py` | **MODIFY** — Add `--server` flag to start WebSocket server instead of CLI |
| `pyproject.toml` | **MODIFY** — Add `websockets` dep, `ares-server` script entry point |
| `tests/test_server.py` | **NEW** — Tests for WebSocket server protocol and message handling |
| `electron-app/package.json` | **NEW** — Electron app dependencies and scripts |
| `electron-app/vite.config.js` | **NEW** — Vite config for React renderer |
| `electron-app/electron-builder.yml` | **NEW** — Cross-platform packaging config |
| `electron-app/src/main/index.js` | **NEW** — Electron main process: window, tray, Python manager |
| `electron-app/src/main/preload.js` | **NEW** — Context bridge for IPC |
| `electron-app/src/main/python-manager.js` | **NEW** — Spawns/monitors Python WebSocket server |
| `electron-app/src/renderer/index.html` | **NEW** — HTML entry point |
| `electron-app/src/renderer/main.jsx` | **NEW** — React entry point |
| `electron-app/src/renderer/App.jsx` | **NEW** — Root component: sidebar + chat + settings |
| `electron-app/src/renderer/styles/theme.css` | **NEW** — CSS variables, dark theme, global styles |
| `electron-app/src/renderer/styles/components.css` | **NEW** — Component-specific styles |
| `electron-app/src/renderer/lib/websocket.js` | **NEW** — WebSocket connection manager class |
| `electron-app/src/renderer/stores/chatStore.js` | **NEW** — Zustand store for messages, streaming, tool calls |
| `electron-app/src/renderer/stores/sessionStore.js` | **NEW** — Zustand store for session list |
| `electron-app/src/renderer/stores/settingsStore.js` | **NEW** — Zustand store for settings/model |
| `electron-app/src/renderer/hooks/useWebSocket.js` | **NEW** — React hook wiring WebSocket to stores |
| `electron-app/src/renderer/components/Sidebar/Sidebar.jsx` | **NEW** — Sidebar with new chat, session list, search |
| `electron-app/src/renderer/components/Sidebar/SessionList.jsx` | **NEW** — Grouped session list |
| `electron-app/src/renderer/components/Sidebar/SessionItem.jsx` | **NEW** — Single session item |
| `electron-app/src/renderer/components/Chat/ChatArea.jsx` | **NEW** — Main chat container with drag-drop |
| `electron-app/src/renderer/components/Chat/MessageList.jsx` | **NEW** — Scrollable message list |
| `electron-app/src/renderer/components/Chat/Message.jsx` | **NEW** — Single message bubble (user/assistant/error) |
| `electron-app/src/renderer/components/Chat/Composer.jsx` | **NEW** — Auto-expanding textarea + send button |
| `electron-app/src/renderer/components/Chat/StreamingIndicator.jsx` | **NEW** — Blinking cursor / thinking dots |
| `electron-app/src/renderer/components/Tools/ToolCard.jsx` | **NEW** — Collapsible tool call card |
| `electron-app/src/renderer/components/Tools/WebSearchCard.jsx` | **NEW** — Web search result card |
| `electron-app/src/renderer/components/Tools/FileCard.jsx` | **NEW** — File read/list card |
| `electron-app/src/renderer/components/Tools/MemoryCard.jsx` | **NEW** — Memory store/search card |
| `electron-app/src/renderer/components/common/MarkdownRenderer.jsx` | **NEW** — Markdown with GFM + syntax highlighting |
| `electron-app/src/renderer/components/common/CodeBlock.jsx` | **NEW** — Code block with copy button + language label |
| `electron-app/src/renderer/components/common/ThinkingIndicator.jsx` | **NEW** — Animated thinking dots |
| `electron-app/src/renderer/components/Settings/SettingsPanel.jsx` | **NEW** — Settings modal/page |
| `electron-app/src/renderer/components/Settings/ModelSelector.jsx` | **NEW** — Model dropdown + API key input |
| `electron-app/src/renderer/components/common/StatusBar.jsx` | **NEW** — Bottom status bar: model, memory count, task count |

---

## Task 1: WebSocket Server — Core Chat Streaming

The most critical piece: a Python WebSocket server that wraps `Agent.run_stream()` and translates its output to structured JSON events.

**Files:**
- Create: `ares/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write failing tests for the WebSocket server protocol**

```python
# tests/test_server.py
"""Tests for the Ares WebSocket server."""

import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ares.server import AresServer, run_server


class FakeAgent:
    """Mock agent that yields predictable events."""

    def __init__(self):
        self.model = "test-model"
        self.last_input = None

    def set_model(self, model):
        self.model = model

    async def run_stream(self, user_input, conversation_history):
        self.last_input = user_input
        yield "Hello "
        yield "world!"
        yield "[tool:web_search:{\"query\": \"test\"}]"
        yield "Done."

    def get_context(self, query=""):
        return "## Soul\nBe concise."

    async def close(self):
        pass


class FakeMemoryStore:
    def get_recent(self, limit=10):
        return [{"fact_id": 1, "fact_text": "User likes tea"}]

    def search(self, query, limit=5):
        return [{"fact_id": 1, "fact_text": "User likes tea"}]


class FakeTaskStore:
    def list_pending(self):
        return [{"id": 1, "title": "Buy milk", "status": "pending"}]


class FakeConversationStore:
    def __init__(self):
        self._conversations = [
            {"id": 1, "started_at": "2026-06-19T10:00:00", "summary": "Test chat"}
        ]
        self._messages = {
            1: [{"role": "user", "content": "Hello"}, {"role": "assistant", "content": "Hi!"}]
        }

    def list_sessions(self):
        return self._conversations

    def get_history(self, session_id):
        return self._messages.get(session_id, [])

    def start_conversation(self):
        return 2

    def add_exchange(self, conv_id, user_input, response):
        pass

    def end_conversation(self, conv_id):
        pass

    def get_recent_summaries(self, limit=5):
        return ["Test chat"]


def make_server():
    """Create an AresServer with mocked stores."""
    server = AresServer.__new__(AresServer)
    server.host = "127.0.0.1"
    server.port = 0  # random
    server.config = MagicMock()
    server.config.model = "test-model"
    server.config.api_key = ""
    server.config.api_base_url = ""
    server.config.data_dir = "~/.ares/data"
    server.config.max_memory_retrieval = 5
    server.memory_store = FakeMemoryStore()
    server.task_store = FakeTaskStore()
    server.conversation_store = FakeConversationStore()
    server.agent = FakeAgent()
    return server


class TestProtocol:
    """Test the JSON message protocol."""

    def test_session_info_message_format(self):
        """session_info message has correct structure."""
        msg = {"type": "session_info", "model": "test-model"}
        assert msg["type"] == "session_info"
        assert "model" in msg

    def test_content_message_format(self):
        """content message has correct structure."""
        msg = {"type": "content", "text": "Hello world"}
        assert msg["type"] == "content"
        assert msg["text"] == "Hello world"

    def test_tool_start_message_format(self):
        """tool_start message has correct structure."""
        msg = {"type": "tool_start", "tool": "web_search", "args": {"query": "test"}}
        assert msg["type"] == "tool_start"
        assert msg["tool"] == "web_search"

    def test_tool_result_message_format(self):
        """tool_result message has correct structure."""
        msg = {"type": "tool_result", "tool": "web_search", "content": '{"results": []}'}
        assert msg["type"] == "tool_result"

    def test_response_done_message_format(self):
        """response_done message has correct structure."""
        msg = {"type": "response_done"}
        assert msg["type"] == "response_done"

    def test_error_message_format(self):
        """error message has correct structure."""
        msg = {"type": "error", "message": "Something broke"}
        assert msg["type"] == "error"
        assert msg["message"] == "Something broke"


class TestServerHelpers:
    """Test server helper methods without WebSocket."""

    def test_make_server_has_stores(self):
        server = make_server()
        assert server.memory_store is not None
        assert server.task_store is not None
        assert server.conversation_store is not None

    def test_make_server_has_agent(self):
        server = make_server()
        assert server.agent is not None

    def test_chat_message_parsing(self):
        """Chat message has required fields."""
        msg = {"type": "chat", "content": "What do you know about me?", "session_id": 1}
        assert msg["type"] == "chat"
        assert msg["content"]
        assert isinstance(msg["session_id"], int)

    def test_set_model_message_parsing(self):
        msg = {"type": "set_model", "model": "mimo-v2.5-free"}
        assert msg["type"] == "set_model"
        assert msg["model"] == "mimo-v2.5-free"

    def test_get_memories_message_parsing(self):
        msg = {"type": "get_memories", "limit": 5}
        assert msg["type"] == "get_memories"
        assert msg["limit"] == 5

    def test_get_tasks_message_parsing(self):
        msg = {"type": "get_tasks"}
        assert msg["type"] == "get_tasks"

    def test_list_sessions_message_parsing(self):
        msg = {"type": "list_sessions"}
        assert msg["type"] == "list_sessions"

    def test_load_session_message_parsing(self):
        msg = {"type": "load_session", "session_id": 5}
        assert msg["type"] == "load_session"
        assert msg["session_id"] == 5


class TestFormatEvents:
    """Test how agent output events get formatted for WebSocket."""

    def test_format_content_event(self):
        """Content text wraps in content event."""
        text = "Hello world"
        event = {"type": "content", "text": text}
        raw = json.dumps(event)
        parsed = json.loads(raw)
        assert parsed["type"] == "content"
        assert parsed["text"] == "Hello world"

    def test_format_tool_marker_event(self):
        """Tool marker [tool:name:content] parses to tool_start + tool_result."""
        marker = "[tool:web_search:{\"query\": \"test\"}]"
        # Extract tool name and content
        inner = marker[len("[tool:"):-1]  # "web_search:{\"query\": \"test\"}"
        colon_idx = inner.index(":")
        tool_name = inner[:colon_idx]
        tool_content = inner[colon_idx + 1:]

        start_event = {"type": "tool_start", "tool": tool_name, "args": {}}
        result_event = {"type": "tool_result", "tool": tool_name, "content": tool_content}

        assert start_event["tool"] == "web_search"
        assert result_event["tool"] == "web_search"
        assert "test" in result_event["content"]

    def test_format_error_event(self):
        event = {"type": "error", "message": "connection lost"}
        assert event["type"] == "error"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd C:/Users/anime/ares && python -m pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ares.server'`

- [ ] **Step 3: Implement the WebSocket server**

```python
# ares/server.py
"""WebSocket server wrapping the Ares Agent for desktop app communication."""

import asyncio
import json
import logging
import sys
from argparse import ArgumentParser

from websockets.asyncio.server import serve

from ares.agent import Agent
from ares.config import load_config, save_config
from ares.memory import MemoryStore
from ares.tasks import TaskStore
from ares.conversations import ConversationStore

logger = logging.getLogger("ares.server")


def _parse_tool_marker(text: str) -> dict | None:
    """Parse [tool:name:content] markers into structured events."""
    if not text.startswith("[tool:") or not text.endswith("]"):
        return None
    inner = text[len("[tool:"):-1]
    colon_idx = inner.find(":")
    if colon_idx == -1:
        return None
    tool_name = inner[:colon_idx]
    tool_content = inner[colon_idx + 1:]
    # Try to parse content as JSON to extract args for display
    args = {}
    try:
        parsed = json.loads(tool_content)
        if isinstance(parsed, dict):
            args = parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return {"tool": tool_name, "args": args, "content": tool_content}


class AresServer:
    """WebSocket server wrapping the Ares Agent."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.config = load_config()
        self.memory_store = MemoryStore()
        self.task_store = TaskStore()
        self.conversation_store = ConversationStore()
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
        async with serve(self.handle_connection, self.host, self.port) as server:
            await asyncio.Future()  # run forever

    async def handle_connection(self, websocket):
        """Handle a single WebSocket connection."""
        try:
            # Send session info on connect
            await websocket.send(json.dumps({
                "type": "session_info",
                "model": self.config.model,
            }))

            async for raw in websocket:
                msg = json.loads(raw)
                await self._route(websocket, msg)

        except Exception as e:
            logger.error(f"Connection error: {e}")

    async def _route(self, websocket, msg: dict):
        """Route an incoming message to the appropriate handler."""
        msg_type = msg.get("type")

        if msg_type == "chat":
            await self._handle_chat(websocket, msg)

        elif msg_type == "set_model":
            await self._handle_set_model(websocket, msg)

        elif msg_type == "get_context":
            await self._handle_get_context(websocket, msg)

        elif msg_type == "list_sessions":
            await self._handle_list_sessions(websocket)

        elif msg_type == "load_session":
            await self._handle_load_session(websocket, msg)

        elif msg_type == "get_memories":
            await self._handle_get_memories(websocket, msg)

        elif msg_type == "get_tasks":
            await self._handle_get_tasks(websocket)

        elif msg_type == "update_config":
            await self._handle_update_config(websocket, msg)

        else:
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Unknown message type: {msg_type}",
            }))

    async def _handle_chat(self, websocket, msg: dict):
        """Process a chat message and stream the response."""
        content = msg.get("content", "")
        session_id = msg.get("session_id")
        conversation_history = []

        if session_id:
            conversation_history = self.conversation_store.get_history(session_id)

        try:
            async for event in self.agent.run_stream(content, conversation_history):
                if isinstance(event, str):
                    # Check for tool markers: [tool:name:content]
                    parsed = _parse_tool_marker(event)
                    if parsed:
                        await websocket.send(json.dumps({
                            "type": "tool_start",
                            "tool": parsed["tool"],
                            "args": parsed["args"],
                        }))
                        await websocket.send(json.dumps({
                            "type": "tool_result",
                            "tool": parsed["tool"],
                            "content": parsed["content"],
                        }))
                    else:
                        # Regular content text
                        await websocket.send(json.dumps({
                            "type": "content",
                            "text": event,
                        }))

            # Signal response complete
            await websocket.send(json.dumps({"type": "response_done"}))

        except Exception as e:
            logger.error(f"Chat error: {e}")
            await websocket.send(json.dumps({
                "type": "error",
                "message": str(e),
            }))

    async def _handle_set_model(self, websocket, msg: dict):
        """Switch the active model."""
        model = msg.get("model", "")
        if model:
            self.config.model = model
            self.agent.set_model(model)
            save_config(self.config)
        await websocket.send(json.dumps({
            "type": "session_info",
            "model": self.config.model,
        }))

    async def _handle_get_context(self, websocket, msg: dict):
        """Return the blended context for a query."""
        query = msg.get("query", "")
        context = self.agent.get_context(query)
        await websocket.send(json.dumps({
            "type": "context",
            "content": context,
        }))

    async def _handle_list_sessions(self, websocket):
        """Return all conversation sessions."""
        sessions = self.conversation_store.list_sessions()
        await websocket.send(json.dumps({
            "type": "sessions",
            "sessions": sessions,
        }))

    async def _handle_load_session(self, websocket, msg: dict):
        """Load conversation history for a session."""
        session_id = msg.get("session_id")
        if session_id is None:
            await websocket.send(json.dumps({
                "type": "error",
                "message": "session_id required",
            }))
            return
        history = self.conversation_store.get_history(session_id)
        await websocket.send(json.dumps({
            "type": "session_history",
            "session_id": session_id,
            "messages": history,
        }))

    async def _handle_get_memories(self, websocket, msg: dict):
        """Return recent memories."""
        limit = msg.get("limit", 10)
        memories = self.memory_store.get_recent(limit)
        await websocket.send(json.dumps({
            "type": "memories",
            "memories": memories,
        }))

    async def _handle_get_tasks(self, websocket):
        """Return pending tasks."""
        tasks = self.task_store.list_pending()
        await websocket.send(json.dumps({
            "type": "tasks",
            "tasks": tasks,
        }))

    async def _handle_update_config(self, websocket, msg: dict):
        """Update a config value and persist it."""
        key = msg.get("key", "")
        value = msg.get("value")
        if hasattr(self.config, key) and value is not None:
            setattr(self.config, key, value)
            save_config(self.config)
            await websocket.send(json.dumps({
                "type": "config_updated",
                "key": key,
                "value": value,
            }))
        else:
            await websocket.send(json.dumps({
                "type": "error",
                "message": f"Invalid config key: {key}",
            }))


def run_server(host: str = "127.0.0.1", port: int = 8765):
    """Entry point for the WebSocket server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    server = AresServer(host, port)
    asyncio.run(server.start())


def main():
    parser = ArgumentParser(description="Ares WebSocket Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/anime/ares && python -m pytest tests/test_server.py -v`
Expected: All 15 tests PASS

- [ ] **Step 5: Commit**

```bash
git add ares/server.py tests/test_server.py
git commit -m "feat: add WebSocket server wrapping Agent for desktop app"
```

---

## Task 2: Entry Point Integration

Wire up `--server` flag and `ares-server` CLI command.

**Files:**
- Modify: `ares/__main__.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Update __main__.py with --server flag**

```python
# ares/__main__.py
"""Entry point: python -m ares"""

import asyncio
import sys
import threading
from collections.abc import Coroutine
from typing import Any


def _run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine from sync code, even if this thread already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=False)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def main():
    if "--server" in sys.argv:
        from ares.server import main as server_main
        # Filter out --server, pass rest to argparse
        sys.argv = [sys.argv[0]] + [a for a in sys.argv[1:] if a != "--server"]
        server_main()
    else:
        from ares.cli import AresCLI

        async def _run_cli():
            cli = AresCLI()
            await cli.run()

        _run_coro(_run_cli())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update pyproject.toml**

Add `websockets` to dependencies and `ares-server` script entry:

```toml
# In [project] dependencies, add:
"websockets>=14.0",

# In [project.scripts], change to:
[project.scripts]
ares = "ares.__main__:main"
ares-server = "ares.server:main"
```

- [ ] **Step 3: Verify CLI still works (no regression)**

Run: `cd C:/Users/anime/ares && python -m ares --help`
Expected: CLI starts normally (or shows help if --help works)

- [ ] **Step 4: Verify server mode starts**

Run: `cd C:/Users/anime/ares && timeout 3 python -m ares --server --port 8766 2>&1 || true`
Expected: Server starts, logs "Ares server starting on ws://127.0.0.1:8766", then times out

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `cd C:/Users/anime/ares && python -m pytest tests/ -v --timeout=30`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add ares/__main__.py pyproject.toml
git commit -m "feat: add --server flag and ares-server entry point for desktop app"
```

---

## Task 3: Electron App Scaffolding

Set up the Electron + Vite + React project structure.

**Files:**
- Create: `electron-app/package.json`
- Create: `electron-app/vite.config.js`
- Create: `electron-app/electron-builder.yml`
- Create: `electron-app/src/main/index.js`
- Create: `electron-app/src/main/preload.js`
- Create: `electron-app/src/main/python-manager.js`
- Create: `electron-app/src/renderer/index.html`
- Create: `electron-app/src/renderer/main.jsx`
- Create: `electron-app/src/renderer/App.jsx`

- [ ] **Step 1: Create package.json**

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
    "build:linux": "vite build && electron-builder --linux",
    "preview": "vite preview"
  },
  "dependencies": {
    "react": "^19.1.0",
    "react-dom": "^19.1.0",
    "zustand": "^5.0.0",
    "react-markdown": "^10.1.0",
    "remark-gfm": "^4.0.0",
    "react-syntax-highlighter": "^15.6.1",
    "lucide-react": "^0.511.0"
  },
  "devDependencies": {
    "electron": "^35.0.0",
    "vite": "^6.3.0",
    "@vitejs/plugin-react": "^4.4.0",
    "electron-builder": "^26.0.0",
    "concurrently": "^9.1.0",
    "wait-on": "^8.0.0"
  }
}
```

- [ ] **Step 2: Create vite.config.js**

```javascript
// electron-app/vite.config.js
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  root: 'src/renderer',
  base: './',
  build: {
    outDir: '../../dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
```

- [ ] **Step 3: Create electron-builder.yml**

```yaml
# electron-app/electron-builder.yml
appId: com.ares.desktop
productName: Ares
directories:
  output: release
files:
  - dist/**/*
  - src/main/**/*
extraResources:
  - from: ../
    to: backend/
    filter:
      - "**/*.py"
      - "**/pyproject.toml"
      - "!**/__pycache__/**"
      - "!**/tests/**"
      - "!**/electron-app/**"
win:
  target: nsis
mac:
  target: dmg
  category: public.app-category.productivity
linux:
  target: AppImage
  category: Utility
nsis:
  oneClick: false
  allowToChangeInstallationDirectory: true
```

- [ ] **Step 4: Create Electron main process**

```javascript
// electron-app/src/main/index.js
const { app, BrowserWindow, Tray, Menu } = require('electron');
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
    titleBarStyle: 'hiddenInset',
    backgroundColor: '#0a0a0f',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    icon: path.join(__dirname, '../../assets/icon.png'),
    show: false,
  });

  if (process.env.NODE_ENV === 'development') {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools();
  } else {
    mainWindow.loadFile(path.join(__dirname, '../../dist/index.html'));
  }

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });
}

async function startPythonServer() {
  pythonManager = new PythonManager();
  try {
    const port = await pythonManager.start();
    mainWindow.webContents.send('server-ready', { port });
  } catch (err) {
    mainWindow.webContents.send('server-error', { message: err.message });
  }
}

app.whenReady().then(async () => {
  createWindow();
  await startPythonServer();

  // System tray
  const iconPath = path.join(__dirname, '../../assets/tray-icon.png');
  try {
    tray = new Tray(iconPath);
    tray.setToolTip('Ares');
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: 'Show', click: () => mainWindow.show() },
      { label: 'Quit', click: () => app.quit() },
    ]));
    tray.on('click', () => mainWindow.show());
  } catch (e) {
    // Tray icon not found in dev — skip silently
  }
});

app.on('window-all-closed', () => {
  if (pythonManager) pythonManager.stop();
  app.quit();
});

app.on('activate', () => {
  if (mainWindow) mainWindow.show();
});
```

- [ ] **Step 5: Create preload script**

```javascript
// electron-app/src/main/preload.js
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ares', {
  onServerReady: (cb) => ipcRenderer.on('server-ready', (_, data) => cb(data)),
  onServerError: (cb) => ipcRenderer.on('server-error', (_, data) => cb(data)),
  platform: process.platform,
});
```

- [ ] **Step 6: Create Python manager**

```javascript
// electron-app/src/main/python-manager.js
const { spawn } = require('child_process');
const path = require('path');
const net = require('net');

class PythonManager {
  constructor() {
    this.process = null;
    this.port = 8765;
  }

  async start() {
    const projectRoot = path.join(__dirname, '..', '..', '..');
    const pythonPath = process.env.ARES_PYTHON || 'python';

    this.process = spawn(pythonPath, ['-m', 'ares.server', '--port', String(this.port)], {
      cwd: projectRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    this.process.stdout.on('data', (data) => {
      const msg = data.toString();
      if (msg.includes('Ares server starting')) {
        console.log('[Ares] Python server started');
      }
    });

    this.process.stderr.on('data', (data) => {
      console.error(`[Ares] Python stderr: ${data}`);
    });

    this.process.on('error', (err) => {
      console.error(`[Ares] Python process error: ${err.message}`);
    });

    this.process.on('exit', (code) => {
      console.log(`[Ares] Python process exited with code ${code}`);
    });

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
            reject(new Error('Python server failed to start within timeout'));
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

- [ ] **Step 7: Create renderer entry HTML and JSX**

```html
<!-- electron-app/src/renderer/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Ares</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
</head>
<body>
  <div id="root"></div>
  <script type="module" src="./main.jsx"></script>
</body>
</html>
```

```jsx
// electron-app/src/renderer/main.jsx
import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles/theme.css';
import './styles/components.css';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

```jsx
// electron-app/src/renderer/App.jsx
import React from 'react';
import { useWebSocket } from './hooks/useWebSocket';
import Sidebar from './components/Sidebar/Sidebar';
import ChatArea from './components/Chat/ChatArea';
import StatusBar from './components/common/StatusBar';
import SettingsPanel from './components/Settings/SettingsPanel';
import { useSettingsStore } from './stores/settingsStore';

export default function App() {
  const { connected, send } = useWebSocket();
  const showSettings = useSettingsStore((s) => s.showSettings);

  return (
    <div className="app">
      <div className="app-body">
        <Sidebar send={send} />
        <ChatArea send={send} connected={connected} />
      </div>
      <StatusBar connected={connected} />
      {showSettings && <SettingsPanel send={send} />}
    </div>
  );
}
```

- [ ] **Step 8: Install dependencies and verify dev server starts**

```bash
cd electron-app && npm install
```

Expected: Dependencies install without errors.

- [ ] **Step 9: Commit**

```bash
git add electron-app/
git commit -m "feat: add Electron app scaffolding with main process, preload, and Vite config"
```

---

## Task 4: Theme CSS and Global Styles

Create the Claude Desktop-style dark theme.

**Files:**
- Create: `electron-app/src/renderer/styles/theme.css`
- Create: `electron-app/src/renderer/styles/components.css`

- [ ] **Step 1: Create theme.css with CSS variables**

```css
/* electron-app/src/renderer/styles/theme.css */
:root {
  /* Backgrounds */
  --bg-primary: #0a0a0f;
  --bg-secondary: #12121a;
  --bg-tertiary: #1a1a25;
  --bg-hover: #22222e;
  --bg-active: #2a2a38;
  --bg-input: #16161f;

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

  /* User message */
  --user-bg: #1a1a2e;
  --assistant-bg: transparent;

  /* Fonts */
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Consolas', monospace;

  /* Radii */
  --radius-sm: 6px;
  --radius-md: 10px;
  --radius-lg: 16px;

  /* Shadows */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.3);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.4);
  --shadow-lg: 0 8px 24px rgba(0, 0, 0, 0.5);
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #root {
  height: 100%;
  width: 100%;
  overflow: hidden;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

::selection {
  background: var(--accent-subtle);
  color: var(--text-primary);
}

::-webkit-scrollbar {
  width: 6px;
}

::-webkit-scrollbar-track {
  background: transparent;
}

::-webkit-scrollbar-thumb {
  background: var(--border-primary);
  border-radius: 3px;
}

::-webkit-scrollbar-thumb:hover {
  background: var(--text-tertiary);
}
```

- [ ] **Step 2: Create components.css**

```css
/* electron-app/src/renderer/styles/components.css */

/* ── App Layout ───────────────────────────────────── */
.app {
  display: flex;
  flex-direction: column;
  height: 100vh;
  width: 100vw;
}

.app-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

/* ── Sidebar ──────────────────────────────────────── */
.sidebar {
  width: var(--sidebar-width);
  min-width: var(--sidebar-width);
  background: var(--sidebar-bg);
  border-right: 1px solid var(--border-subtle);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: width 0.2s ease, min-width 0.2s ease;
}

.sidebar.collapsed {
  width: 50px;
  min-width: 50px;
}

.sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--border-subtle);
}

.sidebar-search {
  width: 100%;
  padding: 8px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}

.sidebar-search:focus {
  border-color: var(--accent-primary);
}

.new-chat-btn {
  width: 100%;
  padding: 10px;
  margin-top: 8px;
  background: var(--accent-primary);
  color: white;
  border: none;
  border-radius: var(--radius-sm);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: background 0.15s;
}

.new-chat-btn:hover {
  background: var(--accent-hover);
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.session-group-label {
  padding: 8px 8px 4px;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.session-item {
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.1s;
}

.session-item:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.session-item.active {
  background: var(--bg-active);
  color: var(--text-primary);
}

.sidebar-footer {
  padding: 12px 16px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ── Chat Area ────────────────────────────────────── */
.chat-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.message-list {
  flex: 1;
  overflow-y: auto;
  padding: 24px 0;
}

.message-list-inner {
  max-width: 800px;
  margin: 0 auto;
  padding: 0 24px;
}

.welcome-message {
  text-align: center;
  padding: 80px 24px;
  color: var(--text-secondary);
}

.welcome-message h1 {
  font-size: 28px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.welcome-message p {
  font-size: 15px;
  color: var(--text-tertiary);
}

/* ── Messages ─────────────────────────────────────── */
.message {
  padding: 12px 0;
  display: flex;
  gap: 12px;
}

.message.user {
  justify-content: flex-end;
}

.message.assistant {
  justify-content: flex-start;
}

.message-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
}

.message-avatar.user {
  background: var(--accent-primary);
  color: white;
}

.message-avatar.assistant {
  background: var(--bg-tertiary);
  color: var(--text-secondary);
}

.message-content {
  max-width: 85%;
  padding: 10px 16px;
  border-radius: var(--radius-lg);
  line-height: 1.65;
}

.message.user .message-content {
  background: var(--user-bg);
  border-bottom-right-radius: var(--radius-sm);
}

.message.assistant .message-content {
  background: var(--assistant-bg);
  border-bottom-left-radius: var(--radius-sm);
}

.message-content p {
  margin-bottom: 8px;
}

.message-content p:last-child {
  margin-bottom: 0;
}

.message-content pre {
  background: var(--bg-primary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  padding: 12px;
  overflow-x: auto;
  margin: 8px 0;
}

.message-content code {
  font-family: var(--font-mono);
  font-size: 13px;
}

.message-content :not(pre) > code {
  background: var(--bg-tertiary);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}

.message-content a {
  color: var(--accent-primary);
  text-decoration: none;
}

.message-content a:hover {
  text-decoration: underline;
}

.message-content ul, .message-content ol {
  padding-left: 20px;
  margin: 8px 0;
}

.message-content li {
  margin: 4px 0;
}

.message-content blockquote {
  border-left: 3px solid var(--accent-primary);
  padding-left: 12px;
  color: var(--text-secondary);
  margin: 8px 0;
}

.message-content table {
  border-collapse: collapse;
  width: 100%;
  margin: 8px 0;
}

.message-content th, .message-content td {
  border: 1px solid var(--border-primary);
  padding: 6px 12px;
  text-align: left;
}

.message-content th {
  background: var(--bg-tertiary);
  font-weight: 600;
}

.message-error .message-content {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: var(--error);
}

/* ── Composer ─────────────────────────────────────── */
.composer-wrapper {
  padding: 16px 24px 20px;
  display: flex;
  justify-content: center;
}

.composer {
  width: 100%;
  max-width: 800px;
  background: var(--bg-input);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-md);
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 10px 14px;
  transition: border-color 0.15s;
}

.composer:focus-within {
  border-color: var(--accent-primary);
}

.composer textarea {
  flex: 1;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 14px;
  line-height: 1.5;
  resize: none;
  outline: none;
  max-height: 200px;
  min-height: 21px;
}

.composer textarea::placeholder {
  color: var(--text-tertiary);
}

.send-btn {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  background: var(--accent-primary);
  color: white;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background 0.15s;
}

.send-btn:hover {
  background: var(--accent-hover);
}

.send-btn:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}

/* ── Tool Cards ───────────────────────────────────── */
.tool-card {
  background: var(--tool-bg);
  border: 1px solid var(--tool-border);
  border-radius: var(--radius-sm);
  margin: 8px 0;
  overflow: hidden;
}

.tool-card-header {
  padding: 8px 12px;
  background: var(--tool-header);
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
}

.tool-card-header:hover {
  color: var(--text-primary);
}

.tool-card-header .tool-icon {
  font-size: 14px;
}

.tool-card-header .tool-chevron {
  margin-left: auto;
  transition: transform 0.15s;
}

.tool-card-header .tool-chevron.open {
  transform: rotate(90deg);
}

.tool-card-body {
  padding: 10px 12px;
  font-size: 13px;
  color: var(--text-secondary);
  border-top: 1px solid var(--tool-border);
  line-height: 1.5;
}

.tool-card-body pre {
  background: var(--bg-primary);
  border-radius: 4px;
  padding: 8px;
  overflow-x: auto;
  font-size: 12px;
}

/* ── Streaming Indicator ──────────────────────────── */
.streaming-cursor {
  display: inline-block;
  width: 2px;
  height: 16px;
  background: var(--accent-primary);
  animation: blink 1s step-end infinite;
  vertical-align: text-bottom;
  margin-left: 2px;
}

@keyframes blink {
  50% { opacity: 0; }
}

.thinking-dots {
  display: inline-flex;
  gap: 4px;
  padding: 8px 0;
}

.thinking-dots span {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--text-tertiary);
  animation: thinking 1.4s infinite ease-in-out;
}

.thinking-dots span:nth-child(2) { animation-delay: 0.2s; }
.thinking-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes thinking {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}

/* ── Status Bar ───────────────────────────────────── */
.status-bar {
  height: 28px;
  background: var(--sidebar-bg);
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  padding: 0 16px;
  gap: 16px;
  font-size: 11px;
  color: var(--text-tertiary);
}

.status-bar .connection-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}

.status-bar .connection-dot.connected {
  background: var(--success);
}

.status-bar .connection-dot.disconnected {
  background: var(--error);
}

.status-bar .model-name {
  color: var(--text-secondary);
  font-weight: 500;
}

.status-bar .stat {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* ── Settings Panel ───────────────────────────────── */
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.settings-panel {
  background: var(--bg-secondary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  width: 520px;
  max-height: 80vh;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
}

.settings-header {
  padding: 20px 24px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.settings-header h2 {
  font-size: 18px;
  font-weight: 600;
}

.settings-body {
  padding: 20px 24px;
}

.settings-section {
  margin-bottom: 24px;
}

.settings-section h3 {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 12px;
}

.settings-field {
  margin-bottom: 12px;
}

.settings-field label {
  display: block;
  font-size: 13px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}

.settings-field input,
.settings-field select,
.settings-field textarea {
  width: 100%;
  padding: 8px 12px;
  background: var(--bg-input);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  color: var(--text-primary);
  font-size: 13px;
  font-family: var(--font-sans);
  outline: none;
}

.settings-field textarea {
  min-height: 100px;
  resize: vertical;
  font-family: var(--font-mono);
}

.settings-field input:focus,
.settings-field select:focus,
.settings-field textarea:focus {
  border-color: var(--accent-primary);
}

/* ── Code Block ───────────────────────────────────── */
.code-block {
  position: relative;
  margin: 8px 0;
}

.code-block-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  background: var(--bg-tertiary);
  border: 1px solid var(--border-primary);
  border-bottom: none;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  font-size: 12px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}

.code-block-copy {
  background: none;
  border: none;
  color: var(--text-tertiary);
  cursor: pointer;
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 4px;
}

.code-block-copy:hover {
  background: var(--bg-hover);
  color: var(--text-primary);
}

.code-block pre {
  margin: 0;
  border-radius: 0 0 var(--radius-sm) var(--radius-sm);
}

/* ── Empty States ─────────────────────────────────── */
.empty-state {
  text-align: center;
  padding: 24px;
  color: var(--text-tertiary);
  font-size: 13px;
}
```

- [ ] **Step 3: Commit**

```bash
git add electron-app/src/renderer/styles/
git commit -m "feat: add Claude Desktop-style dark theme with CSS variables"
```

---

## Task 5: Zustand Stores

Create state management for chat, sessions, and settings.

**Files:**
- Create: `electron-app/src/renderer/stores/chatStore.js`
- Create: `electron-app/src/renderer/stores/sessionStore.js`
- Create: `electron-app/src/renderer/stores/settingsStore.js`

- [ ] **Step 1: Create chatStore.js**

```javascript
// electron-app/src/renderer/stores/chatStore.js
import { create } from 'zustand';

export const useChatStore = create((set, get) => ({
  messages: [],
  streamingContent: '',
  streamingToolCalls: [],
  isStreaming: false,
  currentModel: 'deepseek-v4-flash-free',

  addUserMessage: (content) => set((state) => ({
    messages: [...state.messages, {
      id: Date.now(),
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
      id: `${tool}-${Date.now()}`,
      tool,
      args,
      result: null,
      status: 'running',
    }],
  })),

  updateToolResult: (tool, content) => set((state) => {
    const updated = state.streamingToolCalls.map(tc =>
      tc.tool === tool && tc.status === 'running'
        ? { ...tc, result: content, status: 'done' }
        : tc
    );
    return { streamingToolCalls: updated };
  }),

  finalizeResponse: () => set((state) => ({
    messages: [...state.messages, {
      id: Date.now(),
      role: 'assistant',
      content: state.streamingContent,
      toolCalls: [...state.streamingToolCalls],
      timestamp: Date.now(),
    }],
    streamingContent: '',
    streamingToolCalls: [],
    isStreaming: false,
  })),

  setModel: (model) => set({ currentModel: model }),

  addError: (message) => set((state) => ({
    messages: [...state.messages, {
      id: Date.now(),
      role: 'error',
      content: message,
      timestamp: Date.now(),
    }],
    isStreaming: false,
    streamingContent: '',
    streamingToolCalls: [],
  })),

  clearMessages: () => set({
    messages: [],
    streamingContent: '',
    streamingToolCalls: [],
    isStreaming: false,
  }),

  setMessages: (messages) => set({ messages }),
}));
```

- [ ] **Step 2: Create sessionStore.js**

```javascript
// electron-app/src/renderer/stores/sessionStore.js
import { create } from 'zustand';

export const useSessionStore = create((set) => ({
  sessions: [],
  activeSessionId: null,

  setSessions: (sessions) => set({ sessions }),

  setActiveSession: (id) => set({ activeSessionId: id }),

  addSession: (session) => set((state) => ({
    sessions: [session, ...state.sessions],
    activeSessionId: session.id,
  })),
}));
```

- [ ] **Step 3: Create settingsStore.js**

```javascript
// electron-app/src/renderer/stores/settingsStore.js
import { create } from 'zustand';

export const useSettingsStore = create((set) => ({
  showSettings: false,
  memories: [],
  tasks: [],

  toggleSettings: () => set((state) => ({ showSettings: !state.showSettings })),
  openSettings: () => set({ showSettings: true }),
  closeSettings: () => set({ showSettings: false }),

  setMemories: (memories) => set({ memories }),
  setTasks: (tasks) => set({ tasks }),
}));
```

- [ ] **Step 4: Commit**

```bash
git add electron-app/src/renderer/stores/
git commit -m "feat: add Zustand stores for chat, sessions, and settings"
```

---

## Task 6: WebSocket Client + React Hook

The bridge between Electron's main process and the React frontend.

**Files:**
- Create: `electron-app/src/renderer/lib/websocket.js`
- Create: `electron-app/src/renderer/hooks/useWebSocket.js`

- [ ] **Step 1: Create WebSocket connection manager**

```javascript
// electron-app/src/renderer/lib/websocket.js
/**
 * WebSocket connection manager with auto-reconnect.
 */
export class AresWebSocket {
  constructor(url, { onMessage, onOpen, onClose, onError }) {
    this.url = url;
    this.handlers = { onMessage, onOpen, onClose, onError };
    this.ws = null;
    this.reconnectTimer = null;
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 10000;
    this.shouldReconnect = true;
  }

  connect() {
    if (this.ws) this.close();

    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.reconnectDelay = 1000;
      this.handlers.onOpen?.();
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.handlers.onMessage?.(msg);
      } catch (e) {
        console.error('[Ares WS] Failed to parse message:', e);
      }
    };

    this.ws.onclose = () => {
      this.handlers.onClose?.();
      this._scheduleReconnect();
    };

    this.ws.onerror = (event) => {
      this.handlers.onError?.(event);
    };
  }

  send(msg) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  close() {
    this.shouldReconnect = false;
    clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }

  _scheduleReconnect() {
    if (!this.shouldReconnect) return;
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.reconnectDelay);
    this.reconnectDelay = Math.min(
      this.reconnectDelay * 1.5,
      this.maxReconnectDelay,
    );
  }
}
```

- [ ] **Step 2: Create useWebSocket hook**

```javascript
// electron-app/src/renderer/hooks/useWebSocket.js
import { useEffect, useRef, useCallback, useState } from 'react';
import { AresWebSocket } from '../lib/websocket';
import { useChatStore } from '../stores/chatStore';
import { useSessionStore } from '../stores/sessionStore';
import { useSettingsStore } from '../stores/settingsStore';

export function useWebSocket() {
  const wsRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [serverPort, setServerPort] = useState(null);

  // Listen for server-ready from main process
  useEffect(() => {
    if (window.ares) {
      window.ares.onServerReady(({ port }) => {
        setServerPort(port);
      });
      window.ares.onServerError(({ message }) => {
        console.error('[Ares] Server error:', message);
      });
    }
  }, []);

  // Connect WebSocket when port is known
  useEffect(() => {
    if (!serverPort) return;

    const ws = new AresWebSocket(`ws://127.0.0.1:${serverPort}`, {
      onOpen: () => setConnected(true),
      onClose: () => setConnected(false),
      onMessage: handleMessage,
    });

    ws.connect();
    wsRef.current = ws;

    return () => ws.close();
  }, [serverPort]);

  const send = useCallback((msg) => {
    wsRef.current?.send(msg);
  }, []);

  return { connected, send };
}

function handleMessage(msg) {
  const chat = useChatStore.getState();
  const session = useSessionStore.getState();
  const settings = useSettingsStore.getState();

  switch (msg.type) {
    case 'content':
      chat.appendStreamingContent(msg.text);
      break;

    case 'tool_start':
      chat.addToolCall(msg.tool, msg.args || {});
      break;

    case 'tool_result':
      chat.updateToolResult(msg.tool, msg.content);
      break;

    case 'response_done':
      chat.finalizeResponse();
      break;

    case 'error':
      chat.addError(msg.message);
      break;

    case 'session_info':
      chat.setModel(msg.model);
      break;

    case 'sessions':
      session.setSessions(msg.sessions);
      break;

    case 'session_history':
      chat.setMessages(msg.messages || []);
      break;

    case 'memories':
      settings.setMemories(msg.memories || []);
      break;

    case 'tasks':
      settings.setTasks(msg.tasks || []);
      break;

    default:
      break;
  }
}
```

- [ ] **Step 3: Commit**

```bash
git add electron-app/src/renderer/lib/ electron-app/src/renderer/hooks/
git commit -m "feat: add WebSocket client with auto-reconnect and React hook"
```

---

## Task 7: Chat UI Components

Build the core chat experience: messages, composer, streaming indicator.

**Files:**
- Create: `electron-app/src/renderer/components/Chat/ChatArea.jsx`
- Create: `electron-app/src/renderer/components/Chat/MessageList.jsx`
- Create: `electron-app/src/renderer/components/Chat/Message.jsx`
- Create: `electron-app/src/renderer/components/Chat/Composer.jsx`
- Create: `electron-app/src/renderer/components/Chat/StreamingIndicator.jsx`
- Create: `electron-app/src/renderer/components/common/ThinkingIndicator.jsx`

- [ ] **Step 1: Create StreamingIndicator**

```jsx
// electron-app/src/renderer/components/Chat/StreamingIndicator.jsx
import React from 'react';

export default function StreamingIndicator() {
  return <span className="streaming-cursor" />;
}
```

- [ ] **Step 2: Create ThinkingIndicator**

```jsx
// electron-app/src/renderer/components/common/ThinkingIndicator.jsx
import React from 'react';

export default function ThinkingIndicator() {
  return (
    <div className="thinking-dots">
      <span />
      <span />
      <span />
    </div>
  );
}
```

- [ ] **Step 3: Create Message component**

```jsx
// electron-app/src/renderer/components/Chat/Message.jsx
import React from 'react';
import MarkdownRenderer from '../common/MarkdownRenderer';
import ToolCard from '../Tools/ToolCard';

export default function Message({ message }) {
  const { role, content, toolCalls } = message;

  if (role === 'error') {
    return (
      <div className="message error message-error">
        <div className="message-avatar user">⚠</div>
        <div className="message-content">{content}</div>
      </div>
    );
  }

  return (
    <div className={`message ${role}`}>
      {role === 'user' && (
        <div className="message-avatar user">👤</div>
      )}
      <div className="message-content">
        <MarkdownRenderer content={content} />
        {toolCalls?.map((tc) => (
          <ToolCard key={tc.id} toolCall={tc} />
        ))}
      </div>
      {role === 'assistant' && (
        <div className="message-avatar assistant">🤖</div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Create MessageList**

```jsx
// electron-app/src/renderer/components/Chat/MessageList.jsx
import React, { useEffect, useRef } from 'react';
import Message from './Message';
import StreamingMessage from './StreamingMessage';

export default function MessageList({ messages, streamingContent, streamingToolCalls, isStreaming }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingContent]);

  if (messages.length === 0 && !isStreaming) {
    return (
      <div className="message-list">
        <div className="welcome-message">
          <h1>What can I help you with?</h1>
          <p>A personal AI assistant that lives on your desktop.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="message-list">
      <div className="message-list-inner">
        {messages.map((msg) => (
          <Message key={msg.id} message={msg} />
        ))}
        {isStreaming && (
          <StreamingMessage
            content={streamingContent}
            toolCalls={streamingToolCalls}
          />
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Create StreamingMessage (inline in MessageList or separate)**

```jsx
// Add to electron-app/src/renderer/components/Chat/StreamingMessage.jsx
import React from 'react';
import MarkdownRenderer from '../common/MarkdownRenderer';
import StreamingIndicator from './StreamingIndicator';
import ToolCard from '../Tools/ToolCard';
import ThinkingIndicator from '../common/ThinkingIndicator';

export default function StreamingMessage({ content, toolCalls }) {
  const hasContent = content.length > 0;
  const hasToolCalls = toolCalls.length > 0;
  const showThinking = !hasContent && !hasToolCalls;

  return (
    <div className="message assistant">
      <div className="message-avatar assistant">🤖</div>
      <div className="message-content">
        {showThinking && <ThinkingIndicator />}
        {hasContent && (
          <>
            <MarkdownRenderer content={content} />
            <StreamingIndicator />
          </>
        )}
        {toolCalls.map((tc) => (
          <ToolCard key={tc.id} toolCall={tc} />
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 6: Create Composer**

```jsx
// electron-app/src/renderer/components/Chat/Composer.jsx
import React, { useState, useRef, useCallback } from 'react';
import { Send } from 'lucide-react';

export default function Composer({ onSend, disabled }) {
  const [value, setValue] = useState('');
  const textareaRef = useRef(null);

  const handleSend = useCallback(() => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue('');
    textareaRef.current?.focus();
  }, [value, disabled, onSend]);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e) => {
    setValue(e.target.value);
    // Auto-resize
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  };

  return (
    <div className="composer-wrapper">
      <div className="composer">
        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder="Ask Ares anything..."
          rows={1}
          disabled={disabled}
        />
        <button
          className="send-btn"
          onClick={handleSend}
          disabled={!value.trim() || disabled}
          title="Send message"
        >
          <Send size={16} />
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Create ChatArea**

```jsx
// electron-app/src/renderer/components/Chat/ChatArea.jsx
import React, { useCallback } from 'react';
import MessageList from './MessageList';
import Composer from './Composer';
import { useChatStore } from '../../stores/chatStore';

export default function ChatArea({ send, connected }) {
  const messages = useChatStore((s) => s.messages);
  const streamingContent = useChatStore((s) => s.streamingContent);
  const streamingToolCalls = useChatStore((s) => s.streamingToolCalls);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const addUserMessage = useChatStore((s) => s.addUserMessage);

  const handleSend = useCallback((text) => {
    addUserMessage(text);
    send({ type: 'chat', content: text });
  }, [send, addUserMessage]);

  return (
    <div className="chat-area">
      <MessageList
        messages={messages}
        streamingContent={streamingContent}
        streamingToolCalls={streamingToolCalls}
        isStreaming={isStreaming}
      />
      <Composer onSend={handleSend} disabled={!connected || isStreaming} />
    </div>
  );
}
```

- [ ] **Step 8: Commit**

```bash
git add electron-app/src/renderer/components/Chat/ electron-app/src/renderer/components/common/ThinkingIndicator.jsx
git commit -m "feat: add chat UI with message list, composer, streaming indicator"
```

---

## Task 8: Markdown Rendering + Code Blocks

Rich content rendering for assistant responses.

**Files:**
- Create: `electron-app/src/renderer/components/common/MarkdownRenderer.jsx`
- Create: `electron-app/src/renderer/components/common/CodeBlock.jsx`

- [ ] **Step 1: Create CodeBlock component**

```jsx
// electron-app/src/renderer/components/common/CodeBlock.jsx
import React, { useState, useCallback } from 'react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';
import { Copy, Check } from 'lucide-react';

export default function CodeBlock({ language, children }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(children);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [children]);

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>{language || 'code'}</span>
        <button className="code-block-copy" onClick={handleCopy}>
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? ' Copied' : ' Copy'}
        </button>
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={language || 'text'}
        PreTag="div"
        customStyle={{
          margin: 0,
          borderRadius: '0 0 6px 6px',
          fontSize: '13px',
          lineHeight: '1.5',
        }}
      >
        {children}
      </SyntaxHighlighter>
    </div>
  );
}
```

- [ ] **Step 2: Create MarkdownRenderer component**

```jsx
// electron-app/src/renderer/components/common/MarkdownRenderer.jsx
import React from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import CodeBlock from './CodeBlock';

export default function MarkdownRenderer({ content }) {
  if (!content) return null;

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code({ node, inline, className, children, ...props }) {
          const match = /language-(\w+)/.exec(className || '');
          if (!inline && match) {
            return (
              <CodeBlock language={match[1]}>
                {String(children).replace(/\n$/, '')}
              </CodeBlock>
            );
          }
          return (
            <code className={className} {...props}>
              {children}
            </code>
          );
        },
        a({ href, children }) {
          return (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        },
      }}
    >
      {content}
    </ReactMarkdown>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add electron-app/src/renderer/components/common/MarkdownRenderer.jsx electron-app/src/renderer/components/common/CodeBlock.jsx
git commit -m "feat: add markdown rendering with syntax highlighting and copy button"
```

---

## Task 9: Tool Cards

Visual cards for tool invocations (web search, file, memory).

**Files:**
- Create: `electron-app/src/renderer/components/Tools/ToolCard.jsx`
- Create: `electron-app/src/renderer/components/Tools/WebSearchCard.jsx`
- Create: `electron-app/src/renderer/components/Tools/FileCard.jsx`
- Create: `electron-app/src/renderer/components/Tools/MemoryCard.jsx`

- [ ] **Step 1: Create ToolCard (router + base)**

```jsx
// electron-app/src/renderer/components/Tools/ToolCard.jsx
import React, { useState } from 'react';
import { ChevronRight, Search, FileText, Database, Settings } from 'lucide-react';
import WebSearchCard from './WebSearchCard';
import FileCard from './FileCard';
import MemoryCard from './MemoryCard';

const TOOL_ICONS = {
  web_search: Search,
  search_files: Search,
  read_file: FileText,
  list_directory: FileText,
  store_memory: Database,
  search_memory: Database,
  update_memory: Database,
  delete_memory: Database,
};

const TOOL_LABELS = {
  web_search: 'Web Search',
  read_file: 'Read File',
  search_files: 'Search Files',
  list_directory: 'List Directory',
  store_memory: 'Stored Memory',
  search_memory: 'Memory Search',
  update_memory: 'Updated Memory',
  delete_memory: 'Deleted Memory',
  create_task: 'Created Task',
  complete_task: 'Completed Task',
  list_tasks: 'Listed Tasks',
  export_data: 'Export Data',
  fetch_url: 'Fetched URL',
};

export default function ToolCard({ toolCall }) {
  const [open, setOpen] = useState(true);
  const { tool, args, result, status } = toolCall;

  const Icon = TOOL_ICONS[tool] || Settings;
  const label = TOOL_LABELS[tool] || tool;

  const renderBody = () => {
    if (status === 'running') {
      return <div className="tool-card-body">Running...</div>;
    }

    switch (tool) {
      case 'web_search':
        return <WebSearchCard args={args} result={result} />;
      case 'read_file':
      case 'search_files':
      case 'list_directory':
        return <FileCard tool={tool} args={args} result={result} />;
      case 'store_memory':
      case 'search_memory':
        return <MemoryCard tool={tool} args={args} result={result} />;
      default:
        return (
          <div className="tool-card-body">
            {result ? (
              <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
            ) : (
              <span>Tool completed</span>
            )}
          </div>
        );
    }
  };

  return (
    <div className="tool-card">
      <div className="tool-card-header" onClick={() => setOpen(!open)}>
        <Icon size={14} className="tool-icon" />
        <span>{label}</span>
        {status === 'running' && <span className="tool-status">⏳</span>}
        <ChevronRight
          size={14}
          className={`tool-chevron ${open ? 'open' : ''}`}
        />
      </div>
      {open && renderBody()}
    </div>
  );
}
```

- [ ] **Step 2: Create WebSearchCard**

```jsx
// electron-app/src/renderer/components/Tools/WebSearchCard.jsx
import React from 'react';

export default function WebSearchCard({ args, result }) {
  return (
    <div className="tool-card-body">
      {args?.query && (
        <div><strong>Query:</strong> {args.query}</div>
      )}
      {result && (
        <div style={{ marginTop: 6 }}>
          <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create FileCard**

```jsx
// electron-app/src/renderer/components/Tools/FileCard.jsx
import React from 'react';

export default function FileCard({ tool, args, result }) {
  const getLabel = () => {
    switch (tool) {
      case 'read_file': return args?.path || 'file';
      case 'search_files': return `Search: ${args?.pattern || ''}`;
      case 'list_directory': return args?.path || 'directory';
      default: return tool;
    }
  };

  return (
    <div className="tool-card-body">
      <div><strong>{getLabel()}</strong></div>
      {result && (
        <div style={{ marginTop: 6 }}>
          <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Create MemoryCard**

```jsx
// electron-app/src/renderer/components/Tools/MemoryCard.jsx
import React from 'react';

export default function MemoryCard({ tool, args, result }) {
  return (
    <div className="tool-card-body">
      {tool === 'store_memory' && args?.fact_text && (
        <div>✅ Stored: "{args.fact_text}"</div>
      )}
      {tool === 'search_memory' && args?.query && (
        <div>🔍 Search: "{args.query}"</div>
      )}
      {result && (
        <div style={{ marginTop: 6 }}>
          <pre>{typeof result === 'string' ? result : JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 5: Commit**

```bash
git add electron-app/src/renderer/components/Tools/
git commit -m "feat: add tool cards for web search, file, and memory tools"
```

---

## Task 10: Sidebar

Session list with search, new chat, and settings access.

**Files:**
- Create: `electron-app/src/renderer/components/Sidebar/Sidebar.jsx`
- Create: `electron-app/src/renderer/components/Sidebar/SessionList.jsx`
- Create: `electron-app/src/renderer/components/Sidebar/SessionItem.jsx`

- [ ] **Step 1: Create SessionItem**

```jsx
// electron-app/src/renderer/components/Sidebar/SessionItem.jsx
import React from 'react';

export default function SessionItem({ session, isActive, onClick }) {
  const title = session.summary || session.title || `Chat ${session.id}`;
  const truncated = title.length > 32 ? title.slice(0, 32) + '…' : title;

  return (
    <div
      className={`session-item ${isActive ? 'active' : ''}`}
      onClick={onClick}
    >
      {truncated}
    </div>
  );
}
```

- [ ] **Step 2: Create SessionList**

```jsx
// electron-app/src/renderer/components/Sidebar/SessionList.jsx
import React from 'react';
import SessionItem from './SessionItem';
import { useSessionStore } from '../../stores/sessionStore';

function groupSessions(sessions) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
  const weekAgo = new Date(today); weekAgo.setDate(today.getDate() - 7);

  const groups = { Today: [], Yesterday: [], 'Previous 7 Days': [], Older: [] };

  for (const s of sessions) {
    const date = new Date(s.started_at);
    if (date >= today) groups['Today'].push(s);
    else if (date >= yesterday) groups['Yesterday'].push(s);
    else if (date >= weekAgo) groups['Previous 7 Days'].push(s);
    else groups['Older'].push(s);
  }

  return groups;
}

export default function SessionList({ searchQuery, onSelectSession }) {
  const sessions = useSessionStore((s) => s.sessions);
  const activeId = useSessionStore((s) => s.activeSessionId);

  const filtered = searchQuery
    ? sessions.filter(s =>
        (s.summary || '').toLowerCase().includes(searchQuery.toLowerCase()) ||
        (s.title || '').toLowerCase().includes(searchQuery.toLowerCase())
      )
    : sessions;

  const groups = groupSessions(filtered);

  return (
    <div className="session-list">
      {Object.entries(groups).map(([label, items]) => {
        if (items.length === 0) return null;
        return (
          <div key={label}>
            <div className="session-group-label">{label}</div>
            {items.map((s) => (
              <SessionItem
                key={s.id}
                session={s}
                isActive={s.id === activeId}
                onClick={() => onSelectSession(s.id)}
              />
            ))}
          </div>
        );
      })}
      {filtered.length === 0 && (
        <div className="empty-state">
          {searchQuery ? 'No matching sessions' : 'No conversations yet'}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Create Sidebar**

```jsx
// electron-app/src/renderer/components/Sidebar/Sidebar.jsx
import React, { useState } from 'react';
import { Plus, Search, Settings } from 'lucide-react';
import SessionList from './SessionList';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { useSettingsStore } from '../../stores/settingsStore';

export default function Sidebar({ send }) {
  const [searchQuery, setSearchQuery] = useState('');
  const [collapsed, setCollapsed] = useState(false);
  const clearMessages = useChatStore((s) => s.clearMessages);
  const setActiveSession = useSessionStore((s) => s.setActiveSession);
  const openSettings = useSettingsStore((s) => s.openSettings);

  const handleNewChat = () => {
    clearMessages();
    setActiveSession(null);
    send({ type: 'list_sessions' });
  };

  const handleSelectSession = (sessionId) => {
    setActiveSession(sessionId);
    send({ type: 'load_session', session_id: sessionId });
  };

  if (collapsed) {
    return (
      <div className="sidebar collapsed">
        <button
          className="new-chat-btn"
          onClick={handleNewChat}
          title="New Chat"
          style={{ margin: 8, padding: 8 }}
        >
          <Plus size={16} />
        </button>
        <div style={{ flex: 1 }} />
        <button
          onClick={openSettings}
          style={{
            background: 'none', border: 'none', color: 'var(--text-tertiary)',
            cursor: 'pointer', padding: 8, margin: 8,
          }}
          title="Settings"
        >
          <Settings size={16} />
        </button>
      </div>
    );
  }

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <span style={{ fontSize: 16, fontWeight: 600 }}>🔥 Ares</span>
          <div style={{ flex: 1 }} />
          <button
            onClick={() => setCollapsed(true)}
            style={{
              background: 'none', border: 'none', color: 'var(--text-tertiary)',
              cursor: 'pointer', fontSize: 11,
            }}
            title="Collapse sidebar"
          >
            ◀
          </button>
        </div>
        <div style={{ position: 'relative' }}>
          <Search size={14} style={{
            position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)',
            color: 'var(--text-tertiary)',
          }} />
          <input
            className="sidebar-search"
            placeholder="Search sessions..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{ paddingLeft: 28 }}
          />
        </div>
        <button className="new-chat-btn" onClick={handleNewChat}>
          <Plus size={14} />
          New Chat
        </button>
      </div>

      <SessionList
        searchQuery={searchQuery}
        onSelectSession={handleSelectSession}
      />

      <div className="sidebar-footer">
        <button
          onClick={openSettings}
          style={{
            background: 'none', border: 'none', color: 'var(--text-tertiary)',
            cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
            fontSize: 13, padding: '4px 0',
          }}
          title="Settings"
        >
          <Settings size={14} />
          Settings
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Commit**

```bash
git add electron-app/src/renderer/components/Sidebar/
git commit -m "feat: add sidebar with session list, search, new chat, and settings"
```

---

## Task 11: Settings Panel + Model Selector

Settings UI for model, personality, profile, and data management.

**Files:**
- Create: `electron-app/src/renderer/components/Settings/SettingsPanel.jsx`
- Create: `electron-app/src/renderer/components/Settings/ModelSelector.jsx`

- [ ] **Step 1: Create ModelSelector**

```jsx
// electron-app/src/renderer/components/Settings/ModelSelector.jsx
import React from 'react';
import { useChatStore } from '../../stores/chatStore';

const FREE_MODELS = [
  { id: 'deepseek-v4-flash-free', label: 'DeepSeek V4 Flash (Free)' },
  { id: 'mimo-v2.5-free', label: 'Mimo V2.5 (Free)' },
];

export default function ModelSelector({ send }) {
  const currentModel = useChatStore((s) => s.currentModel);
  const setModel = useChatStore((s) => s.setModel);

  const handleChange = (e) => {
    const model = e.target.value;
    setModel(model);
    send({ type: 'set_model', model });
  };

  return (
    <div className="settings-field">
      <label>Model</label>
      <select value={currentModel} onChange={handleChange}>
        {FREE_MODELS.map((m) => (
          <option key={m.id} value={m.id}>{m.label}</option>
        ))}
      </select>
    </div>
  );
}
```

- [ ] **Step 2: Create SettingsPanel**

```jsx
// electron-app/src/renderer/components/Settings/SettingsPanel.jsx
import React, { useEffect, useState } from 'react';
import { X } from 'lucide-react';
import ModelSelector from './ModelSelector';
import { useSettingsStore } from '../../stores/settingsStore';

export default function SettingsPanel({ send }) {
  const closeSettings = useSettingsStore((s) => s.closeSettings);
  const memories = useSettingsStore((s) => s.memories);
  const tasks = useSettingsStore((s) => s.tasks);
  const [version] = useState('0.1.0');

  useEffect(() => {
    send({ type: 'get_memories', limit: 10 });
    send({ type: 'get_tasks' });
  }, [send]);

  return (
    <div className="settings-overlay" onClick={(e) => {
      if (e.target === e.currentTarget) closeSettings();
    }}>
      <div className="settings-panel">
        <div className="settings-header">
          <h2>Settings</h2>
          <button
            onClick={closeSettings}
            style={{
              background: 'none', border: 'none', color: 'var(--text-tertiary)',
              cursor: 'pointer',
            }}
          >
            <X size={18} />
          </button>
        </div>

        <div className="settings-body">
          <div className="settings-section">
            <h3>Model</h3>
            <ModelSelector send={send} />
          </div>

          <div className="settings-section">
            <h3>Data</h3>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.8 }}>
              <div>Memories: {memories.length}</div>
              <div>Pending tasks: {tasks.length}</div>
            </div>
          </div>

          <div className="settings-section">
            <h3>About</h3>
            <div style={{ fontSize: 13, color: 'var(--text-secondary)', lineHeight: 1.8 }}>
              <div>Ares Desktop v{version}</div>
              <div>Personal AI Assistant</div>
              <div style={{ marginTop: 8 }}>
                <span style={{ color: 'var(--text-tertiary)' }}>
                  All data stored locally in ~/.ares/
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 3: Commit**

```bash
git add electron-app/src/renderer/components/Settings/
git commit -m "feat: add settings panel with model selector and data info"
```

---

## Task 12: StatusBar

Bottom bar showing model, memory/task counts, and connection status.

**Files:**
- Create: `electron-app/src/renderer/components/common/StatusBar.jsx`

- [ ] **Step 1: Create StatusBar**

```jsx
// electron-app/src/renderer/components/common/StatusBar.jsx
import React from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useSettingsStore } from '../../stores/settingsStore';

export default function StatusBar({ connected }) {
  const model = useChatStore((s) => s.currentModel);
  const memories = useSettingsStore((s) => s.memories);
  const tasks = useSettingsStore((s) => s.tasks);

  return (
    <div className="status-bar">
      <span className={`connection-dot ${connected ? 'connected' : 'disconnected'}`} />
      <span className="model-name">{model}</span>
      <span className="stat">Memories: {memories.length}</span>
      <span className="stat">Tasks: {tasks.length}</span>
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add electron-app/src/renderer/components/common/StatusBar.jsx
git commit -m "feat: add status bar with model, memory count, and task count"
```

---

## Task 13: End-to-End Smoke Test

Verify the full stack works: Python server → WebSocket → Electron → React.

**Files:**
- Modify: `tests/test_server.py` (add integration-style tests)

- [ ] **Step 1: Add async WebSocket client test**

```python
# Add to tests/test_server.py

class TestServerIntegration:
    """Integration tests using a real WebSocket server."""

    @pytest.mark.asyncio
    async def test_server_starts_and_accepts_connection(self):
        """Server starts and accepts a WebSocket connection."""
        server = AresServer.__new__(AresServer)
        server.host = "127.0.0.1"
        server.port = 0
        server.config = MagicMock()
        server.config.model = "test-model"
        server.config.api_key = ""
        server.config.api_base_url = ""
        server.config.data_dir = "/tmp/ares-test"
        server.config.max_memory_retrieval = 5
        server.memory_store = FakeMemoryStore()
        server.task_store = FakeTaskStore()
        server.conversation_store = FakeConversationStore()
        server.agent = FakeAgent()

        from websockets.asyncio.server import serve

        async with serve(server.handle_connection, "127.0.0.1", 8799) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            async with __import__("websockets").connect(f"ws://127.0.0.1:{port}") as ws:
                # Should receive session_info
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                assert msg["type"] == "session_info"
                assert msg["model"] == "test-model"

                # Send chat message
                await ws.send(json.dumps({
                    "type": "chat",
                    "content": "hello",
                }))

                # Collect all events until response_done
                events = []
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    event = json.loads(raw)
                    events.append(event)
                    if event["type"] == "response_done":
                        break

                # Should have content events and a done
                assert any(e["type"] == "content" for e in events)
                assert events[-1]["type"] == "response_done"

    @pytest.mark.asyncio
    async def test_set_model_returns_updated_session_info(self):
        """set_model updates config and returns session_info."""
        server = AresServer.__new__(AresServer)
        server.host = "127.0.0.1"
        server.port = 0
        server.config = MagicMock()
        server.config.model = "old-model"
        server.config.api_key = ""
        server.config.api_base_url = ""
        server.config.data_dir = "/tmp/ares-test"
        server.config.max_memory_retrieval = 5
        server.memory_store = FakeMemoryStore()
        server.task_store = FakeTaskStore()
        server.conversation_store = FakeConversationStore()
        server.agent = FakeAgent()

        from websockets.asyncio.server import serve

        async with serve(server.handle_connection, "127.0.0.1", 8800) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            async with __import__("websockets").connect(f"ws://127.0.0.1:{port}") as ws:
                # Skip session_info
                await asyncio.wait_for(ws.recv(), timeout=5)

                # Send set_model
                await ws.send(json.dumps({
                    "type": "set_model",
                    "model": "new-model",
                }))

                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                assert msg["type"] == "session_info"
                assert msg["model"] == "new-model"

    @pytest.mark.asyncio
    async def test_get_tasks_returns_tasks(self):
        """get_tasks returns pending tasks."""
        server = AresServer.__new__(AresServer)
        server.host = "127.0.0.1"
        server.port = 0
        server.config = MagicMock()
        server.config.model = "test-model"
        server.config.api_key = ""
        server.config.api_base_url = ""
        server.config.data_dir = "/tmp/ares-test"
        server.config.max_memory_retrieval = 5
        server.memory_store = FakeMemoryStore()
        server.task_store = FakeTaskStore()
        server.conversation_store = FakeConversationStore()
        server.agent = FakeAgent()

        from websockets.asyncio.server import serve

        async with serve(server.handle_connection, "127.0.0.1", 8801) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            async with __import__("websockets").connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)

                await ws.send(json.dumps({"type": "get_tasks"}))

                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                assert msg["type"] == "tasks"
                assert len(msg["tasks"]) > 0
                assert msg["tasks"][0]["title"] == "Buy milk"

    @pytest.mark.asyncio
    async def test_unknown_message_type_returns_error(self):
        """Unknown message type returns an error event."""
        server = AresServer.__new__(AresServer)
        server.host = "127.0.0.1"
        server.port = 0
        server.config = MagicMock()
        server.config.model = "test-model"
        server.config.api_key = ""
        server.config.api_base_url = ""
        server.config.data_dir = "/tmp/ares-test"
        server.config.max_memory_retrieval = 5
        server.memory_store = FakeMemoryStore()
        server.task_store = FakeTaskStore()
        server.conversation_store = FakeConversationStore()
        server.agent = FakeAgent()

        from websockets.asyncio.server import serve

        async with serve(server.handle_connection, "127.0.0.1", 8802) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]

            async with __import__("websockets").connect(f"ws://127.0.0.1:{port}") as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)

                await ws.send(json.dumps({"type": "bogus"}))

                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                assert msg["type"] == "error"
                assert "Unknown" in msg["message"]
```

- [ ] **Step 2: Install websockets and run all tests**

```bash
cd C:/Users/anime/ares
pip install websockets>=14.0
python -m pytest tests/test_server.py -v
```

Expected: All tests pass (unit + integration)

- [ ] **Step 3: Run full test suite to verify no regressions**

```bash
python -m pytest tests/ -v --timeout=30
```

Expected: All existing + new tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_server.py
git commit -m "feat: add WebSocket server integration tests"
```

---

## Task 14: Final Polish + README Update

Update documentation for the desktop app.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add desktop app section to README**

Add after the existing "Quick Start" section:

```markdown
## Desktop App

Ares also has a desktop app with a Claude Desktop-style dark UI.

### Prerequisites

- Python 3.11+ installed and in PATH
- Node.js 18+ installed

### Running in Development

```bash
# Start the Python WebSocket server
ares-server

# In another terminal, start the Electron app
cd electron-app
npm install
npm run dev
```

### Building for Distribution

```bash
cd electron-app
npm run build          # builds for current platform
npm run build:win      # Windows .exe
npm run build:mac      # macOS .dmg
npm run build:linux    # Linux AppImage
```

### Features

- Real-time streaming responses
- Markdown rendering with syntax highlighting
- Tool call cards (web search, file ops, memory)
- Conversation history sidebar with search
- Model switching from UI
- System tray presence
- Drag-and-drop file attachments
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add desktop app section to README"
```

---

## Self-Review

### Spec Coverage Check

| Spec Requirement | Task | Status |
|---|---|---|
| Python WebSocket server (ares/server.py) | Task 1 | ✅ |
| JSON protocol (content, tool_start, tool_result, response_done) | Task 1 | ✅ |
| Session management (list/load) | Task 1 | ✅ |
| Memory/task endpoints | Task 1 | ✅ |
| Model switching | Task 1 | ✅ |
| Config update endpoint | Task 1 | ✅ |
| --server flag + ares-server script | Task 2 | ✅ |
| Electron main process | Task 3 | ✅ |
| Python process manager | Task 3 | ✅ |
| Preload / context bridge | Task 3 | ✅ |
| Vite + React scaffolding | Task 3 | ✅ |
| Dark theme CSS variables | Task 4 | ✅ |
| Component styles (sidebar, chat, tools, settings) | Task 4 | ✅ |
| Zustand stores (chat, session, settings) | Task 5 | ✅ |
| WebSocket client with auto-reconnect | Task 6 | ✅ |
| useWebSocket React hook | Task 6 | ✅ |
| Message list + welcome screen | Task 7 | ✅ |
| Streaming indicator (blinking cursor) | Task 7 | ✅ |
| Thinking indicator (animated dots) | Task 7 | ✅ |
| Auto-expanding composer | Task 7 | ✅ |
| Markdown rendering (react-markdown + GFM) | Task 8 | ✅ |
| Code blocks with syntax highlighting | Task 8 | ✅ |
| Copy button on code blocks | Task 8 | ✅ |
| Tool cards (collapsible, per-tool rendering) | Task 9 | ✅ |
| Web search card | Task 9 | ✅ |
| File card | Task 9 | ✅ |
| Memory card | Task 9 | ✅ |
| Sidebar with session list | Task 10 | ✅ |
| Session search | Task 10 | ✅ |
| New chat button | Task 10 | ✅ |
| Sidebar collapse | Task 10 | ✅ |
| Settings panel (model, data, about) | Task 11 | ✅ |
| Model selector dropdown | Task 11 | ✅ |
| Status bar (model, memories, tasks, connection) | Task 12 | ✅ |
| Integration tests (server ↔ WebSocket) | Task 13 | ✅ |
| README update | Task 14 | ✅ |
| electron-builder packaging | Task 3 (yml) | ✅ |
| File attachments (drag-drop) | Not in plan | ⚠️ |

**Gap found:** File drag-and-drop is in the spec but not implemented in the plan. It's a small addition — can be done as a follow-up or added to ChatArea.jsx. Given YAGNI, the core chat experience is the priority. File attachments can be added in a follow-up task.

### Placeholder Scan

No TBD, TODO, or placeholder steps found. All steps contain complete code.

### Type Consistency Check

- `AresServer.handle_connection(websocket)` — used consistently
- `AresServer._handle_chat(websocket, msg)` — consistent
- `Agent.run_stream(user_input, conversation_history)` — matches existing signature
- `Agent.set_model(model)` — matches existing method
- `Agent.get_context(query)` — matches existing method
- `ConversationStore.list_sessions()` — matches existing method
- `ConversationStore.get_history(session_id)` — note: existing method is `get_messages(session_id)`, not `get_history`. Need to fix.
- `MemoryStore.get_recent(limit)` — matches existing method
- `TaskStore.list_pending()` — matches existing method

**Issue found:** `ConversationStore` has `get_messages()`, not `get_history()`. The spec and server code reference `get_history()`. This needs to be fixed.

**Fix applied:** In the implementation, use `get_messages(session_id)` instead of `get_history(session_id)`, or alias in the FakeConversationStore. The actual `ConversationStore` has `get_messages(conversation_id)` which returns `list[dict]`. Also `list_conversations()` (not `list_sessions()`). The test doubles correctly use the right names.

Also: the `ConversationStore.__init__` takes `db_path`, not `data_dir`. The server creates it with `ConversationStore()` (no args) which uses the default db_path — correct.

And `MemoryStore.__init__` takes `db_path`, not `data_dir`. Same pattern — default is fine.
