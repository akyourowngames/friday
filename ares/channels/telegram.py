"""Telegram long-polling channel for the local Ares server.

The implementation deliberately uses Telegram's HTTPS Bot API directly rather
than a framework.  It keeps the dependency surface small and makes the two
important reliability properties explicit: provider offsets are stored in the
same SQLite database as Ares conversations, and a remote chat is never trusted
until its exact chat ID is in the local allowlist.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import mimetypes
import os
import re
import secrets
import shlex
import time
from collections import defaultdict
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any, Callable

import httpx

from ares.agent import Agent
from ares.attachments import build_attachment_context, inspect_attachment
from ares.channels.audio import AudioTranscriptionError, EnglishAudioTranscriber, EnglishTranscript
from ares.channels.store import ChannelStore
from ares.config import get_db_path, load_config, save_config
from ares.context.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.integrations.mcp_registry import MCPRegistryClient
from ares.models import AppConfig, TelegramConfig
from ares.skills.proactive import ProactiveService
from ares.skills.reminders import DesktopNotifier
from ares.integrations.llm import (
    MODEL_REGISTRY,
    PROVIDER_BASE_URLS,
    SUPPORTED_PROVIDERS,
    activate_provider_config,
    configured_provider_api_key,
    default_model_for_provider,
    normalize_provider,
    provider_for_model,
)
from ares.multi_agent.display import ACTIVE_STATUSES, active_runs, telegram_overview, telegram_run
from ares.skills.registry import (
    RegistryError as SkillRegistryError,
    SafeSkillInstaller,
    SkillRegistryClient,
    SkillValidationError,
    marketplace_record,
)
from ares.skills.discovery import SkillManager
from ares.tools.mcp_client import MCPClientManager


logger = logging.getLogger(__name__)

CHANNEL_NAME = "telegram"
MAX_TELEGRAM_MESSAGE_CHARS = 4096
MAX_HISTORY_MESSAGES = 40
MARKETPLACE_CONFIRMATION_TTL_SECONDS = 5 * 60
TOOL_TOKEN_RE = re.compile(r"^\[tool:([^:]+):(.*)\]$", re.DOTALL)
TOOL_START_TOKEN_RE = re.compile(r"^\[tool_start:([^\]]+)\]$")
TOOL_PROGRESS_TOKEN_RE = re.compile(r"^\[tool_progress:([^:]+):(.*)\]$", re.DOTALL)
FILE_MARKER_RE = re.compile(r"\[\[telegram_file:(.+?)\]\]", re.IGNORECASE)
FILE_REQUEST_RE = re.compile(
    r"\b(?:send|upload|share|attach)\b[\s\S]{0,120}\b(?:file|document|report|output|result|it|this)\b"
    r"|\b(?:telegram|sendfile)\b",
    re.IGNORECASE,
)
TELEGRAM_COMMANDS = (
    ("help", "Show Ares commands"),
    ("new", "Start a fresh Ares session"),
    ("status", "Show runtime and active team status"),
    ("model", "Change the active model or list models"),
    ("provider", "Switch the provider or list providers"),
    ("agents", "Inspect specialist teams and workers"),
    ("workers", "Show every active specialist worker"),
    ("monitors", "List proactive watchers"),
    ("alerts", "Show watcher incidents"),
    ("skills", "Manage Ares skills"),
    ("mcp", "Manage connected MCP servers"),
)


class TelegramAPIError(RuntimeError):
    """A Telegram response that cannot safely be retried by the caller."""


def resolve_bot_token(config: TelegramConfig) -> str:
    """Prefer the environment so a bot token needn't be saved in JSON."""
    return os.getenv("ARES_TELEGRAM_BOT_TOKEN", config.bot_token).strip()


class TelegramBotAPI:
    """Small, retrying client for the Telegram Bot API."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base_url: str = "https://api.telegram.org",
        max_attempts: int = 4,
    ) -> None:
        if not token:
            raise ValueError("A Telegram bot token is required")
        self.token = token
        self.api_base_url = api_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0))
        self._owns_client = client is None
        self.max_attempts = max(1, max_attempts)

    async def get_me(self) -> dict[str, Any]:
        return await self._request("getMe")

    async def delete_webhook(self) -> bool:
        return bool(await self._request("deleteWebhook", {"drop_pending_updates": False}))

    async def get_updates(self, *, offset: int, timeout: int) -> list[dict[str, Any]]:
        result = await self._request(
            "getUpdates",
            {
                "offset": int(offset),
                "timeout": int(timeout),
                "allowed_updates": ["message"],
            },
            read_timeout=float(timeout) + 15.0,
        )
        return result if isinstance(result, list) else []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            params["reply_parameters"] = {"message_id": reply_to_message_id}
        result = await self._request("sendMessage", params)
        return result if isinstance(result, dict) else {}

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> dict[str, Any]:
        result = await self._request(
            "editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text}
        )
        return result if isinstance(result, dict) else {}

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._request("sendChatAction", {"chat_id": chat_id, "action": action})

    async def set_commands(self, commands: tuple[tuple[str, str], ...]) -> bool:
        return bool(await self._request(
            "setMyCommands",
            {"commands": [{"command": command, "description": description} for command, description in commands]},
        ))

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            params["caption"] = caption[:1024]
        if reply_to_message_id:
            params["reply_parameters"] = json.dumps({"message_id": reply_to_message_id})
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as handle:
            result = await self._request(
                "sendDocument",
                params,
                files={"document": (path.name, handle, content_type)},
                read_timeout=90.0,
            )
        return result if isinstance(result, dict) else {}

    async def download_file(self, file_id: str, destination: Path, *, max_bytes: int) -> Path:
        metadata = await self._request("getFile", {"file_id": file_id})
        if not isinstance(metadata, dict) or not metadata.get("file_path"):
            raise TelegramAPIError("Telegram did not return a downloadable file path")
        declared_size = int(metadata.get("file_size") or 0)
        if declared_size and declared_size > max_bytes:
            raise ValueError(f"Attachment is larger than the {max_bytes // (1024 * 1024)} MB limit")

        url = f"{self.api_base_url}/file/bot{self.token}/{metadata['file_path']}"
        timeout = httpx.Timeout(90.0, connect=10.0)
        try:
            async with self._client.stream("GET", url, timeout=timeout) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length and int(length) > max_bytes:
                    raise ValueError(f"Attachment is larger than the {max_bytes // (1024 * 1024)} MB limit")
                destination.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"Attachment is larger than the {max_bytes // (1024 * 1024)} MB limit")
                        output.write(chunk)
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            destination.unlink(missing_ok=True)
            raise TelegramAPIError(f"Telegram file download failed: {exc}") from exc
        return destination

    async def _request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        files: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        url = f"{self.api_base_url}/bot{self.token}/{method}"
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                timeout = httpx.Timeout(read_timeout or 30.0, connect=10.0)
                if files:
                    # HTTPX consumes file handles during a failed attempt.
                    # Rewind them so a transient retry never uploads an empty
                    # document.
                    for value in files.values():
                        handle = value[1] if isinstance(value, tuple) and len(value) > 1 else None
                        if hasattr(handle, "seek"):
                            handle.seek(0)
                    response = await self._client.post(url, data=params or {}, files=files, timeout=timeout)
                else:
                    response = await self._client.post(url, json=params or {}, timeout=timeout)
                payload = response.json()
            except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
                last_error = exc
            else:
                if response.status_code < 500 and payload.get("ok"):
                    return payload.get("result")

                description = str(payload.get("description") or f"HTTP {response.status_code}")
                retry_after = payload.get("parameters", {}).get("retry_after")
                if response.status_code < 500 and payload.get("error_code") not in {429}:
                    raise TelegramAPIError(f"Telegram {method} failed: {description}")
                last_error = TelegramAPIError(f"Telegram {method} failed: {description}")
                if retry_after:
                    await asyncio.sleep(min(float(retry_after), 30.0))
                    continue

            if attempt < self.max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt, 8))

        raise TelegramAPIError(f"Telegram {method} did not succeed after retries: {last_error}")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class TelegramChannel:
    """Bridge an allowlisted Telegram chat to an existing Ares Agent."""

    def __init__(
        self,
        *,
        config: AppConfig,
        agent: Agent,
        conversation_store: ConversationStore,
        api: Any | None = None,
        config_provider: Callable[[], AppConfig] | None = None,
        state_store: ChannelStore | None = None,
        audio_transcriber: Any | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        agent_lock: asyncio.Lock | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.conversation_store = conversation_store
        self._config_provider = config_provider or (lambda: self.config)
        runtime_config = self._telegram_config()
        token = resolve_bot_token(runtime_config)
        self.api = api or TelegramBotAPI(token)
        db_path = getattr(conversation_store, "db_path", None) or get_db_path(
            Path(config.data_dir).expanduser()
        )
        self.state_store = state_store or ChannelStore(Path(db_path))
        self._owns_state_store = state_store is None
        self.audio_transcriber = audio_transcriber or EnglishAudioTranscriber(self._telegram_config)
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Retained for backwards-compatible construction. Per-chat locks keep
        # order, while the Agent serializes only the shared browser surface.
        self._agent_lock = agent_lock
        self.skill_manager = SkillManager(skill_dirs=list(config.skill_dirs or []) or None)
        # Remote config changes are deliberately two-step.  A Telegram message
        # may be forwarded, mistyped, or stale; only the originating allowlisted
        # chat can confirm its own short-lived, reviewable plan.
        self._pending_marketplace_actions: dict[tuple[int, str], tuple[float, str, str, str | None]] = {}
        self._marketplace_results: dict[int, dict[str, list[str]]] = {}
        self._active_marketplace_requests: dict[int, str] = {}
        executor = getattr(agent, "tool_executor", None)
        attach = getattr(executor, "set_telegram_channel", None)
        if callable(attach):
            attach(self)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        if not self._is_enabled():
            logger.info("Telegram channel is disabled")
            return
        self._task = asyncio.create_task(self.run_forever(), name="ares-telegram")

    async def run_forever(self) -> None:
        """Poll Telegram until stopped, retaining cursor state across restarts."""
        runtime = self._telegram_config()
        if not runtime.enabled:
            print("Ares Telegram is disabled. Run `python -m ares --telegram-setup` first.")
            return
        if not resolve_bot_token(runtime):
            print("Ares Telegram needs a bot token. Run `python -m ares --telegram-setup` first.")
            return

        try:
            print("Ares Telegram: connecting...")
            await self.api.delete_webhook()
            me = await self.api.get_me()
            set_commands = getattr(self.api, "set_commands", None)
            if callable(set_commands):
                with suppress(Exception):
                    await set_commands(TELEGRAM_COMMANDS)
            logger.info("Telegram channel connected as @%s", me.get("username", "ares-bot"))
            print(f"Ares Telegram: connected as @{me.get('username', 'ares-bot')}; waiting for messages.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Telegram startup failed: %s", exc)
            print(f"Ares Telegram could not connect: {exc}")
            return

        backoff = 1.0
        while not self._stop_event.is_set():
            if not self._is_enabled():
                logger.info("Telegram channel was disabled in config")
                return
            try:
                cfg = self._telegram_config()
                offset = self.state_store.get_offset(CHANNEL_NAME)
                updates = await self.api.get_updates(offset=offset, timeout=cfg.poll_timeout_seconds)
                backoff = 1.0
                for update in updates:
                    update_id = int(update.get("update_id") or 0)
                    if update_id and update_id < offset:
                        continue
                    should_ack = False
                    try:
                        await self._handle_update(update)
                        should_ack = True
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Telegram update %s could not be handled", update_id)
                        # A malformed update or an unrecoverable Ares turn
                        # should not block every later message forever.
                        should_ack = True
                    finally:
                        # Never acknowledge a message whose work was cut off
                        # by shutdown; Telegram will redeliver it next time.
                        if update_id and should_ack:
                            self.state_store.advance_offset(CHANNEL_NAME, update_id + 1)
                            offset = update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Telegram polling interrupted: %s; retrying in %.0fs", exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        close = getattr(self.api, "close", None)
        if close:
            result = close()
            if result is not None:
                await result
        if self._owns_state_store:
            self.state_store.close()
        executor = getattr(self.agent, "tool_executor", None)
        detach = getattr(executor, "set_telegram_channel", None)
        if callable(detach) and getattr(executor, "telegram_channel", None) is self:
            detach(None)

    def _telegram_config(self) -> TelegramConfig:
        candidate = self._config_provider()
        return getattr(candidate, "telegram", self.config.telegram)

    def _is_enabled(self) -> bool:
        cfg = self._telegram_config()
        return bool(cfg.enabled and resolve_bot_token(cfg))

    async def deliver_proactive(self, message: str) -> list[str]:
        """Send one initiative message to the primary allowlisted private chat."""
        cfg = self._telegram_config()
        if not self._is_enabled() or not cfg.allowed_chat_ids:
            return []
        chat_id = int(cfg.allowed_chat_ids[0])
        await self.api.send_message(chat_id, _telegram_trim(message))
        self.conversation_store.add_message(
            self._conversation_id(chat_id), "assistant", message,
        )
        return ["telegram"]

    def _is_authorized(self, chat: dict[str, Any]) -> bool:
        cfg = self._telegram_config()
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return False
        if chat.get("type") != "private" and not cfg.allow_group_chats:
            return False
        return chat_id in {int(value) for value in cfg.allowed_chat_ids}

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            return
        chat_id = int(chat["id"])
        text = str(message.get("text") or message.get("caption") or "").strip()
        reply_to = int(message.get("message_id") or 0) or None

        if not self._is_authorized(chat):
            if text.startswith("/start"):
                await self.api.send_message(
                    chat_id,
                    "Ares is running, but this chat is not authorized. "
                    f"Chat ID: {chat_id}. On the PC, add it to telegram.allowed_chat_ids, then restart Ares.",
                    reply_to_message_id=reply_to,
                )
            logger.warning("Ignored Telegram message from unauthorized chat %s", chat_id)
            return

        async with self._chat_locks[chat_id]:
            command, argument = self._command(text)
            if command in {"/start", "/help"}:
                await self.api.send_message(
                    chat_id,
                    "Ares is connected to this PC. Send a message, document, photo, or voice note. "
                    "Hindi, English, and Hinglish voice notes are transcribed to English.\n\n"
                    "Commands:\n/new — start a fresh Ares session\n/resume [ID|latest] — continue a saved chat\n/status — channel status\n"
                    "/file <path> — upload a local PC file\n"
                    "/skills [list|search|info|install] — manage skills\n"
                    "/mcp [list|search|info|add|test|refresh] — manage MCPs\n"
                    "/model [id|list] — change the active model\n"
                    "/provider [name|list] — switch provider\n"
                    "/monitors — list proactive watchers\n"
                    "/monitor [add|status|pause|resume|remove|events|test] — control watchers\n"
                    "/alerts — recent watcher incidents\n"
                    "/agents [status|active|roles|runs|show|cancel] — inspect specialist teams\n"
                    "/agents resume RUN_ID — resume a safe checkpoint\n"
                    "/workers — show all workers running now\n"
                    "/confirm <code> — approve the last reviewed install\n/cancel — discard it\n"
                    "/help — show this help\n\n"
                    "Examples: /skills search research  •  /mcp search github",
                    reply_to_message_id=reply_to,
                )
                return
            if command == "/new":
                await self._start_new_session(chat_id)
                await self.api.send_message(chat_id, "Started a new Ares session for this chat.", reply_to_message_id=reply_to)
                return
            if command == "/resume":
                await self._handle_resume_command(chat_id, argument, reply_to)
                return
            if command == "/status":
                runtime = getattr(self.agent, "multi_agent_runtime", None)
                team_status = "Specialists: unavailable"
                if runtime is not None:
                    runtime_session_id = self._telegram_runtime_session(chat_id)
                    recent = runtime.list_runs(limit=100, session_id=runtime_session_id)
                    active = active_runs(recent)
                    active_workers = sum(
                        str(child.get("status") or "") in ACTIVE_STATUSES
                        for run in active for child in (run.get("children") or [])
                    )
                    team_status = f"Specialists: {len(active)} active teams · {active_workers} active workers"
                await self.api.send_message(
                    chat_id,
                    f"Ares Telegram channel is online. Model: {self.agent.config.model}. "
                    "This chat is allowlisted and its conversation is saved locally on the PC.\n"
                    f"{team_status}",
                    reply_to_message_id=reply_to,
                )
                return
            if command == "/file":
                await self._send_requested_file(chat_id, argument, reply_to)
                return
            if command in {"/model", "/provider"}:
                await self._handle_model_command(chat_id, command, argument, reply_to)
                return
            if command in {"/monitor", "/monitors", "/alerts"}:
                await self._handle_watcher_command(chat_id, command, argument, reply_to)
                return
            if command in {"/agents", "/agent", "/workers"}:
                await self._handle_agents_command(chat_id, command, argument, reply_to)
                return
            if command in {"/skills", "/mcp", "/confirm", "/cancel"}:
                await self._handle_marketplace_command(chat_id, command, argument, reply_to)
                return
            if command and command.startswith("/") and not self._has_attachment(message):
                await self.api.send_message(chat_id, "Unknown command. Use /help for Telegram commands, or send it as a normal request.", reply_to_message_id=reply_to)
                return

            await self._handle_chat_message(chat_id, message, update, text, reply_to)

    async def _handle_agents_command(
        self, chat_id: int, command: str, argument: str, reply_to: int | None
    ) -> None:
        """Expose the native supervisor without routing operational commands through the LLM."""
        runtime = getattr(self.agent, "multi_agent_runtime", None)
        if runtime is None:
            await self.api.send_message(
                chat_id, "Native specialist mode is unavailable. Enable multi_agent and run Ares with --all.",
                reply_to_message_id=reply_to,
            )
            return
        runtime_session_id = self._telegram_runtime_session(chat_id)
        pieces = argument.split(maxsplit=1)
        action = ("active" if command == "/workers" else (pieces[0].casefold() if pieces and pieces[0] else "status"))
        value = pieces[1].strip() if len(pieces) > 1 else ""
        if action in {"status", "overview"}:
            text = telegram_overview(
                enabled=bool(runtime.config.enabled), agents=runtime.list_agents(),
                runs=runtime.list_runs(limit=100, session_id=runtime_session_id),
            )
        elif action in {"active", "workers"}:
            runs = active_runs(runtime.list_runs(limit=100, session_id=runtime_session_id))
            text = "All active specialist workers"
            if runs:
                text += "\n\n" + "\n\n".join(telegram_run(run, include_results=False) for run in runs[:8])
            else:
                text += "\n\nNo specialist teams are currently running."
        elif action in {"roles", "list"}:
            lines = ["Ares specialist roles"]
            for item in runtime.list_agents():
                mode = "mutation-capable" if item.get("can_mutate") else "read-only"
                lines.append(
                    f"• {item.get('name')} · {mode} · {item.get('max_iterations')} iterations · "
                    f"{float(item.get('timeout_seconds') or 0):.0f}s\n  {_one_line(item.get('description'), 150)}"
                )
            text = "\n".join(lines)
        elif action == "runs":
            try:
                limit = max(1, min(int(value or 10), 30))
            except ValueError:
                limit = 10
            runs = runtime.list_runs(limit=limit, session_id=runtime_session_id)
            lines = [f"Recent specialist teams · {len(runs)}"]
            for run in runs:
                workers = run.get("children") or []
                lines.append(
                    f"• {run.get('run_id')} · {run.get('status')} · {len(workers)} workers\n"
                    f"  {_one_line(run.get('prompt_summary') or run.get('activity'), 130)}"
                )
            text = "\n".join(lines) if runs else "No specialist runs yet."
        elif action == "show" and value:
            run = runtime.get_run(value, session_id=runtime_session_id)
            text = telegram_run(run) if run else "Agent run not found. Use /agents runs to copy a run ID."
        elif action == "cancel" and value:
            cancelled = await runtime.cancel(value, session_id=runtime_session_id)
            text = f"{'Cancelled' if cancelled else 'Not active'} · {value}"
        elif action == "resume" and value:
            try:
                team = await runtime.resume(value, session_id=runtime_session_id)
            except Exception as exc:
                text = f"Could not resume {value}: {type(exc).__name__}: {exc}"
            else:
                text = f"Resumed {value} as {team.root_run_id}."
        else:
            text = (
                "Agent commands\n"
                "/agents status — supervisor overview\n"
                "/agents active — all running workers\n"
                "/agents roles — configured specialists\n"
                "/agents runs [limit] — recent teams\n"
                "/agents show RUN_ID — execution tree and results\n"
                "/agents cancel RUN_ID — stop an active team\n"
                "/agents resume RUN_ID — resume safe read-only checkpoint work\n"
                "/workers — shortcut for active workers"
            )
        await self._send_text_chunks(chat_id, _telegram_trim(text), reply_to)

    async def _handle_model_command(self, chat_id: int, command: str, argument: str, reply_to: int | None) -> None:
        """Switch the active model/provider from Telegram.

        Mirrors the local ``/model`` and ``/provider`` commands: listing is
        read-only, while a change is applied to the live Agent and persisted to
        the shared config.
        """
        arg = (argument or "").strip()
        if command == "/provider":
            if not arg or arg.casefold() == "list":
                lines = ["Ares providers"]
                current = normalize_provider(getattr(self.config, "provider", "opencode"))
                for name, url in PROVIDER_BASE_URLS.items():
                    status = "current" if name == current else "available"
                    lines.append(f"• {name} — {url or 'GitHub Copilot SDK (OAuth)'} · {status}")
                lines.append("\nUsage: /provider <name>  (e.g. /provider nim)")
                await self.api.send_message(chat_id, _telegram_trim("\n".join(lines)), reply_to_message_id=reply_to)
                return
            provider = normalize_provider(arg)
            if provider not in SUPPORTED_PROVIDERS:
                valid = ", ".join((*SUPPORTED_PROVIDERS, "nvidia (alias for nim)"))
                await self.api.send_message(
                    chat_id, f"Unknown provider: {arg}. Valid: {valid}", reply_to_message_id=reply_to
                )
                return
            self._activate_provider(provider)
            replacement_model = None
            if provider_for_model(self.config.model) != provider:
                replacement_model = default_model_for_provider(provider)
                self.config.model = replacement_model
                self.agent.set_model(replacement_model)
            save_config(self.config)
            note = f" Model set to {replacement_model}." if replacement_model else ""
            await self.api.send_message(
                chat_id, f"Provider switched to {provider}.{note}", reply_to_message_id=reply_to
            )
            return

        # command == "/model"
        if not arg or arg.casefold() == "list":
            lines = ["Ares models"]
            for group_key, group in MODEL_REGISTRY.items():
                for m in group["models"]:
                    backend = provider_for_model(m["id"])
                    endpoint = (
                        "NVIDIA NIM" if backend == "nim"
                        else "GitHub Copilot" if backend == "copilot"
                        else "OpenCode Zen"
                    )
                    status = "current" if m["id"] == self.config.model else "available"
                    lines.append(f"• {m['id']} — {endpoint} · {status}")
            lines.append("\nUsage: /model <id>  (e.g. /model gpt-oss-120b)")
            await self.api.send_message(chat_id, _telegram_trim("\n".join(lines)), reply_to_message_id=reply_to)
            return
        selected_provider = provider_for_model(arg)
        switched_provider = False
        if selected_provider and selected_provider != normalize_provider(self.config.provider):
            self._activate_provider(selected_provider)
            switched_provider = True
        self.config.model = arg
        save_config(self.config)
        self.agent.set_model(arg)
        provider_note = f" Provider switched to {selected_provider}." if switched_provider else ""
        await self.api.send_message(
            chat_id, f"Model switched to {arg}.{provider_note}", reply_to_message_id=reply_to
        )

    def _activate_provider(self, provider: str) -> str:
        """Apply a provider switch to config and the live LLM client."""
        active = activate_provider_config(self.config, provider)
        llm = getattr(self.agent, "llm", None)
        if llm is not None:
            llm.provider = active
            llm.base_url = self.config.api_base_url.rstrip("/")
            llm.api_key = configured_provider_api_key(self.config, active)
            llm.config = self.config
        return active

    async def _handle_watcher_command(self, chat_id: int, command: str, argument: str, reply_to: int | None) -> None:
        """Run the same watcher controls exposed by the local terminal."""
        from ares.watcher.commands import WatcherCommands
        from ares.watcher.database import resolve_watcher_database_path
        watcher_tools = getattr(getattr(self.agent, "tool_executor", None), "watcher_tools", None)
        controller = WatcherCommands(
            database_path=resolve_watcher_database_path(self.config),
            defaults=self.config.watcher.defaults,
            db=watcher_tools.db if watcher_tools is not None else None,
            goal_store=getattr(self.agent, "goal_store", None),
        )
        try:
            if command == "/alerts":
                events = controller.db.list_events(limit=10, unacknowledged=True)
                if not events:
                    text = "Watcher alerts · all clear. No unacknowledged changes."
                else:
                    monitors = {item.id:item.name for item in controller.db.list_monitors()}
                    lines = ["Watcher alerts"] + [f"• {item.severity.upper()} · {monitors.get(item.monitor_id, item.monitor_id[:8])}\n  {item.change_summary or item.event_type}" for item in events]
                    text = "\n".join(lines)
                await self.api.send_message(chat_id, _telegram_trim(text), reply_to_message_id=reply_to)
                return
            result = controller.execute("list" if command == "/monitors" else argument)
            action = result["action"]
            if action == "list":
                values = result["monitors"]
                text = "Ares watchers\n" + ("\n".join(f"• {item['id'][:8]} · {item['name']} · {'paused' if not item['enabled'] else item['last_status'] or 'armed'} · {item['interval_seconds']}s" for item in values) if values else "No watchers configured.")
            elif action == "status":
                item = result["monitor"]
                linked = ", ".join(f"#{goal['goal_id']} {goal['title']}" for goal in result.get("linked_goals") or []) or "none"
                text = f"Watcher · {item['name']}\nStatus: {item['last_status'] or 'armed'}\nEnabled: {item['enabled']}\nChecks: {item['total_checks']} · Changes: {item['total_changes']}\nErrors: {item['error_count']}\nLast: {item['last_checked_at'] or 'never'}\nLinked goals: {linked}"
            elif action == "events":
                values = result["events"]
                text = f"Events · {result['monitor']['name']}\n" + ("\n".join(f"• {item['severity'].upper()} · {item['change_summary'] or item['event_type']}" for item in values) if values else "No changes recorded.")
            elif action == "test":
                service = getattr(watcher_tools, "service", None)
                if service is None:
                    text = "Watcher runtime is not active. Start Ares with --all."
                else:
                    monitor = service.db.get_monitor(result["monitor"]["id"])
                    event = await service.scheduler.check_monitor(monitor, force=True) if monitor else None
                    text = f"Watcher check complete · {result['monitor']['name']}\n" + (f"Signal: {event.change_summary}" if event else "No change detected.")
            else:
                linked = f" · linked to goal #{result['linked_goal_id']}" if result.get("linked_goal_id") else ""
                text = f"Watcher {action} complete · {result['monitor']['name']} ({result['monitor']['id'][:8]}){linked}"
            await self.api.send_message(chat_id, _telegram_trim(text), reply_to_message_id=reply_to)
        except (ValueError, KeyError) as exc:
            await self.api.send_message(chat_id, str(exc), reply_to_message_id=reply_to)
        finally:
            controller.close()

    async def _handle_chat_message(
        self,
        chat_id: int,
        message: dict[str, Any],
        update: dict[str, Any],
        text: str,
        reply_to: int | None,
    ) -> None:
        inspection_context = ""
        attachment_label = ""
        audio_path: Path | None = None
        audio_metadata: dict[str, Any] | None = None
        try:
            attachment_label, inspection_context, audio_path, audio_metadata = await self._attachment_context(
                chat_id, message, update
            )
        except ValueError as exc:
            await self.api.send_message(chat_id, f"I couldn't accept that attachment: {exc}", reply_to_message_id=reply_to)
            return
        except Exception as exc:
            logger.exception("Telegram attachment failed")
            await self.api.send_message(chat_id, f"I couldn't download that attachment: {exc}", reply_to_message_id=reply_to)
            return

        if not text and not attachment_label:
            await self.api.send_message(
                chat_id,
                "Send a message, document, photo, or voice note for Ares to work with.",
                reply_to_message_id=reply_to,
            )
            return

        session_id = self._conversation_id(chat_id)
        history = self._conversation_history(session_id)
        status = _TelegramProgress(self, chat_id, reply_to)
        await status.start()
        transcript: EnglishTranscript | None = None
        if audio_path is not None:
            await status.event("Transcribing voice note to English (first use may prepare a local model)")
            try:
                transcript = await self.audio_transcriber.transcribe_to_english(
                    audio_path,
                    duration_seconds=int((audio_metadata or {}).get("duration") or 0),
                )
            except AudioTranscriptionError as exc:
                await status.finish("⚠️ Voice transcription could not finish.")
                await self.api.send_message(chat_id, str(exc), reply_to_message_id=reply_to)
                return
            except Exception as exc:
                logger.exception("Telegram audio transcription failed")
                await status.finish("⚠️ Voice transcription could not finish.")
                await self.api.send_message(
                    chat_id,
                    f"I could not transcribe that voice note: {exc}",
                    reply_to_message_id=reply_to,
                )
                return
            await status.event("Voice transcription complete")
            await self.api.send_message(
                chat_id,
                f"Transcript (English): {transcript.text}",
                reply_to_message_id=reply_to,
            )

        if transcript is not None:
            visible_content = "Voice note (English transcript): " + transcript.text
            if text:
                visible_content += "\nCaption: " + text
            prompt = (
                "## Telegram voice note\n"
                "The user spoke Hindi, English, or Hinglish. This is the speech translated into English:\n"
                f"{transcript.text}\n\n"
                "Reply only in English. Do not switch to Hindi or any other language."
            )
            if text:
                prompt += "\n\nAdditional caption from the user:\n" + text
        else:
            visible_content = text or f"Attached: {attachment_label}"
            prompt = text or "Inspect and explain the attached file."
            if attachment_label:
                prompt += " The file is attached to this turn. Do not ask the user to re-upload it."
            if inspection_context:
                prompt += "\n\n" + inspection_context

        prompt += (
            "\n\n## Telegram reply rules\n"
            "Reply as a compact Telegram message: plain text, short sections, and no Markdown tables. "
            "Use the conversation history for immediate follow-ups. If the user asks what a recently inspected "
            "skill or MCP does, answer specifically from that marketplace result; do not replace it with a generic "
            "catalogue of Ares capabilities. State uncertainty instead of inventing missing registry details.\n\n"
            "This is an active Ares Telegram channel with verified file upload support. Never say Telegram delivery "
            "is unavailable, ask for a BotFather token, or suggest setting up a Telegram bot. A later conversation "
            "entry beginning 'Telegram delivery verified' is authoritative evidence that its named file was uploaded. "
            "A previous delivery failure applies only to that specific path; it does not mean the channel cannot send files."
        )

        if self._file_delivery_requested(text):
            prompt += (
                "\n\n## Telegram delivery\nThe user explicitly asked for a local file to be uploaded "
                "to this Telegram chat. If you create or locate that file, append exactly "
                "[[telegram_file:ABSOLUTE_PATH]] on its own line. Never use this marker unless "
                "the user explicitly asked for delivery."
            )

        self.conversation_store.add_message(session_id, "user", visible_content)
        try:
            response, tool_calls = await self._run_agent(
                prompt, history, status, session_id, reflection_input=visible_content,
            )
        except Exception as exc:
            logger.exception("Telegram agent turn failed")
            await status.finish("⚠️ Ares could not finish that request.")
            await self.api.send_message(chat_id, f"Ares hit an error: {exc}", reply_to_message_id=reply_to)
            return

        clean_response, paths = self._extract_file_markers(response, text)
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        # Always record the assistant response (even empty) so reflection and
        # history stay consistent across turns.
        self.conversation_store.add_message(
            session_id, "assistant", clean_response, tool_calls_json,
        )
        await status.finish("✅ Done")

        if clean_response:
            await self._send_text_chunks(chat_id, clean_response, reply_to)
        elif not paths:
            logger.warning(
                "Telegram agent returned empty response for %r",
                text[:80],
            )
            await self.api.send_message(
                chat_id,
                "Got it — anything else you need?",
                reply_to_message_id=reply_to,
            )
        for path in paths:
            delivery_status = await self._send_local_file(chat_id, path, reply_to)
            self.conversation_store.add_message(session_id, "assistant", delivery_status)

    async def _run_agent(
        self,
        prompt: str,
        history: list[dict[str, str]],
        status: "_TelegramProgress",
        session_id: int,
        *,
        reflection_input: str | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        response_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        scope_factory = getattr(self.agent, "session_scope", None)
        runtime_session_id = f"telegram-{session_id}"
        scope = scope_factory(runtime_session_id) if callable(scope_factory) else nullcontext()
        runtime = getattr(self.agent, "multi_agent_runtime", None)
        unsubscribe = None
        if runtime is not None:
            async def handle_agent_event(event: dict[str, Any]) -> None:
                if str(event.get("session_id") or "") == runtime_session_id:
                    await status.agent_event(event)
            unsubscribe = runtime.subscribe(handle_agent_event)
        try:
            with scope:
                run_stream = self.agent.run_stream
                stream_kwargs: dict[str, Any] = {"conversation_history": history}
                try:
                    parameters = inspect.signature(run_stream).parameters.values()
                    accepts_reflection = any(
                        parameter.name == "reflection_input"
                        or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                except (TypeError, ValueError):
                    accepts_reflection = False
                if accepts_reflection:
                    stream_kwargs["reflection_input"] = reflection_input
                async for chunk in run_stream(prompt, **stream_kwargs):
                    start = TOOL_START_TOKEN_RE.match(chunk)
                    if start:
                        await status.event(self._tool_label(start.group(1), "Using"))
                        continue
                    progress = TOOL_PROGRESS_TOKEN_RE.match(chunk)
                    if progress:
                        await status.event(self._tool_label(progress.group(1), progress.group(2)))
                        continue
                    result = TOOL_TOKEN_RE.match(chunk)
                    if result:
                        tool_name, raw = result.groups()
                        with suppress(json.JSONDecodeError):
                            raw_payload: Any = json.loads(raw)
                            tool_calls.append({"tool": tool_name, "content": raw_payload})
                        if not tool_calls or tool_calls[-1].get("tool") != tool_name:
                            tool_calls.append({"tool": tool_name, "content": raw})
                        await status.event(self._tool_label(tool_name, "Finished"))
                        continue
                    response_parts.append(chunk)
        finally:
            if unsubscribe is not None:
                unsubscribe()
        return "".join(response_parts).strip(), tool_calls

    @staticmethod
    def _tool_label(tool_name: str, action: str) -> str:
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__")
            name = " / ".join(part.replace("_", " ") for part in parts[1:] if part)
            return f"{action} MCP: {name or 'integration'}"
        return f"{action} tool: {tool_name.replace('_', ' ')}"

    def _conversation_id(self, chat_id: int) -> int:
        conversation_id = self.state_store.get_conversation_id(CHANNEL_NAME, chat_id)
        if conversation_id is None:
            conversation_id = self.conversation_store.start_conversation()
        # Also records the current legacy conversation in the resumable-chat
        # history once, without exposing another Telegram chat's data.
        self.state_store.set_conversation_id(CHANNEL_NAME, chat_id, conversation_id)
        return conversation_id

    def _telegram_runtime_session(self, chat_id: int) -> str:
        return f"telegram-{self._conversation_id(chat_id)}"

    async def _start_new_session(self, chat_id: int) -> None:
        old_id = self.state_store.get_conversation_id(CHANNEL_NAME, chat_id)
        if old_id is not None:
            with suppress(Exception):
                self.conversation_store.end_conversation(old_id)
        self._marketplace_results.pop(chat_id, None)
        self._clear_pending_marketplace_actions(chat_id)
        new_id = self.conversation_store.start_conversation()
        self.state_store.set_conversation_id(CHANNEL_NAME, chat_id, new_id)

    def _resume_options(self, chat_id: int, limit: int = 8) -> list[dict[str, Any]]:
        ids = self.state_store.list_conversation_ids(CHANNEL_NAME, chat_id, limit=limit)
        rows = getattr(self.conversation_store, "list_conversations", lambda: [])()
        by_id = {int(row.get("id")): row for row in rows if row.get("id") is not None}
        return [{"id": conversation_id, **dict(by_id.get(conversation_id) or {})} for conversation_id in ids]

    async def _handle_resume_command(self, chat_id: int, argument: str, reply_to: int | None) -> None:
        options = self._resume_options(chat_id)
        selected = argument.strip().casefold()
        if not selected:
            if not options:
                await self.api.send_message(chat_id, "No earlier chats are available yet. Use /new to start one.", reply_to_message_id=reply_to)
                return
            lines = ["Saved chats — reply with /resume ID:"]
            for row in options:
                label = " ".join(str(row.get("summary") or "Untitled chat").split())[:80]
                stamp = str(row.get("ended_at") or row.get("started_at") or "")[:16].replace("T", " ")
                lines.append(f"• /resume {row['id']} — {label} {f'({stamp})' if stamp else ''}")
            await self.api.send_message(chat_id, "\n".join(lines), reply_to_message_id=reply_to)
            return
        if selected == "latest":
            target = next((int(row["id"]) for row in options if int(row["id"]) != self._conversation_id(chat_id)), None)
        else:
            try:
                target = int(selected)
            except ValueError:
                target = None
        allowed = {int(row["id"]) for row in options}
        if target is None or target not in allowed:
            await self.api.send_message(chat_id, "That chat is not available in this Telegram conversation. Use /resume to choose one.", reply_to_message_id=reply_to)
            return
        current = self._conversation_id(chat_id)
        if target != current:
            with suppress(Exception):
                self.conversation_store.end_conversation(current)
            self.state_store.set_conversation_id(CHANNEL_NAME, chat_id, target)
        history = self._conversation_history(target)
        await self.api.send_message(
            chat_id,
            f"Resumed chat #{target}. {len(history)} recent messages are back in context.",
            reply_to_message_id=reply_to,
        )

    def _conversation_history(self, conversation_id: int) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for row in self.conversation_store.get_messages(conversation_id)[-MAX_HISTORY_MESSAGES:]:
            role = str(row.get("role") or "assistant")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = str(row.get("content") or "").replace("\x00", "")
            history.append({"role": role, "content": content[:50_000]})
        return history

    async def _attachment_context(
        self, chat_id: int, message: dict[str, Any], update: dict[str, Any]
    ) -> tuple[str, str, Path | None, dict[str, Any] | None]:
        item = self._attachment_metadata(message, update)
        if item is None:
            return "", "", None, None
        cfg = self._telegram_config()
        if int(item.get("size") or 0) > cfg.max_attachment_bytes:
            raise ValueError(f"{item['name']} is larger than the configured attachment limit")
        root = Path(self.config.data_dir).expanduser() / "channels" / CHANNEL_NAME / "inbox" / str(chat_id)
        safe_name = Path(str(item["name"])).name.replace("\x00", "") or "attachment"
        destination = root / str(int(update.get("update_id") or 0)) / safe_name
        await self.api.download_file(str(item["file_id"]), destination, max_bytes=cfg.max_attachment_bytes)
        if item.get("kind") == "audio":
            return safe_name, "", destination, item
        inspection = inspect_attachment({"name": safe_name, "type": item.get("type", ""), "path": str(destination)})
        return safe_name, build_attachment_context([inspection]), None, item

    @staticmethod
    def _attachment_metadata(message: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            document_name = str(document.get("file_name") or f"document-{update.get('update_id', 'file')}")
            document_type = str(document.get("mime_type") or "application/octet-stream")
            suffix = Path(document_name).suffix.lower()
            return {
                "file_id": document["file_id"],
                "name": document_name,
                "type": document_type,
                "size": document.get("file_size") or 0,
                "duration": document.get("duration") or 0,
                "kind": "audio" if document_type.startswith("audio/") or suffix in {
                    ".aac", ".aiff", ".amr", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm", ".wma"
                } else "file",
            }
        photos = message.get("photo")
        if isinstance(photos, list) and photos and isinstance(photos[-1], dict) and photos[-1].get("file_id"):
            return {
                "file_id": photos[-1]["file_id"],
                "name": f"photo-{update.get('update_id', 'image')}.jpg",
                "type": "image/jpeg",
                "size": photos[-1].get("file_size") or 0,
                "kind": "image",
            }
        for key, default_type in (("video", "video/mp4"), ("audio", "audio/mpeg"), ("voice", "audio/ogg")):
            value = message.get(key)
            if isinstance(value, dict) and value.get("file_id"):
                return {
                    "file_id": value["file_id"],
                    "name": value.get("file_name") or f"{key}-{update.get('update_id', 'file')}",
                    "type": value.get("mime_type") or default_type,
                    "size": value.get("file_size") or 0,
                    "duration": value.get("duration") or 0,
                    "kind": "audio" if key in {"audio", "voice"} else "video",
                }
        return None

    @staticmethod
    def _has_attachment(message: dict[str, Any]) -> bool:
        return any(key in message for key in ("document", "photo", "video", "audio", "voice"))

    async def _handle_marketplace_command(
        self, chat_id: int, command: str, argument: str, reply_to: int | None
    ) -> None:
        """Run the Telegram-safe subset of the local marketplace commands.

        Discovery is read-only.  An install is never implicit: Ares shows the
        item and makes the user send a short-lived confirmation code before a
        local file or shared config is changed.
        """
        self._active_marketplace_requests[chat_id] = f"{command} {argument}".strip()
        try:
            if command == "/cancel":
                self._clear_pending_marketplace_actions(chat_id)
                await self._send_marketplace_message(chat_id, "Cancelled pending marketplace actions for this chat.", reply_to)
                return
            if command == "/confirm":
                await self._confirm_marketplace_action(chat_id, argument, reply_to)
                return
            try:
                tokens = shlex.split(argument, posix=True) if argument else []
            except ValueError as exc:
                await self._send_marketplace_message(chat_id, f"Invalid command quoting: {exc}", reply_to)
                return
            if command == "/skills":
                await self._telegram_skills_command(chat_id, tokens, reply_to)
            else:
                await self._telegram_mcp_command(chat_id, tokens, reply_to)
        finally:
            self._active_marketplace_requests.pop(chat_id, None)

    async def _telegram_skills_command(self, chat_id: int, tokens: list[str], reply_to: int | None) -> None:
        action = tokens[0].casefold() if tokens else "list"
        if (action in {"list", "ls"} and len(tokens) == 1) or not tokens:
            skills = self.skill_manager.list_all()
            if not skills:
                text = "Skills hub\n\nNo local skills are installed yet.\n\nStart with: /skills search <what you want to achieve>\nExamples: research, code review, daily planning, browser forms"
            else:
                categories: dict[str, list[str]] = defaultdict(list)
                marketplace_count = 0
                for skill in skills:
                    categories[skill.category].append(skill.name)
                    marketplace_count += int(bool(marketplace_record(skill)))
                category_rows = [
                    f"• {category} ({len(names)}): {', '.join(names[:4])}{'…' if len(names) > 4 else ''}"
                    for category, names in sorted(categories.items())
                ]
                text = (
                    f"Skills hub · {len(skills)} installed ({marketplace_count} from marketplace)\n\n"
                    "By category:\n" + "\n".join(category_rows[:8]) +
                    "\n\nFind something new: /skills search <goal>\n"
                    "Examples: /skills search research · /skills search code review"
                )
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "search" and len(tokens) > 1:
            query = " ".join(tokens[1:])
            client = SkillRegistryClient(self.config.skill_registries)
            try:
                results = await client.search(query)
            except (ValueError, SkillRegistryError) as exc:
                await self._send_marketplace_message(chat_id, f"Skill search failed: {exc}", reply_to)
                return
            if not results:
                text = f"No skills found for '{query}'." + _registry_errors(client.last_errors)
            else:
                choices = results[:5]
                self._marketplace_results.setdefault(chat_id, {})["skills"] = [item.reference for item in choices]
                rows = []
                for index, item in enumerate(choices, 1):
                    installed = self.skill_manager.get_skill(item.slug) is not None
                    availability = "✓ installed" if installed else "available"
                    rows.append(
                        f"{index}. {item.name} · {availability}\n"
                        f"   { _one_line(item.description) or 'No description supplied.'}\n"
                        f"   {item.registry} · {_community_label(item)}\n"
                        f"   ID: {item.reference}"
                    )
                text = (
                    f"Best skill matches for “{query}”\n\n" + "\n\n".join(rows) +
                    "\n\nNext: /skills info 1  ·  Install: /skills install 1\n"
                    "Numbers refer to this search; you can also use the full skill name."
                )
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "info" and len(tokens) == 2:
            name = self._resolve_marketplace_result(chat_id, "skills", tokens[1])
            if name is None:
                await self._send_marketplace_message(chat_id, "That skill number is not available. Search again with /skills search <goal>.", reply_to)
                return
            local = self.skill_manager.get_skill(name)
            if local is not None:
                source = marketplace_record(local) or {}
                text = (
                    f"Installed skill · {local.name}\nCategory: {local.category}\nVersion: {local.version}\n"
                    f"Source: {source.get('registry') or 'bundled/local'}\n\n"
                    f"What it helps with: {_one_line(local.description) or 'No description supplied.'}"
                )
                await self._send_marketplace_message(chat_id, text, reply_to)
                return
            client = SkillRegistryClient(self.config.skill_registries)
            try:
                detail = await client.get_skill(name)
            except (ValueError, SkillRegistryError) as exc:
                await self._send_marketplace_message(chat_id, f"Skill lookup failed: {exc}", reply_to)
                return
            if detail is None:
                await self._send_marketplace_message(chat_id, f"Skill '{name}' was not found." + _registry_errors(client.last_errors), reply_to)
                return
            dependencies = ", ".join(f"{item.type}:{item.name}" for item in detail.dependencies) or "none declared"
            text = (
                f"Skill · {detail.reference}\n\n"
                f"What it does: {_one_line(detail.description) or 'No description supplied.'}\n"
                f"Trust: {detail.security_status}{' · flagged' if detail.suspicious else ''}\n"
                f"Community: {_community_label(detail)}\n"
                f"Needs: {dependencies}\n\n"
                f"Install: /skills install {detail.reference}"
            )
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "install" and len(tokens) == 2:
            name = self._resolve_marketplace_result(chat_id, "skills", tokens[1])
            if name is None:
                await self._send_marketplace_message(chat_id, "That skill number is not available. Search again with /skills search <goal>.", reply_to)
                return
            await self._review_marketplace_action(chat_id, "skill_install", name, None, reply_to)
            return
        await self._send_marketplace_message(chat_id, "Usage: /skills [list|search <goal>|info <name-or-number>|install <name-or-number>]", reply_to)

    async def _telegram_mcp_command(self, chat_id: int, tokens: list[str], reply_to: int | None) -> None:
        action = tokens[0].casefold() if tokens else "list"
        manager = getattr(self.agent, "mcp_manager", None)
        if action in {"list", "ls", "status"} and len(tokens) <= 1:
            report = manager.readiness_report() if manager is not None else {"servers": {}}
            servers = report.get("servers") or {}
            if not servers:
                text = "MCP hub\n\nNo MCP servers are active. Find an integration with /mcp search <need>.\nExamples: github, calendar, database, browser testing"
            else:
                ready = sum(bool(item.get("ready")) for item in servers.values())
                rows = "\n".join(
                    f"• {name} · {'ready' if item.get('ready') else 'needs attention'} · {item.get('tools') or 0} tools"
                    + (f" · {str(item.get('error'))[:90]}" if item.get("error") else "")
                    for name, item in sorted(servers.items())
                )
                text = f"MCP hub · {ready}/{len(servers)} ready\n\n{rows}\n\nDiscover more: /mcp search <need>"
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "search" and len(tokens) > 1:
            query = " ".join(tokens[1:])
            client = MCPRegistryClient(self.config.mcp_registries)
            try:
                results = await client.search(query)
            except ValueError as exc:
                await self._send_marketplace_message(chat_id, f"MCP search failed: {exc}", reply_to)
                return
            if not results:
                text = f"No MCP servers found for '{query}'." + _registry_errors(client.last_errors)
            else:
                choices = results[:5]
                self._marketplace_results.setdefault(chat_id, {})["mcp"] = [item.name for item in choices]
                configured = {str(item.get("name") or "") for item in self.config.mcp_servers if isinstance(item, dict)}
                rows = []
                for index, item in enumerate(choices, 1):
                    availability = "✓ configured" if item.name in configured else "available"
                    trust = "verified" if item.verified else "registry listing"
                    rows.append(
                        f"{index}. {item.title or item.name} · {availability}\n"
                        f"   {_one_line(item.description) or 'No description supplied.'}\n"
                        f"   {trust} · {_community_label(item)}"
                    )
                text = (
                    f"Best MCP matches for “{query}”\n\n" + "\n\n".join(rows) +
                    "\n\nNext: /mcp info 1  ·  Add: /mcp add 1\n"
                    "Numbers refer to this search; you can also use the full server name."
                )
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "info" and len(tokens) == 2:
            name = self._resolve_marketplace_result(chat_id, "mcp", tokens[1])
            if name is None:
                await self._send_marketplace_message(chat_id, "That MCP number is not available. Search again with /mcp search <need>.", reply_to)
                return
            client = MCPRegistryClient(self.config.mcp_registries)
            try:
                detail = await client.get_server(name)
                plan = await client.get_install_command(name) if detail is not None else None
            except ValueError as exc:
                await self._send_marketplace_message(chat_id, f"MCP lookup failed: {exc}", reply_to)
                return
            if detail is None:
                await self._send_marketplace_message(chat_id, f"MCP server '{name}' was not found." + _registry_errors(client.last_errors), reply_to)
                return
            install = _format_mcp_plan(plan) if plan else "No safe automatic configuration is available."
            text = (
                f"MCP · {detail.title or detail.name}\n\n"
                f"What it does: {_one_line(detail.description) or 'No description supplied.'}\n"
                f"Trust: {'verified registry entry' if detail.verified else 'registry listing'}\n"
                f"Community: {_community_label(detail)}\n"
                f"Setup: {install}\n\n"
                f"Add: /mcp add {detail.name}"
            )
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "add" and len(tokens) == 2:
            name = self._resolve_marketplace_result(chat_id, "mcp", tokens[1])
            if name is None:
                await self._send_marketplace_message(chat_id, "That MCP number is not available. Search again with /mcp search <need>.", reply_to)
                return
            await self._review_marketplace_action(chat_id, "mcp_add", name, None, reply_to)
            return
        if action == "remove" and len(tokens) == 2:
            name = tokens[1].strip()
            await self._review_marketplace_action(chat_id, "mcp_remove", name, None, reply_to)
            return
        if action == "test" and len(tokens) <= 2:
            if manager is None:
                await self._send_marketplace_message(chat_id, "No MCP manager is active in this Ares process.", reply_to)
                return
            report = manager.readiness_report()
            name = tokens[1] if len(tokens) == 2 else ""
            servers = report.get("servers") or {}
            item = servers.get(name) if name else None
            if name and item is None:
                text = f"MCP '{name}' is not configured. Run /mcp status to see active servers."
            elif item is not None:
                text = f"MCP check · {name}\nStatus: {'ready' if item.get('ready') else 'needs attention'}\nTools: {item.get('tools') or 0}\nDetails: {item.get('error') or 'connection looks healthy'}"
            else:
                text = "MCP health\n" + "\n".join(f"• {server}: {'ready' if data.get('ready') else 'needs attention'}" for server, data in sorted(servers.items()))
            await self._send_marketplace_message(chat_id, text, reply_to)
            return
        if action == "refresh" and len(tokens) == 1:
            if manager is None:
                await self._send_marketplace_message(chat_id, "No MCP manager is active in this Ares process.", reply_to)
                return
            await manager.start()
            self.agent.refresh_tools()
            await self._send_marketplace_message(chat_id, "MCP connections refreshed. Run /mcp status for readiness.", reply_to)
            return
        await self._send_marketplace_message(chat_id, "Usage: /mcp [list|status|search <need>|info <name-or-number>|add <name-or-number>|remove <name>|test [server]|refresh]", reply_to)

    def _resolve_marketplace_result(self, chat_id: int, kind: str, value: str) -> str | None:
        """Resolve a numbered result from the most recent search for this chat."""
        value = str(value or "").strip()
        if not value.isdecimal():
            return value or None
        results = self._marketplace_results.get(chat_id, {}).get(kind, [])
        index = int(value) - 1
        return results[index] if 0 <= index < len(results) else None

    async def _send_marketplace_message(self, chat_id: int, text: str, reply_to: int | None) -> None:
        """Send and persist a compact command result for natural follow-ups.

        Telegram commands bypass the LLM by design.  Persisting their request
        and result in the shared conversation fixes the otherwise confusing
        case where a user asks “what does it do?” immediately after `/mcp info`.
        """
        text = _telegram_trim(text)
        request = self._active_marketplace_requests.get(chat_id)
        if request:
            session_id = self._conversation_id(chat_id)
            self.conversation_store.add_message(session_id, "user", request)
            self.conversation_store.add_message(session_id, "assistant", text)
        await self.api.send_message(chat_id, text, reply_to_message_id=reply_to)

    async def _review_marketplace_action(self, chat_id: int, action: str, name: str, registry: str | None, reply_to: int | None) -> None:
        if action == "skill_install":
            client = SkillRegistryClient(self.config.skill_registries)
            try:
                detail = await client.get_skill(name, registry)
            except (ValueError, SkillRegistryError) as exc:
                await self._send_marketplace_message(chat_id, f"Skill review failed: {exc}", reply_to)
                return
            if detail is None:
                await self._send_marketplace_message(chat_id, f"Skill '{name}' was not found." + _registry_errors(client.last_errors), reply_to)
                return
            if detail.suspicious:
                await self._send_marketplace_message(chat_id, "Install blocked: this skill is flagged by its registry. Review it manually.", reply_to)
                return
            summary = (
                f"Install skill · {detail.reference}\n\n"
                f"What it does: {_one_line(detail.description) or 'No description supplied.'}\n"
                f"Trust: {detail.security_status}\nCommunity: {_community_label(detail)}\n"
                f"Needs: {', '.join(f'{item.type}:{item.name}' for item in detail.dependencies) or 'none declared'}"
            )
        elif action == "mcp_remove":
            existing = {str(item.get("name") or "") for item in self.config.mcp_servers if isinstance(item, dict)}
            if name not in existing:
                await self._send_marketplace_message(chat_id, f"MCP server '{name}' is not configured.", reply_to)
                return
            summary = f"Remove MCP · {name}\n\nThis disconnects the server and removes it from the shared Ares config. Any tools it provided will no longer be available."
        else:
            existing = {str(item.get("name") or "") for item in self.config.mcp_servers if isinstance(item, dict)}
            if name in existing:
                await self._send_marketplace_message(chat_id, f"MCP server '{name}' is already configured.", reply_to)
                return
            client = MCPRegistryClient(self.config.mcp_registries)
            try:
                plan = await client.get_install_command(name, registry)
            except ValueError as exc:
                await self._send_marketplace_message(chat_id, f"MCP review failed: {exc}", reply_to)
                return
            if plan is None:
                await self._send_marketplace_message(chat_id, f"No safe automatic install plan was found for '{name}'.", reply_to)
                return
            summary = f"Add MCP · {name}\n\nSetup: {_format_mcp_plan(plan)}\nRequired env: {', '.join(plan.env_requirements) or 'none'}"
        code = secrets.token_urlsafe(4).replace("-", "").replace("_", "")[:6].upper()
        self._clear_pending_marketplace_actions(chat_id)
        self._pending_marketplace_actions[(chat_id, code)] = (time.monotonic() + MARKETPLACE_CONFIRMATION_TTL_SECONDS, action, name, registry)
        await self._send_marketplace_message(chat_id, f"{summary}\n\nNo changes made. Confirm within 5 minutes: /confirm {code}\nCancel: /cancel", reply_to)

    async def _confirm_marketplace_action(self, chat_id: int, code: str, reply_to: int | None) -> None:
        key = (chat_id, code.strip().upper())
        pending = self._pending_marketplace_actions.pop(key, None)
        if pending is None or pending[0] < time.monotonic():
            await self._send_marketplace_message(chat_id, "That confirmation code is invalid or expired. Run the install command again to review a fresh plan.", reply_to)
            return
        _, action, name, registry = pending
        if action == "skill_install":
            client = SkillRegistryClient(self.config.skill_registries)
            try:
                detail = await client.get_skill(name, registry)
                archive = await client.download(detail.reference, detail.version, detail.registry) if detail and not detail.suspicious else None
            except (ValueError, SkillRegistryError) as exc:
                await self._send_marketplace_message(chat_id, f"Skill install failed: {exc}", reply_to)
                return
            if detail is None or archive is None:
                await self._send_marketplace_message(chat_id, "The skill is no longer available as a safe hosted archive; nothing was installed.", reply_to)
                return
            try:
                installed = SafeSkillInstaller(Path(self.config.skill_dirs[0]).expanduser()).install(
                    archive, provenance={"registry": detail.registry, "slug": detail.reference, "version": detail.version, "canonical_url": detail.canonical_url}
                )
            except (FileExistsError, SkillValidationError) as exc:
                await self._send_marketplace_message(chat_id, f"Skill was not installed: {exc}", reply_to)
                return
            self.skill_manager = SkillManager(skill_dirs=list(self.config.skill_dirs or []) or None)
            await self._send_marketplace_message(chat_id, f"Installed skill '{installed.skill.name}' on this PC. It is now available to Ares everywhere.", reply_to)
            return
        if action == "mcp_remove":
            current = self._config_provider()
            before = len(current.mcp_servers)
            current.mcp_servers = [
                item for item in current.mcp_servers
                if str(item.get("name") or "") != name
            ]
            if len(current.mcp_servers) == before:
                await self._send_marketplace_message(chat_id, f"MCP server '{name}' was not found; nothing changed.", reply_to)
                return
            save_config(current)
            self.config = current
            self.agent.apply_config(current)
            previous = getattr(self.agent, "mcp_manager", None)
            if previous is not None:
                await previous.close()
            manager = MCPClientManager(current.mcp_servers, data_dir=current.data_dir)
            await manager.start()
            self.agent.set_mcp_manager(manager)
            await self._send_marketplace_message(chat_id, f"Removed MCP '{name}' from the shared Ares config and refreshed this process. Run /mcp status to confirm.", reply_to)
            return
        client = MCPRegistryClient(self.config.mcp_registries)
        try:
            plan = await client.get_install_command(name, registry)
        except ValueError as exc:
            await self._send_marketplace_message(chat_id, f"MCP install failed: {exc}", reply_to)
            return
        if plan is None:
            await self._send_marketplace_message(chat_id, "The MCP install plan is no longer available; nothing was changed.", reply_to)
            return
        current = self._config_provider()
        existing = {str(item.get("name") or "") for item in current.mcp_servers if isinstance(item, dict)}
        current.mcp_servers.append(plan.as_config(existing_names=existing))
        save_config(current)
        self.config = current
        self.agent.apply_config(current)
        previous = getattr(self.agent, "mcp_manager", None)
        if previous is not None:
            await previous.close()
        manager = MCPClientManager(current.mcp_servers, data_dir=current.data_dir)
        await manager.start()
        self.agent.set_mcp_manager(manager)
        await self._send_marketplace_message(chat_id, f"Added MCP '{name}' to the shared Ares config and refreshed this process. Run /mcp status to check it.", reply_to)

    def _clear_pending_marketplace_actions(self, chat_id: int) -> None:
        for key in [key for key in self._pending_marketplace_actions if key[0] == chat_id]:
            self._pending_marketplace_actions.pop(key, None)

    @staticmethod
    def _command(text: str) -> tuple[str, str]:
        if not text.startswith("/"):
            return "", ""
        command, _, argument = text.partition(" ")
        return command.split("@", 1)[0].lower(), argument.strip()

    @staticmethod
    def _file_delivery_requested(text: str) -> bool:
        return bool(FILE_REQUEST_RE.search(text or ""))

    def _extract_file_markers(self, response: str, user_text: str) -> tuple[str, list[Path]]:
        if not self._file_delivery_requested(user_text):
            return response, []
        paths = [Path(match.group(1).strip()).expanduser() for match in FILE_MARKER_RE.finditer(response)]
        return FILE_MARKER_RE.sub("", response).strip(), paths[:3]

    async def _send_requested_file(self, chat_id: int, argument: str, reply_to: int | None) -> None:
        if not argument:
            await self.api.send_message(chat_id, "Usage: /file C:\\path\\to\\file.ext", reply_to_message_id=reply_to)
            return
        delivery_status = await self._send_local_file(chat_id, Path(argument.strip().strip('"')).expanduser(), reply_to)
        self.conversation_store.add_message(self._conversation_id(chat_id), "assistant", delivery_status)

    async def _send_local_file(self, chat_id: int, path: Path, reply_to: int | None) -> str:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            message = "That local file was not found."
            await self.api.send_message(chat_id, message, reply_to_message_id=reply_to)
            return f"Telegram delivery failed: requested path was not found ({path})."
        if not resolved.is_file():
            message = "That path is not a regular file."
            await self.api.send_message(chat_id, message, reply_to_message_id=reply_to)
            return f"Telegram delivery failed: requested path is not a regular file ({resolved})."
        max_bytes = self._telegram_config().max_outbound_file_bytes
        if resolved.stat().st_size > max_bytes:
            message = (
                f"{resolved.name} is larger than the configured {max_bytes // (1024 * 1024)} MB Telegram upload limit."
            )
            await self.api.send_message(
                chat_id,
                message,
                reply_to_message_id=reply_to,
            )
            return f"Telegram delivery failed: {resolved.name} exceeds the configured upload limit."
        try:
            await self.api.send_chat_action(chat_id, "upload_document")
            await self.api.send_document(chat_id, resolved, caption=f"Ares file: {resolved.name}", reply_to_message_id=reply_to)
        except Exception as exc:
            logger.exception("Telegram file upload failed")
            message = f"I couldn't upload {resolved.name}: {exc}"
            await self.api.send_message(chat_id, message, reply_to_message_id=reply_to)
            return f"Telegram delivery failed: {resolved.name} could not be uploaded."
        return f"Telegram delivery verified: {resolved.name} was uploaded successfully to this Telegram chat."

    async def deliver_file(
        self,
        *,
        path: str | Path,
        chat_id: int | None = None,
        caption: str = "",
    ) -> dict[str, Any]:
        """Deliver a file only to a configured allowlisted chat.

        This method is intentionally separate from the inbound /file command:
        tools need a structured result and must never infer a recipient from a
        transient incoming message.
        """
        cfg = self._telegram_config()
        if not cfg.enabled or not resolve_bot_token(cfg):
            return {"ok": False, "error": "Telegram delivery is disabled or not configured."}
        allowed = {int(value) for value in cfg.allowed_chat_ids}
        if not allowed:
            return {"ok": False, "error": "Telegram delivery has no allowlisted chat IDs."}
        if chat_id is None:
            if len(allowed) != 1:
                return {
                    "ok": False,
                    "error": "Specify chat_id because more than one Telegram chat is allowlisted.",
                }
            target_chat_id = next(iter(allowed))
        else:
            try:
                target_chat_id = int(chat_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Telegram chat_id must be a valid integer."}
        if target_chat_id not in allowed:
            return {"ok": False, "error": "Telegram chat_id is not allowlisted for delivery."}

        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return {"ok": False, "error": "The requested local file was not found."}
        if not resolved.is_file():
            return {"ok": False, "error": "The requested path is not a regular file."}
        try:
            size = resolved.stat().st_size
        except OSError:
            return {"ok": False, "error": "The requested local file could not be inspected."}
        if size > cfg.max_outbound_file_bytes:
            return {
                "ok": False,
                "error": f"File exceeds the configured {cfg.max_outbound_file_bytes // (1024 * 1024)} MB Telegram upload limit.",
            }
        try:
            await self.api.send_chat_action(target_chat_id, "upload_document")
            response = await self.api.send_document(
                target_chat_id,
                resolved,
                caption=(caption or f"Ares file: {resolved.name}"),
            )
        except Exception as exc:
            logger.exception("Telegram tool file upload failed")
            return {"ok": False, "error": f"Telegram file upload failed: {exc}"}
        return {
            "ok": True,
            "chat_id": target_chat_id,
            "path": str(resolved),
            "name": resolved.name,
            "bytes": size,
            "telegram_message_id": response.get("message_id") if isinstance(response, dict) else None,
        }

    async def _send_text_chunks(self, chat_id: int, text: str, reply_to: int | None) -> None:
        for index, chunk in enumerate(_split_message(text)):
            await self.api.send_message(
                chat_id,
                chunk,
                reply_to_message_id=reply_to if index == 0 else None,
            )


class _TelegramProgress:
    """Low-noise visible activity while Ares is working on a Telegram turn."""

    def __init__(self, channel: TelegramChannel, chat_id: int, reply_to: int | None) -> None:
        self.channel = channel
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.message_id: int | None = None
        self.events: list[str] = ["Thinking"]
        self.workers: dict[str, dict[str, str]] = {}
        self.root_run_id = ""
        self.root_task = ""
        self.root_status = ""
        self.started_at = time.monotonic()
        self._last_edit_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.channel._telegram_config().show_tool_progress:
            with suppress(Exception):
                response = await self.channel.api.send_message(
                    self.chat_id, "⌛ Ares is working…\n• Thinking", reply_to_message_id=self.reply_to
                )
                self.message_id = int(response.get("message_id") or 0) or None
        self._typing_task = asyncio.create_task(self._typing_loop())

    async def event(self, value: str) -> None:
        if value not in self.events:
            self.events.append(value)
        await self._refresh()

    async def agent_event(self, event: dict[str, Any]) -> None:
        """Merge one stable supervisor event into the compact Telegram team view."""
        event_type = str(event.get("event_type") or "")
        root_run_id = str(event.get("root_run_id") or "")
        run_id = str(event.get("run_id") or "")
        if root_run_id:
            self.root_run_id = root_run_id
        if event.get("root_task"):
            self.root_task = _one_line(event["root_task"], 160)
        if event_type.startswith("orchestration_") or event_type == "synthesis_started":
            self.root_status = str(event.get("status") or event_type.removeprefix("orchestration_"))
        if run_id and run_id != root_run_id:
            worker = self.workers.setdefault(run_id, {
                "agent": str(event.get("agent") or "specialist"),
                "task_id": str(event.get("task_id") or "task"),
                "status": "queued",
                "detail": "Queued",
                "tool": "",
            })
            worker["agent"] = str(event.get("agent") or worker["agent"])
            worker["task_id"] = str(event.get("task_id") or worker["task_id"])
            worker["status"] = str(event.get("status") or worker["status"])
            if event.get("detail"):
                worker["detail"] = _one_line(event["detail"], 115)
            if event.get("tool"):
                worker["tool"] = str(event["tool"])
            if event_type in {"tool_completed", "agent_completed", "agent_failed", "agent_timed_out", "agent_blocked", "agent_cancelled"}:
                worker["tool"] = ""
        await self._refresh()

    def _render(self) -> str:
        if not self.workers:
            lines = "\n".join(f"• {event}" for event in self.events[-5:])
            return f"⌛ Ares is working…\n{lines}"
        statuses = [worker["status"] for worker in self.workers.values()]
        running = sum(status in ACTIVE_STATUSES for status in statuses)
        succeeded = statuses.count("succeeded")
        failed = sum(status in {"failed", "timed_out", "blocked"} for status in statuses)
        elapsed = max(0, round(time.monotonic() - self.started_at))
        lines = [
            f"⌛ Ares team is working · {elapsed}s",
            f"Workers: {running} active · {succeeded} done · {failed} issues",
        ]
        if self.root_task:
            lines.append(f"Task: {self.root_task}")
        lines.append("")
        for worker in list(self.workers.values())[:8]:
            status = worker["status"]
            mark = {
                "queued": "○", "running": "●", "succeeded": "✓", "failed": "✗",
                "timed_out": "⌛", "blocked": "⊘", "cancelled": "–",
            }.get(status, "·")
            activity = worker["detail"]
            if worker["tool"]:
                activity = f"{worker['tool'].replace('_', ' ')} · {activity}"
            lines.append(f"{mark} {worker['agent']} · {worker['task_id']}\n  {_one_line(activity, 130)}")
        if len(self.workers) > 8:
            lines.append(f"… and {len(self.workers) - 8} more workers")
        return _telegram_trim("\n".join(lines))

    async def _refresh(self, *, force: bool = False) -> None:
        if not self.message_id:
            return
        interval = 0.8
        now = time.monotonic()
        remaining = interval - (now - self._last_edit_at)
        if not force and remaining > 0:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._delayed_refresh(remaining), name=f"ares-telegram-progress:{self.chat_id}"
                )
            return
        self._last_edit_at = now
        with suppress(Exception):
            await self.channel.api.edit_message(self.chat_id, self.message_id, self._render())

    async def _delayed_refresh(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.05, delay))
            await self._refresh(force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def finish(self, text: str) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._refresh_task
        if self._typing_task is not None:
            self._typing_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._typing_task
        if self.message_id:
            if self.workers and text.startswith("✅"):
                statuses = [worker["status"] for worker in self.workers.values()]
                succeeded = statuses.count("succeeded")
                issues = sum(status in {"failed", "timed_out", "blocked"} for status in statuses)
                text = f"{text} · {succeeded}/{len(statuses)} specialists completed"
                if issues:
                    text += f" · {issues} issues"
            with suppress(Exception):
                await self.channel.api.edit_message(self.chat_id, self.message_id, text)

    async def _typing_loop(self) -> None:
        while True:
            with suppress(Exception):
                await self.channel.api.send_chat_action(self.chat_id, "typing")
            await asyncio.sleep(4.0)


def _telegram_trim(text: str, limit: int = 3800) -> str:
    """Keep command responses under Telegram's message limit with a clear tail."""
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 32].rstrip() + "\n… response truncated"


def _one_line(text: object, limit: int = 180) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _community_label(item: Any) -> str:
    """Format registry-provided popularity without confusing rank with stars."""
    stars = getattr(item, "stars", None)
    downloads = getattr(item, "downloads", None)
    parts: list[str] = []
    if isinstance(stars, int) and stars >= 0:
        parts.append(f"★ {stars:,}")
    if isinstance(downloads, int) and downloads >= 0:
        parts.append(f"↓ {downloads:,}")
    if parts:
        return " · ".join(parts)
    score = getattr(item, "score", None)
    if isinstance(score, (int, float)) and score > 0:
        return f"search match {min(int(score * 100), 100)}%"
    return "popularity not published"


def _registry_errors(errors: dict[str, str]) -> str:
    if not errors:
        return ""
    names = ", ".join(sorted(errors)[:3])
    return f"\nRegistry unavailable: {names}."


def _format_mcp_plan(plan: Any) -> str:
    if plan is None:
        return "none"
    if getattr(plan, "server_url", ""):
        return f"{plan.transport} {plan.server_url}"
    arguments = " ".join(getattr(plan, "args", ()) or ())
    return f"{plan.transport}: {plan.command} {arguments}".strip()


def _split_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE_CHARS) -> list[str]:
    """Break an answer at a line/word boundary without losing characters."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = max(remaining.rfind("\n", 0, limit), remaining.rfind(" ", 0, limit))
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n ")
    if remaining:
        chunks.append(remaining)
    return chunks


async def run_telegram_channel() -> None:
    """Run Telegram alone, for a headless always-on PC deployment."""
    config = load_config()
    if not config.telegram.enabled:
        print("Ares Telegram is not configured. Run `python -m ares --telegram-setup` first.")
        return
    if not resolve_bot_token(config.telegram):
        print("Ares Telegram needs a bot token. Run `python -m ares --telegram-setup` first.")
        return
    memory_store = MemoryStore()
    conversation_store = ConversationStore()
    manager = MCPClientManager(config.mcp_servers, data_dir=config.data_dir) if config.mcp_servers else None
    agent = Agent(
        config=config,
        memory_store=memory_store,
        conversation_store=conversation_store,
        mcp_manager=manager,
    )
    channel = TelegramChannel(
        config=config,
        agent=agent,
        conversation_store=conversation_store,
        config_provider=load_config,
    )
    mcp_start_task: asyncio.Task | None = None
    watcher_service = None
    proactive_notifier = DesktopNotifier(
        enabled=bool(
            config.enable_desktop_notifications and config.proactive.desktop_enabled
        )
    )

    async def deliver_proactive(message: str, candidate: dict[str, Any]) -> list[str]:
        channels: list[str] = []
        if config.proactive.workspace_enabled:
            conversation_id = conversation_store.start_conversation()
            conversation_store.rename_conversation(
                conversation_id,
                f"Ares follow-up · {str(candidate.get('title') or candidate.get('description') or 'initiative')[:55]}",
            )
            conversation_store.add_message(conversation_id, "assistant", message)
            channels.append("workspace")
        if proactive_notifier.notify("Ares follow-up", message):
            channels.append("desktop")
        if config.proactive.telegram_enabled:
            channels.extend(await channel.deliver_proactive(message))
        return channels

    proactive_service = (
        ProactiveService(
            goal_store=agent.goal_store,
            commitment_store=getattr(agent, "commitment_store", None),
            follow_up_store=getattr(agent, "follow_up_store", None),
            memory_store=memory_store,
            profile_manager=getattr(agent, "profile_manager", None),
            conversation_store=conversation_store,
            llm_client=getattr(agent, "llm", None),
            config=config.proactive,
            deliver=deliver_proactive,
        )
        if getattr(agent, "goal_store", None) is not None
        else None
    )
    try:
        if config.watcher.enabled:
            from ares.watcher.integration import create_agent_watcher_service
            watcher_service = create_agent_watcher_service(config, agent)
            await watcher_service.start()
        if manager is not None:
            async def start_mcp() -> None:
                try:
                    await manager.start()
                except Exception as exc:
                    logger.warning("Telegram started without MCP integrations: %s", exc)
                    return
                agent.refresh_tools()

            # A first-run MCP may download a package. Telegram must remain
            # reachable while that optional work completes.
            mcp_start_task = asyncio.create_task(start_mcp(), name="ares-telegram-mcp")
        if proactive_service is not None:
            await proactive_service.start()
        await channel.run_forever()
    finally:
        if proactive_service is not None:
            with suppress(Exception):
                await proactive_service.stop()
        if watcher_service is not None:
            with suppress(Exception):
                await watcher_service.stop()
            setter = getattr(agent.tool_executor, "set_watcher_service", None)
            if setter is not None:
                setter(None)
        await channel.stop()
        if mcp_start_task is not None and not mcp_start_task.done():
            mcp_start_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await mcp_start_task
        if manager is not None:
            with suppress(Exception):
                await manager.close()
        await agent.close()
        conversation_store.close()
        memory_store.close()
