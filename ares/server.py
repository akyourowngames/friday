"""WebSocket server for the Ares desktop app."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

try:  # websockets 13+
    from websockets.asyncio.server import ServerConnection, serve
except ImportError:  # pragma: no cover
    from websockets.server import WebSocketServerProtocol as ServerConnection, serve

try:
    from websockets.exceptions import ConnectionClosed
except ImportError:  # pragma: no cover
    from websockets.exceptions import ConnectionClosedError as ConnectionClosed

from ares.agent import Agent
from ares.config import load_config, save_config
from ares.context_manager import ContextManager
from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.mcp_client import MCPClientManager


TOOL_TOKEN_RE = re.compile(r"^\[tool:([^:]+):(.*)\]$", re.DOTALL)
MAX_CONTEXT_MESSAGES = 40


def parse_tool_token(token: str) -> tuple[str, str] | None:
    """Parse an Agent tool token into a tool name and serialized payload."""
    match = TOOL_TOKEN_RE.match(token)
    if not match:
        return None
    return match.group(1), match.group(2)


def _trim_history(history: list[dict], max_messages: int = MAX_CONTEXT_MESSAGES) -> list[dict]:
    """Trim conversation history to fit within context limits.

    Keeps the system prompt (first message), then the most recent messages.
    Strips tool call details from older messages to save tokens.
    """
    if len(history) <= max_messages:
        return history

    trimmed = history[-max_messages:]

    for index, msg in enumerate(trimmed[:-6]):
        if msg.get("tool_calls"):
            trimmed[index] = {**msg, "tool_calls": None}
    return trimmed


def _as_jsonable(value: Any) -> Any:
    """Best-effort conversion for sqlite rows, dataclasses, and plain objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _as_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_as_jsonable(v) for v in value]
    if hasattr(value, "keys"):
        return {str(k): _as_jsonable(value[k]) for k in value.keys()}
    if hasattr(value, "__dict__"):
        return {
            str(k): _as_jsonable(v)
            for k, v in vars(value).items()
            if not k.startswith("_")
        }
    return str(value)


def _safe_json_loads(text: str) -> Any:
    with suppress(json.JSONDecodeError, TypeError):
        return json.loads(text)
    return text


class AresServer:
    """WebSocket JSON bridge around the existing Ares Agent."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        config: AppConfig | None = None,
        agent: Agent | None = None,
        memory_store: MemoryStore | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config or load_config()
        self.memory_store = memory_store or MemoryStore()
        self.conversation_store = conversation_store or ConversationStore()
        self.mcp_manager = (
            MCPClientManager(self.config.mcp_servers, data_dir=self.config.data_dir)
            if self.config.mcp_servers
            else None
        )
        self.agent = agent or Agent(
            config=self.config,
            memory_store=self.memory_store,
            mcp_manager=self.mcp_manager,
        )

        self._connected_websockets: list = []
        self.conversation_id = None
        self.conversation_store.delete_empty_conversations()
        self._server = None
        self.context_manager = ContextManager(
            config=self.config,
            llm_client=self.agent.llm,
            memory_store=self.memory_store,
        )
        self._terminal_output_buffer: dict[str, str] = {}
        self._terminal_command_events: dict[str, asyncio.Event] = {}

        # Wire terminal display callback to ToolExecutor
        if hasattr(self.agent, "tool_executor"):
            self.agent.tool_executor._terminal_display_callback = self._terminal_display_only

    async def _push_status_to_clients(self) -> None:
        """Push updated status to all connected websockets."""
        status = self._status()
        for ws in list(self._connected_websockets):
            try:
                await self._send(ws, status)
            except Exception:
                pass

    async def run_forever(self) -> None:
        """Start the WebSocket server and block until cancelled."""
        if self.mcp_manager is not None:
            await self.mcp_manager.start()
            if hasattr(self.agent, "refresh_tools"):
                self.agent.refresh_tools()
        async with serve(self.handle_client, self.host, self.port) as ws_server:
            self._server = ws_server
            print(f"Ares desktop server listening on ws://{self.host}:{self.port}")
            await asyncio.Future()

    async def handle_client(self, websocket: ServerConnection) -> None:
        """Handle a connected desktop renderer."""
        self._connected_websockets.append(websocket)
        try:
            await self._send(websocket, self._session_info())
            await self._send(websocket, self._status())
            async for raw in websocket:
                await self.handle_message(websocket, raw)
        except ConnectionClosed:
            pass  # client disconnected (e.g. health-check probe or user closed app)
        finally:
            if websocket in self._connected_websockets:
                self._connected_websockets.remove(websocket)

    async def handle_message(self, websocket: Any, raw: str | bytes) -> None:
        """Handle one client JSON message."""
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            await self._send_error(websocket, "Invalid JSON message")
            return

        msg_type = message.get("type")
        try:
            if msg_type == "chat":
                await self._handle_chat(websocket, message)
            elif msg_type == "new_session":
                await self._handle_new_session(websocket)
            elif msg_type == "list_sessions":
                await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})
            elif msg_type == "load_session":
                await self._handle_load_session(websocket, message)
            elif msg_type == "set_model":
                await self._handle_set_model(websocket, message)
            elif msg_type == "get_context":
                await self._send(
                    websocket,
                    {"type": "context", "content": self.agent.get_context(message.get("query", ""))},
                )
            elif msg_type == "get_memories":
                await self._send(websocket, {"type": "memories", "memories": self._memories()})
            elif msg_type == "get_status":
                await self._send(websocket, self._status())
            elif msg_type == "rename_session":
                await self._handle_rename_session(websocket, message)
            elif msg_type == "delete_session":
                await self._handle_delete_session(websocket, message)
            elif msg_type == "terminal:exec":
                await self._handle_terminal_exec(websocket, message)
            elif msg_type == "terminal:exec_result":
                cmd_id = message.get("cmd_id", "")
                output = message.get("output", "")
                if cmd_id in self._terminal_command_events:
                    self._terminal_output_buffer[cmd_id] = output
                    self._terminal_command_events[cmd_id].set()
            else:
                await self._send_error(websocket, f"Unknown message type: {msg_type}")
        except Exception as exc:  # pragma: no cover - guardrail for desktop runtime
            await self._send_error(websocket, str(exc))

    async def _handle_chat(self, websocket: Any, message: dict[str, Any]) -> None:
        content = str(message.get("content") or "").strip()
        if not content:
            await self._send_error(websocket, "Message content is required")
            return

        session_id = message.get("session_id")
        if session_id:
            session_id = int(session_id)
            # Validate session exists; if not, create a new one
            existing = self.conversation_store.get_messages(session_id)
            if not existing and not self._conversation_exists(session_id):
                session_id = self.conversation_store.start_conversation()
        else:
            session_id = self.conversation_store.start_conversation()
        self.conversation_id = session_id
        history = self._conversation_history(session_id)
        history = self._sanitize_history(history)
        history = self.context_manager.before_send(history)
        response_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                async for chunk in self.agent.run_stream(content, conversation_history=history):
                    parsed = parse_tool_token(chunk)
                    if parsed:
                        tool_name, tool_content = parsed
                        payload = _safe_json_loads(tool_content)
                        args = self._tool_args(tool_name, payload)
                        tool_call = {
                            "tool": tool_name,
                            "args": args,
                            "content": payload,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                        tool_calls.append(tool_call)
                        await self._send(
                            websocket,
                            {"type": "tool_start", "tool": tool_name, "args": args},
                        )
                        await self._send(
                            websocket,
                            {"type": "tool_result", "tool": tool_name, "content": payload},
                        )
                        continue

                    response_parts.append(chunk)
                    await self._send(websocket, {"type": "content", "text": chunk})
                break  # Success, exit retry loop
            except Exception as exc:
                error_str = str(exc)
                is_context_error = (
                    "400" in error_str
                    or "too long" in error_str.lower()
                    or "context" in error_str.lower()
                    or "token" in error_str.lower()
                    or "maximum" in error_str.lower()
                )
                if is_context_error and attempt < max_retries:
                    keep = max(4, len(history) // (2 ** (attempt + 1)))
                    history = history[:1] + history[-keep:]
                    response_parts.clear()
                    tool_calls.clear()
                    continue
                await self._send_error(websocket, error_str)
                return

        full_response = "".join(response_parts).strip()
        import json as _json
        tool_calls_json = _json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        self.conversation_store.add_exchange(session_id, content, full_response, tool_calls_json)

        await self._send(
            websocket,
            {
                "type": "response_done",
                "content": full_response,
                "tool_calls": tool_calls,
                "session_id": session_id,
            },
        )
        await self._send(websocket, self._session_info())
        await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})
        await self._send(websocket, self._status())

    async def _handle_new_session(self, websocket: Any) -> None:
        if self.conversation_id:
            try:
                history = self._conversation_history(self.conversation_id)
                self.context_manager.after_session(history)
            except Exception:
                pass
        self.conversation_id = None
        await self._send(websocket, self._session_info())
        await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})

    async def _handle_rename_session(self, websocket: Any, message: dict[str, Any]) -> None:
        session_id = int(message.get("session_id") or 0)
        title = str(message.get("title") or "").strip()
        if not session_id or not title:
            await self._send_error(websocket, "session_id and title are required")
            return
        self.conversation_store.rename_conversation(session_id, title)
        await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})

    async def _handle_delete_session(self, websocket: Any, message: dict[str, Any]) -> None:
        session_id = int(message.get("session_id") or 0)
        if not session_id:
            await self._send_error(websocket, "session_id is required")
            return
        self.conversation_store.delete_conversation(session_id)
        if self.conversation_id == session_id:
            self.conversation_id = None
            await self._send(websocket, self._session_info())
        await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})

    async def _handle_load_session(self, websocket: Any, message: dict[str, Any]) -> None:
        session_id = int(message.get("session_id") or self.conversation_id)
        self.conversation_id = session_id
        await self._send(
            websocket,
            {
                "type": "session_history",
                "session_id": session_id,
                "messages": self._conversation_history(session_id),
            },
        )
        await self._send(websocket, self._session_info())

    async def _handle_set_model(self, websocket: Any, message: dict[str, Any]) -> None:
        model = str(message.get("model") or "").strip()
        if not model:
            await self._send_error(websocket, "Model is required")
            return
        self.config.model = model
        save_config(self.config)
        self.agent.set_model(model)
        await self._send(websocket, {"type": "model_updated", "model": model})
        await self._send(websocket, self._status())

    def _session_info(self) -> dict[str, Any]:
        return {
            "type": "session_info",
            "session_id": self.conversation_id,
            "model": self.config.model,
        }

    def _conversation_exists(self, conversation_id: int) -> bool:
        """Check if a conversation row exists in the database."""
        row = self.conversation_store.conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return row is not None

    def _status(self) -> dict[str, Any]:
        context_usage = {"used": 0, "total": 128000, "percent": 0, "breakdown": {}}
        if self.conversation_id:
            history = self._conversation_history(self.conversation_id)
            from ares.context_blend import (
                TokenEstimator,
                estimate_token_breakdown,
            )
            est = TokenEstimator()
            used = est.estimate_history(history)
            total = est.estimate_context_window(self.config.model)

            # Estimate token breakdown for UI display
            system_prompt = ""
            try:
                from ares.prompts import SYSTEM_PROMPT
                system_prompt = SYSTEM_PROMPT or ""
            except Exception:
                pass
            breakdown = estimate_token_breakdown(system_prompt, history)

            context_usage = {
                "used": used,
                "total": total,
                "percent": round(used / total * 100, 1) if total > 0 else 0,
                "breakdown": breakdown,
            }
        return {
            "type": "status",
            "model": self.config.model,
            "memory_count": len(self._memories()),
            "session_id": self.conversation_id,
            "context_usage": context_usage,
        }

    def _conversation_history(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conversation_store.get_messages(session_id)
        history: list[dict[str, Any]] = []
        for row in rows:
            item = _as_jsonable(row)
            role = item.get("role") or item.get("speaker") or "assistant"
            content = item.get("content") or item.get("message") or item.get("text") or ""
            msg = {
                "id": item.get("id"),
                "role": role,
                "content": content,
                "created_at": item.get("created_at") or item.get("timestamp"),
            }
            if item.get("tool_calls"):
                msg["tool_calls"] = item["tool_calls"]
            history.append(msg)
        return history

    def _sanitize_history(self, history: list[dict]) -> list[dict]:
        """Clean conversation history to avoid API 400 errors."""
        sanitized = []
        for msg in history:
            content = msg.get("content", "")
            if not isinstance(content, str):
                content = str(content)
            content = content.replace("\x00", "")
            if len(content) > 50000:
                content = content[:50000] + "\n... [truncated]"
            sanitized.append({**msg, "content": content})
        return sanitized

    def _sessions(self) -> list[dict[str, Any]]:
        self.conversation_store.delete_empty_conversations()
        sessions: list[dict[str, Any]] = []
        for row in self.conversation_store.list_conversations():
            item = _as_jsonable(row)
            session_id = int(item.get("id") or item.get("conversation_id"))
            history = self._conversation_history(session_id)
            title = self._session_title(item, history)
            sessions.append(
                {
                    "id": session_id,
                    "title": title,
                    "summary": item.get("summary") or "",
                    "started_at": item.get("started_at"),
                    "ended_at": item.get("ended_at"),
                    "message_count": len(history),
                }
            )
        return sessions

    def _session_title(self, row: dict[str, Any], history: list[dict[str, Any]]) -> str:
        summary = str(row.get("summary") or "").strip()
        if summary:
            return summary[:80]
        for message in history:
            if message.get("role") == "user" and str(message.get("content") or "").strip():
                return str(message["content"]).strip().replace("\n", " ")[:80]
        return "New session"

    def _memories(self) -> list[dict[str, Any]]:
        with suppress(TypeError):
            return [_as_jsonable(item) for item in self.memory_store.get_recent(limit=100)]
        return [_as_jsonable(item) for item in self.memory_store.get_recent()]

    def _tool_args(self, tool_name: str, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            if tool_name == "web_search":
                return {
                    key: payload[key]
                    for key in ("query", "max_results", "fetch_top")
                    if key in payload
                }
            if "path" in payload:
                return {"path": payload["path"]}
            if "query" in payload:
                return {"query": payload["query"]}
        return {}

    async def _handle_terminal_exec(self, websocket: Any, message: dict) -> None:
        """Forward a command to the frontend terminal."""
        command = message.get("command", "")
        cmd_id = f"cmd-{id(message)}"

        self._terminal_output_buffer[cmd_id] = ""
        self._terminal_command_events[cmd_id] = asyncio.Event()

        await self._send(websocket, {
            "type": "terminal:exec",
            "command": command,
            "cmd_id": cmd_id,
        })

        timeout = message.get("timeout", 30)
        try:
            await asyncio.wait_for(
                self._terminal_command_events[cmd_id].wait(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            await self._send_error(websocket, f"Terminal command timed out after {timeout}s")
        finally:
            output = self._terminal_output_buffer.pop(cmd_id, "")
            self._terminal_command_events.pop(cmd_id, None)

    async def _terminal_exec_via_websocket(self, command: str, wait: bool = True, timeout: int = 30) -> str:
        """Send a command to the frontend terminal and optionally wait for result."""
        if not self._connected_websockets:
            return "Error: No desktop client connected."

        cmd_id = f"cmd-{int(asyncio.get_event_loop().time() * 1000)}"
        self._terminal_output_buffer[cmd_id] = ""
        event = asyncio.Event()
        self._terminal_command_events[cmd_id] = event

        ws = self._connected_websockets[0]
        await self._send(ws, {
            "type": "terminal:exec",
            "command": command,
            "cmd_id": cmd_id,
        })

        if not wait:
            return f"Command sent to terminal: {command}"

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return f"Error: Terminal command timed out after {timeout}s"
        finally:
            output = self._terminal_output_buffer.pop(cmd_id, "")
            self._terminal_command_events.pop(cmd_id, None)

        return output if output else f"Command completed (no output): {command}"

    def _terminal_exec_sync(self, command: str, wait: bool = True, timeout: int = 30) -> str:
        """Synchronous wrapper — fire command to terminal, return immediately."""
        if not self._connected_websockets:
            return "Error: No desktop client connected. Open the terminal panel first."

        # Schedule the async version in the running event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            asyncio.ensure_future(self._terminal_exec_via_websocket(command, wait=False, timeout=timeout))
            return f"Command sent to terminal: {command}"

        return "Error: No event loop available for terminal command."

    def _terminal_display_only(self, command: str) -> None:
        """Fire-and-forget: display a command in the visual terminal panel."""
        if not self._connected_websockets:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            ws = self._connected_websockets[0]
            asyncio.ensure_future(self._send(ws, {
                "type": "terminal:exec",
                "command": command,
                "cmd_id": f"display-{id(command)}",
            }))

    async def _send_error(self, websocket: Any, message: str) -> None:
        await self._send(websocket, {"type": "error", "message": message})

    async def _send(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    async def close(self) -> None:
        """Shut down stores."""
        if self.mcp_manager is not None:
            with suppress(Exception):
                await self.mcp_manager.close()
        for obj in (
            self.agent,
            self.conversation_store,
            self.memory_store,
        ):
            close = getattr(obj, "close", None)
            if close:
                with suppress(Exception):
                    result = close()
                    if result is not None:
                        await result


async def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = AresServer(host=host, port=port)
    try:
        await server.run_forever()
    finally:
        await server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ares desktop WebSocket server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(run_server(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
