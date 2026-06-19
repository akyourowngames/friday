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
except ImportError:  # pragma: no cover - compatibility for older supported releases
    from websockets.server import WebSocketServerProtocol as ServerConnection
    from websockets.server import serve

from ares.agent import Agent
from ares.config import load_config, save_config
from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tasks import TaskStore


TOOL_TOKEN_RE = re.compile(r"^\[tool:([^:]+):(.*)\]$", re.DOTALL)


def parse_tool_token(token: str) -> tuple[str, str] | None:
    """Parse an Agent tool token into a tool name and serialized payload."""
    match = TOOL_TOKEN_RE.match(token)
    if not match:
        return None
    return match.group(1), match.group(2)


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
        task_store: TaskStore | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.config = config or load_config()
        self.memory_store = memory_store or MemoryStore()
        self.task_store = task_store or TaskStore()
        self.conversation_store = conversation_store or ConversationStore()
        self.agent = agent or Agent(
            config=self.config,
            memory_store=self.memory_store,
            task_store=self.task_store,
        )
        self.conversation_id = self.conversation_store.start_conversation()
        self._server = None

    async def run_forever(self) -> None:
        """Start the WebSocket server and block until cancelled."""
        async with serve(self.handle_client, self.host, self.port) as ws_server:
            self._server = ws_server
            print(f"Ares desktop server listening on ws://{self.host}:{self.port}")
            await asyncio.Future()

    async def handle_client(self, websocket: ServerConnection) -> None:
        """Handle a connected desktop renderer."""
        await self._send(websocket, self._session_info())
        await self._send(websocket, self._status())
        async for raw in websocket:
            await self.handle_message(websocket, raw)

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
            elif msg_type == "get_tasks":
                await self._send(websocket, {"type": "tasks", "tasks": self._tasks()})
            elif msg_type == "get_status":
                await self._send(websocket, self._status())
            else:
                await self._send_error(websocket, f"Unknown message type: {msg_type}")
        except Exception as exc:  # pragma: no cover - guardrail for desktop runtime
            await self._send_error(websocket, str(exc))

    async def _handle_chat(self, websocket: Any, message: dict[str, Any]) -> None:
        content = str(message.get("content") or "").strip()
        if not content:
            await self._send_error(websocket, "Message content is required")
            return

        session_id = int(message.get("session_id") or self.conversation_id)
        self.conversation_id = session_id
        history = self._conversation_history(session_id)
        response_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        async for chunk in self.agent.run_stream(content, history=history):
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

        full_response = "".join(response_parts).strip()
        self.conversation_store.add_exchange(session_id, content, full_response)
        await self._send(
            websocket,
            {
                "type": "response_done",
                "content": full_response,
                "tool_calls": tool_calls,
                "session_id": session_id,
            },
        )
        await self._send(websocket, {"type": "sessions", "sessions": self._sessions()})
        await self._send(websocket, self._status())

    async def _handle_new_session(self, websocket: Any) -> None:
        self.conversation_id = self.conversation_store.start_conversation()
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

    def _status(self) -> dict[str, Any]:
        return {
            "type": "status",
            "model": self.config.model,
            "memory_count": len(self._memories()),
            "task_count": len(self._tasks()),
            "session_id": self.conversation_id,
        }

    def _conversation_history(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.conversation_store.get_messages(session_id)
        history: list[dict[str, Any]] = []
        for row in rows:
            item = _as_jsonable(row)
            role = item.get("role") or item.get("speaker") or "assistant"
            content = item.get("content") or item.get("message") or item.get("text") or ""
            history.append(
                {
                    "id": item.get("id"),
                    "role": role,
                    "content": content,
                    "created_at": item.get("created_at") or item.get("timestamp"),
                }
            )
        return history

    def _sessions(self) -> list[dict[str, Any]]:
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

    def _tasks(self) -> list[dict[str, Any]]:
        if hasattr(self.task_store, "list_pending"):
            return [_as_jsonable(item) for item in self.task_store.list_pending()]
        if hasattr(self.task_store, "list_tasks"):
            return [_as_jsonable(item) for item in self.task_store.list_tasks()]
        return []

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

    async def _send_error(self, websocket: Any, message: str) -> None:
        await self._send(websocket, {"type": "error", "message": message})

    async def _send(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send(json.dumps(payload, ensure_ascii=False))

    def close(self) -> None:
        for obj in (
            self.agent,
            self.conversation_store,
            self.memory_store,
            self.task_store,
        ):
            close = getattr(obj, "close", None)
            if close:
                with suppress(Exception):
                    close()


async def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = AresServer(host=host, port=port)
    try:
        await server.run_forever()
    finally:
        server.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Ares desktop WebSocket server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(run_server(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
