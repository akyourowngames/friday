# MCP Client Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect Ares to external MCP servers (Google Calendar, Gmail) as an MCP client using Streamable HTTP transport.

**Architecture:** New `ares/tools/mcp_client.py` module with `MCPServerConfig` (data model), `MCPAuthProvider` (OAuth 2.1 PKCE flow), and `MCPClientManager` (session lifecycle + tool routing). Wired into `Agent.__init__` and `process_tool_calls`, with lifecycle managed by CLI and Server.

**Tech Stack:** Python 3.11+, `mcp>=1.28.0`, `httpx` (existing), `pydantic` (existing), stdlib `asyncio` + `http.server` for OAuth callback.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `ares/tools/mcp_client.py` | **Create** | `MCPServerConfig`, `MCPAuthProvider`, `MCPClientManager` — all MCP client logic |
| `ares/models.py` | Modify | Add `mcp_servers: list[dict]` field to `AppConfig` |
| `ares/agent.py` | Modify | Accept `mcp_manager`, build tool list including MCP tools, route `mcp__` calls |
| `ares/cli.py` | Modify | Init/start/close manager in lifecycle |
| `ares/server.py` | Modify | Init/start/close manager in lifecycle |
| `pyproject.toml` | Modify | Add `mcp>=1.28.0` dependency |
| `tests/test_mcp_client.py` | **Create** | Tests for MCPAuthProvider + MCPClientManager |

---

### Task 1: Add mcp dependency and config field

**Files:**
- Modify: `pyproject.toml`
- Modify: `ares/models.py:70-107`

- [ ] **Step 1: Add mcp dependency to pyproject.toml**

Edit `pyproject.toml` dependencies list to add `"mcp>=1.28.0"`:

```
dependencies = [
    "rich>=13.0",
    "prompt_toolkit>=3.0",
    "sentence-transformers[onnx]>=3.2",
    "sqlite-vec>=0.1",
    "httpx>=0.27",
    "pydantic>=2.0",
    "dateparser>=1.2",
    "tzlocal>=5.0",
    "plyer>=2.1",
    "ddgs>=9.0",
    "websockets>=12.0",
    "Pillow>=10.0",
    "mcp>=1.28.0",
]
```

- [ ] **Step 2: Add mcp_servers field to AppConfig**

Edit `ares/models.py`, add after `skill_auto_suggest: bool = True` (line 106):

```python
    mcp_servers: list[dict] = Field(
        default_factory=list,
        description="MCP server configurations. Each entry: {name, server_url, oauth_client_id, oauth_client_secret, oauth_scopes}",
    )
```

- [ ] **Step 3: Install the dependency and commit**

Run: `cd C:\Users\anime\ares && pip install mcp>=1.28.0`

```bash
git add pyproject.toml ares/models.py
git commit -m "chore: add mcp dependency and config field for MCP client integration"
```

---

### Task 2: Create MCPAuthProvider with PKCE OAuth flow

**Files:**
- Create: `ares/tools/mcp_client.py`

This is the first half of the new module — data model + auth provider.

- [ ] **Step 1: Write MCPServerConfig and MCPAuthProvider**

Create `ares/tools/mcp_client.py` with the following content:

```python
"""MCP client integration — connect Ares to external MCP servers via Streamable HTTP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel


class MCPServerConfig(BaseModel):
    """Configuration for one MCP server."""
    name: str
    server_url: str
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scopes: list[str] = []


class MCPAuthProvider:
    """OAuth 2.1 PKCE authorization for MCP servers.

    Manages token storage in ~/.ares/data/mcp_tokens/{name}.json,
    runs the browser-based PKCE flow on first auth, and refreshes
    tokens automatically.
    """

    def __init__(self, data_dir: str = "~/.ares/data"):
        self.token_dir = Path(data_dir).expanduser() / "mcp_tokens"
        self.token_dir.mkdir(parents=True, exist_ok=True)

    def load_token(self, name: str) -> dict | None:
        """Load a stored token for the given server, or None."""
        path = self.token_dir / f"{name}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def store_token(self, name: str, token_data: dict) -> None:
        """Store token data, computing expires_at from expires_in if needed."""
        data = dict(token_data)
        if "expires_in" in data and "expires_at" not in data:
            data["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=int(data["expires_in"]))
            ).isoformat()
        (self.token_dir / f"{name}.json").write_text(json.dumps(data, indent=2))

    def _is_expired(self, token: dict) -> bool:
        expires = token.get("expires_at")
        if not expires:
            return False
        try:
            return datetime.now(timezone.utc) >= datetime.fromisoformat(expires)
        except (ValueError, TypeError):
            return False

    async def ensure_token(self, config: MCPServerConfig) -> str:
        """Return a valid Bearer token, running OAuth flow if needed."""
        token = self.load_token(config.name)
        if token and not self._is_expired(token):
            return token["access_token"]
        if token and token.get("refresh_token"):
            return await self._refresh_token(config, token)
        return await self._run_pkce_flow(config)

    async def _discover_endpoints(self, config: MCPServerConfig) -> dict[str, str]:
        """Discover OAuth endpoints. Google uses fixed endpoints; others use RFC8414."""
        if "googleapis.com" in config.server_url:
            return {
                "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                "token_endpoint": "https://oauth2.googleapis.com/token",
            }
        parsed = urlparse(config.server_url)
        auth_base = f"{parsed.scheme}://{parsed.netloc}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{auth_base}/.well-known/oauth-authorization-server",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "authorization_endpoint": data.get(
                            "authorization_endpoint", f"{auth_base}/authorize"
                        ),
                        "token_endpoint": data.get(
                            "token_endpoint", f"{auth_base}/token"
                        ),
                    }
        except Exception:
            pass
        return {
            "authorization_endpoint": f"{auth_base}/authorize",
            "token_endpoint": f"{auth_base}/token",
        }

    async def _run_pkce_flow(self, config: MCPServerConfig) -> str:
        """Run the PKCE authorization code flow via browser + local callback server."""
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        state = secrets.token_urlsafe(32)
        endpoints = await self._discover_endpoints(config)
        redirect_uri = "http://127.0.0.1:9865/callback"

        auth_url = (
            f"{endpoints['authorization_endpoint']}"
            f"?response_type=code"
            f"&client_id={config.oauth_client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&code_challenge={code_challenge}"
            f"&code_challenge_method=S256"
            f"&state={state}"
            f"&scope={' '.join(config.oauth_scopes)}"
            f"&access_type=offline"
            f"&prompt=consent"
        )

        print(f"\n[MCP] Opening browser for {config.name} authorization...")
        webbrowser.open(auth_url)
        code = await self._wait_for_callback(state, timeout=300)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                endpoints["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": config.oauth_client_id,
                    "client_secret": config.oauth_client_secret,
                    "code_verifier": code_verifier,
                },
            )
            token_data = resp.json()

        if "access_token" not in token_data:
            raise ValueError(
                f"OAuth token exchange failed for {config.name}: {token_data}"
            )

        self.store_token(config.name, token_data)
        return token_data["access_token"]

    async def _wait_for_callback(
        self, expected_state: str, timeout: int = 300
    ) -> str:
        """Start a local HTTP server on 127.0.0.1:9865 and wait for the OAuth callback."""
        received_code: str | None = None
        received_state: str | None = None
        event = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            nonlocal received_code, received_state
            # Read request line + headers (consume entire request)
            request_data = await reader.readuntil(b"\r\n\r\n")
            request_line = request_data.split(b"\r\n")[0].decode()
            # Parse query params from the path
            path = request_line.split(" ")[1]
            params = parse_qs(urlparse(path).query)
            received_code = params.get("code", [None])[0]
            received_state = params.get("state", [None])[0]
            event.set()
            # Return success page to browser
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Connection: close\r\n\r\n"
                b"<html><body><h1>Authorized!</h1>"
                b"<p>You can close this tab and return to Ares.</p></body></html>"
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 9865)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            if received_state != expected_state:
                raise ValueError(
                    f"OAuth state mismatch. Expected {expected_state}, got {received_state}"
                )
            if not received_code:
                raise ValueError("No authorization code received in callback")
            return received_code
        finally:
            server.close()
            await server.wait_closed()

    async def _refresh_token(self, config: MCPServerConfig, token: dict) -> str:
        """Refresh an expired token using its refresh token."""
        endpoints = await self._discover_endpoints(config)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                endpoints["token_endpoint"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "client_id": config.oauth_client_id,
                    "client_secret": config.oauth_client_secret,
                },
            )
            new_token = resp.json()

        if "access_token" not in new_token:
            raise ValueError(
                f"Token refresh failed for {config.name}: {new_token}"
            )

        if "refresh_token" not in new_token:
            new_token["refresh_token"] = token["refresh_token"]

        self.store_token(config.name, new_token)
        return new_token["access_token"]
```

- [ ] **Step 2: Write a quick test to verify the module imports and MCPServerConfig works**

Add to the end of the file temporarily and run:
```python
if __name__ == "__main__":
    cfg = MCPServerConfig(name="test", server_url="https://example.com/mcp")
    print(cfg)
    print("MCPAuthProvider OK")
```

Run: `cd C:\Users\anime\ares && python -c "from ares.tools.mcp_client import MCPServerConfig, MCPAuthProvider; print('imports OK')"`

Expected: `imports OK`

- [ ] **Step 3: Remove the test block and commit**

```bash
git add ares/tools/mcp_client.py
git commit -m "feat: add MCPServerConfig and MCPAuthProvider with PKCE OAuth flow"
```

---

### Task 3: Create MCPClientManager

**Files:**
- Modify: `ares/tools/mcp_client.py` (append MCPClientManager class)

- [ ] **Step 1: Add the import block and MCPClientManager class**

Add these imports at the top of `ares/tools/mcp_client.py` (after the existing imports):

```python
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
```

Then append `MCPClientManager` at the end of the file:

```python
class MCPClientManager:
    """Manages connections to all configured MCP servers.

    On start(), connects each server, lists available tools, and caches
    them as OpenAI-format schemas with mcp__{server}__{tool} namespacing.
    Provides call_tool() to route tool calls to the correct server session.
    """

    def __init__(
        self,
        server_configs: list[dict],
        data_dir: str = "~/.ares/data",
    ):
        self.servers: dict[str, MCPServerConfig] = {
            cfg["name"]: MCPServerConfig(**cfg) for cfg in server_configs
        }
        self.sessions: dict[str, ClientSession] = {}
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self.auth = MCPAuthProvider(data_dir)
        self.tool_definitions: list[dict] = []

    async def start(self) -> None:
        """Connect to all configured servers and cache their tool definitions."""
        for name in self.servers:
            try:
                await self._connect_server(name)
            except Exception as e:
                print(f"[MCP] Failed to connect '{name}': {e}")

    async def close(self) -> None:
        """Disconnect all servers and clean up resources."""
        for name in list(self._exit_stacks.keys()):
            try:
                await self._exit_stacks[name].aclose()
            except Exception as e:
                print(f"[MCP] Error closing '{name}': {e}")
        self.sessions.clear()
        self._exit_stacks.clear()
        self.tool_definitions = []

    async def _connect_server(self, name: str) -> None:
        """Connect to a single server, authenticate, list tools, and cache definitions."""
        config = self.servers[name]
        token = await self.auth.ensure_token(config)

        http_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "ares-mcp-client/0.1"}
        )

        exit_stack = AsyncExitStack()
        await exit_stack.enter_async_context(http_client)
        self._exit_stacks[name] = exit_stack

        streams = await exit_stack.enter_async_context(
            streamable_http_client(config.server_url, http_client=http_client)
        )
        session = await exit_stack.enter_async_context(
            ClientSession(streams[0], streams[1])
        )
        await session.initialize()
        self.sessions[name] = session

        # List and cache tool definitions
        tools_response = await session.list_tools()
        for tool in tools_response.tools:
            self.tool_definitions.append(self._to_openai_schema(name, tool))

    def _to_openai_schema(self, server_name: str, tool: Any) -> dict:
        """Convert an MCP Tool object to OpenAI function-calling format."""
        return {
            "type": "function",
            "function": {
                "name": f"mcp__{server_name}__{tool.name}",
                "description": tool.description or "",
                "parameters": tool.inputSchema or {"type": "object", "properties": {}},
            },
        }

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Execute an MCP tool by its namespaced name (mcp__server__tool)."""
        parts = tool_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            return f"Error: invalid MCP tool name '{tool_name}'"
        _, server_name, tool = parts

        if server_name not in self.sessions:
            return f"Error: MCP server '{server_name}' is not connected"

        session = self.sessions[server_name]
        try:
            result = await session.call_tool(tool, arguments=arguments)
            text_parts = []
            for item in result.content:
                if hasattr(item, "text") and item.text:
                    text_parts.append(item.text)
                elif hasattr(item, "data"):
                    text_parts.append(f"[binary data: {getattr(item, 'mimeType', 'unknown')}]")
                else:
                    text_parts.append(str(item))
            return "\n".join(text_parts) if text_parts else "(no output)"
        except Exception as e:
            return f"Error calling MCP tool '{tool_name}': {e}"
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd C:\Users\anime\ares && python -c "from ares.tools.mcp_client import MCPServerConfig, MCPAuthProvider, MCPClientManager; print('all imports OK')"`

Expected: `all imports OK`

- [ ] **Step 3: Commit**

```bash
git add ares/tools/mcp_client.py
git commit -m "feat: add MCPClientManager with server connection, tool caching, and call routing"
```

---

### Task 4: Wire MCPClientManager into Agent

**Files:**
- Modify: `ares/agent.py`

- [ ] **Step 1: Accept mcp_manager in Agent.__init__**

Edit `ares/agent.py`, modify the `__init__` signature and tool setup:

After the existing imports, no new imports needed. Change `__init__` signature to accept `mcp_manager` parameter and build tools list:

```python
    def __init__(
        self,
        memory_store: MemoryStore,
        task_store: TaskStore,
        conversation_store: ConversationStore | None = None,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        config: AppConfig | None = None,
        task_executor: Any | None = None,
        mcp_manager: Any | None = None,            # NEW
    ):
        self.mcp_manager = mcp_manager              # NEW
        # ... existing code ...
```

Then after `self.tools = get_tool_definitions()` (line 45), add:

```python
        if mcp_manager is not None:
            self.tools.extend(mcp_manager.tool_definitions)
```

- [ ] **Step 2: Route mcp__ tool calls in process_tool_calls**

Change `process_tool_calls` to handle MCP tools. The method is currently synchronous
but both callers (`run` and `run_stream`) are async, so make it async:

Replace the method signature and add the MCP routing check at the top of the try block:

```python
    async def process_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """Execute tool calls locally and return results with local metadata."""
        results = []
        auto_task_created = False
        for i, call in enumerate(tool_calls):
            tool_name = call.get("function", {}).get("name", "unknown")

            # --- NEW: Route MCP tools to the manager ---
            if tool_name.startswith("mcp__") and self.mcp_manager is not None:
                fn = call["function"]
                args = self._tool_call_args(call)
                try:
                    result = await self.mcp_manager.call_tool(tool_name, args)
                except Exception as e:
                    result = f"Error: {e}"
                results.append({
                    "tool_call_id": call.get("id") or f"call_{i}",
                    "role": "tool",
                    "content": result,
                    "tool_name": tool_name,
                })
                continue
            # --- END NEW ---

            if auto_task_created:
                result = (
                    "Skipped: an auto-executable task was just queued. "
                    "The background TaskExecutor must perform the work so events and artifacts are tracked."
                )
                results.append({
                    "tool_call_id": call.get("id") or f"call_{i}",
                    "role": "tool",
                    "content": result,
                    "tool_name": tool_name,
                    "skipped_after_auto_task": True,
                })
                continue

            try:
                fn = call["function"]
                tool_name = fn["name"]
                args = self._tool_call_args(call)
                result = self.tool_executor.execute(tool_name, args)
            except Exception as e:
                result = f"Error: {e}"
                args = {}

            is_auto_task = tool_name == "create_task" and bool(args.get("auto_executable", False)) and not str(result).lower().startswith("error:")
            if is_auto_task:
                auto_task_created = True

            results.append({
                "tool_call_id": call.get("id") or f"call_{i}",
                "role": "tool",
                "content": result,
                "tool_name": tool_name,
                "auto_task_created": is_auto_task,
            })
        return results
```

- [ ] **Step 3: Update the two call sites to use await**

In `run()` (line ~226), change:

```python
                tool_results = self.process_tool_calls(response["tool_calls"])
```
to:
```python
                tool_results = await self.process_tool_calls(response["tool_calls"])
```

In `run_stream()` (line ~307), change:

```python
                tool_results = self.process_tool_calls(formatted_calls)
```
to:
```python
                tool_results = await self.process_tool_calls(formatted_calls)
```

- [ ] **Step 4: Verify the module compiles**

Run: `cd C:\Users\anime\ares && python -c "from ares.agent import Agent; print('Agent imports OK')"`

Expected: `Agent imports OK`

- [ ] **Step 5: Commit**

```bash
git add ares/agent.py
git commit -m "feat: wire MCPClientManager into Agent for tool routing"
```

---

### Task 5: Wire lifecycle into CLI

**Files:**
- Modify: `ares/cli.py`

- [ ] **Step 1: Import and wire MCPClientManager into AresCLI.__init__**

Add import at top of `ares/cli.py`:

```python
from ares.tools.mcp_client import MCPClientManager
```

Inside `AresCLI.__init__`, after `self.config = load_config()` and before creating the Agent, add:

```python
        self.mcp_manager: MCPClientManager | None = None
        if self.config.mcp_servers:
            self.mcp_manager = MCPClientManager(
                self.config.mcp_servers,
                data_dir=self.config.data_dir,
            )
```

Then in the `Agent(...)` constructor call, add `mcp_manager=self.mcp_manager`:

```python
        self.agent = Agent(
            memory_store=self.memory_store,
            task_store=self.task_store,
            conversation_store=self.conversation_store,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            config=self.config,
            mcp_manager=self.mcp_manager,
        )
```

- [ ] **Step 2: Start MCP connections after agent creation**

After the `self.agent.tool_executor.skill_manager = self.skill_manager` line (shortly after the Agent constructor), add MCP startup:

Since `CLI.__init__` is synchronous, we need to start MCP connections later. The `run()` method is async — add the startup there.

Edit `AresCLI.run()`, at the start of the try block, before the while loop (after the banner display), add:

```python
            if self.mcp_manager is not None:
                await self.mcp_manager.start()
```

- [ ] **Step 3: Close MCP connections during shutdown**

In the `finally` block of `run()`, before `await self.agent.close()`, add:

```python
            if self.mcp_manager is not None:
                try:
                    await self.mcp_manager.close()
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (mcp): {exc}[/dim yellow]")
```

- [ ] **Step 4: Commit**

```bash
git add ares/cli.py
git commit -m "feat: wire MCPClientManager lifecycle into CLI"
```

---

### Task 6: Wire lifecycle into Server

**Files:**
- Modify: `ares/server.py`

- [ ] **Step 1: Import and wire into AresServer.__init__**

Add import at top of `ares/server.py`:

```python
from ares.tools.mcp_client import MCPClientManager
```

Inside `AresServer.__init__`, after the config assignment and before the Agent constructor, add:

```python
        self.mcp_manager: MCPClientManager | None = None
        if self.config.mcp_servers:
            self.mcp_manager = MCPClientManager(
                self.config.mcp_servers,
                data_dir=self.config.data_dir,
            )

```

In the `Agent(...)` constructor in `server.py` (line ~111-115), add `mcp_manager=self.mcp_manager`:

```python
        self.agent = agent or Agent(
            config=self.config,
            memory_store=self.memory_store,
            task_store=self.task_store,
            mcp_manager=self.mcp_manager,
        )
```

- [ ] **Step 2: Start MCP connections during server startup**

In `run_forever()` (line ~333), before the `async with serve(...)`:

```python
        if self.mcp_manager is not None:
            await self.mcp_manager.start()
```

- [ ] **Step 3: Close MCP connections during shutdown**

In `close()` (line ~818), add after `await self.task_executor.stop()`:

```python
            if self.mcp_manager is not None:
                try:
                    await self.mcp_manager.close()
                except Exception:
                    pass
```

- [ ] **Step 4: Commit**

```bash
git add ares/server.py
git commit -m "feat: wire MCPClientManager lifecycle into WebSocket server"
```

---

### Task 7: Write tests

**Files:**
- Create: `tests/test_mcp_client.py`

- [ ] **Step 1: Write unit tests for MCPServerConfig, MCPAuthProvider, and MCPClientManager**

Create `tests/test_mcp_client.py`:

```python
"""Tests for MCP client integration — config, auth, and manager."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ares.tools.mcp_client import MCPServerConfig, MCPAuthProvider, MCPClientManager


class TestMCPServerConfig:
    def test_minimal_config(self):
        cfg = MCPServerConfig(name="test", server_url="https://example.com/mcp")
        assert cfg.name == "test"
        assert cfg.server_url == "https://example.com/mcp"
        assert cfg.oauth_client_id == ""

    def test_full_config(self):
        cfg = MCPServerConfig(
            name="calendar",
            server_url="https://calendarmcp.googleapis.com/mcp/v1",
            oauth_client_id="my-id",
            oauth_client_secret="my-secret",
            oauth_scopes=["https://www.googleapis.com/auth/calendar.events.readonly"],
        )
        assert cfg.oauth_client_id == "my-id"
        assert cfg.oauth_scopes == ["https://www.googleapis.com/auth/calendar.events.readonly"]

    def test_config_from_dict(self):
        raw = {
            "name": "gmail",
            "server_url": "https://gmailmcp.googleapis.com/mcp/v1",
            "oauth_client_id": "id",
            "oauth_client_secret": "secret",
            "oauth_scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        }
        cfg = MCPServerConfig(**raw)
        assert cfg.name == "gmail"


class TestMCPAuthProvider:
    def test_token_storage_roundtrip(self, tmp_path: Path):
        data_dir = str(tmp_path)
        auth = MCPAuthProvider(data_dir=data_dir)
        token_data = {"access_token": "abc", "expires_in": 3600}
        auth.store_token("test-server", token_data)

        stored = auth.load_token("test-server")
        assert stored is not None
        assert stored["access_token"] == "abc"
        # expires_at should have been computed from expires_in
        assert "expires_at" in stored

    def test_load_token_missing(self, tmp_path: Path):
        auth = MCPAuthProvider(data_dir=str(tmp_path))
        assert auth.load_token("nonexistent") is None

    def test_is_expired_with_no_expiry(self, tmp_path: Path):
        auth = MCPAuthProvider(data_dir=str(tmp_path))
        assert not auth._is_expired({"access_token": "abc"})

    def test_is_expired_with_future_expiry(self, tmp_path: Path):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        auth = MCPAuthProvider(data_dir=str(tmp_path))
        assert not auth._is_expired({"access_token": "abc", "expires_at": future})

    def test_is_expired_with_past_expiry(self, tmp_path: Path):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        auth = MCPAuthProvider(data_dir=str(tmp_path))
        assert auth._is_expired({"access_token": "abc", "expires_at": past})


class TestMCPClientManager:
    def test_empty_config(self):
        """No servers configured = no tools, no errors."""
        mgr = MCPClientManager([])
        assert mgr.tool_definitions == []
        assert mgr.servers == {}

    def test_parses_server_configs(self):
        mgr = MCPClientManager([
            {"name": "calendar", "server_url": "https://calendarmcp.googleapis.com/mcp/v1"},
        ])
        assert "calendar" in mgr.servers
        assert mgr.servers["calendar"].server_url == "https://calendarmcp.googleapis.com/mcp/v1"

    def test_call_tool_invalid_name(self):
        mgr = MCPClientManager([])
        result = mgr.call_tool("invalid_name", {})
        assert "invalid MCP tool name" in result

    @pytest.mark.asyncio
    async def test_call_tool_disconnected_server(self):
        mgr = MCPClientManager([
            {"name": "calendar", "server_url": "https://example.com/mcp"},
        ])
        result = await mgr.call_tool("mcp__calendar__list_events", {})
        assert "not connected" in result
```

- [ ] **Step 2: Run the tests**

Run: `cd C:\Users\anime\ares && python -m pytest tests/test_mcp_client.py -v`

Expected: all tests pass (the 2 async tests that depend on network may need the mcp SDK installed but test isolated logic; `test_call_tool_disconnected_server` should pass since it runs after start() which hasn't been called)

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_client.py
git commit -m "test: add unit tests for MCP client integration"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All spec requirements mapped: MCPServerConfig (Task 2), MCPAuthProvider (Task 2), MCPClientManager (Task 3), config field (Task 1), agent wiring (Task 4), CLI lifecycle (Task 5), server lifecycle (Task 6), tests (Task 7).
- [x] **Placeholder scan:** No TBD, TODO, or incomplete code blocks. Every step has concrete code or commands.
- [x] **Type consistency:** MCPServerConfig uses same fields as spec. Agent accepts `mcp_manager` as `Any | None` (avoiding circular imports). `tool_definitions` is a sync attribute on MCPClientManager, consumed synchronously in Agent.__init__. `process_tool_calls` changed to async consistently across both call sites.
