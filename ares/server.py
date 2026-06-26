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
from ares.task_executor import TaskExecutor
from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.tasks import TaskStore


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
        self.task_executor = TaskExecutor(
            self.task_store,
            self._execute_task_in_background,
            self._notify_auto_complete,
            poll_seconds=self.config.task_executor_poll_seconds,
            max_turns=self.config.task_executor_max_turns,
            enabled=self.config.task_executor_enabled,
        )
        if hasattr(self.agent, "tool_executor"):
            self.agent.tool_executor.task_executor = self.task_executor
        self.task_executor.status_callback = self._push_status_to_clients
        self.task_executor.event_callback = self._push_task_event_to_clients

        # v2: Wire planner, LLM, and tool executor
        from ares.planner import TaskPlanner
        from ares.llm import LLMClient
        from ares.tools import get_tool_definitions
        from ares.task_executor import ALLOWED_TOOLS as _EXEC_ALLOWED
        llm = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
        )
        all_tools = get_tool_definitions()
        allowed_tool_defs = [t for t in all_tools if t["function"]["name"] in _EXEC_ALLOWED]
        self.task_executor.planner = TaskPlanner(llm)
        self.task_executor.llm = llm
        self.task_executor.tool_executor = self.agent.tool_executor
        self.task_executor.allowed_tools = allowed_tool_defs
        self.agent.tool_executor.task_executor_ref = self.task_executor

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

    async def _execute_task_in_background(self, prompt: str, max_turns: int) -> dict:
        """Run an isolated agent loop for background task execution."""
        from ares.llm import LLMClient
        from ares.tools import get_tool_definitions, ToolExecutor
        from ares.task_executor import ALLOWED_TOOLS
        import json as _json

        llm = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
        )
        try:
            tools = get_tool_definitions()
            allowed_defs = [t for t in tools if t["function"]["name"] in ALLOWED_TOOLS]
            messages = [{"role": "user", "content": prompt}]
            summary_parts = []

            for turn_idx in range(max_turns):
                response = await llm.chat(messages, tools=allowed_defs)
                if response.get("tool_calls"):
                    # Ensure every tool call has a non-empty id
                    for i, tc in enumerate(response["tool_calls"]):
                        if not tc.get("id"):
                            tc["id"] = f"call_{turn_idx}_{i}"

                    messages.append({
                        "role": "assistant",
                        "content": response.get("content") or "",
                        "tool_calls": response["tool_calls"],
                    })
                    executor = ToolExecutor(
                        memory_store=self.memory_store,
                        task_store=self.task_store,
                        conversation_store=self.conversation_store,
                    )
                    for i, call in enumerate(response["tool_calls"]):
                        fn = call["function"]
                        args = _json.loads(fn.get("arguments") or "{}")
                        result = executor.execute(fn["name"], args)
                        messages.append({
                            "tool_call_id": call.get("id") or f"call_{turn_idx}_{i}",
                            "role": "tool",
                            "content": result,
                        })
                        summary_parts.append(f"{fn['name']}: {result[:200]}")
                else:
                    content = response.get("content", "")
                    if content:
                        summary_parts.append(content)
                    break
            return {"summary": "\n".join(summary_parts) if summary_parts else "Task completed."}
        finally:
            await llm.close()

    async def _notify_auto_complete(self, task_info: dict) -> None:
        """Send task_auto_complete event and inject result into chat."""
        task_id = task_info.get("id")
        title = task_info.get("title")
        status = task_info.get("status")
        notes = task_info.get("notes", "")

        event = {
            "type": "task_auto_complete",
            "task_id": task_id,
            "title": title,
            "status": status,
            "notes": notes,
        }

        # Include completion report if available
        updated_task = self.task_store.get(task_id) if task_id else None
        if updated_task and updated_task.get("completion_report"):
            try:
                event["report"] = json.loads(updated_task["completion_report"])
            except (json.JSONDecodeError, TypeError):
                pass

        ws_count = len(self._connected_websockets)
        print(f"[Server] Task #{task_id} complete ({status}), ws_count={ws_count}")

        for ws in list(self._connected_websockets):
            try:
                await self._send(ws, event)
                print(f"[Server] task_auto_complete sent")
            except Exception as e:
                print(f"[Server] Failed to send task_auto_complete: {e}")

        chat_msg = await self._compose_task_completion_message(
            task_id=task_id,
            title=title or "Untitled task",
            status=status or "unknown",
            notes=notes or "",
            report=event.get("report"),
        )
        for ws in list(self._connected_websockets):
            try:
                await self._send(ws, {"type": "content", "text": chat_msg})
                await self._send(ws, {"type": "response_done", "content": chat_msg, "tool_calls": []})
                print(f"[Server] Result injected into chat")
            except Exception as e:
                print(f"[Server] Failed to inject chat: {e}")


    async def _compose_task_completion_message(
        self,
        *,
        task_id: int | None,
        title: str,
        status: str,
        notes: str,
        report: dict | None = None,
    ) -> str:
        """Compose a natural chat update for a background task."""
        is_done = status in {"done", "completed"}
        get_artifacts = getattr(self.task_store, "get_artifacts", None)
        artifacts = get_artifacts(task_id) if task_id and callable(get_artifacts) else []
        artifact_paths = [a.get("path") for a in artifacts if a.get("path")]
        report = report or {}
        summary = report.get("summary") or notes or "Task finished."

        prompt = (
            "Write a short, natural, first-person completion message to Krish for a background task. "
            "Do not sound like a generic system notification. Mention concrete files/artifacts if present. "
            "Keep it to 2-4 sentences and use casual but clear language.\n\n"
            f"Task id: {task_id}\n"
            f"Task title: {title}\n"
            f"Status: {'completed' if is_done else 'needs attention'}\n"
            f"Summary: {summary}\n"
            f"Artifacts: {artifact_paths}\n"
            f"Key results: {report.get('key_results', [])}\n"
        )
        try:
            response = await self.agent.llm.chat([{"role": "user", "content": prompt}])
            content = str(response.get("content") or "").strip()
            if content:
                return content
        except Exception as exc:
            print(f"[Server][TaskDebug] Failed to compose task #{task_id} LLM message: {exc}")

        status_phrase = "done" if is_done else "partially done"
        artifact_text = f" I saved: {', '.join(artifact_paths)}." if artifact_paths else ""
        return f"Hey Krish — task #{task_id} is {status_phrase}: {title}. {summary}{artifact_text}"

    async def _push_task_event_to_clients(self, event: dict[str, Any]) -> None:
        """Print and push live task execution events for debugging."""
        print(
            "[Server][TaskDebug] "
            f"task=#{event.get('task_id')} "
            f"level={event.get('level')} "
            f"step={event.get('step')} "
            f"message={event.get('message')}"
        )
        payload = {"type": "task_event", "event": _as_jsonable(event)}
        for ws in list(self._connected_websockets):
            try:
                await self._send(ws, payload)
            except Exception as exc:
                print(f"[Server][TaskDebug] Failed to send task event: {exc}")
        await self._push_status_to_clients()

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
        self.task_executor.start()
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
            elif msg_type == "get_tasks":
                await self._send(websocket, {"type": "tasks", "tasks": self._tasks()})
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
            elif msg_type == "task:resume":
                task_id = message.get("task_id")
                if task_id:
                    result = self.agent.tool_executor.execute("resume_task", {"task_id": int(task_id)})
                    await self._send(websocket, {"type": "task:resumed", "task_id": task_id, "message": result})
            elif msg_type == "task:events":
                task_id = message.get("task_id")
                if task_id:
                    events = self.task_store.get_events(int(task_id), limit=message.get("limit", 50))
                    await self._send(websocket, {"type": "task:events", "task_id": task_id, "events": _as_jsonable(events)})
            elif msg_type == "task:artifacts":
                task_id = message.get("task_id")
                if task_id:
                    artifacts = self.task_store.get_artifacts(int(task_id))
                    await self._send(websocket, {"type": "task:artifacts", "task_id": task_id, "artifacts": _as_jsonable(artifacts)})
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
        executor_stats = self.task_executor.stats
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
            "task_count": len(self._pending_tasks()),
            "total_task_count": len(self._tasks()),
            "completed_task_count": len(self._completed_tasks()),
            "auto_exec_count": len(self.task_store.get_auto_executable()),
            "executor_state": executor_stats["state"],
            "executor_current_task": executor_stats["current_task_title"],
            "executor_tasks_completed": executor_stats["tasks_completed"],
            "executor_tasks_failed": executor_stats["tasks_failed"],
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

    def _tasks(self) -> list[dict[str, Any]]:
        if hasattr(self.task_store, "list_all"):
            return [_as_jsonable(item) for item in self.task_store.list_all()]
        if hasattr(self.task_store, "list_pending"):
            return [_as_jsonable(item) for item in self.task_store.list_pending()]
        if hasattr(self.task_store, "list_tasks"):
            return [_as_jsonable(item) for item in self.task_store.list_tasks()]
        return []


    def _pending_tasks(self) -> list[dict[str, Any]]:
        tasks = self._tasks()
        return [
            task for task in tasks
            if (task.get("state") or task.get("status") or "pending") not in {"completed", "failed", "cancelled"}
            and task.get("status") not in {"done", "partial", "cancelled"}
        ]

    def _completed_tasks(self) -> list[dict[str, Any]]:
        tasks = self._tasks()
        return [
            task for task in tasks
            if (task.get("state") == "completed") or task.get("status") == "done"
        ]

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
            await self.task_executor.stop()
            for obj in (
                self.agent,
                self.conversation_store,
                self.memory_store,
                self.task_store,
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
