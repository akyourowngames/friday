"""Telegram long-polling channel for the local Ares server.

The implementation deliberately uses Telegram's HTTPS Bot API directly rather
than a framework.  It keeps the dependency surface small and makes the two
important reliability properties explicit: provider offsets are stored in the
same SQLite database as Ares conversations, and a remote chat is never trusted
until its exact chat ID is in the local allowlist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from collections import defaultdict
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

import httpx

from ares.agent import Agent
from ares.attachments import build_attachment_context, inspect_attachment
from ares.channels.store import ChannelStore
from ares.config import get_db_path, load_config
from ares.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig, TelegramConfig
from ares.tools.mcp_client import MCPClientManager


logger = logging.getLogger(__name__)

CHANNEL_NAME = "telegram"
MAX_TELEGRAM_MESSAGE_CHARS = 4096
MAX_HISTORY_MESSAGES = 40
TOOL_TOKEN_RE = re.compile(r"^\[tool:([^:]+):(.*)\]$", re.DOTALL)
TOOL_START_TOKEN_RE = re.compile(r"^\[tool_start:([^\]]+)\]$")
FILE_MARKER_RE = re.compile(r"\[\[telegram_file:(.+?)\]\]", re.IGNORECASE)
FILE_REQUEST_RE = re.compile(
    r"\b(?:send|upload|share|attach)\b[\s\S]{0,120}\b(?:file|document|report|output|result|it|this)\b"
    r"|\b(?:telegram|sendfile)\b",
    re.IGNORECASE,
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
        sleep: Callable[[float], Any] = asyncio.sleep,
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
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Agent state and local tools are not generally safe to execute in
        # parallel.  Telegram conversations remain separate while executions
        # are serialized deterministically.
        self._agent_lock = asyncio.Lock()

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
            print("Ares Telegram: connecting…")
            await self.api.delete_webhook()
            me = await self.api.get_me()
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

    def _telegram_config(self) -> TelegramConfig:
        candidate = self._config_provider()
        return getattr(candidate, "telegram", self.config.telegram)

    def _is_enabled(self) -> bool:
        cfg = self._telegram_config()
        return bool(cfg.enabled and resolve_bot_token(cfg))

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
                    "Ares is connected to this PC. Send a message or a document/photo.\n\n"
                    "Commands:\n/new — start a fresh Ares session\n/status — channel status\n"
                    "/file <path> — upload a local PC file\n/help — show this help",
                    reply_to_message_id=reply_to,
                )
                return
            if command == "/new":
                await self._start_new_session(chat_id)
                await self.api.send_message(chat_id, "Started a new Ares session for this chat.", reply_to_message_id=reply_to)
                return
            if command == "/status":
                await self.api.send_message(
                    chat_id,
                    f"Ares Telegram channel is online. Model: {self.agent.config.model}. "
                    "This chat is allowlisted and its conversation is saved locally on the PC.",
                    reply_to_message_id=reply_to,
                )
                return
            if command == "/file":
                await self._send_requested_file(chat_id, argument, reply_to)
                return
            if command and command.startswith("/") and not self._has_attachment(message):
                await self.api.send_message(chat_id, "Unknown command. Use /help for Telegram commands, or send it as a normal request.", reply_to_message_id=reply_to)
                return

            await self._handle_chat_message(chat_id, message, update, text, reply_to)

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
        try:
            attachment_label, inspection_context = await self._attachment_context(chat_id, message, update)
        except ValueError as exc:
            await self.api.send_message(chat_id, f"I couldn't accept that attachment: {exc}", reply_to_message_id=reply_to)
            return
        except Exception as exc:
            logger.exception("Telegram attachment failed")
            await self.api.send_message(chat_id, f"I couldn't download that attachment: {exc}", reply_to_message_id=reply_to)
            return

        if not text and not attachment_label:
            await self.api.send_message(chat_id, "Send a message, document, or photo for Ares to work with.", reply_to_message_id=reply_to)
            return

        visible_content = text or f"Attached: {attachment_label}"
        prompt = text or "Inspect and explain the attached file."
        if attachment_label:
            prompt += " The file is attached to this turn. Do not ask the user to re-upload it."
        if inspection_context:
            prompt += "\n\n" + inspection_context
        if self._file_delivery_requested(text):
            prompt += (
                "\n\n## Telegram delivery\nThe user explicitly asked for a local file to be uploaded "
                "to this Telegram chat. If you create or locate that file, append exactly "
                "[[telegram_file:ABSOLUTE_PATH]] on its own line. Never use this marker unless "
                "the user explicitly asked for delivery."
            )

        session_id = self._conversation_id(chat_id)
        history = self._conversation_history(session_id)
        self.conversation_store.add_message(session_id, "user", visible_content)
        status = _TelegramProgress(self, chat_id, reply_to)
        await status.start()
        try:
            response, tool_calls = await self._run_agent(prompt, history, status)
        except Exception as exc:
            logger.exception("Telegram agent turn failed")
            await status.finish("⚠️ Ares could not finish that request.")
            await self.api.send_message(chat_id, f"Ares hit an error: {exc}", reply_to_message_id=reply_to)
            return

        clean_response, paths = self._extract_file_markers(response, text)
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        if clean_response or tool_calls_json:
            self.conversation_store.add_message(session_id, "assistant", clean_response, tool_calls_json)
        await status.finish("✅ Done")

        if clean_response:
            await self._send_text_chunks(chat_id, clean_response, reply_to)
        elif not paths:
            await self.api.send_message(chat_id, "Done.", reply_to_message_id=reply_to)
        for path in paths:
            await self._send_local_file(chat_id, path, reply_to)

    async def _run_agent(
        self,
        prompt: str,
        history: list[dict[str, str]],
        status: "_TelegramProgress",
    ) -> tuple[str, list[dict[str, Any]]]:
        response_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        # See the lock comment in __init__. This still keeps a separate
        # persistent history for each remote chat.
        async with self._agent_lock:
            async for chunk in self.agent.run_stream(prompt, conversation_history=history):
                start = TOOL_START_TOKEN_RE.match(chunk)
                if start:
                    await status.event(self._tool_label(start.group(1), "Using"))
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
            self.state_store.set_conversation_id(CHANNEL_NAME, chat_id, conversation_id)
        return conversation_id

    async def _start_new_session(self, chat_id: int) -> None:
        old_id = self.state_store.get_conversation_id(CHANNEL_NAME, chat_id)
        if old_id is not None:
            with suppress(Exception):
                self.conversation_store.end_conversation(old_id)
        new_id = self.conversation_store.start_conversation()
        self.state_store.set_conversation_id(CHANNEL_NAME, chat_id, new_id)

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
    ) -> tuple[str, str]:
        item = self._attachment_metadata(message, update)
        if item is None:
            return "", ""
        cfg = self._telegram_config()
        if int(item.get("size") or 0) > cfg.max_attachment_bytes:
            raise ValueError(f"{item['name']} is larger than the configured attachment limit")
        root = Path(self.config.data_dir).expanduser() / "channels" / CHANNEL_NAME / "inbox" / str(chat_id)
        safe_name = Path(str(item["name"])).name.replace("\x00", "") or "attachment"
        destination = root / str(int(update.get("update_id") or 0)) / safe_name
        await self.api.download_file(str(item["file_id"]), destination, max_bytes=cfg.max_attachment_bytes)
        inspection = inspect_attachment({"name": safe_name, "type": item.get("type", ""), "path": str(destination)})
        return safe_name, build_attachment_context([inspection])

    @staticmethod
    def _attachment_metadata(message: dict[str, Any], update: dict[str, Any]) -> dict[str, Any] | None:
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            return {
                "file_id": document["file_id"],
                "name": document.get("file_name") or f"document-{update.get('update_id', 'file')}",
                "type": document.get("mime_type") or "application/octet-stream",
                "size": document.get("file_size") or 0,
            }
        photos = message.get("photo")
        if isinstance(photos, list) and photos and isinstance(photos[-1], dict) and photos[-1].get("file_id"):
            return {
                "file_id": photos[-1]["file_id"],
                "name": f"photo-{update.get('update_id', 'image')}.jpg",
                "type": "image/jpeg",
                "size": photos[-1].get("file_size") or 0,
            }
        for key, default_type in (("video", "video/mp4"), ("audio", "audio/mpeg"), ("voice", "audio/ogg")):
            value = message.get(key)
            if isinstance(value, dict) and value.get("file_id"):
                return {
                    "file_id": value["file_id"],
                    "name": value.get("file_name") or f"{key}-{update.get('update_id', 'file')}",
                    "type": value.get("mime_type") or default_type,
                    "size": value.get("file_size") or 0,
                }
        return None

    @staticmethod
    def _has_attachment(message: dict[str, Any]) -> bool:
        return any(key in message for key in ("document", "photo", "video", "audio", "voice"))

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
        await self._send_local_file(chat_id, Path(argument.strip().strip('"')).expanduser(), reply_to)

    async def _send_local_file(self, chat_id: int, path: Path, reply_to: int | None) -> None:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            await self.api.send_message(chat_id, "That local file was not found.", reply_to_message_id=reply_to)
            return
        if not resolved.is_file():
            await self.api.send_message(chat_id, "That path is not a regular file.", reply_to_message_id=reply_to)
            return
        max_bytes = self._telegram_config().max_outbound_file_bytes
        if resolved.stat().st_size > max_bytes:
            await self.api.send_message(
                chat_id,
                f"{resolved.name} is larger than the configured {max_bytes // (1024 * 1024)} MB Telegram upload limit.",
                reply_to_message_id=reply_to,
            )
            return
        try:
            await self.api.send_chat_action(chat_id, "upload_document")
            await self.api.send_document(chat_id, resolved, caption=f"Ares file: {resolved.name}", reply_to_message_id=reply_to)
        except Exception as exc:
            logger.exception("Telegram file upload failed")
            await self.api.send_message(chat_id, f"I couldn't upload {resolved.name}: {exc}", reply_to_message_id=reply_to)

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
        if not self.message_id:
            return
        lines = "\n".join(f"• {event}" for event in self.events[-5:])
        with suppress(Exception):
            await self.channel.api.edit_message(self.chat_id, self.message_id, f"⌛ Ares is working…\n{lines}")

    async def finish(self, text: str) -> None:
        if self._typing_task is not None:
            self._typing_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self._typing_task
        if self.message_id:
            with suppress(Exception):
                await self.channel.api.edit_message(self.chat_id, self.message_id, text)

    async def _typing_loop(self) -> None:
        while True:
            with suppress(Exception):
                await self.channel.api.send_chat_action(self.chat_id, "typing")
            await asyncio.sleep(4.0)


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
    try:
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
        await channel.run_forever()
    finally:
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
