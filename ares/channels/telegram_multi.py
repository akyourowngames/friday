"""Multi-bot Telegram channel for running multiple Ares instances in one group.

Allows multiple Telegram bots (each with its own Ares agent) to coexist
in a single Telegram group. Users @mention specific bots to interact with them.

Each bot has:
- Its own bot token and long-poll loop
- Its own Ares agent instance and tool executor
- Its own conversation history
- @mention-based routing (only responds when tagged)

Bots process messages **in parallel** — one bot never waits for another.
While a bot is working it posts the same live tool-progress feed the single
Telegram channel uses (Thinking → Using tool → Finished tool → Done).

Usage:
    Set up multiple bots in config.json:

    {
        "telegram_multi": {
            "enabled": true,
            "bots": [
                {
                    "name": "Jarvis",
                    "bot_token": "BOT_TOKEN_1",
                    "mention": "@jarvis_bot"
                },
                {
                    "name": "Friday",
                    "bot_token": "BOT_TOKEN_2",
                    "mention": "@friday_bot"
                }
            ],
            "allowed_chat_ids": [-1001234567890],
            "require_mention": true,
            "show_tool_progress": true
        }
    }
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ares.agent import Agent
from ares.channels.telegram import (
    TelegramBotAPI,
    TOOL_TOKEN_RE,
    TOOL_START_TOKEN_RE,
    TOOL_PROGRESS_TOKEN_RE,
    FILE_MARKER_RE,
    FILE_REQUEST_RE,
    _telegram_trim,
    _split_message,
    _one_line,
)
from ares.channels.store import ChannelStore
from ares.config import get_db_path
from ares.context.conversations import ConversationStore
from ares.memory import MemoryStore
from ares.models import AppConfig, TelegramMultiConfig, TelegramBotConfig
from ares.tools.mcp_client import MCPClientManager
from ares.skills.discovery import SkillManager

logger = logging.getLogger(__name__)

CHANNEL_NAME = "telegram_multi"
MAX_HISTORY_MESSAGES = 40


@dataclass
class BotInstance:
    """Runtime state for a single bot."""

    config: TelegramBotConfig
    api: TelegramBotAPI
    agent: Agent
    skill_manager: SkillManager
    conversation_store: ConversationStore
    bot_user_id: int = 0
    bot_username: str = ""  # Telegram username without @
    # Active background turns for this bot (for clean shutdown)
    inflight: set[asyncio.Task] = field(default_factory=set)


class _BotDeliveryAdapter:
    """Minimal adapter so ToolExecutor.telegram_send_file works per-bot."""

    def __init__(self, instance: BotInstance, multi: "MultiTelegramChannel") -> None:
        self._instance = instance
        self._multi = multi

    async def deliver_file(
        self,
        *,
        path: str | Path,
        chat_id: int | None = None,
        caption: str = "",
    ) -> dict[str, Any]:
        cfg = self._multi._telegram_multi_config()
        allowed = {int(value) for value in cfg.allowed_chat_ids}
        if not allowed:
            return {"ok": False, "error": "Telegram multi delivery has no allowlisted chat IDs."}
        if chat_id is None:
            if len(allowed) != 1:
                return {
                    "ok": False,
                    "error": "Specify chat_id because more than one Telegram chat is allowlisted.",
                }
            target = next(iter(allowed))
        else:
            try:
                target = int(chat_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "Telegram chat_id must be a valid integer."}
        if target not in allowed:
            return {"ok": False, "error": "Telegram chat_id is not allowlisted for delivery."}

        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return {"ok": False, "error": "The requested local file was not found."}
        if not resolved.is_file():
            return {"ok": False, "error": "The requested path is not a regular file."}

        max_bytes = 50 * 1024 * 1024
        try:
            size = resolved.stat().st_size
        except OSError:
            return {"ok": False, "error": "The requested local file could not be inspected."}
        if size > max_bytes:
            return {"ok": False, "error": "File exceeds the 50 MB Telegram upload limit."}

        try:
            await self._instance.api.send_chat_action(target, "upload_document")
            response = await self._instance.api.send_document(
                target,
                resolved,
                caption=(caption or f"{self._instance.config.name} file: {resolved.name}"),
            )
        except Exception as exc:
            logger.exception(
                "Multi-bot %s file upload failed", self._instance.config.name
            )
            return {"ok": False, "error": f"Telegram file upload failed: {exc}"}
        return {
            "ok": True,
            "chat_id": target,
            "path": str(resolved),
            "name": resolved.name,
            "bytes": size,
            "bot": self._instance.config.name,
            "telegram_message_id": response.get("message_id") if isinstance(response, dict) else None,
        }


class _MultiBotProgress:
    """Live progress message for one multi-bot turn — mirrors single-bot UX."""

    def __init__(
        self,
        instance: BotInstance,
        chat_id: int,
        reply_to: int | None,
        *,
        show_progress: bool = True,
    ) -> None:
        self.instance = instance
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.show_progress = show_progress
        self.message_id: int | None = None
        self.events: list[str] = ["Thinking"]
        self.started_at = time.monotonic()
        self._last_edit_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._typing_task: asyncio.Task | None = None
        self._bot_name = instance.config.name

    async def start(self) -> None:
        if self.show_progress:
            with suppress(Exception):
                response = await self.instance.api.send_message(
                    self.chat_id,
                    f"⌛ {self._bot_name} is working…\n• Thinking",
                    reply_to_message_id=self.reply_to,
                )
                self.message_id = int(response.get("message_id") or 0) or None
        self._typing_task = asyncio.create_task(
            self._typing_loop(),
            name=f"ares-multi-typing-{self._bot_name}-{self.chat_id}",
        )

    async def event(self, value: str) -> None:
        if value not in self.events:
            self.events.append(value)
        await self._refresh()

    def _render(self) -> str:
        elapsed = max(0, round(time.monotonic() - self.started_at))
        lines = "\n".join(f"• {event}" for event in self.events[-6:])
        return f"⌛ {self._bot_name} is working · {elapsed}s\n{lines}"

    async def _refresh(self, *, force: bool = False) -> None:
        if not self.message_id:
            return
        interval = 0.8
        now = time.monotonic()
        remaining = interval - (now - self._last_edit_at)
        if not force and remaining > 0:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._delayed_refresh(remaining),
                    name=f"ares-multi-progress-{self._bot_name}:{self.chat_id}",
                )
            return
        self._last_edit_at = now
        with suppress(Exception):
            await self.instance.api.edit_message(
                self.chat_id, self.message_id, _telegram_trim(self._render())
            )

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
            with suppress(Exception):
                await self.instance.api.edit_message(
                    self.chat_id, self.message_id, _telegram_trim(text)
                )

    async def _typing_loop(self) -> None:
        while True:
            with suppress(Exception):
                await self.instance.api.send_chat_action(self.chat_id, "typing")
            await asyncio.sleep(4.0)


class MultiTelegramChannel:
    """Manages multiple Telegram bots in a single group.

    Each bot runs its own polling loop. Incoming messages are handled on
    background tasks so bots work in true parallel — one bot never blocks
    another, and long tool runs keep the poll loop free.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        conversation_store: ConversationStore,
        config_provider: Callable[[], AppConfig] | None = None,
    ) -> None:
        self.config = config
        self.conversation_store = conversation_store
        self._config_provider = config_provider or (lambda: self.config)

        multi_config = getattr(config, "telegram_multi", None)
        if multi_config is None:
            multi_config = TelegramMultiConfig()

        self._multi_config = multi_config
        self._bots: dict[str, BotInstance] = {}  # mention key -> BotInstance
        # Per (bot_name, chat_id) lock: same bot serializes turns in one chat,
        # different bots in the same chat run fully in parallel.
        self._turn_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._memory_store = MemoryStore()

        db_path = get_db_path(Path(config.data_dir).expanduser())
        self.state_store = ChannelStore(Path(db_path))

    def _telegram_multi_config(self) -> TelegramMultiConfig:
        candidate = self._config_provider()
        return getattr(candidate, "telegram_multi", self._multi_config)

    def _turn_lock(self, bot_name: str, chat_id: int) -> asyncio.Lock:
        key = (bot_name, chat_id)
        if key not in self._turn_locks:
            self._turn_locks[key] = asyncio.Lock()
        return self._turn_locks[key]

    async def start(self) -> None:
        """Start all configured bots."""
        cfg = self._telegram_multi_config()
        if not cfg.enabled or not cfg.bots:
            logger.info("Multi-bot Telegram is disabled or has no bots configured")
            return

        for bot_cfg in cfg.bots:
            try:
                await self._start_bot(bot_cfg)
            except Exception as exc:
                logger.error("Failed to start bot %s: %s", bot_cfg.name, exc)

        if not self._bots:
            logger.warning("No bots were started successfully")
            return

        for instance in self._bots.values():
            task = asyncio.create_task(
                self._poll_bot(instance),
                name=f"ares-telegram-multi-{instance.config.name}",
            )
            self._tasks.append(task)

        logger.info(
            "Multi-bot Telegram started with %d bots (parallel): %s",
            len(self._bots),
            ", ".join(
                f"{b.config.name} (@{b.bot_username or b.config.mention.lstrip('@')})"
                for b in self._bots.values()
            ),
        )

    async def _start_bot(self, bot_cfg: TelegramBotConfig) -> None:
        """Initialize a single bot instance with its own agent and tools."""
        api = TelegramBotAPI(bot_cfg.bot_token)

        try:
            me = await api.get_me()
            bot_user_id = me.get("id", 0)
            bot_username = str(me.get("username") or "")
        except Exception as exc:
            logger.error("Failed to get info for bot %s: %s", bot_cfg.name, exc)
            await api.close()
            return

        # Isolated memory + conversation store per bot so concurrent turns
        # never share mutable agent state across bots.
        memory_store = MemoryStore()
        bot_conversation_store = ConversationStore()
        mcp_manager = (
            MCPClientManager(
                self.config.mcp_servers,
                data_dir=self.config.data_dir,
            )
            if self.config.mcp_servers
            else None
        )

        agent = Agent(
            config=self.config,
            memory_store=memory_store,
            conversation_store=bot_conversation_store,
            mcp_manager=mcp_manager,
        )

        if bot_cfg.model:
            agent.set_model(bot_cfg.model)

        skill_manager = SkillManager(
            skill_dirs=list(self.config.skill_dirs or []) or None
        )

        instance = BotInstance(
            config=bot_cfg,
            api=api,
            agent=agent,
            skill_manager=skill_manager,
            conversation_store=bot_conversation_store,
            bot_user_id=bot_user_id,
            bot_username=bot_username,
        )

        # Wire telegram_send_file through this bot's own API
        executor = getattr(agent, "tool_executor", None)
        attach = getattr(executor, "set_telegram_channel", None)
        if callable(attach):
            attach(_BotDeliveryAdapter(instance, self))

        # Index by configured mention and live username so either form works
        mention_key = bot_cfg.mention.lower().strip()
        if not mention_key.startswith("@"):
            mention_key = f"@{mention_key}"
        self._bots[mention_key] = instance
        if bot_username:
            username_key = f"@{bot_username.lower()}"
            if username_key != mention_key:
                self._bots[username_key] = instance

        await api.delete_webhook()
        set_commands = getattr(api, "set_commands", None)
        if callable(set_commands):
            with suppress(Exception):
                await set_commands((
                    ("start", "Start with this bot"),
                    ("help", "Show commands"),
                    ("new", "New session"),
                    ("status", "Bot status"),
                ))

        logger.info(
            "Bot %s started: @%s (ID: %d) — independent agent + parallel turns",
            bot_cfg.name,
            bot_username,
            bot_user_id,
        )

    async def stop(self) -> None:
        """Stop all bots and cancel in-flight turns."""
        self._stop_event.set()

        for instance in list(self._bots.values()):
            for task in list(instance.inflight):
                task.cancel()
            for task in list(instance.inflight):
                with suppress(asyncio.CancelledError, Exception):
                    await task
            instance.inflight.clear()

        for task in self._tasks:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        # Deduplicate instances (same bot may be under two mention keys)
        closed: set[int] = set()
        for instance in self._bots.values():
            if id(instance) in closed:
                continue
            closed.add(id(instance))
            executor = getattr(instance.agent, "tool_executor", None)
            detach = getattr(executor, "set_telegram_channel", None)
            if callable(detach):
                with suppress(Exception):
                    detach(None)
            close = getattr(instance.api, "close", None)
            if close:
                result = close()
                if result is not None:
                    with suppress(Exception):
                        await result

        self._bots.clear()

    def _is_authorized(self, chat: dict[str, Any], cfg: TelegramMultiConfig) -> bool:
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return False
        if chat.get("type") != "private" and not cfg.allow_group_chats:
            return False
        return chat_id in {int(value) for value in cfg.allowed_chat_ids}

    def _find_mentioned_bot(self, text: str) -> BotInstance | None:
        """Find which bot was @mentioned. First match wins; longest mention preferred."""
        text_lower = text.lower()
        # Prefer longer mentions first so @friday_bot beats @friday if both exist
        keys = sorted(self._bots.keys(), key=len, reverse=True)
        for mention in keys:
            if mention in text_lower:
                return self._bots[mention]
        return None

    def _is_this_bot_mentioned(self, text: str, instance: BotInstance) -> bool:
        text_lower = text.lower()
        candidates = {
            instance.config.mention.lower().strip(),
            f"@{instance.bot_username.lower()}" if instance.bot_username else "",
        }
        for raw in candidates:
            if not raw:
                continue
            mention = raw if raw.startswith("@") else f"@{raw}"
            if mention in text_lower:
                return True
        # Also accept bare bot name as a soft mention when unique enough
        return False

    def _strip_mention(self, text: str, instance: BotInstance) -> str:
        """Remove this bot's @mentions from the message text."""
        cleaned = text
        usernames = [
            instance.bot_username,
            instance.config.mention.lstrip("@"),
        ]
        for username in usernames:
            if not username:
                continue
            pattern = re.compile(r"@" + re.escape(username) + r"\b", re.IGNORECASE)
            cleaned = pattern.sub("", cleaned)
        return cleaned.strip()

    def _conversation_id(self, chat_id: int, bot_name: str) -> int:
        external_key = f"{chat_id}_{bot_name}"
        conversation_id = self.state_store.get_conversation_id(CHANNEL_NAME, external_key)
        if conversation_id is None:
            conversation_id = self.conversation_store.start_conversation()
            self.state_store.set_conversation_id(CHANNEL_NAME, external_key, conversation_id)
        return conversation_id

    def _conversation_history(self, conversation_id: int) -> list[dict[str, str]]:
        loader = getattr(self.conversation_store, "get_messages_for_model", None)
        if callable(loader):
            try:
                return list(loader(conversation_id))
            except TypeError:
                pass
        history: list[dict[str, str]] = []
        for row in self.conversation_store.get_messages(conversation_id)[-MAX_HISTORY_MESSAGES:]:
            role = str(row.get("role") or "assistant")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = str(row.get("content") or "").replace("\x00", "")
            history.append({"role": role, "content": content[:50_000]})
        return history

    async def _poll_bot(self, instance: BotInstance) -> None:
        """Long-poll for updates. Message work is spawned, never awaited inline."""
        backoff = 1.0
        offset = 0

        logger.info("Starting parallel poll loop for bot %s", instance.config.name)

        while not self._stop_event.is_set():
            try:
                updates = await instance.api.get_updates(
                    offset=offset,
                    timeout=30,
                )
                backoff = 1.0

                for update in updates:
                    update_id = int(update.get("update_id") or 0)
                    if update_id and update_id < offset:
                        continue

                    # Advance offset immediately so a long agent turn never
                    # stalls the poll cursor or other bots.
                    if update_id:
                        offset = update_id + 1

                    task = asyncio.create_task(
                        self._safe_handle_update(instance, update),
                        name=(
                            f"ares-multi-turn-{instance.config.name}"
                            f"-{update_id or 'x'}"
                        ),
                    )
                    instance.inflight.add(task)

                    def _cleanup(
                        done: asyncio.Task,
                        *,
                        bot: BotInstance = instance,
                    ) -> None:
                        bot.inflight.discard(done)
                        with suppress(asyncio.CancelledError, Exception):
                            exc = done.exception()
                            if exc is not None:
                                logger.exception(
                                    "Bot %s background turn failed: %s",
                                    bot.config.name,
                                    exc,
                                )

                    task.add_done_callback(_cleanup)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Bot %s polling error: %s; retrying in %.0fs",
                    instance.config.name,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        logger.info("Poll loop stopped for bot %s", instance.config.name)

    async def _safe_handle_update(
        self,
        instance: BotInstance,
        update: dict[str, Any],
    ) -> None:
        try:
            await self._handle_update(instance, update)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Bot %s failed to handle update %s",
                instance.config.name,
                update.get("update_id"),
            )

    async def _handle_update(
        self,
        instance: BotInstance,
        update: dict[str, Any],
    ) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return

        chat = message.get("chat")
        if not isinstance(chat, dict) or "id" not in chat:
            return

        chat_id = int(chat["id"])
        text = str(message.get("text") or message.get("caption") or "").strip()
        reply_to = int(message.get("message_id") or 0) or None

        cfg = self._telegram_multi_config()

        if not self._is_authorized(chat, cfg):
            if text.startswith("/start"):
                await instance.api.send_message(
                    chat_id,
                    f"Bot {instance.config.name} is running, but this chat is not authorized. "
                    f"Chat ID: {chat_id}. Add it to telegram_multi.allowed_chat_ids.",
                    reply_to_message_id=reply_to,
                )
            return

        is_group = chat.get("type") in ("group", "supergroup")
        if is_group and cfg.require_mention:
            if not self._is_this_bot_mentioned(text, instance):
                # Different bot tagged, or no mention — leave it alone.
                # Do not block; just return so this bot stays free.
                return

        # Serialize only this bot's turns in this chat. Other bots keep running.
        async with self._turn_lock(instance.config.name, chat_id):
            command, argument = self._parse_command(text)

            if command in {"/start", "/help"}:
                siblings = sorted({
                    b.config.name for b in self._bots.values()
                })
                await instance.api.send_message(
                    chat_id,
                    f"🤖 {instance.config.name} is ready (parallel multi-bot mode)!\n\n"
                    f"Tag me with {instance.config.mention} "
                    f"(or @{instance.bot_username}) to chat.\n"
                    f"I run tools independently — other bots do not wait on me.\n\n"
                    f"Bots in this group: {', '.join(siblings)}\n\n"
                    "Commands:\n"
                    "/new — start fresh session\n"
                    "/status — bot status\n"
                    "/help — this message",
                    reply_to_message_id=reply_to,
                )
                return

            if command == "/new":
                external_key = f"{chat_id}_{instance.config.name}"
                old_id = self.state_store.get_conversation_id(CHANNEL_NAME, external_key)
                if old_id is not None:
                    with suppress(Exception):
                        self.conversation_store.end_conversation(old_id)
                new_id = self.conversation_store.start_conversation()
                self.state_store.set_conversation_id(CHANNEL_NAME, external_key, new_id)
                await instance.api.send_message(
                    chat_id,
                    f"✅ New session started for {instance.config.name}",
                    reply_to_message_id=reply_to,
                )
                return

            if command == "/status":
                model = getattr(instance.agent, "config", None)
                model_name = getattr(model, "model", None) or getattr(
                    self.config, "model", "unknown"
                )
                active = sum(1 for t in instance.inflight if not t.done())
                await instance.api.send_message(
                    chat_id,
                    f"🤖 {instance.config.name}\n"
                    f"Model: {model_name}\n"
                    f"Mention: {instance.config.mention}\n"
                    f"Username: @{instance.bot_username or '—'}\n"
                    f"Active turns: {active}\n"
                    f"Mode: parallel multi-bot (tools run like single bot)",
                    reply_to_message_id=reply_to,
                )
                return

            if not text:
                return

            await self._handle_chat_message(instance, chat_id, text, reply_to)

    def _parse_command(self, text: str) -> tuple[str, str]:
        cleaned = re.sub(r"@\w+\s*", "", text).strip()
        if not cleaned.startswith("/"):
            return "", ""
        parts = cleaned.split(maxsplit=1)
        command = parts[0].lower().split("@", 1)[0]
        argument = parts[1] if len(parts) > 1 else ""
        return command, argument

    async def _handle_chat_message(
        self,
        instance: BotInstance,
        chat_id: int,
        text: str,
        reply_to: int | None,
    ) -> None:
        """Process a regular chat message with live tool progress."""
        clean_text = self._strip_mention(text, instance)
        if not clean_text:
            clean_text = text

        prompt = (
            f"{clean_text}\n\n"
            "## Telegram multi-bot reply rules\n"
            f"You are {instance.config.name}, one of several Ares bots in this group. "
            "Reply as a compact Telegram message: plain text, short sections, no Markdown tables. "
            "You have full Ares tools — use them the same way a single-bot channel would. "
            "Execute tools when needed; do not claim you cannot run tools. "
            "Other bots may be working in parallel; ignore their work unless the user asks.\n\n"
            "## File Delivery\n"
            "This bot can upload local files (charts, images, reports) directly to this Telegram chat. "
            "When you create or locate a file the user wants, append exactly "
            "[[telegram_file:ABSOLUTE_PATH]] on its own line. Never use this marker unless "
            "the user explicitly asked for file delivery. Never say Telegram delivery is unavailable — "
            "the channel supports file uploads.\n\n"
            "## File Discovery\n"
            "When the user asks about files, charts, reports, or artifacts from past work, do NOT "
            "hallucinate or claim to have sent files that are not in the conversation history. "
            "Instead, use search_files or list_directory to find actual files on disk. "
            "Check ~/.ares/data/healthcare-reports/ and ~/.ares/data/research/downloads/ for "
            "healthcare charts and reports."
        )
        if instance.config.system_prompt_suffix:
            prompt += f"\n\n{instance.config.system_prompt_suffix}"

        if FILE_REQUEST_RE.search(text):
            prompt += (
                "\n\nThe user explicitly asked for a local file to be uploaded "
                "to this Telegram chat. If you create or locate that file, append exactly "
                "[[telegram_file:ABSOLUTE_PATH]] on its own line."
            )

        session_id = self._conversation_id(chat_id, instance.config.name)
        history = self._conversation_history(session_id)
        cfg = self._telegram_multi_config()
        show_progress = bool(getattr(cfg, "show_tool_progress", True))

        status = _MultiBotProgress(
            instance, chat_id, reply_to, show_progress=show_progress
        )
        await status.start()

        try:
            response, tool_calls = await self._run_agent(
                instance, prompt, history, status, session_id
            )
        except Exception as exc:
            logger.exception("Bot %s failed to process message", instance.config.name)
            await status.finish(f"⚠️ {instance.config.name} could not finish that request.")
            await instance.api.send_message(
                chat_id,
                f"⚠️ {instance.config.name} hit an error: {exc}",
                reply_to_message_id=reply_to,
            )
            return

        if not response:
            response = "Got it — anything else you need?"

        # Extract [[telegram_file:...]] markers from the response
        paths: list[Path] = []
        if FILE_REQUEST_RE.search(text):
            paths = [
                Path(match.group(1).strip()).expanduser()
                for match in FILE_MARKER_RE.finditer(response)
            ][:3]
        clean_response = FILE_MARKER_RE.sub("", response).strip() if paths else response

        self.conversation_store.add_message(session_id, "user", clean_text)
        tool_calls_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        try:
            self.conversation_store.add_message(
                session_id, "assistant", clean_response, tool_calls_json
            )
        except TypeError:
            self.conversation_store.add_message(session_id, "assistant", clean_response)

        tool_summary = ""
        if tool_calls:
            names = [str(t.get("tool") or "?") for t in tool_calls]
            tool_summary = f" · {len(tool_calls)} tool{'s' if len(tool_calls) != 1 else ''}: " + ", ".join(
                n.replace("_", " ") for n in names[:4]
            )
            if len(names) > 4:
                tool_summary += f" +{len(names) - 4} more"

        await status.finish(f"✅ {instance.config.name} done{tool_summary}")

        if clean_response:
            for i, chunk in enumerate(_split_message(clean_response)):
                await instance.api.send_message(
                    chat_id,
                    chunk,
                    reply_to_message_id=reply_to if i == 0 else None,
                )
        elif not paths:
            await instance.api.send_message(
                chat_id,
                "Got it — anything else you need?",
                reply_to_message_id=reply_to,
            )

        # Send any files the agent marked for delivery
        for path in paths:
            await self._send_local_file(instance, chat_id, path, reply_to, session_id)

    async def _run_agent(
        self,
        instance: BotInstance,
        prompt: str,
        history: list[dict[str, str]],
        status: _MultiBotProgress,
        session_id: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Stream the agent turn and surface tool activity like the single bot."""
        response_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        scope_factory = getattr(instance.agent, "session_scope", None)
        runtime_session_id = f"telegram-multi-{instance.config.name}-{session_id}"
        scope = scope_factory(runtime_session_id) if callable(scope_factory) else nullcontext()

        with scope:
            run_stream = instance.agent.run_stream
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
                stream_kwargs["reflection_input"] = prompt

            async for chunk in run_stream(prompt, **stream_kwargs):
                if not isinstance(chunk, str):
                    continue

                start = TOOL_START_TOKEN_RE.match(chunk)
                if start:
                    await status.event(self._tool_label(start.group(1), "Using"))
                    continue

                progress = TOOL_PROGRESS_TOKEN_RE.match(chunk)
                if progress:
                    await status.event(
                        self._tool_label(progress.group(1), progress.group(2))
                    )
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

                # Soft fallback for any tool-ish tokens we don't fully parse
                if chunk.startswith("[tool:") or chunk.startswith("[tool_start:") or chunk.startswith("[tool_progress:"):
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

    async def _send_local_file(
        self,
        instance: BotInstance,
        chat_id: int,
        path: Path,
        reply_to: int | None,
        session_id: int,
    ) -> None:
        """Upload a local file to Telegram via this bot's API."""
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            await instance.api.send_message(
                chat_id, "That local file was not found.",
                reply_to_message_id=reply_to,
            )
            return
        if not resolved.is_file():
            await instance.api.send_message(
                chat_id, "That path is not a regular file.",
                reply_to_message_id=reply_to,
            )
            return
        max_bytes = 50 * 1024 * 1024
        try:
            size = resolved.stat().st_size
        except OSError:
            await instance.api.send_message(
                chat_id, "Could not inspect the file.",
                reply_to_message_id=reply_to,
            )
            return
        if size > max_bytes:
            await instance.api.send_message(
                chat_id,
                f"{resolved.name} is larger than the 50 MB Telegram upload limit.",
                reply_to_message_id=reply_to,
            )
            return
        try:
            await instance.api.send_chat_action(chat_id, "upload_document")
            await instance.api.send_document(
                chat_id,
                resolved,
                caption=f"{instance.config.name} file: {resolved.name}",
                reply_to_message_id=reply_to,
            )
            delivery_status = f"Telegram delivery verified: {resolved.name} was uploaded successfully."
        except Exception as exc:
            logger.exception("Multi-bot %s file upload failed", instance.config.name)
            await instance.api.send_message(
                chat_id,
                f"I couldn't upload {resolved.name}: {exc}",
                reply_to_message_id=reply_to,
            )
            delivery_status = f"Telegram delivery failed: {resolved.name} could not be uploaded."
        self.conversation_store.add_message(session_id, "assistant", delivery_status)
