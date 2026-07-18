"""Terminal UI using Rich and prompt_toolkit."""

import asyncio
from contextlib import nullcontext, suppress
from difflib import SequenceMatcher
import inspect
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import has_completions
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle, message_dialog, radiolist_dialog
from prompt_toolkit.styles import Style

from rich import box
from rich.console import Console
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from ares.agent import Agent
from ares.browser import BrowserManager, VALID_BROWSER_MODES
from ares.context import ProjectContext
from ares.context_blend import build_context_prompt
from ares.conversations import ConversationStore
from ares.tools.exporter import export_data, import_data
from ares.memory import MemoryStore
from ares.memory_cleaner import MemoryCleaner
from ares.profile import ProfileManager, PROFILE_TEMPLATE
from ares.proactive import ProactiveService
from ares.onboarding import OnboardingWizard
from ares.reminders import DesktopNotifier
from ares.tools.renders import get_renderer, render_generic_tool
from ares.soul import SoulManager, SOUL_TEMPLATE
from ares.config import _ensure_mcp_defaults, load_config, save_config
from ares.llm import (
    MODEL_REGISTRY,
    PROVIDER_BASE_URLS,
    SUPPORTED_PROVIDERS,
    activate_provider_config,
    configured_provider_api_key,
    default_model_for_provider,
    normalize_provider,
    provider_for_model,
)
from ares.multi_agent_display import ACTIVE_STATUSES, elapsed_label, one_line, summarize_runs
from ares.skills import SkillManager
from ares.tools.mcp_client import MCPClientManager, redact_mcp_text
from ares.tools.adb_bridge import phone_status as get_phone_status
from ares.cron import CronScheduler, CronStore
from ares.cron.toast import CronToastManager
from ares.session import SessionManager
from ares.sessions import SessionStore
from .constants import CLI_BOX, COMPLETER, STYLE, TOOL_OUTPUT_MODES
from .marketplace import MarketplaceCommandMixin
from .runtime import clear_current_task_cancellation, history_path, supports_unicode_output

# Compatibility exports for integrations that imported CLI internals before the
# package split. New code should use ``ares.cli.runtime`` directly.
_history_path = history_path

INTERACTIVE_DIALOG_STYLE = Style.from_dict({
    "dialog": "bg:ansiblack",
    "dialog.body": "bg:ansiblack fg:ansiwhite",
    "dialog shadow": "bg:ansiblack",
    "frame.label": "fg:ansicyan bold",
    "radio-list": "bg:ansiblack fg:ansiwhite",
    "radio": "fg:ansicyan",
    "radio-selected": "fg:ansibrightcyan bold",
    "radio-checked": "fg:ansigreen bold",
    "button": "bg:ansicyan fg:ansiblack bold",
    "button.focused": "bg:ansigreen fg:ansiblack bold",
})


class InteractiveCLICompleter(Completer):
    """Contextual slash-command completion with selectable model/provider rows."""

    def get_completions(self, document, complete_event):
        before_cursor = document.text_before_cursor
        model_match = re.fullmatch(r"/model\s+(.*)", before_cursor, re.IGNORECASE)
        if model_match:
            prefix = model_match.group(1)
            for group_key, group in MODEL_REGISTRY.items():
                endpoint = "NVIDIA NIM" if group_key == "nvidia" else (
                    "GitHub Copilot" if group_key == "copilot" else "OpenCode Zen"
                )
                for item in group["models"]:
                    model_id = item["id"]
                    if model_id.casefold().startswith(prefix.casefold()):
                        yield Completion(
                            model_id,
                            start_position=-len(prefix),
                            display_meta=endpoint,
                        )
            return

        provider_match = re.fullmatch(r"/provider\s+(.*)", before_cursor, re.IGNORECASE)
        if provider_match:
            prefix = provider_match.group(1)
            options = [*PROVIDER_BASE_URLS, "nvidia"]
            for provider in options:
                if provider.casefold().startswith(prefix.casefold()):
                    detail = "alias for nim" if provider == "nvidia" else (
                        PROVIDER_BASE_URLS[provider] or "GitHub Copilot SDK"
                    )
                    yield Completion(
                        provider,
                        start_position=-len(prefix),
                        display_meta=detail,
                    )
            return

        if not before_cursor.startswith("/"):
            return
        for command in COMPLETER.words:
            if command.casefold().startswith(before_cursor.casefold()):
                yield Completion(command, start_position=-len(before_cursor))


def _interactive_completion_bindings() -> KeyBindings:
    """Let Up/Down choose a visible completion without losing history keys."""
    bindings = KeyBindings()

    @bindings.add("down", filter=has_completions)
    def select_next(event) -> None:
        event.current_buffer.complete_next()

    @bindings.add("up", filter=has_completions)
    def select_previous(event) -> None:
        event.current_buffer.complete_previous()

    return bindings


def _clear_current_task_cancellation() -> None:
    """Backward-compatible wrapper around the split runtime helper."""
    current_task = asyncio.current_task()
    if current_task is None or not hasattr(current_task, "uncancel"):
        return
    while current_task.cancelling():
        current_task.uncancel()

TOOL_LABELS = {
    "web_search": "web search",
    "read_file": "file read",
    "search_files": "file search",
    "list_directory": "directory scan",
    "store_memory": "memory",
    "search_memory": "memory search",
    "update_memory": "memory",
    "delete_memory": "memory",
    "list_skills": "skills",
    "load_skill": "skills",
    "create_skill": "skills",
    "export_data": "export",
    "fetch_url": "web page",
    "get_file_info": "file info",
    "glob_pattern": "file match",
    "write_file": "file write",
    "edit_file": "file edit",
    "create_directory": "directory create",
    "delete_file": "file delete",
    "move_file": "file move",
    "batch_edit": "file edits",
    "glob_apply": "file edits",
    "show_file_with_line_numbers": "file preview",
    "insert_line": "file edit",
    "replace_lines": "file edit",
    "delete_lines": "file edit",
    "preview_diff": "diff preview",
    "backup_file": "file backup",
    "undo_last_edit": "file undo",
    "batch_file_ops": "file operations",
    "find_text": "text search",
    "append_to_file": "file edit",
    "prepend_to_file": "file edit",
    "compare_files": "file compare",
    "create_file_from_template": "file create",
    "safe_path_status": "path check",
    "disk_usage": "disk usage",
    "checksum": "checksum",
    "copy_file": "file copy",
    "find_duplicates": "duplicate search",
    "tail_file": "file tail",
    "head_file": "file head",
    "count_lines": "line count",
    "file_tree": "file tree",
    "run_code": "code run",
    "run_command": "command",
    "terminal_exec": "terminal",
    "generate_image": "image",
    "image_info": "image info",
    "resize_image": "image resize",
    "convert_image": "image convert",
    "crop_image": "image crop",
    "create_cron_job": "scheduler",
    "list_cron_jobs": "scheduler",
    "get_cron_job": "scheduler",
    "update_cron_job": "scheduler",
    "delete_cron_job": "scheduler",
    "run_cron_job_now": "scheduler",
    "get_cron_logs": "scheduler logs",
    "get_watcher_capabilities": "watcher integrations",
    "create_watcher": "watcher deploy",
    "list_watchers": "watcher fleet",
    "get_watcher": "watcher detail",
    "update_watcher": "watcher config",
    "pause_watcher": "watcher pause",
    "resume_watcher": "watcher resume",
    "run_watcher_now": "watcher check",
    "list_watcher_events": "watcher incidents",
    "acknowledge_watcher_event": "watcher incident",
    "get_watcher_overview": "watcher health",
    "delete_watcher": "watcher delete",
    "phone_status": "phone",
    "phone_get_notifications": "phone",
    "phone_search_contact": "phone",
    "phone_send_sms": "phone",
    "phone_call_number": "phone",
    "phone_launch_app": "phone",
    "phone_open_url": "phone",
    "telephony_status": "telephony",
    "telephony_call": "telephony call",
    "telephony_answer": "telephony",
    "telephony_hangup": "telephony",
    "telephony_mute": "telephony",
    "telephony_get_call": "telephony",
    "telephony_list_calls": "telephony",
    "telephony_list_contacts": "telephony contacts",
    "telephony_save_contact": "telephony contacts",
    "telephony_transfer": "telephony",
    "update_config": "config",
    "get_current_datetime": "clock",
}


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class AresCLI(MarketplaceCommandMixin):
    """The main CLI application for Ares."""

    def __init__(self):
        self.console = Console(color_system="auto", highlight=False)
        self.unicode_output = supports_unicode_output()
        self.icons = {
            "fire": "",
            "thinking": "",
            "tool": "",
            "bot": "Ares",
            "bye": "",
            "prompt": "> ",
            "current": " < current",
        }
        self.tool_output_mode = "summary"
        self.config = load_config()
        configured_model_provider = provider_for_model(self.config.model)
        if configured_model_provider and configured_model_provider != normalize_provider(self.config.provider):
            activate_provider_config(self.config, configured_model_provider)
            save_config(self.config)
        self.memory_store = MemoryStore()
        data_dir = Path(self.config.data_dir).expanduser()
        self.soul_manager = SoulManager(data_dir=data_dir, soul_path=self.config.soul_path)
        self.profile_manager = ProfileManager(data_dir=data_dir, profile_path=self.config.profile_path)
        self.project_context = ProjectContext(
            enabled=self.config.project_context_enabled,
            max_files=self.config.project_context_max_files,
        )
        self.soul_manager.ensure_exists()
        self.profile_manager.ensure_exists()
        if (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and not self.config.onboarding_completed
            and not self.profile_manager.is_populated()
        ):
            OnboardingWizard(
                console=self.console,
                config=self.config,
                profile_manager=self.profile_manager,
                soul_manager=self.soul_manager,
            ).run(re_run=False)
        self.skill_manager = SkillManager(skill_dirs=list(self.config.skill_dirs or []) or None)
        self.mcp_manager = (
            MCPClientManager(self.config.mcp_servers, data_dir=self.config.data_dir)
            if self.config.mcp_servers
            else None
        )
        self._mcp_config_signature = self._get_mcp_config_signature(self.config)
        self._mcp_reconfigure_pending = False
        self.conversation_store = ConversationStore()
        self.conversation_id = self.conversation_store.start_conversation()
        self.conversation_store.summarize_ended_without_summary(
            min_messages=self.config.session_summary_messages
        )
        self.session_manager = SessionManager()
        self.session_store = SessionStore(data_dir=data_dir)
        self.agent = Agent(
            memory_store=self.memory_store,
            conversation_store=self.conversation_store,
            config=self.config,
            mcp_manager=self.mcp_manager,
            session_store=self.session_store,
            session_id=self.session_manager.get_id(),
        )
        self._session_finalized = False
        # A brand-new CLI conversation never inherits globally recent messages.
        # Explicitly resumed conversations may use the conversation-scoped API,
        # but old turns are context only and cannot become direct chat history.
        self.conversation_history: list[dict] = self._conversation_history_for_model(
            self.conversation_id
        )
        self.notifier = DesktopNotifier(enabled=self.config.enable_desktop_notifications)
        self.proactive_service = ProactiveService(
            goal_store=self.agent.goal_store,
            commitment_store=getattr(self.agent, "commitment_store", None),
            follow_up_store=getattr(self.agent, "follow_up_store", None),
            memory_store=self.memory_store,
            profile_manager=getattr(self.agent, "profile_manager", None),
            conversation_store=self.conversation_store,
            llm_client=getattr(self.agent, "llm", None),
            config=self.config.proactive,
            deliver=self._deliver_proactive_message,
        ) if getattr(self.agent, "goal_store", None) is not None else None
        cron_root = Path(self.config.data_dir).expanduser().parent
        self.cron_store = CronStore(cron_root)
        self.toast_manager = CronToastManager(self.console)
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            tick_seconds=self.config.cron_tick_seconds,
            max_concurrent=self.config.cron_max_concurrent,
            on_complete=self.toast_manager,
        ) if self.config.cron_enabled else None
        self.session = self._create_prompt_session()

    def _conversation_history_for_model(self, conversation_id: int) -> list[dict]:
        """Load only direct messages belonging to one explicit conversation."""
        loader = getattr(self.conversation_store, "get_messages_for_model", None)
        if not callable(loader):
            return []
        try:
            rows = loader(conversation_id, limit=self.config.max_context_messages)
        except TypeError:
            rows = loader(conversation_id)
        return list(rows or [])

    def _resume_conversations(self, limit: int = 10) -> list[dict]:
        bounded = max(1, min(int(limit), 50))
        listing = getattr(self.conversation_store, "list_resumable_conversations", None)
        if not callable(listing):
            listing = getattr(self.conversation_store, "list_conversations", None)
        if not callable(listing):
            return []
        try:
            rows = list(listing(limit=bounded) or [])
        except TypeError:
            rows = list(listing() or [])

        # The current CLI session is created before the prompt appears. It is
        # not a saved chat to restore, and selecting it used to make /resume
        # look broken because its history is empty.
        return [
            row
            for row in rows
            if str(row.get("id") or "") != str(self.conversation_id)
        ][:bounded]

    def _resume_conversation(self, conversation_id: int) -> bool:
        if conversation_id != self.conversation_id:
            available = {
                int(row.get("id"))
                for row in self._resume_conversations(50)
                if row.get("id") is not None
            }
            if conversation_id not in available:
                return False

        restored_history = self._conversation_history_for_model(conversation_id)
        if conversation_id != self.conversation_id and not restored_history:
            return False
        if conversation_id != self.conversation_id:
            with suppress(Exception):
                self.conversation_store.end_conversation(self.conversation_id)
            self.conversation_id = conversation_id
        self.conversation_history = restored_history
        self.session_manager = SessionManager()
        set_session_id = getattr(self.agent, "set_session_id", None)
        if callable(set_session_id):
            set_session_id(self.session_manager.get_id())
        if hasattr(self.agent, "last_messages"):
            self.agent.last_messages = []
        self._session_finalized = False
        return True

    def _create_prompt_session(self) -> PromptSession | None:
        """Create an interactive prompt session when attached to a TTY."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        return PromptSession(
            history=FileHistory(history_path()),
            auto_suggest=AutoSuggestFromHistory(),
            completer=InteractiveCLICompleter(),
            complete_while_typing=True,
            complete_style=CompleteStyle.MULTI_COLUMN,
            reserve_space_for_menu=8,
            key_bindings=_interactive_completion_bindings(),
            style=STYLE,
        )

    def _reset_prompt_session(self) -> None:
        """Recreate prompt_toolkit state after a handled cancellation."""
        cron_root = Path(self.config.data_dir).expanduser().parent
        self.cron_store = CronStore(cron_root)
        self.cron_scheduler = CronScheduler(
            self.cron_store,
            tick_seconds=self.config.cron_tick_seconds,
            max_concurrent=self.config.cron_max_concurrent,
            on_complete=self.toast_manager,
        ) if self.config.cron_enabled else None
        self.session = self._create_prompt_session()
        self._mcp_config_signature = self._get_mcp_config_signature(self.config)
        self._mcp_reconfigure_pending = False

    async def _deliver_proactive_message(self, message: str, candidate: dict) -> list[str]:
        """Make CLI initiative visible without interrupting the active prompt."""
        channels: list[str] = []
        if self.config.proactive.workspace_enabled:
            conversation_id = self.conversation_store.start_conversation()
            self.conversation_store.rename_conversation(
                conversation_id,
                f"Ares follow-up · {str(candidate.get('title') or candidate.get('description') or 'initiative')[:55]}",
            )
            self.conversation_store.add_message(conversation_id, "assistant", message)
            channels.append("workspace")
        self.notifier.enabled = bool(
            self.config.enable_desktop_notifications and self.config.proactive.desktop_enabled
        )
        if self.notifier.notify("Ares follow-up", message):
            channels.append("desktop")
        return channels

    @staticmethod
    def _get_mcp_config_signature(config) -> str:
        """Return the portion of shared settings that requires reconnecting MCP."""
        return json.dumps(
            {"data_dir": config.data_dir, "mcp_servers": config.mcp_servers},
            sort_keys=True,
            default=str,
        )

    def _sync_shared_state(self) -> None:
        """Reload settings saved by another local Ares surface before handling input."""
        latest = load_config()
        if latest.model_dump() == self.config.model_dump():
            return

        previous_mcp_signature = self._mcp_config_signature
        self.config = latest
        data_dir = Path(latest.data_dir).expanduser()
        self.soul_manager = SoulManager(data_dir=data_dir, soul_path=latest.soul_path)
        self.profile_manager = ProfileManager(data_dir=data_dir, profile_path=latest.profile_path)
        self.soul_manager.ensure_exists()
        self.profile_manager.ensure_exists()
        self.browser_manager = BrowserManager(self.config)
        self.project_context = ProjectContext(
            enabled=latest.project_context_enabled,
            max_files=latest.project_context_max_files,
        )
        if hasattr(self.agent, "apply_config"):
            self.agent.apply_config(latest)
        else:  # Keep lightweight test doubles compatible.
            self.agent.set_model(latest.model)
        self._mcp_config_signature = self._get_mcp_config_signature(latest)
        self._mcp_reconfigure_pending = previous_mcp_signature != self._mcp_config_signature
        self.browser_manager = BrowserManager(self.config)
        if self.proactive_service is not None:
            self.proactive_service.config = latest.proactive
            if latest.proactive.enabled and not self.proactive_service.running:
                asyncio.create_task(self.proactive_service.start())

    async def _refresh_mcp_manager_if_needed(self) -> None:
        """Reconnect integrations after another Ares surface changed them."""
        if not getattr(self, "_mcp_reconfigure_pending", False):
            return
        self._mcp_reconfigure_pending = False
        previous_manager = self.mcp_manager
        self.mcp_manager = (
            MCPClientManager(self.config.mcp_servers, data_dir=self.config.data_dir)
            if self.config.mcp_servers
            else None
        )
        if hasattr(self.agent, "set_mcp_manager"):
            self.agent.set_mcp_manager(self.mcp_manager)
        else:
            self.agent.mcp_manager = self.mcp_manager
        if previous_manager is not None:
            with suppress(Exception):
                await previous_manager.close()
        if self.mcp_manager is not None:
            with suppress(Exception):
                await self.mcp_manager.start()
            if hasattr(self.agent, "refresh_tools"):
                self.agent.refresh_tools()

    def _browser(self) -> BrowserManager:
        """Return a manager tied to the current shared configuration."""
        manager = getattr(self, "browser_manager", None)
        if manager is None or manager.config is not self.config:
            manager = BrowserManager(self.config)
            self.browser_manager = manager
        return manager

    def _set_browser_mode(self, mode: str) -> None:
        """Persist a browser mode and queue an in-process Playwright reconnect."""
        normalized = str(mode or "").strip().lower()
        if normalized not in VALID_BROWSER_MODES:
            raise ValueError("Browser mode must be isolated, system, extension, or auto.")
        self.config.browser_mode = normalized
        _ensure_mcp_defaults(self.config)
        save_config(self.config)
        self.browser_manager = BrowserManager(self.config)
        self._mcp_config_signature = self._get_mcp_config_signature(self.config)
        self._mcp_reconfigure_pending = True
        if hasattr(self.agent, "apply_config"):
            self.agent.apply_config(self.config)

    async def _apply_browser_mode_hint(self, user_input: str) -> None:
        """Honor a direct request for a real, extension, or clean browser now."""
        hint = self._browser().get_mode_from_request(user_input)
        if hint is None or hint == self.config.browser_mode:
            return
        self._set_browser_mode(hint)
        effective = self._browser().resolve_mode()
        if hint == "system" and effective != "system":
            self.console.print(
                "[yellow]System Chrome mode is saved, but CDP is not available yet. "
                "Using the isolated profile until Chrome is launched with /browser launch.[/yellow]"
            )
        elif hint == "system":
            self.console.print("[yellow]Using system Chrome mode through the available CDP session.[/yellow]")
        elif hint == "extension":
            self.console.print("[green]Using Playwright extension mode for your selected existing tab.[/green]")
        else:
            self.console.print("[green]Using Ares' isolated Playwright browser profile.[/green]")
        await self._refresh_mcp_manager_if_needed()

    async def _prompt(self) -> str:
        """Read one prompt line from an interactive session or plain stdin."""
        if self.session is not None:
            return await self.session.prompt_async(self.icons["prompt"])
        return await asyncio.to_thread(input, self.icons["prompt"])

    async def _select_interactive_option(
        self,
        *,
        title: str,
        text: str,
        values: list[tuple[str, str]],
        default: str | None = None,
    ) -> str | None:
        """Show an arrow-key radio selector when this is an interactive TTY."""
        if self.session is None or not values:
            return None
        choice_values = [(value, label) for value, label in values]
        selected_default = default if any(value == default for value, _ in choice_values) else choice_values[0][0]
        try:
            return await radiolist_dialog(
                title=title,
                text=text,
                values=choice_values,
                default=selected_default,
                style=INTERACTIVE_DIALOG_STYLE,
            ).run_async()
        except (KeyboardInterrupt, EOFError):
            return None

    async def _show_interactive_message(self, *, title: str, text: str) -> None:
        """Render read-only CLI information in a dismissible terminal dialog."""
        if self.session is None:
            return
        try:
            await message_dialog(
                title=title,
                text=text,
                style=INTERACTIVE_DIALOG_STYLE,
            ).run_async()
        except (KeyboardInterrupt, EOFError):
            return

    def _interactive_model_choices(self) -> list[tuple[str, str]]:
        choices = []
        for group_key, group in MODEL_REGISTRY.items():
            endpoint = "NVIDIA NIM" if group_key == "nvidia" else (
                "GitHub Copilot" if group_key == "copilot" else "OpenCode Zen"
            )
            for item in group["models"]:
                choices.append((item["id"], f"{item['id']}  —  {endpoint}"))
        return choices

    def _interactive_provider_choices(self) -> list[tuple[str, str]]:
        current = normalize_provider(self.config.provider)
        choices = []
        for provider, base_url in PROVIDER_BASE_URLS.items():
            suffix = "  (current)" if provider == current else ""
            detail = base_url or "GitHub Copilot SDK / OAuth"
            choices.append((provider, f"{provider}  —  {detail}{suffix}"))
        return choices

    async def _interactive_command(self, command_line: str) -> str | None:
        """Turn bare slash views into keyboard-driven terminal screens.

        Explicit command arguments keep their script-friendly behaviour.  The
        menus activate only for an interactive terminal and are cancelled with
        Escape or the dialog's Cancel button.
        """
        if self.session is None:
            return command_line
        parts = command_line.strip().split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command == "/menu":
            destination = await self._select_interactive_option(
                title="Ares Command Center",
                text="Use ↑/↓ then Enter. Escape cancels.",
                values=[
                    ("/provider", "Provider & endpoint"),
                    ("/model", "Model"),
                    ("/profile", "Profile"),
                    ("/soul", "Ares personality"),
                    ("/tools", "Tool activity detail"),
                    ("/resume", "Resume a saved conversation"),
                ],
            )
            return await self._interactive_command(destination) if destination else None

        if command == "/provider" and not argument:
            selected = await self._select_interactive_option(
                title="Choose provider",
                text="The matching endpoint and a compatible model will be applied.",
                values=self._interactive_provider_choices(),
                default=normalize_provider(self.config.provider),
            )
            return f"/provider {selected}" if selected else None

        if command == "/model" and not argument:
            selected = await self._select_interactive_option(
                title="Choose model",
                text="Select a model. Ares switches to its matching provider when needed.",
                values=self._interactive_model_choices(),
                default=self.config.model,
            )
            return f"/model {selected}" if selected else None

        if command == "/tools" and not argument:
            selected = await self._select_interactive_option(
                title="Tool activity",
                text="Choose how much live tool activity Ares shows.",
                values=[
                    ("summary", "Summary  —  compact progress and completion"),
                    ("details", "Details  —  render tool results where safe"),
                    ("hidden", "Hidden  —  only show final errors"),
                ],
                default=getattr(self, "tool_output_mode", "summary"),
            )
            return f"/tools {selected}" if selected else None

        if command in {"/profile", "/soul"} and not argument:
            label = "Profile" if command == "/profile" else "Ares personality"
            action = await self._select_interactive_option(
                title=label,
                text="Choose an action.",
                values=[("show", f"View {label.lower()}"), ("edit", "Open editor")],
                default="show",
            )
            if action == "show":
                manager = self.profile_manager if command == "/profile" else self.soul_manager
                content = manager.read().strip() or f"No {label.lower()} has been created yet."
                await self._show_interactive_message(title=label, text=content[:12_000])
                return None
            return f"{command} edit" if action else None

        if command == "/resume" and not argument:
            conversations = self._resume_conversations(50)
            if not conversations:
                await self._show_interactive_message(
                    title="Saved conversations", text="No saved conversations are available."
                )
                return None
            values = [
                (str(row["id"]), f"#{row['id']}  —  {one_line(row.get('summary') or 'Untitled', 70)}")
                for row in conversations
            ]
            selected = await self._select_interactive_option(
                title="Resume conversation",
                text="Select a saved conversation to restore.",
                values=values,
            )
            return f"/resume {selected}" if selected else None

        return command_line

    def _show_banner(self):
        """Display the welcome banner."""
        memory_count = len(self.memory_store.list_all())
        skill_count = 0
        with suppress(Exception):
            skill_count = len(self.skill_manager.list_all())
        mcp_summary = "not configured"
        manager = getattr(self, "mcp_manager", None)
        if manager is not None:
            with suppress(Exception):
                report = manager.readiness_report()
                mcp_summary = (
                    f"{report.get('connected', 0)}/{report.get('configured', 0)} connected"
                    f"{self._activity_separator()}{report.get('tools', 0)} tools"
                )
        overview = Table.grid(expand=True, padding=(0, 1))
        overview.add_column(ratio=1)
        overview.add_row(Text("Ready. Research, build, edit files, run commands, or control your desktop.", style="bright_white"))
        overview.add_row(Text.assemble(
            ("model  ", "dim"),
            (self.config.model, "bright_cyan"),
            ("    memory  ", "dim"),
            (f"{memory_count} facts", "bright_green"),
        ))
        overview.add_row(Text.assemble(
            ("MCP  ", "dim"),
            (mcp_summary, "bright_cyan"),
            ("    skills  ", "dim"),
            (str(skill_count), "bright_magenta"),
            ("    activity  ", "dim"),
            (getattr(self, "tool_output_mode", "summary"), "bright_green"),
        ))
        overview.add_row(Text.assemble(
            ("Try  ", "dim"),
            ("review this repo", "cyan"),
            ("  /  ", "dim"),
            ("research a topic", "cyan"),
            ("  /  ", "dim"),
            ("open Notepad and write a note", "cyan"),
        ))
        overview.add_row(Text.assemble(
            ("Commands  ", "dim"), ("/help", "cyan"),
            ("    Type / for suggestions; use ", "dim"), ("↑ ↓", "cyan"),
            (" to choose", "dim"),
        ))

        self.console.print()
        self.console.print(Panel(
            overview,
            title="[bold bright_cyan]Ares[/bold bright_cyan] [dim]v0.1.0[/dim]",
            border_style="bright_cyan",
            box=self._ui_box(),
            padding=(0, 1),
            width=self._panel_width(),
            safe_box=True,
        ))
        self.console.print()

    def _print_memories(self, memories: list[dict], title: str = "Memories") -> None:
        if not memories:
            self.console.print("[dim]No memories found.[/dim]")
            return
        table = Table(title=title, border_style="bright_green", box=CLI_BOX)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Importance", justify="right", no_wrap=True)
        table.add_column("Fact", ratio=4)
        table.add_column("Updated", style="dim", no_wrap=True)
        for memory in memories:
            table.add_row(
                str(memory["fact_id"]),
                memory.get("category", "note"),
                str(memory.get("importance", 0.5)),
                memory["fact_text"],
                memory.get("updated_at") or "-",
            )
        self.console.print(table)

    def _show_model_list(self, provider: str | None = None) -> None:
        table = Table(title="Models", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Model", style="cyan")
        table.add_column("Endpoint", style="dim")
        table.add_column("Status", no_wrap=True)
        rows = []
        for group_key, group in MODEL_REGISTRY.items():
            for m in group["models"]:
                backend = provider_for_model(m["id"])
                if provider and backend != normalize_provider(provider):
                    continue
                status = "[green]current[/green]" if m["id"] == self.config.model else "available"
                endpoint = "NVIDIA NIM" if backend == "nim" else "GitHub Copilot" if backend == "copilot" else "OpenCode Zen"
                rows.append((m["id"], endpoint, status))
        for model_id, endpoint, status in rows:
            table.add_row(model_id, endpoint, status)
        if not rows:
            self.console.print("[dim]No models available for this provider.[/dim]")
            return
        self.console.print(table)

    def _show_provider_list(self) -> None:
        table = Table(title="Providers", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Provider", style="cyan")
        table.add_column("Base URL", style="dim")
        table.add_column("Status", no_wrap=True)
        providers = [(name, url or "GitHub Copilot SDK (OAuth)") for name, url in PROVIDER_BASE_URLS.items()]
        current_provider = normalize_provider(getattr(self.config, "provider", "opencode"))
        for name, url in providers:
            status = "[green]current[/green]" if name == current_provider else "available"
            table.add_row(name, url, status)
        self.console.print(table)

    def _activate_provider(self, provider: str) -> str:
        """Apply a provider switch to config and the existing LLM client."""
        active = activate_provider_config(self.config, provider)
        llm = getattr(self.agent, "llm", None)
        if llm is not None:
            llm.provider = active
            llm.base_url = self.config.api_base_url.rstrip("/")
            llm.api_key = configured_provider_api_key(self.config, active)
            llm.config = self.config
        return active

    @staticmethod
    def _mcp_target(server: dict) -> str:
        """Return the safe connection target shown in MCP tables."""
        return redact_mcp_text(server.get("endpoint") or server.get("command") or "-")

    def _show_mcp_status(self, report: dict, server_name: str | None = None) -> None:
        """Render the manager's readiness report in a compact, stable table."""
        servers = report.get("servers") or {}
        if server_name:
            servers = {server_name: servers[server_name]} if server_name in servers else {}
        if not servers:
            message = (
                f"MCP server '{server_name}' is not configured."
                if server_name
                else "No MCP servers are configured."
            )
            self.console.print(f"[dim]{message}[/dim]")
            return

        title = (
            f"MCP Status  {report.get('connected', 0)}/{report.get('configured', 0)} connected"
            f"  {report.get('tools', 0)} tools"
        )
        table = Table(title=title, border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Server", style="cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Transport", no_wrap=True)
        table.add_column("Target", ratio=2)
        table.add_column("Tools", justify="right", no_wrap=True)
        table.add_column("Timeout", justify="right", no_wrap=True)
        table.add_column("Error", ratio=2)
        for name, server in servers.items():
            ready = bool(server.get("ready"))
            status = "[green]ready[/green]" if ready else "[yellow]disconnected[/yellow]"
            error = redact_mcp_text(server.get("error") or "")
            table.add_row(
                name,
                status,
                str(server.get("transport") or "-"),
                self._mcp_target(server),
                str(server.get("tools", 0)),
                f"{float(server.get('timeout_seconds', 0)):g}s",
                self._clip_tool_detail(error, 100) if error else "-",
            )
        self.console.print(table)

    def _show_mcp_tools(self, server_name: str | None = None) -> None:
        """Render discovered MCP tools grouped by server."""
        if self.mcp_manager is None:
            self.console.print("[dim]No MCP servers are configured.[/dim]")
            return
        groups = self.mcp_manager.tools_by_server(server_name)
        if server_name and not groups:
            self.console.print(f"[red]MCP server '{server_name}' is not configured.[/red]")
            return
        for name, tools in groups.items():
            if not tools:
                self.console.print(f"[dim]{name}: no tools discovered yet. Run /mcp reconnect {name}.[/dim]")
                continue
            table = Table(title=f"MCP Tools: {name}", border_style="bright_cyan", box=CLI_BOX)
            table.add_column("Tool", style="cyan", no_wrap=True)
            table.add_column("Description", ratio=4)
            for tool in tools:
                table.add_row(tool["name"], tool["description"] or "-")
            self.console.print(table)

    def _show_mcp_config(self) -> None:
        """Show configured servers without displaying arguments, env values, or tokens."""
        if self.mcp_manager is None or not self.mcp_manager.servers:
            self.console.print("[dim]No MCP servers are configured.[/dim]")
            return
        table = Table(title="MCP Configuration", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Server", style="cyan", no_wrap=True)
        table.add_column("Transport", no_wrap=True)
        table.add_column("Target", ratio=3)
        table.add_column("Timeout", justify="right", no_wrap=True)
        table.add_column("Private settings", ratio=2)
        for name, config in sorted(self.mcp_manager.servers.items()):
            private = []
            if config.env:
                private.append(f"{len(config.env)} env value(s) hidden")
            if config.oauth_client_id or config.oauth_client_secret or config.oauth_scopes:
                private.append("OAuth settings hidden")
            table.add_row(
                name,
                config.transport,
                redact_mcp_text(config.endpoint or config.command or "-"),
                f"{config.timeout_seconds:g}s",
                "; ".join(private) if private else "-",
            )
        self.console.print(table)

    def _show_mcp_help(self) -> None:
        table = Table(title="MCP Commands", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description", ratio=4)
        table.add_row("/mcp", "Show this MCP command guide")
        table.add_row("/mcp status", "Show configured server readiness, tool counts, timeouts, and errors")
        table.add_row("/mcp tools [SERVER]", "List discovered MCP tools, optionally for one server")
        table.add_row("/mcp reconnect SERVER", "Reconnect one MCP server and refresh its tools")
        table.add_row("/mcp health", "Probe connected servers and refresh readiness")
        table.add_row("/mcp reload", "Reload every MCP server from the current shared config")
        table.add_row("/mcp config", "Show safe server configuration without private values")
        table.add_row("/mcp search QUERY", "Discover servers from configured trusted registries")
        table.add_row("/mcp info NAME", "Inspect a registry server and its safe install plan")
        table.add_row("/mcp add NAME", "Review, confirm, and add a registry server")
        table.add_row("/mcp list", "Show configured server readiness")
        table.add_row("/mcp remove NAME", "Remove a server after confirmation")
        table.add_row("/mcp test [NAME]", "Health-check all servers or reconnect one")
        table.add_row("/mcp refresh", "Rebuild MCP connections from shared config")
        self.console.print(table)

    async def _handle_mcp_command(self, cmd: str) -> None:
        """Handle async MCP control commands from the interactive CLI loop."""
        if await self._handle_marketplace_command(cmd):
            return
        parts = cmd.strip().split()
        action = parts[1].lower() if len(parts) > 1 else "help"
        server_name = parts[2] if len(parts) > 2 else ""

        if action in {"help", "-h", "--help"}:
            self._show_mcp_help()
            return
        if action == "status" and len(parts) == 2:
            report = self.mcp_manager.readiness_report() if self.mcp_manager else {"servers": {}}
            self._show_mcp_status(report)
            return
        if action == "tools" and len(parts) <= 3:
            self._show_mcp_tools(server_name or None)
            return
        if action == "config" and len(parts) == 2:
            self._show_mcp_config()
            return
        if self.mcp_manager is None:
            self.console.print("[red]No MCP servers are configured.[/red]")
            return
        if action == "reconnect" and server_name and len(parts) == 3:
            report = await self.mcp_manager.reconnect_server(server_name)
            if hasattr(self.agent, "refresh_tools"):
                self.agent.refresh_tools()
            self._show_mcp_status({"servers": {server_name: report}, "connected": int(report.get("ready", False)), "configured": 1, "tools": report.get("tools", 0)})
            return
        if action == "health" and len(parts) == 2:
            report = await self.mcp_manager.health_probe()
            if hasattr(self.agent, "refresh_tools"):
                self.agent.refresh_tools()
            self._show_mcp_status(report)
            return
        if action == "reload" and len(parts) == 2:
            self._sync_shared_state()
            await self._refresh_mcp_manager_if_needed()
            if self.mcp_manager is None:
                self.console.print("[red]No MCP servers are configured.[/red]")
                return
            await self.mcp_manager.start()
            if hasattr(self.agent, "refresh_tools"):
                self.agent.refresh_tools()
            self._show_mcp_status(self.mcp_manager.readiness_report())
            return
        self.console.print("[red]Usage: /mcp [status|tools [SERVER]|reconnect SERVER|health|reload|config][/red]")

    def _tool_status(self, tool_name: str) -> tuple[str, str]:
        """Return status text and border style for a running tool."""
        statuses = {
            "web_search": ("Searching the web", "bright_green"),
            "read_file": ("Reading a file", "bright_blue"),
            "search_files": ("Searching files", "bright_yellow"),
            "list_directory": ("Scanning a directory", "bright_magenta"),
            "store_memory": ("Saving memory", "green"),
        }
        if tool_name.startswith("mcp__windows__"):
            action = tool_name.rsplit("__", 1)[-1].lower()
            return (f"Windows {action}", "bright_cyan")
        label = self._tool_label(tool_name)
        return statuses.get(tool_name, (f"Using {label}", "dim"))

    def _tool_label(self, tool_name: str) -> str:
        """Return a friendly tool name for compact CLI status text."""
        if tool_name in TOOL_LABELS:
            return TOOL_LABELS[tool_name]
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3 and parts[1] and parts[2]:
                server = parts[1].replace("_", " ").title()
                action = parts[2].replace("_", " ").title()
                return f"{server}: {action}"
        return tool_name.replace("_", " ") or "tool"

    @staticmethod
    def _pretty_activity_name(value: str) -> str:
        """Turn protocol identifiers into compact human-facing labels."""
        clean = value.replace("-", "_")
        for prefix in ("browser_", "page_", "desktop_"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
                break
        return " ".join(part for part in clean.split("_") if part).title() or "Tool"

    def _activity_label(self, tool_name: str) -> str:
        """Identify activity without exposing protocol noise or sounding mechanical."""
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3:
                action = self._pretty_activity_name(parts[2])
                server = self._pretty_activity_name(parts[1])
                return f"{server} {action}".strip()
        return self._pretty_activity_name(self._tool_label(tool_name))

    def _activity_separator(self) -> str:
        """Use plain ASCII separators in legacy Windows command prompts."""
        return " · " if getattr(self, "unicode_output", False) else " | "

    def _ui_box(self):
        """Avoid Unicode border redraw issues in classic Command Prompt."""
        return CLI_BOX if getattr(self, "unicode_output", False) else box.ASCII

    def _activity_marker(self, state: str) -> str:
        unicode_markers = {
            "working": "◌",
            "skill": "◆",
            "start": "→",
            "done": "✓",
            "failed": "×",
        }
        ascii_markers = {
            "working": "...",
            "skill": "*",
            "start": ">",
            "done": "OK",
            "failed": "X",
        }
        markers = unicode_markers if getattr(self, "unicode_output", False) else ascii_markers
        return markers.get(state, "-")

    def _activity_line(self, state: str, label: str, detail: str = "") -> Text:
        """Build one durable, bounded activity receipt."""
        marker_style = {
            "working": "bright_cyan",
            "skill": "bright_magenta",
            "start": "bright_cyan",
            "done": "bright_green",
            "failed": "bright_red",
        }.get(state, "dim")
        line = Text("  ")
        line.append(f"{self._activity_marker(state)} ", marker_style)
        line.append(label, "bold bright_white")
        if detail:
            line.append(f"  {self._activity_separator().strip()}  ", "dim")
            line.append(self._clip_tool_detail(detail, 110), "red" if state == "failed" else "dim")
        return line

    def _working_text(self, label: str) -> Text:
        return Text.assemble(
            (f"{self._activity_marker('working')} ", "bright_cyan"),
            (label, "bold bright_white"),
        )

    def _active_skills(self, user_input: str) -> list[tuple[object, str]]:
        """Return the same auto-selected skills the agent will use this turn."""
        if not getattr(self.config, "skills_enabled", True):
            return []
        if not getattr(self.config, "skill_auto_suggest", True):
            return []
        manager = getattr(self.agent, "skill_manager", None)
        relevant = getattr(manager, "relevant_skills", None)
        if not callable(relevant):
            return []
        try:
            reason_for = getattr(manager, "selection_reason", None)
            skills = list(relevant(user_input))
            return [
                (
                    skill,
                    reason_for(skill, user_input) if callable(reason_for) else "matches this task",
                )
                for skill in skills
            ]
        except Exception:
            return []

    def _activity_card_width(self) -> int:
        return min(self._panel_width(), 96)

    def _print_skill_card(self, selected_skills: list[tuple[object, str]]) -> None:
        """Show selected instruction sets separately from model/tool activity."""
        if not selected_skills:
            return
        rows = Table.grid(expand=True, padding=(0, 1))
        rows.add_column(no_wrap=True, style="bold bright_magenta")
        rows.add_column(ratio=1)
        for skill, reason in selected_skills:
            name = str(getattr(skill, "name", "unknown"))
            category = str(getattr(skill, "category", "workflow"))
            rows.add_row(
                f"{name}  [{category}]",
                f"Why: {self._clip_tool_detail(str(reason), 72)}",
            )
        self.console.print(Panel(
            rows,
            title="[bold bright_magenta]Skills selected[/bold bright_magenta] [dim]instruction sets for this task[/dim]",
            border_style="bright_magenta",
            box=self._ui_box(),
            padding=(0, 1),
            width=self._activity_card_width(),
            safe_box=True,
        ))

    def _parse_tool_token(self, token: str) -> tuple[str, str]:
        """Parse [tool:name:content] tokens with a fallback for legacy tokens."""
        inner = token[6:-1]
        parts = inner.split(":", 1)
        if len(parts) == 2 and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", parts[0]):
            return parts[0] or "unknown", parts[1]
        return "unknown", inner

    def _parse_tool_start_token(self, token: str) -> str:
        """Parse [tool_start:name] tokens."""
        inner = token.removeprefix("[tool_start:").removesuffix("]")
        return inner if re.match(r"^[A-Za-z][A-Za-z0-9_]*$", inner) else "unknown"

    def _parse_tool_progress_token(self, token: str) -> tuple[str, str]:
        """Parse internal progress events without letting them become prose."""
        inner = token.removeprefix("[tool_progress:").removesuffix("]")
        tool_name, separator, detail = inner.partition(":")
        if not separator or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", tool_name):
            return "unknown", "Working"
        return tool_name, self._clip_tool_detail(detail or "Working", 72)

    def _clip_tool_detail(self, text: str, limit: int = 90) -> str:
        """Keep tool summaries short enough to stay out of the user's way."""
        clean = re.sub(r"\s+", " ", text).strip()
        if len(clean) <= limit:
            return clean
        return clean[: limit - 3].rstrip() + "..."

    def _summarize_tool_result(self, tool_name: str, content: str) -> dict[str, str]:
        """Build a non-leaky summary of one tool result."""
        clean = content.strip()
        label = self._tool_label(tool_name)
        event = {
            "tool": tool_name,
            "label": label,
            "state": "done",
            "detail": "completed",
            "style": "dim",
        }

        if clean.lower().startswith("error:"):
            event.update({
                "state": "failed",
                "detail": self._clip_tool_detail(clean.removeprefix("Error:").strip() or clean),
                "style": "red",
            })
            return event

        if tool_name == "web_search":
            try:
                payload = json.loads(clean)
                if isinstance(payload, list):
                    result_count = len(payload)
                    query = ""
                else:
                    result_count = len(payload.get("results") or [])
                    query = str(payload.get("query") or "")
                detail = f"{result_count} result{'s' if result_count != 1 else ''}"
                if query:
                    detail += f" for {self._clip_tool_detail(query, 48)}"
                event["detail"] = detail
            except (TypeError, json.JSONDecodeError):
                event["detail"] = "search completed"
        elif tool_name == "read_file":
            match = re.search(r"\[File: (.+?) \((\d+) lines total\)\]", clean)
            if match:
                event["detail"] = self._clip_tool_detail(f"{match.group(1)} ({match.group(2)} lines)")
            else:
                event["detail"] = "file read"
        elif tool_name == "search_files":
            first_line = next((line.strip() for line in clean.splitlines() if line.strip()), "")
            event["detail"] = self._clip_tool_detail(first_line or "search completed")
        elif tool_name == "list_directory":
            first_line = next((line.strip() for line in clean.splitlines() if line.strip()), "")
            event["detail"] = self._clip_tool_detail(first_line.removeprefix("[Directory:").removesuffix("]").strip() or "directory scanned")
        elif tool_name in {"store_memory", "update_memory", "delete_memory", "search_memory"}:
            event["detail"] = "memory updated" if tool_name != "search_memory" else "memory checked"
        elif tool_name in {
            "write_file", "edit_file", "create_directory", "delete_file", "move_file",
            "batch_edit", "glob_apply", "insert_line", "replace_lines", "delete_lines",
            "append_to_file", "prepend_to_file", "create_file_from_template",
            "copy_file", "backup_file", "undo_last_edit", "batch_file_ops",
        }:
            event["detail"] = "filesystem updated"
        elif tool_name in {"run_code", "run_command", "terminal_exec"}:
            event["detail"] = "execution completed"
        elif tool_name.startswith("phone_"):
            event["detail"] = "phone action completed"
        elif tool_name.startswith("mcp__windows__"):
            action = tool_name.rsplit("__", 1)[-1].replace("_", " ").lower()
            lines = len(clean.splitlines()) if clean else 0
            if "snapshot" in action:
                event["detail"] = f"snapshot captured{self._activity_separator()}{lines:,} lines collapsed"
            elif "screenshot" in action:
                event["detail"] = "screenshot captured · visual payload collapsed"
            else:
                event["detail"] = f"Windows {action} completed"
        elif tool_name.startswith("mcp__"):
            action = tool_name.rsplit("__", 1)[-1].lower()
            lines = len(clean.splitlines()) if clean else 0
            if "snapshot" in action:
                event["detail"] = f"snapshot captured{self._activity_separator()}{lines:,} lines collapsed"
            elif "screenshot" in action:
                event["detail"] = "screenshot captured · visual payload collapsed"
            else:
                event["detail"] = f"{self._pretty_activity_name(action).lower()} completed"
        else:
            event["detail"] = "completed"

        return event

    def _render_tool_activity(self, events: list[dict[str, str]]):
        """Render a compact post-run activity trail."""
        mode = getattr(self, "tool_output_mode", "summary")
        if mode == "hidden" or not events:
            return None

        table = Table(title="Tools", border_style="bright_cyan", box=CLI_BOX, header_style="bold bright_cyan")
        table.add_column("Tool", style="bright_cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail", ratio=4)
        for event in events:
            state = event.get("state", "done")
            label = event.get("label", "tool")
            detail = event.get("detail", "")
            status = Text("failed", style="red") if state == "failed" else Text("done", style="green")
            table.add_row(label, status, detail)
        return table

    def _print_tool_start(self, tool_name: str, live_status=None, step: int | None = None) -> None:
        """Show a tool call as soon as the model asks for it."""
        if getattr(self, "tool_output_mode", "summary") == "hidden":
            return
        label = self._activity_label(tool_name)
        if step is not None:
            label = f"[{step:02d}] {label}"
        if live_status is not None:
            live_status.update(self._working_text(f"Checking {label}"))
        else:
            self.console.print(self._activity_line("start", f"Checking {label}", "on it"))

    def _print_tool_done(self, event: dict[str, str]) -> None:
        """Show a compact tool completion line."""
        if getattr(self, "tool_output_mode", "summary") == "hidden":
            return
        label = self._activity_label(event.get("tool", "unknown"))
        step = event.get("step")
        if isinstance(step, int):
            label = f"[{step:02d}] {label}"
        detail = event.get("detail", "completed")
        failed = event.get("state") == "failed"
        state = "Couldn’t finish" if failed else "Done"
        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(no_wrap=True)
        body.add_column(ratio=1)
        body.add_row(
            Text(state.upper(), style="bold red" if failed else "bold bright_green"),
            Text(self._clip_tool_detail(detail, 100), style="red" if failed else "default"),
        )
        self.console.print(Panel(
            body,
            title=f"[bold]{'Problem' if failed else 'Finished'}[/bold] [dim]{label}[/dim]",
            border_style="red" if failed else "bright_cyan",
            box=self._ui_box(),
            padding=(0, 1),
            width=self._activity_card_width(),
            safe_box=True,
        ))

    def _collapse_noisy_tool_output(self, tool_name: str, content: str) -> bool:
        """Keep snapshots and very large MCP payloads out of detailed mode."""
        if not tool_name.startswith("mcp__"):
            return False
        action = tool_name.rsplit("__", 1)[-1].lower()
        return "snapshot" in action or "screenshot" in action or len(content) > 5000

    def _print_static_activity_header(self, label: str) -> None:
        """Start a stable per-turn activity area for legacy terminals."""
        self.console.print(Panel(
            self._working_text(label),
            title="[bold bright_cyan]Ares is on it[/bold bright_cyan]",
            border_style="bright_cyan",
            box=self._ui_box(),
            padding=(0, 1),
            width=self._panel_width(),
            safe_box=True,
        ))

    def _clean_assistant_text(self, text: str) -> str:
        """Remove accidental tool protocol tokens from assistant-facing text."""
        clean = ANSI_RE.sub("", text)
        clean = re.sub(r"\[tool:[^\]]+\]", "", clean)
        clean = clean.replace("\r\n", "\n").replace("\r", "\n")
        clean = "".join(
            ch for ch in clean
            if unicodedata.category(ch) not in {"So", "Sk"}
            and ch not in {"\ufe0e", "\ufe0f", "\u200d"}
        )
        clean = re.sub(r"[ \t]+", " ", clean)
        clean = re.sub(r" *\n *", "\n", clean)
        return clean.strip()

    def _opening_paragraph(self, text: str) -> str:
        """Return the first meaningful paragraph from assistant text."""
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        return paragraphs[0] if paragraphs else ""

    def _normalize_opening(self, text: str) -> str:
        """Normalize an opening paragraph for repetition checks."""
        text = self._plain_response_line(text)
        text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        return re.sub(r"\s+", " ", text).strip()

    def _recent_assistant_openings(self, limit: int = 6) -> list[str]:
        """Return normalized openings from recent assistant messages."""
        openings: list[str] = []
        for msg in reversed(self.conversation_history[-limit * 2:]):
            if msg.get("role") != "assistant" or not msg.get("content"):
                continue
            opening = self._normalize_opening(self._opening_paragraph(str(msg["content"])))
            if len(opening) >= 24:
                openings.append(opening)
            if len(openings) >= limit:
                break
        return openings

    def _drop_repeated_opening(self, text: str) -> str:
        """Remove a recycled opening paragraph from the new answer."""
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
        if len(paragraphs) < 2:
            return text

        opening = self._normalize_opening(paragraphs[0])
        if len(opening) < 24:
            return text

        for previous in self._recent_assistant_openings():
            similarity = SequenceMatcher(None, opening, previous).ratio()
            if similarity >= 0.72:
                return "\n\n".join(paragraphs[1:]).strip()
        return text

    def _print_markdown_section(self, title: str, content: str, subtitle: str = "") -> None:
        """Render a readable section without terminal box drawing."""
        self.console.print()
        self.console.print(f"[bold cyan]{title}[/bold cyan]")
        if subtitle:
            self.console.print(f"[dim]{subtitle}[/dim]")
        self.console.print(Markdown(self._clean_assistant_text(content)))

    def _response_width(self) -> int:
        """Return a conservative width for readable assistant text."""
        fallback_width = getattr(self.console, "width", 100) or 100
        columns = shutil.get_terminal_size((fallback_width, 24)).columns
        return max(48, min(columns, fallback_width, 110) - 2)

    def _panel_width(self) -> int:
        """Return a panel width that fits inside the live terminal."""
        fallback_width = getattr(self.console, "width", 100) or 100
        columns = shutil.get_terminal_size((fallback_width, 24)).columns
        return max(52, min(columns, fallback_width, 112) - 1)

    def _plain_response_line(self, line: str) -> str:
        """Remove lightweight Markdown that makes plain terminal alignment noisy."""
        line = line.strip()
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*]\s+", "- ", line)
        line = re.sub(r"^\+\s+", "- ", line)
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
        line = re.sub(r"__(.*?)__", r"\1", line)
        line = re.sub(r"`([^`]+)`", r"\1", line)
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", line)
        return line.strip()

    def _assistant_table(self, lines: list[str]) -> Table | None:
        """Parse a GitHub-style Markdown table into a bounded Rich table."""
        rows = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("|") or not stripped.endswith("|"):
                return None
            rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
        if len(rows) < 2 or not all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in rows[1]):
            return None
        headers = rows[0]
        body = rows[2:]
        if not headers or any(len(row) != len(headers) for row in body):
            return None

        table = Table(box=self._ui_box(), border_style="bright_cyan", header_style="bold bright_cyan", expand=True)
        for header in headers:
            table.add_column(header or " ", overflow="fold", ratio=1)
        for row in body:
            table.add_row(*row)
        return table

    def _render_inline_text(self, line: str, style: str = "default") -> Text:
        """Render a small safe subset of inline Markdown with explicit resets."""
        text = Text(style=style)
        pattern = re.compile(r"(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))")
        pos = 0
        for match in pattern.finditer(line):
            if match.start() > pos:
                text.append(line[pos:match.start()], style)
            if match.group(2) is not None:
                text.append(match.group(2), "bold bright_white")
            elif match.group(3) is not None:
                text.append(match.group(3), "bold yellow")
            elif match.group(4) is not None:
                label = match.group(4)
                url = match.group(5)
                text.append(label, "underline bright_blue")
                text.append(f" ({url})", "dim")
            pos = match.end()
        if pos < len(line):
            text.append(line[pos:], style)
        return text

    def _assistant_renderables(self, text: str) -> list:
        """Render assistant Markdown with bounded tables and no style bleed."""
        renderables = []
        paragraph: list[str] = []
        table_lines: list[str] = []

        def flush_paragraph() -> None:
            if not paragraph:
                return
            content = " ".join(part.strip() for part in paragraph if part.strip())
            paragraph.clear()
            if content:
                renderables.append(self._render_inline_text(content))

        def flush_table() -> None:
            if not table_lines:
                return
            table = self._assistant_table(table_lines)
            copied = list(table_lines)
            table_lines.clear()
            renderables.append(table or Text("\n".join(copied), style="default"))

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()

            if stripped.startswith("|") and stripped.endswith("|"):
                flush_paragraph()
                table_lines.append(stripped)
                continue

            flush_table()
            if not stripped:
                flush_paragraph()
                renderables.append(Text(""))
                continue

            heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
            bullet = re.match(r"^[-*]\s+(.+)$", stripped)
            numbered = re.match(r"^(\d+[.)])\s+(.+)$", stripped)
            rule = re.fullmatch(r"[-*_]{3,}", stripped)

            if heading:
                flush_paragraph()
                renderables.append(self._render_inline_text(heading.group(2), "bold bright_cyan"))
            elif rule:
                flush_paragraph()
                renderables.append(Text("-" * min(self._response_width(), 72), style="dim"))
            elif bullet:
                flush_paragraph()
                item = Text("• " if getattr(self, "unicode_output", False) else "- ", style="bright_cyan")
                item.append_text(self._render_inline_text(bullet.group(1)))
                renderables.append(item)
            elif numbered:
                flush_paragraph()
                item = Text(f"{numbered.group(1)} ", style="bright_cyan")
                item.append_text(self._render_inline_text(numbered.group(2)))
                renderables.append(item)
            else:
                paragraph.append(stripped)

        flush_paragraph()
        flush_table()
        return renderables or [Text("")]

    def _print_assistant_response(self, text: str) -> None:
        """Render final assistant output with Rich Markdown, tables, and color."""
        self.console.print()
        clean = self._clean_assistant_text(text)
        renderables = self._assistant_renderables(clean)
        self.console.print(Panel(
            Group(*renderables),
            title="[bold bright_cyan]Ares[/bold bright_cyan]",
            border_style="bright_cyan",
            box=self._ui_box(),
            padding=(0, 1),
            width=self._panel_width(),
            safe_box=True,
        ))
        self.console.print(Text("", style="default"))

    def _cleanup_step(self, label: str, func) -> None:
        """Run one shutdown step without letting cleanup errors crash Ares."""
        try:
            func()
        except sqlite3.OperationalError as exc:
            self.console.print(f"[dim yellow]Shutdown warning ({label}): {exc}[/dim yellow]")
        except Exception as exc:
            self.console.print(f"[dim yellow]Shutdown warning ({label}): {exc}[/dim yellow]")

    def _finalize_session(self) -> None:
        """Persist a deterministic local summary and close the session once."""
        if getattr(self, "_session_finalized", False):
            return
        self._session_finalized = True
        try:
            summary = self.conversation_store.summarize_conversation(self.conversation_id) or ""
            if summary:
                self.session_store.write_summary(self.session_manager.get_id(), summary)
            self.conversation_store.end_conversation(self.conversation_id)
        except Exception as exc:
            self.console.print(f"[dim yellow]Shutdown warning (session memory): {exc}[/dim yellow]")

    def _edit_file(self, file_path: Path, name: str) -> None:
        """Open a context file in the user's editor, or print its path."""
        import os
        import subprocess

        if not file_path.exists():
            self.console.print(f"[yellow]Creating {name} file...[/yellow]")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            template = SOUL_TEMPLATE if name == "soul" else PROFILE_TEMPLATE
            file_path.write_text(template, encoding="utf-8")

        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
        if editor:
            self.console.print(f"[dim]Opening {file_path} in {editor}...[/dim]")
            try:
                subprocess.run([editor, str(file_path)], check=False)
            except FileNotFoundError:
                self.console.print(f"[red]Editor '{editor}' not found. Edit manually: {file_path}[/red]")
        elif sys.platform == "win32":
            self.console.print(f"[dim]Opening {file_path}...[/dim]")
            try:
                os.startfile(str(file_path))
            except OSError:
                self.console.print(f"[yellow]Could not open editor. Edit manually: {file_path}[/yellow]")
        else:
            self.console.print(f"[yellow]No $EDITOR set. Edit manually: {file_path}[/yellow]")

    @staticmethod
    def _agent_status_text(status: object) -> Text:
        value = str(status or "unknown")
        style = {
            "running": "bold bright_cyan", "queued": "cyan", "succeeded": "green",
            "failed": "bold red", "timed_out": "yellow", "blocked": "yellow",
            "cancelled": "dim",
        }.get(value, "dim")
        return Text(value.replace("_", " "), style=style)

    def _agent_runs_table(self, runs: list[dict], title: str = "Native Agent Runs") -> Table:
        table = Table(title=title, border_style="bright_cyan", box=self._ui_box())
        table.add_column("Run", style="cyan", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Time", justify="right", no_wrap=True)
        table.add_column("Workers", justify="right", no_wrap=True)
        table.add_column("Active", justify="right", no_wrap=True)
        table.add_column("Task", ratio=4)
        for run in runs:
            children = run.get("children") or []
            active = sum(str(item.get("status") or "") in ACTIVE_STATUSES for item in children)
            table.add_row(
                str(run.get("run_id") or ""), self._agent_status_text(run.get("status")),
                elapsed_label(run), str(len(children)), str(active),
                Text(one_line(run.get("prompt_summary") or run.get("activity"), 120)),
            )
        return table

    def _agent_workers_table(self, runs: list[dict], title: str = "Specialist Workers") -> Table:
        table = Table(title=title, border_style="bright_magenta", box=self._ui_box())
        table.add_column("Team", style="cyan", no_wrap=True)
        table.add_column("Worker", style="bright_magenta", no_wrap=True)
        table.add_column("Run ID", style="dim", no_wrap=True)
        table.add_column("Task", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Current activity", ratio=4)
        for run in runs:
            metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
            waves = run.get("execution_waves") or metadata.get("execution_waves") or []
            wave_by_task = {
                str(task_id): wave_index
                for wave_index, wave in enumerate(waves, 1)
                for task_id in (wave if isinstance(wave, (list, tuple)) else [])
            }
            for child in run.get("children") or []:
                activity = (
                    child.get("activity") or child.get("current_tool") or
                    child.get("result_summary") or child.get("error_summary") or "Waiting"
                )
                task_id = str(child.get("task_id") or "task")
                wave = wave_by_task.get(task_id)
                table.add_row(
                    str(run.get("run_id") or "")[:19], str(child.get("agent_role") or "specialist"),
                    str(child.get("run_id") or "")[:19],
                    f"W{wave} · {task_id}" if wave else task_id,
                    self._agent_status_text(child.get("status")),
                    Text(one_line(activity, 140)),
                )
        return table

    def _print_agent_overview(self, runtime, *, active_only: bool = False) -> None:
        runs = runtime.list_runs(limit=100, session_id=self._agent_session_id())
        selected = [run for run in runs if str(run.get("status") or "") in ACTIVE_STATUSES] if active_only else runs
        summary = summarize_runs(runs)
        self.console.print(Panel(
            Text.assemble(
                ("Enabled", "bold green"),
                f"{self._activity_separator()}{summary['active_runs']} active teams",
                f"{self._activity_separator()}{summary['active_workers']} active workers",
                f"{self._activity_separator()}{len(runtime.list_agents())} configured roles",
            ),
            title="[bold]Ares Supervisor[/bold]", border_style="bright_cyan", box=self._ui_box(),
        ))
        if selected:
            self.console.print(self._agent_runs_table(selected, "Active Agent Teams" if active_only else "Recent Agent Teams"))
            workers = [run for run in selected if run.get("children")]
            if workers:
                self.console.print(self._agent_workers_table(workers, "All Active Workers" if active_only else "Specialist Workers"))
        else:
            self.console.print("[dim]No specialist teams are currently running.[/dim]" if active_only else "[dim]No multi-agent runs yet.[/dim]")

    def _print_agent_run(self, run: dict) -> None:
        children = list(run.get("children") or [])
        metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
        manifest = run.get("manifest") if isinstance(run.get("manifest"), dict) else {}
        waves = (
            run.get("execution_waves") or manifest.get("execution_waves") or
            metadata.get("execution_waves") or []
        )
        manifest_children = {
            str(item.get("run_id") or item.get("task_id") or ""): item
            for item in (manifest.get("child_runs") or [])
            if isinstance(item, dict)
        }
        failures = [
            child for child in children
            if str(child.get("status") or "") in {"failed", "timed_out", "blocked", "cancelled"}
        ]
        self.console.print(Panel(
            Text.assemble(
                (one_line(run.get("prompt_summary") or "Delegated task", 500), "bold white"),
                "\n\n", ("Status: ", "dim"), self._agent_status_text(run.get("status")),
                (f"  {self._activity_separator().strip()}  Elapsed: {elapsed_label(run)}", "dim"),
                (f"\nSession: {run.get('session_id') or '—'}", "dim"),
                (f"\nRequest: {run.get('request_id') or manifest.get('request_id') or '—'}", "dim"),
                (f"\nAgents: {len(children)}  ·  Waves: {len(waves)}  ·  Issues: {len(failures)}", "dim"),
                (f"\nActivity: {one_line(run.get('activity') or '—', 300)}", "dim"),
            ),
            title=f"[bold bright_cyan]{run.get('run_id', 'Agent run')}[/bold bright_cyan]",
            border_style="bright_cyan", box=self._ui_box(),
        ))
        if waves:
            for index, wave in enumerate(waves, 1):
                self.console.print(self._activity_line(
                    "working" if str(run.get("status")) in ACTIVE_STATUSES else "done",
                    f"Wave {index}", ", ".join(str(item) for item in wave),
                ))
        if children:
            self.console.print(self._agent_workers_table([run], "Execution Tree"))
        for child in children:
            content = child.get("result_summary") or child.get("error_summary")
            manifest_child = manifest_children.get(str(child.get("run_id") or "")) or manifest_children.get(str(child.get("task_id") or "")) or {}
            tools = child.get("tools") or manifest_child.get("tools") or (
                child.get("metadata", {}).get("tools") if isinstance(child.get("metadata"), dict) else []
            )
            if tools:
                self.console.print(self._activity_line(
                    "done", f"{child.get('agent_role') or 'specialist'} tools",
                    ", ".join(str(tool) for tool in tools),
                ))
            if content:
                self.console.print(self._activity_line(
                    "failed" if str(child.get("status")) in {"failed", "timed_out", "blocked"} else "done",
                    str(child.get("agent_role") or "specialist"), one_line(content, 180),
                ))
        artifacts = [
            (child, artifact)
            for child in children
            for artifact in (child.get("artifacts") or [])
            if isinstance(artifact, dict)
        ]
        if artifacts:
            table = Table(title="Agent Artifacts", border_style="bright_blue", box=self._ui_box())
            table.add_column("Role", style="cyan", no_wrap=True)
            table.add_column("Path", ratio=4)
            table.add_column("Type", no_wrap=True)
            table.add_column("Description", ratio=2)
            for child, artifact in artifacts:
                table.add_row(
                    str(child.get("agent_role") or "specialist"),
                    str(artifact.get("path") or ""),
                    str(artifact.get("media_type") or "file"),
                    one_line(artifact.get("description"), 100),
                )
            self.console.print(table)

    def _agent_session_id(self) -> str:
        return str(getattr(self.agent, "session_id", None) or self.session_manager.get_id())

    @staticmethod
    def _agent_payload(value: object) -> dict:
        if isinstance(value, dict):
            return value
        serializer = getattr(value, "as_dict", None)
        if callable(serializer):
            payload = serializer()
            return payload if isinstance(payload, dict) else {"result": payload}
        try:
            return dict(vars(value))
        except (TypeError, AttributeError):
            return {"result": str(value)}

    def _print_agent_payload(self, title: str, value: object) -> None:
        payload = self._agent_payload(value)
        if payload.get("run_id") or payload.get("root_run_id"):
            self._print_agent_run(payload)
            return
        self.console.print(Panel(
            Text(json.dumps(payload, ensure_ascii=False, indent=2, default=str)),
            title=title, border_style="bright_cyan", box=self._ui_box(),
        ))

    async def _run_agent_operation(self, runtime, action: str, value: str = "") -> None:
        session_id = self._agent_session_id()
        try:
            if action == "run":
                result = runtime.delegate_request(value, session_id=session_id)
                title = "Native Delegation"
            elif action == "doctor":
                result = runtime.doctor()
                title = "Multi-Agent Doctor"
            else:
                result = runtime.smoke_test(session_id=session_id)
                title = "Multi-Agent Smoke Test"
            if inspect.isawaitable(result):
                result = await result
            payload = self._agent_payload(result)
            run_id = str(payload.get("run_id") or payload.get("root_run_id") or "")
            if run_id:
                persisted = runtime.get_run(run_id, session_id=session_id)
                if inspect.isawaitable(persisted):
                    persisted = await persisted
                if persisted:
                    result = persisted
            self._print_agent_payload(title, result)
        except Exception as exc:
            self.console.print(f"[red]{action.replace('_', ' ').title()} failed: {exc}[/red]")

    def _print_agent_event(self, event: dict, seen: set[tuple[str, str, str]]) -> None:
        event_type = str(event.get("event_type") or "")
        task_id = str(event.get("task_id") or "")
        tool = str(event.get("tool") or "")
        key = (event_type, task_id, tool)
        if key in seen and event_type not in {"agent_progress"}:
            return
        seen.add(key)
        role = str(event.get("agent") or "team")
        detail = one_line(event.get("detail"), 110)
        if event_type == "orchestration_started":
            run_id = str(event.get("root_run_id") or event.get("run_id") or "")
            self.console.print(self._activity_line("start", f"Specialist team {run_id}", detail or "delegation started"))
        elif event_type == "agent_started":
            self.console.print(self._activity_line("start", f"Agent | {role}", detail or task_id))
        elif event_type == "tool_started":
            self.console.print(self._activity_line("working", f"{role} | {tool.replace('_', ' ')}", detail))
        elif event_type in {"agent_completed", "agent_failed", "agent_timed_out", "agent_blocked", "agent_cancelled"}:
            failed = event_type in {"agent_failed", "agent_timed_out", "agent_blocked"}
            self.console.print(self._activity_line("failed" if failed else "done", f"Agent | {role}", detail or event_type.removeprefix("agent_").replace("_", " ")))
        elif event_type == "synthesis_started":
            self.console.print(self._activity_line("working", "Root synthesis", detail))
        elif event_type in {"orchestration_completed", "orchestration_cancelled"}:
            failed = str(event.get("status") or "") not in {"succeeded", "running"}
            self.console.print(self._activity_line("failed" if failed else "done", "Specialist team", str(event.get("status") or "complete")))

    def _handle_command(self, cmd: str) -> bool:
        """Handle slash commands. Returns False if should exit."""
        parts = cmd.strip().split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if command == "/help":
            table = Table(title="Commands", border_style="bright_cyan", box=CLI_BOX)
            table.add_column("Command", style="cyan", no_wrap=True)
            table.add_column("Description", ratio=4)
            table.add_row("/help", "Show available commands")
            table.add_row("/menu", "Open the keyboard-driven command center")
            table.add_row(
                "/memory [search|edit|archive|restore|learning|explain|clean]",
                "Review automatic memory, procedural learning, and retrieval diagnostics",
            )
            table.add_row("/latency", "Show the latest message timing, model, and schema count")
            table.add_row("/goals [search|show|due|signals]", "Track goals, evidence, proactive watcher signals, and hierarchy")
            table.add_row("/forget ID", "Delete a memory by ID")
            table.add_row("/model [MODEL]", f"Show or switch model: {self.config.model}")
            table.add_row("/provider [PROVIDER]", f"Show or switch provider: {getattr(self.config, 'provider', 'opencode')}")
            table.add_row("/copilot [login|token|status]", "Connect GitHub Copilot with an OAuth token")
            table.add_row("/clear", "Clear terminal screen")
            table.add_row("/export", "Export data to JSON")
            table.add_row("/import PATH [--config]", "Import data from an Ares JSON export")
            table.add_row("/reset", "Reset conversation context")
            table.add_row("/resume [ID|latest]", "List or restore a saved conversation")
            table.add_row("/soul [show|edit]", "View or edit Ares' personality")
            table.add_row("/profile [show|edit]", "View or edit your profile")
            table.add_row("/context", "Show active context for this session")
            table.add_row("/skills [search|install|create|update|publish]", "Manage local and marketplace skills")
            table.add_row("/setup", "Run the onboarding wizard again")
            table.add_row("/browser [status|isolated|system|extension|auto|launch]", "Manage Playwright browser connection mode")
            table.add_row("/phone status", "Check Android phone bridge pairing health")
            table.add_row("/call CONTACT|NUMBER [--confirm]", "Place a provider-backed telephone call")
            table.add_row("/telephony [status|contacts|recent|hangup|mute|unmute]", "Manage Twilio/LiveKit telephony")
            table.add_row("/tools [summary|details|hidden]", "Control tool activity display")
            table.add_row("/agents run REQUEST", "Force a real native specialist delegation")
            table.add_row("/agents [doctor|smoke-test]", "Verify runtime, persistence, roles, limits, and harmless parallel execution")
            table.add_row("/agents [status|active|roles|runs|show|cancel|resume|on|off]", "Inspect and control this session's specialist teams")
            table.add_row("/mcp [search|add|list|test|refresh]", "Discover, configure, and manage MCP servers")
            table.add_row("/monitor [list|add|status|pause|resume|remove|events|test]", "Control proactive watchers")
            table.add_row("/skill-name", "Load a skill directly by slash command")
            table.add_row("/exit", "Exit Ares")
            self.console.print(table)

        elif command == "/memory":
            if not arg:
                self._print_memories(self.memory_store.get_recent(limit=10), "Recent Memories")
            elif arg.startswith("search "):
                self._print_memories(self.memory_store.search(arg[7:].strip(), limit=10), "Memory Search")
            elif arg == "clean":
                stats = MemoryCleaner(self.memory_store).cleanup()
                table = Table(title="Memory Cleanup", border_style="bright_green", box=CLI_BOX)
                table.add_column("Metric", style="cyan")
                table.add_column("Count", justify="right")
                table.add_row("Duplicates merged", str(stats.get("duplicates_merged", 0)))
                table.add_row("Stale archived", str(stats.get("stale_archived", 0)))
                table.add_row("Remaining", str(stats.get("total_after", 0)))
                self.console.print("[green]Memory cleaned.[/green]")
                self.console.print(table)
            elif arg == "explain":
                diagnostics = self.agent.explain_last_memory_retrieval()
                self.console.print_json(
                    json.dumps(diagnostics or {"status": "no retrieval recorded"})
                )
            elif arg in {"learning", "learning pending", "learning active"}:
                service = getattr(self.agent, "reflection_service", None)
                requested_status = "pending_approval" if arg == "learning pending" else "active"
                learnings = (
                    service.self_improvement_store.list(status=requested_status, limit=20)
                    if service is not None else []
                )
                title = (
                    "Pending Hermes Learning Review"
                    if requested_status == "pending_approval"
                    else "Approved Procedural Learning"
                )
                table = Table(title=title, box=CLI_BOX)
                table.add_column("ID", justify="right", style="cyan")
                table.add_column("Kind")
                table.add_column("Learning")
                table.add_column("Uses", justify="right")
                for learning in learnings:
                    table.add_row(
                        str(learning["improvement_id"]),
                        str(learning["kind"]),
                        f"{learning['title']}: {learning['summary']}",
                        str(learning["occurrence_count"]),
                    )
                self.console.print(table)
                if requested_status == "pending_approval" and learnings:
                    self.console.print(
                        "[dim]Review with /memory learning approve ID or reject ID.[/dim]"
                    )
            elif arg.startswith("learning approve ") or arg.startswith("learning reject "):
                service = getattr(self.agent, "reflection_service", None)
                if service is None:
                    self.console.print("[red]Procedural learning is unavailable.[/red]")
                else:
                    action, raw_id = arg.removeprefix("learning ").split(maxsplit=1)
                    improvement_id = int(raw_id)
                    store = service.self_improvement_store
                    before = store.get(improvement_id)
                    if before is None:
                        self.console.print(f"[red]Learning #{improvement_id} was not found.[/red]")
                    elif before.get("status") != "pending_approval":
                        self.console.print(
                            f"[yellow]Learning #{improvement_id} is {before.get('status')}.[/yellow]"
                        )
                    else:
                        updated = (
                            store.approve(improvement_id)
                            if action == "approve"
                            else store.reject(improvement_id)
                        )
                        verb = "Approved" if action == "approve" else "Rejected"
                        color = "green" if action == "approve" else "yellow"
                        self.console.print(
                            f"[{color}]{verb} learning #{updated['improvement_id']}.[/{color}]"
                        )
            elif arg.startswith("edit "):
                edit_parts = arg.split(maxsplit=2)
                if len(edit_parts) < 3:
                    self.console.print("[red]Usage: /memory edit ID NEW_TEXT[/red]")
                else:
                    fact_id = int(edit_parts[1])
                    if self.memory_store.update(fact_id, fact_text=edit_parts[2]):
                        self.console.print(f"[green]Updated memory #{fact_id}.[/green]")
                    else:
                        self.console.print(f"[red]Memory #{fact_id} was not found.[/red]")
            elif arg.startswith("archive "):
                fact_id = int(arg.split(maxsplit=1)[1])
                if self.memory_store.archive(fact_id, reason="cli"):
                    self.console.print(f"[yellow]Archived memory #{fact_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Memory #{fact_id} was not found or was already archived.[/red]")
            elif arg.startswith("restore "):
                fact_id = int(arg.split(maxsplit=1)[1])
                if self.memory_store.restore(fact_id):
                    self.console.print(f"[green]Restored memory #{fact_id}.[/green]")
                else:
                    self.console.print(f"[red]Memory #{fact_id} was not found or is not archived.[/red]")
            elif arg.startswith("delete "):
                fact_id = int(arg.split(maxsplit=1)[1])
                if self.memory_store.delete(fact_id):
                    self.console.print(f"[yellow]Forgot memory #{fact_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Memory #{fact_id} was not found.[/red]")
            else:
                self.console.print(
                    "[red]Usage: /memory [search QUERY|edit ID NEW_TEXT|archive ID|restore ID|"
                    "delete ID|learning [pending|active|approve ID|reject ID]|explain|clean][/red]"
                )

        elif command == "/latency":
            records = list(getattr(self.agent, "recent_latency_metrics", ()) or ())
            if not records:
                self.console.print("[yellow]No completed message timing is available yet.[/yellow]")
            else:
                latest = records[-1]
                table = Table(title="Latest Message Latency", box=CLI_BOX)
                table.add_column("Metric", style="cyan")
                table.add_column("Value", justify="right")
                table.add_row("Model", str(latest.get("model") or "unknown"))
                table.add_row("Tool schemas", str(latest.get("tool_schema_count", 0)))
                for name, value in (latest.get("metrics") or {}).items():
                    table.add_row(str(name), f"{float(value):.1f} ms")
                self.console.print(table)

        elif command == "/forget":
            if not arg:
                self.console.print("[red]Usage: /forget MEMORY_ID[/red]")
            else:
                fact_id = int(arg)
                if self.memory_store.delete(fact_id):
                    self.console.print(f"[yellow]Forgot memory #{fact_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Memory #{fact_id} was not found.[/red]")

        elif command == "/goals":
            goal_store = getattr(self.agent, "goal_store", None)
            if goal_store is None:
                self.console.print("[red]Goal store is unavailable.[/red]")
            else:
                def print_goal_table(goals, title):
                    table = Table(title=title, border_style="bright_red", box=CLI_BOX)
                    table.add_column("ID", style="bright_red", justify="right", no_wrap=True)
                    table.add_column("Goal", ratio=4)
                    table.add_column("Status", no_wrap=True)
                    table.add_column("Priority", no_wrap=True)
                    table.add_column("Progress", justify="right", no_wrap=True)
                    table.add_column("Target", no_wrap=True)
                    for goal in goals:
                        status = str(goal.get("status", "active"))
                        status_style = "green" if status == "completed" else "yellow" if status == "paused" else "red" if goal.get("is_overdue") else "cyan"
                        table.add_row(
                            str(goal.get("goal_id", "")), str(goal.get("title", "")),
                            f"[{status_style}]{status}[/{status_style}]", str(goal.get("priority", "normal")),
                            f"{int(goal.get('progress_percent', 0))}%", str(goal.get("target_date") or "—"),
                        )
                    self.console.print(table if goals else "[dim]No matching goals.[/dim]")

                def print_signal_table(signals, title="Pending Goal Signals"):
                    table = Table(title=title, border_style="bright_red", box=CLI_BOX)
                    table.add_column("Signal", style="bright_red", justify="right", no_wrap=True)
                    table.add_column("Goal", justify="right", no_wrap=True)
                    table.add_column("Severity", no_wrap=True)
                    table.add_column("Watcher", no_wrap=True)
                    table.add_column("Detected change", ratio=4)
                    table.add_column("Surfaces", justify="right")
                    for signal in signals:
                        table.add_row(
                            str(signal.get("signal_id", "")), str(signal.get("goal_id", "")),
                            str(signal.get("severity", "info")),
                            str((signal.get("metadata") or {}).get("watcher_name") or signal.get("watcher_id", "")),
                            str(signal.get("event_summary", "")), str(signal.get("surfaced_count", 0)),
                        )
                    self.console.print(table if signals else "[dim]No matching goal signals.[/dim]")

                if not arg:
                    print_goal_table(goal_store.list_all(statuses=["active"], limit=50), "Active Goals")
                elif arg == "due":
                    due = goal_store.due_soon(within_days=7)
                    overdue = goal_store.overdue()
                    print_goal_table(overdue, "Overdue Goals")
                    print_goal_table(due, "Goals Due Within 7 Days")
                elif arg.startswith("search "):
                    query = arg.split(maxsplit=1)[1]
                    print_goal_table(goal_store.search(query, limit=50), f"Goal Search: {query}")
                elif arg == "signals" or arg.startswith("signals "):
                    try:
                        signal_goal_id = int(arg.split(maxsplit=1)[1]) if " " in arg else None
                        signals = goal_store.list_watcher_signals(signal_goal_id, limit=100)
                    except ValueError:
                        signals = []
                    print_signal_table(signals)
                elif arg.startswith("show "):
                    try:
                        goal_id = int(arg.split(maxsplit=1)[1])
                        goal = goal_store.get(goal_id)
                    except ValueError:
                        goal = None
                    if goal is None:
                        self.console.print("[red]Goal not found. Usage: /goals show ID[/red]")
                    else:
                        tree = goal_store.tree(goal_id)
                        links = goal_store.linked_refs(goal_id)
                        signals = goal_store.list_watcher_signals(goal_id, limit=25)
                        timeline = goal_store.list_events(goal_id, limit=10)
                        self.console.print(Panel(
                            f"[bold]{goal['title']}[/bold]\n{goal['description'] or '[dim]No description[/dim]'}\n\n"
                            f"Status: {goal['status']} · Priority: {goal['priority']} · Progress: {goal['progress_percent']}% ({goal['progress_mode']})\n"
                            f"Target: {goal['target_date'] or 'none'} · Revision: {goal['revision']}\n"
                            f"Children: {len(tree.get('children', []))} · Tasks: {len(links['tasks'])} · Actions: {len(links['actions'])} · Watchers: {len(links['watchers'])} · Pending signals: {len(signals)}",
                            title=f"Goal #{goal_id}", border_style="bright_red",
                        ))
                        if signals:
                            print_signal_table(signals, f"Goal #{goal_id} · Pending Watcher Signals")
                        if timeline:
                            event_table = Table(title="Recent Goal Timeline", border_style="dim", box=CLI_BOX)
                            event_table.add_column("When", style="dim")
                            event_table.add_column("Event")
                            event_table.add_column("Progress", justify="right")
                            event_table.add_column("Note", ratio=3)
                            for event in timeline:
                                progress = "—" if event.get("progress_percent") is None else f"{event['progress_percent']}%"
                                event_table.add_row(str(event.get("created_at", "")), str(event.get("event_type", "")), progress, str(event.get("note", "")))
                            self.console.print(event_table)
                else:
                    self.console.print("[red]Usage: /goals [search QUERY|show ID|due|signals [GOAL_ID]][/red]")

        elif command == "/agents":
            runtime = getattr(self.agent, "multi_agent_runtime", None)
            action, _, value = arg.partition(" ")
            action = action.casefold() or "list"
            if action in {"on", "off"}:
                enabled = action == "on"
                self.config.multi_agent.enabled = enabled
                save_config(self.config)
                if enabled and runtime is None:
                    from ares.multi_agent_runtime import MultiAgentRuntime
                    runtime = MultiAgentRuntime(self.agent)
                    self.agent.multi_agent_runtime = runtime
                self.agent.refresh_tools()
                self.console.print(f"[green]Native multi-agent mode {'enabled' if enabled else 'disabled'}.[/green]")
            elif runtime is None:
                self.console.print("[yellow]Native multi-agent mode is unavailable. Use /agents on.[/yellow]")
            elif action == "run":
                if not value.strip():
                    self.console.print("[red]Usage: /agents run REQUEST[/red]")
                else:
                    asyncio.create_task(
                        self._run_agent_operation(runtime, "run", value.strip()),
                        name="ares-cli-agents-run",
                    )
            elif action in {"doctor", "smoke-test", "smoke_test"}:
                normalized = "doctor" if action == "doctor" else "smoke-test"
                asyncio.create_task(
                    self._run_agent_operation(runtime, normalized),
                    name=f"ares-cli-agents-{normalized}",
                )
            elif action in {"list", "roles"}:
                table = Table(title="Ares Specialists", border_style="bright_cyan", box=CLI_BOX)
                table.add_column("Role", style="cyan")
                table.add_column("Mode")
                table.add_column("Budget")
                table.add_column("Purpose", ratio=4)
                for item in runtime.list_agents():
                    table.add_row(item["name"], "mutation" if item["can_mutate"] else "read-only", f"{item['max_iterations']} iter · {item['timeout_seconds']:.0f}s", item["description"])
                self.console.print(table)
            elif action == "status":
                self._print_agent_overview(runtime)
            elif action == "active":
                self._print_agent_overview(runtime, active_only=True)
            elif action == "runs":
                try:
                    limit = max(1, min(int(value or 20), 100))
                except ValueError:
                    limit = 20
                runs = runtime.list_runs(limit=limit, session_id=self._agent_session_id())
                self.console.print(self._agent_runs_table(runs) if runs else "[dim]No multi-agent runs yet.[/dim]")
            elif action == "show" and value:
                run = runtime.get_run(value.strip(), session_id=self._agent_session_id())
                if run is None:
                    self.console.print("[red]Agent run not found.[/red]")
                else:
                    self._print_agent_run(run)
            elif action == "cancel" and value:
                target = value.strip()
                async def cancel_run() -> None:
                    cancelled = await runtime.cancel(target, session_id=self._agent_session_id())
                    self.console.print(f"[yellow]{'Cancelled' if cancelled else 'Not active'}: {target}[/yellow]")
                asyncio.create_task(cancel_run(), name=f"ares-cli-cancel:{target}")
            elif action == "resume" and value:
                target = value.strip()
                async def resume_run() -> None:
                    try:
                        team = await runtime.resume(
                            target, session_id=self._agent_session_id()
                        )
                    except Exception as exc:
                        self.console.print(
                            f"[red]Could not resume {target}: {type(exc).__name__}: {exc}[/red]"
                        )
                    else:
                        self.console.print(
                            f"[green]Resumed {target} as {team.root_run_id}.[/green]"
                        )
                asyncio.create_task(resume_run(), name=f"ares-cli-resume:{target}")
            else:
                self.console.print("[red]Usage: /agents [run REQUEST|doctor|smoke-test|status|active|roles|runs [LIMIT]|show RUN_ID|cancel RUN_ID|resume RUN_ID|on|off][/red]")

        elif command == "/model":
            if not arg or arg == "list":
                self._show_model_list()
            else:
                selected_provider = provider_for_model(arg)
                switched_provider = False
                if selected_provider and selected_provider != normalize_provider(self.config.provider):
                    self._activate_provider(selected_provider)
                    switched_provider = True
                self.config.model = arg
                save_config(self.config)
                self.agent.set_model(arg)
                provider_note = (
                    f" Provider switched to {selected_provider}."
                    if switched_provider
                    else ""
                )
                self.console.print(f"[green]Model switched to {arg}.{provider_note}[/green]")

        elif command == "/provider":
            if not arg or arg == "list":
                self._show_provider_list()
            else:
                provider = normalize_provider(arg)
                if provider not in SUPPORTED_PROVIDERS:
                    valid = ", ".join((*SUPPORTED_PROVIDERS, "nvidia (alias for nim)"))
                    self.console.print(f"[red]Unknown provider: {arg}. Valid: {valid}[/red]")
                    return
                self._activate_provider(provider)
                replacement_model = None
                if provider_for_model(self.config.model) != provider:
                    replacement_model = default_model_for_provider(provider)
                    self.config.model = replacement_model
                    self.agent.set_model(replacement_model)
                save_config(self.config)
                model_note = f" Model set to {replacement_model}." if replacement_model else ""
                self.console.print(f"[green]Provider switched to {provider}.{model_note}[/green]")
                self._show_model_list(provider)

        elif command == "/copilot":
            pieces = arg.split(maxsplit=1)
            action = pieces[0].lower() if pieces else "status"
            value = pieces[1].strip() if len(pieces) > 1 else ""
            if action == "status":
                state = "[green]connected[/green]" if self.config.copilot_github_token else "[yellow]not connected[/yellow]"
                self.console.print(
                    f"GitHub Copilot: {state}. Provider: {self.config.provider}. Model: {self.config.model}."
                )
            elif action == "token" and value:
                self.config.copilot_github_token = value
                self._activate_provider("copilot")
                self.config.model = "auto"
                self.agent.set_model("auto")
                save_config(self.config)
                self.console.print("[green]Copilot token saved. Ares will use automatic model selection.[/green]")
            elif action == "login":
                client_id = value or self.config.copilot_oauth_client_id
                if not client_id:
                    self.console.print("[red]Usage: /copilot login GITHUB_OAUTH_CLIENT_ID[/red]")
                else:
                    from ares.copilot_oauth import authorize_github_device_flow
                    self.console.print("Requesting a GitHub device code…")
                    try:
                        token = authorize_github_device_flow(
                            client_id=client_id,
                            on_device_code=lambda device: self.console.print(
                                "Open this link and enter the code shown below:\n"
                                f"[link={device.verification_uri}]{device.verification_uri}[/link]\n"
                                f"[bold cyan]Code: {device.user_code}[/bold cyan]"
                            ),
                        )
                    except Exception as exc:
                        self.console.print(f"[red]Copilot authorization failed: {exc}[/red]")
                    else:
                        self.config.copilot_oauth_client_id = client_id
                        self.config.copilot_github_token = token.access_token
                        self._activate_provider("copilot")
                        self.config.model = "auto"
                        self.agent.set_model("auto")
                        save_config(self.config)
                        self.console.print("[green]GitHub Copilot connected. Ares will use automatic model selection.[/green]")
            else:
                self.console.print("[red]Usage: /copilot [status|login CLIENT_ID|token TOKEN][/red]")

        elif command == "/clear":
            self.console.clear()

        elif command == "/setup":
            completed = OnboardingWizard(
                console=self.console,
                config=self.config,
                profile_manager=self.profile_manager,
                soul_manager=self.soul_manager,
            ).run(re_run=True)
            if completed:
                self.agent.set_model(self.config.model)

        elif command == "/browser":
            pieces = arg.split()
            action = pieces[0].lower() if pieces else "status"
            manager = self._browser()
            if action == "status" and len(pieces) == 1:
                table = Table(title="Browser", border_style="bright_cyan", box=CLI_BOX)
                table.add_column("Setting", style="cyan", no_wrap=True)
                table.add_column("Value", ratio=3)
                cdp_ready = manager.detect_chrome_cdp()
                extension_ready = manager.detect_extension_available()
                executable, profile = manager._chrome_paths()
                table.add_row("Configured mode", self.config.browser_mode)
                table.add_row("Effective mode", manager.resolve_mode())
                table.add_row("CDP port", str(self.config.browser_cdp_port))
                table.add_row("CDP", "[green]available[/green]" if cdp_ready else "[dim]not available[/dim]")
                table.add_row("Extension", "[green]token configured[/green]" if extension_ready else "[dim]manual approval or token required[/dim]")
                table.add_row("Chrome", executable)
                table.add_row("Chrome profile", profile)
                self.console.print(table)
            elif action in VALID_BROWSER_MODES and len(pieces) == 1:
                self._set_browser_mode(action)
                if action == "system":
                    self.console.print(
                        "[yellow]System mode uses Chrome's CDP session and may access your real cookies. "
                        "Use /browser launch if CDP is not available.[/yellow]"
                    )
                elif action == "extension":
                    if manager.detect_extension_available():
                        self.console.print("[green]Extension mode will connect to an approved existing browser tab.[/green]")
                    else:
                        self.console.print(
                            "[yellow]Extension mode requires the Playwright Chrome Extension. "
                            "Without a token, Chrome will ask you to approve each connection.[/yellow]"
                        )
                effective = self._browser().resolve_mode()
                if action == "system" and effective != "system":
                    self.console.print(
                        "[yellow]CDP is not available, so Playwright will reconnect in isolated mode until Chrome CDP is ready.[/yellow]"
                    )
                else:
                    self.console.print(f"[green]Browser mode set to {action}; Playwright will reconnect now.[/green]")
            elif action == "launch" and len(pieces) <= 2:
                if len(pieces) == 2:
                    try:
                        port = int(pieces[1])
                    except ValueError:
                        self.console.print("[red]Browser launch port must be a number.[/red]")
                        return True
                else:
                    port = None
                self.console.print(manager.launch_system_chrome(port))
                if manager.wait_for_chrome_cdp():
                    # The system setting may have safely fallen back to
                    # isolated mode before Chrome existed. Rebuild the MCP
                    # entry now that the endpoint is actually reachable.
                    _ensure_mcp_defaults(self.config)
                    save_config(self.config)
                    self.browser_manager = BrowserManager(self.config)
                    self._mcp_config_signature = self._get_mcp_config_signature(self.config)
                    self._mcp_reconfigure_pending = True
                    if hasattr(self.agent, "apply_config"):
                        self.agent.apply_config(self.config)
                    self.console.print("[green]Chrome CDP is ready; Playwright will reconnect to it now.[/green]")
                else:
                    self.console.print(
                        "[yellow]Chrome CDP was not ready yet. It will remain on the isolated profile; "
                        "run /mcp reconnect playwright once Chrome finishes starting.[/yellow]"
                    )
            else:
                self.console.print(
                    "[red]Usage: /browser [status|isolated|system|extension|auto|launch [port]][/red]"
                )

        elif command == "/export":
            path = export_data(
                memory_store=self.memory_store,
                conversation_store=self.conversation_store,
                people_store=getattr(self.agent, "people_store", None),
                action_ledger=getattr(self.agent, "action_ledger", None),
                goal_store=getattr(self.agent, "goal_store", None),
                commitment_store=getattr(self.agent, "commitment_store", None),
                config=self.config,
                path=arg or None,
            )
            self.console.print(f"[green]Exported data to {path}[/green]")

        elif command == "/import":
            if not arg:
                self.console.print("[red]Usage: /import PATH [--config][/red]")
            else:
                import_config = "--config" in arg.split()
                path = arg.replace("--config", "").strip()
                counts = import_data(
                    path,
                    memory_store=self.memory_store,
                    conversation_store=self.conversation_store,
                    people_store=getattr(self.agent, "people_store", None),
                    action_ledger=getattr(self.agent, "action_ledger", None),
                    goal_store=getattr(self.agent, "goal_store", None),
                    commitment_store=getattr(self.agent, "commitment_store", None),
                    import_config=import_config,
                )
                if import_config:
                    self.config = load_config()
                    self.agent.set_model(self.config.model)
                self.console.print(
                    "[green]Imported "
                    f"{counts['memories']} memories, "
                    f"{counts['conversations']} conversations.[/green]"
                    f" {counts['goals']} goals restored."
                )

        elif command == "/reset":
            with suppress(Exception):
                self.conversation_store.end_conversation(self.conversation_id)
            self.conversation_id = self.conversation_store.start_conversation()
            self.conversation_history = self._conversation_history_for_model(self.conversation_id)
            self.session_manager = SessionManager()
            set_session_id = getattr(self.agent, "set_session_id", None)
            if callable(set_session_id):
                set_session_id(self.session_manager.get_id())
            if hasattr(self.agent, "last_messages"):
                self.agent.last_messages = []
            self._session_finalized = False
            self.console.print(f"[dim]Started a new conversation #{self.conversation_id}. Memory preserved.[/dim]")

        elif command == "/resume":
            options = self._resume_conversations()
            if not arg:
                if not options:
                    self.console.print("[dim]No saved conversations are available yet.[/dim]")
                else:
                    table = Table(title="Saved conversations", border_style="bright_cyan", box=CLI_BOX)
                    table.add_column("ID", style="cyan", no_wrap=True)
                    table.add_column("Summary", ratio=4)
                    table.add_column("Last active", style="dim")
                    for row in options:
                        marker = "  current" if int(row.get("id") or 0) == self.conversation_id else ""
                        summary = " ".join(str(row.get("summary") or "Untitled chat").split())[:100]
                        stamp = str(row.get("ended_at") or row.get("started_at") or "")[:19].replace("T", " ")
                        table.add_row(str(row.get("id")), summary + marker, stamp)
                    self.console.print(table)
                    self.console.print("[dim]Use /resume ID or /resume latest.[/dim]")
            else:
                if arg.casefold() == "latest":
                    target = next((int(row["id"]) for row in options if int(row.get("id") or 0) != self.conversation_id), None)
                else:
                    try:
                        target = int(arg)
                    except ValueError:
                        target = None
                if target is None or not self._resume_conversation(target):
                    self.console.print("[red]Conversation not found. Use /resume to choose a saved chat.[/red]")
                else:
                    self.console.print(
                        f"[green]Resumed conversation #{target}. "
                        f"{len(self.conversation_history)} messages loaded privately; continue below.[/green]"
                    )

        elif command == "/soul":
            if not arg or arg == "show":
                content = self.soul_manager.read()
                if content:
                    self._print_markdown_section("Soul", content, "Ares personality")
                else:
                    self.console.print("[dim]No soul file found. Use /soul edit to create one.[/dim]")
            elif arg == "edit":
                self._edit_file(self.soul_manager.soul_path, "soul")
            else:
                self.console.print("[red]Usage: /soul [show|edit][/red]")

        elif command == "/profile":
            if not arg or arg == "show":
                content = self.profile_manager.read()
                if content:
                    self._print_markdown_section("Profile", content, "User identity")
                else:
                    self.console.print("[dim]No profile file found. Use /profile edit to create one.[/dim]")
            elif arg == "edit":
                self._edit_file(self.profile_manager.profile_path, "profile")
            else:
                self.console.print("[red]Usage: /profile [show|edit][/red]")

        elif command == "/context":
            context_str = build_context_prompt(
                soul_context=self.soul_manager.get_context(),
                profile_context=self.profile_manager.get_context(),
                project_context=self.project_context.get_context()
                if self.config.project_context_enabled else "",
                memories=self.memory_store.get_recent(limit=5),
                token_budget=self.config.context_token_budget,
            )
            if context_str:
                self._print_markdown_section("Active Context", context_str)
            else:
                self.console.print("[dim]No context active.[/dim]")


        elif command == "/skills":
            if not arg:
                skills = self.skill_manager.list_all()
                table = Table(title="Skills", border_style="bright_magenta", box=CLI_BOX)
                table.add_column("Name", style="cyan", no_wrap=True)
                table.add_column("Category", no_wrap=True)
                table.add_column("Description", ratio=4)
                for skill in skills:
                    table.add_row(skill.name, skill.category, skill.description)
                self.console.print(table if skills else "[dim]No skills installed.[/dim]")
            elif arg == "categories":
                cats = self.skill_manager.list_categories()
                table = Table(title="Skill Categories", border_style="bright_magenta", box=CLI_BOX)
                table.add_column("Category", style="cyan")
                table.add_column("Skills", justify="right")
                for category, count in cats.items():
                    table.add_row(category, str(count))
                self.console.print(table if cats else "[dim]No skill categories found.[/dim]")
            elif arg.startswith("search "):
                query = arg.split(maxsplit=1)[1]
                skills = self.skill_manager.search(query=query)
                if not skills:
                    self.console.print("[dim]No matching skills found.[/dim]")
                else:
                    table = Table(title=f"Skill Search: {query}", border_style="bright_magenta", box=CLI_BOX)
                    table.add_column("Name", style="cyan", no_wrap=True)
                    table.add_column("Category", no_wrap=True)
                    table.add_column("Description", ratio=4)
                    for skill in skills:
                        table.add_row(skill.name, skill.category, skill.description)
                    self.console.print(table)
            elif arg.startswith("load "):
                name = arg.split(maxsplit=1)[1]
                skill = self.skill_manager.get_skill(name)
                if skill is None:
                    self.console.print(f"[red]Skill '{name}' not found.[/red]")
                else:
                    self._print_markdown_section(f"Skill: {skill.name}", skill.content, skill.description)
            else:
                self.console.print("[red]Usage: /skills [search QUERY|load NAME|categories][/red]")

        elif command == "/phone":
            if arg and arg != "status":
                self.console.print("[red]Usage: /phone status[/red]")
            elif not self.config.phone.enabled:
                self.console.print("[red]Phone bridge is disabled. Set phone.enabled=true in config.[/red]")
            else:
                import json
                payload = json.loads(get_phone_status())
                table = Table(title="Phone Bridge", border_style="bright_cyan", box=CLI_BOX)
                table.add_column("Bridge", style="cyan", no_wrap=True)
                table.add_column("Status")
                table.add_column("Details", style="dim", ratio=4)
                kde = payload.get("kdeconnect", {})
                adb = payload.get("adb", {})
                table.add_row(
                    "KDE Connect",
                    "[green]PASS[/green]" if kde.get("ok") else "[red]FAIL[/red]",
                    kde.get("device_id") or kde.get("error") or "paired",
                )
                battery = adb.get("battery") or {}
                details = ", ".join(adb.get("devices") or []) or adb.get("error") or "connected"
                if battery.get("level"):
                    details += f" · battery {battery['level']}%"
                table.add_row(
                    "ADB",
                    "[green]PASS[/green]" if adb.get("ok") else "[red]FAIL[/red]",
                    details,
                )
                self.console.print(table)

        elif command == "/call":
            if not arg:
                self.console.print("[red]Usage: /call CONTACT_OR_NUMBER [--confirm][/red]")
            else:
                confirm = arg.endswith(" --confirm")
                recipient = arg[:-10].strip() if confirm else arg
                result = self.agent.tool_executor.execute("telephony_call", {"recipient": recipient, "confirm": confirm})
                self.console.print(get_renderer("telephony_call")(result))

        elif command == "/telephony":
            pieces = arg.split()
            action = pieces[0].lower() if pieces else "status"
            mapping = {
                "status": ("telephony_status", {}),
                "contacts": ("telephony_list_contacts", {}),
                "recent": ("telephony_list_calls", {}),
                "hangup": ("telephony_hangup", {"call_id": pieces[1] if len(pieces) > 1 else ""}),
                "mute": ("telephony_mute", {"call_id": pieces[1] if len(pieces) > 1 else "", "muted": True}),
                "unmute": ("telephony_mute", {"call_id": pieces[1] if len(pieces) > 1 else "", "muted": False}),
            }
            if action not in mapping:
                self.console.print("[red]Usage: /telephony [status|contacts|recent|hangup CALL_ID|mute CALL_ID|unmute CALL_ID][/red]")
            else:
                tool_name, arguments = mapping[action]
                result = self.agent.tool_executor.execute(tool_name, arguments)
                self.console.print(get_renderer(tool_name)(result))

        elif command in {"/hangup", "/mute", "/unmute", "/recent-calls", "/contacts"}:
            tool_name, arguments = {
                "/hangup": ("telephony_hangup", {"call_id": arg}),
                "/mute": ("telephony_mute", {"call_id": arg, "muted": True}),
                "/unmute": ("telephony_mute", {"call_id": arg, "muted": False}),
                "/recent-calls": ("telephony_list_calls", {}),
                "/contacts": ("telephony_list_contacts", {}),
            }[command]
            if command in {"/hangup", "/mute", "/unmute"} and not arg:
                self.console.print(f"[red]Usage: {command} CALL_ID[/red]")
            else:
                result = self.agent.tool_executor.execute(tool_name, arguments)
                self.console.print(get_renderer(tool_name)(result))

        elif command in {"/monitor", "/monitors"}:
            from ares.watcher.commands import WatcherCommands
            from ares.watcher.database import resolve_watcher_database_path
            watcher_tools = getattr(self.agent.tool_executor, "watcher_tools", None)
            controller = WatcherCommands(
                database_path=resolve_watcher_database_path(self.config),
                defaults=self.config.watcher.defaults,
                db=watcher_tools.db if watcher_tools is not None else None,
                goal_store=getattr(self.agent, "goal_store", None),
            )
            try:
                result = controller.execute(arg if command == "/monitor" else "list")
                action = result["action"]
                if action == "list":
                    table = Table(title="Ares Watchers", border_style="bright_red", box=CLI_BOX)
                    table.add_column("ID", style="red", no_wrap=True); table.add_column("Watcher", ratio=3)
                    table.add_column("Type"); table.add_column("Status"); table.add_column("Interval", justify="right")
                    for item in result["monitors"]:
                        status = "paused" if not item["enabled"] else item["last_status"] or "armed"
                        table.add_row(item["id"][:8], item["name"], item["type"], status, f"{item['interval_seconds']}s")
                    self.console.print(table if result["monitors"] else "[dim]No watchers configured. Use /monitor add \"Name\" URL[/dim]")
                elif action == "events":
                    table = Table(title=f"Watcher Events · {result['monitor']['name']}", border_style="bright_red", box=CLI_BOX)
                    table.add_column("When"); table.add_column("Severity"); table.add_column("Change", ratio=4)
                    for item in result["events"]: table.add_row(item["created_at"], item["severity"], item["change_summary"] or item["event_type"])
                    self.console.print(table if result["events"] else "[dim]No changes recorded.[/dim]")
                elif action == "status":
                    item = result["monitor"]
                    linked_goals = result.get("linked_goals") or []
                    linked_text = ", ".join(f"#{goal['goal_id']} {goal['title']}" for goal in linked_goals) or "none"
                    self.console.print(Panel(
                        f"[bold]{item['name']}[/bold]\n{item['url'] or item['type']}\n\nStatus: {item['last_status'] or 'armed'} · Enabled: {item['enabled']}\n"
                        f"Checks: {item['total_checks']} · Changes: {item['total_changes']} · Errors: {item['error_count']}\n"
                        f"Last check: {item['last_checked_at'] or 'never'} · Next: {item['next_check_at'] or 'next scheduler tick'}\n"
                        f"Linked goals: {linked_text}",
                        title=f"Watcher {item['id'][:8]}", border_style="bright_red"))
                elif action == "test":
                    monitor_data = result["monitor"]
                    async def run_manual_check() -> None:
                        service = getattr(watcher_tools, "service", None)
                        if service is None:
                            self.console.print("[red]Watcher runtime is not active. Start Ares with --all.[/red]")
                            return
                        monitor = service.db.get_monitor(monitor_data["id"])
                        event = await service.scheduler.check_monitor(monitor, force=True) if monitor else None
                        self.console.print(f"[green]Watcher check complete.[/green] {'Change detected.' if event else 'No change detected.'}")
                    asyncio.create_task(run_manual_check())
                    self.console.print("[cyan]Manual watcher check dispatched.[/cyan]")
                else:
                    item = result["monitor"]
                    linked = f" · linked to goal #{result['linked_goal_id']}" if result.get("linked_goal_id") else ""
                    self.console.print(f"[green]Watcher {action} complete:[/green] {item['name']} ({item['id'][:8]}){linked}")
            except (ValueError, KeyError) as exc:
                self.console.print(f"[red]{exc}[/red]")
            finally:
                controller.close()

        elif command == "/tools":
            if not arg:
                mode = getattr(self, "tool_output_mode", "summary")
                descriptions = {
                    "summary": "Live operation name plus one compact completion line.",
                    "details": "Live activity and rendered results; huge MCP payloads stay collapsed.",
                    "hidden": "No tool activity unless an error reaches the answer.",
                }
                table = Table(title="Tool Output", border_style="bright_cyan", box=CLI_BOX)
                table.add_column("Mode", style="cyan", no_wrap=True)
                table.add_column("Status", no_wrap=True)
                table.add_column("Description", ratio=4)
                for name, description in descriptions.items():
                    status = "[green]active[/green]" if name == mode else "available"
                    table.add_row(name, status, description)
                self.console.print(table)
            elif arg in TOOL_OUTPUT_MODES:
                self.tool_output_mode = arg
                descriptions = {
                    "summary": "Ares shows live tools, MCP calls, skills, and compact results.",
                    "details": "Rendered results are shown, while snapshots remain collapsed.",
                    "hidden": "Tool activity is fully hidden unless an error reaches the final answer.",
                }
                self.console.print(f"[green]Tool output set to {arg}.[/green] [dim]{descriptions[arg]}[/dim]")
            else:
                self.console.print("[red]Usage: /tools [summary|details|hidden][/red]")

        elif command == "/exit":
            return False

        else:
            skill = self.skill_manager.get_skill(command[1:]) if command.startswith("/") else None
            if skill is not None:
                self._print_markdown_section(f"Skill: {skill.name}", skill.content, skill.description)
            else:
                self.console.print(f"[red]Unknown command: {command}. Type /help for available commands.[/red]")

        return True

    async def _process_input(self, user_input: str):
        """Process a user message through the agent and display response."""
        await self._apply_browser_mode_hint(user_input)
        tool_renderables = []
        tool_started_at: dict[str, list[float]] = {}
        tool_steps: dict[str, list[int]] = {}
        next_tool_step = 1
        mode = getattr(self, "tool_output_mode", "summary")
        activity_visible = mode != "hidden"
        live_enabled = (
            activity_visible
            and bool(getattr(self.console, "is_terminal", False))
            and bool(getattr(self, "unicode_output", False))
        )
        thinking_label = f"Let me think this through{self._activity_separator()}{self.config.model}"
        status_context = (
            self.console.status(self._working_text(thinking_label), spinner="dots")
            if live_enabled
            else nullcontext(None)
        )

        self.console.print()
        if activity_visible and not live_enabled:
            self._print_static_activity_header(thinking_label)
        selected_skills = self._active_skills(user_input) if activity_visible else []
        full_response = ""
        agent_unsubscribe = None
        runtime = getattr(self.agent, "multi_agent_runtime", None)
        agent_event_seen: set[tuple[str, str, str]] = set()
        if activity_visible and runtime is not None:
            expected_session = str(getattr(self.agent, "session_id", None) or "")
            def handle_agent_event(event: dict) -> None:
                event_session = str(event.get("session_id") or "")
                if not expected_session or not event_session or event_session == expected_session:
                    self._print_agent_event(event, agent_event_seen)
            agent_unsubscribe = runtime.subscribe(handle_agent_event)
        try:
            with status_context as live_status:
                if selected_skills:
                    self._print_skill_card(selected_skills)
                try:
                    async for token in self.agent.run_stream(user_input, self.conversation_history):
                        if token.startswith("[tool_start:"):
                            tool_name = self._parse_tool_start_token(token)
                            tool_started_at.setdefault(tool_name, []).append(time.monotonic())
                            tool_steps.setdefault(tool_name, []).append(next_tool_step)
                            self._print_tool_start(tool_name, live_status, step=next_tool_step)
                            next_tool_step += 1
                        elif token.startswith("[tool_progress:"):
                            tool_name, detail = self._parse_tool_progress_token(token)
                            # Progress belongs in transient activity, never in
                            # the final assistant response.
                            if live_status is not None:
                                live_status.update(self._working_text(f"{self._activity_label(tool_name)}{self._activity_separator()}{detail}"))
                        elif token.startswith("[tool:"):
                            tool_name, tool_content = self._parse_tool_token(token)
                            event = self._summarize_tool_result(tool_name, tool_content)
                            steps = tool_steps.get(tool_name) or []
                            if steps:
                                event["step"] = steps.pop(0)
                            starts = tool_started_at.get(tool_name) or []
                            if starts:
                                elapsed = max(0.0, time.monotonic() - starts.pop(0))
                                event["detail"] = f"{event['detail']}{self._activity_separator()}{elapsed:.1f}s"
                            self._print_tool_done(event)
                            if live_status is not None:
                                live_status.update(self._working_text(thinking_label))
                            if mode == "details" and not self._collapse_noisy_tool_output(tool_name, tool_content):
                                try:
                                    renderer = get_renderer(tool_name)
                                    tool_renderables.append(renderer(tool_content))
                                except Exception:
                                    tool_renderables.append(render_generic_tool(tool_content))
                        else:
                            full_response += token
                except Exception as e:
                    if activity_visible:
                        self.console.print(self._activity_line("failed", "Request", str(e)))
                    full_response = f"Error: {e}"
        finally:
            if agent_unsubscribe is not None:
                agent_unsubscribe()

        if mode == "details":
            for renderable in tool_renderables:
                self.console.print(renderable)

        # Show final response
        full_response = self._clean_assistant_text(full_response)
        full_response = self._drop_repeated_opening(full_response)
        if full_response.strip():
            self._print_assistant_response(full_response)

        # Keep future turns focused on conversational text, not stale tool-call scaffolding.
        self.conversation_history.append({"role": "user", "content": user_input})
        if full_response.strip():
            self.conversation_history.append({"role": "assistant", "content": full_response})

        self.conversation_store.add_exchange(self.conversation_id, user_input, full_response)

        # Dual-write to per-session JSONL
        session_id = self.session_manager.get_id()
        self.session_store.write_message(session_id, "user", user_input)
        self.session_store.write_message(session_id, "assistant", full_response)


        # Trim conversation history
        max_msgs = self.config.max_context_messages
        if len(self.conversation_history) > max_msgs:
            self.conversation_history = self.conversation_history[-max_msgs:]

        self.console.print()

    async def run(self):
        """Main CLI loop."""
        watcher_service = None
        if self.config.watcher.enabled:
            from ares.watcher.integration import create_agent_watcher_service
            watcher_service = create_agent_watcher_service(self.config, self.agent)
            await watcher_service.start()
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.start()
            except BaseException:
                clear_current_task_cancellation()
            self.agent.refresh_tools()
        if self.cron_scheduler is not None:
            await self.cron_scheduler.start()
        if self.proactive_service is not None:
            await self.proactive_service.start()
        self._show_banner()

        try:
            with patch_stdout():
                while True:
                    try:
                        user_input = await self._prompt()
                        if user_input.strip().startswith("/"):
                            user_input = await self._interactive_command(user_input.strip())
                            if user_input is None:
                                continue
                        self._sync_shared_state()
                        await self._refresh_mcp_manager_if_needed()

                        if not user_input.strip():
                            continue

                        if user_input.strip().startswith("/"):
                            root_command = user_input.strip().split(maxsplit=1)[0].lower()
                            if root_command in {"/mcp", "/skills"}:
                                try:
                                    if await self._handle_marketplace_command(user_input.strip()):
                                        continue
                                except Exception as exc:
                                    self.console.print(f"[red]Marketplace command error: {exc}[/red]")
                                    continue
                                if root_command == "/mcp":
                                    await self._handle_mcp_command(user_input.strip())
                                    continue
                            try:
                                should_continue = self._handle_command(user_input.strip())
                            except Exception as e:
                                self.console.print(f"[red]Command error: {e}[/red]")
                                should_continue = True
                            if not should_continue:
                                break
                            await self._refresh_mcp_manager_if_needed()
                            continue

                        await self._process_input(user_input)

                    except KeyboardInterrupt:
                        self.console.print("\n[dim]Interrupted. Press /exit to quit.[/dim]\n")
                        continue
                    except EOFError:
                        break
                    except asyncio.CancelledError:
                        clear_current_task_cancellation()
                        self._reset_prompt_session()
                        self.console.print(
                            "\n[dim yellow]A background operation cancelled the prompt; recovered. Try again or use /exit to quit.[/dim yellow]\n"
                        )
                        continue
        finally:
            # Cleanup
            if watcher_service is not None:
                try:
                    await watcher_service.stop()
                    setter = getattr(self.agent.tool_executor, "set_watcher_service", None)
                    if setter is not None:
                        setter(None)
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (watcher): {exc}[/dim yellow]")
            if self.cron_scheduler is not None:
                try:
                    await self.cron_scheduler.stop()
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (cron): {exc}[/dim yellow]")
            if self.proactive_service is not None:
                try:
                    await self.proactive_service.stop()
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (proactive): {exc}[/dim yellow]")
            if self.mcp_manager is not None:
                try:
                    await self.mcp_manager.close()
                except Exception as exc:
                    self.console.print(f"[dim yellow]Shutdown warning (MCP): {exc}[/dim yellow]")
            self._finalize_session()
            try:
                await self.agent.close()
            except Exception as exc:
                self.console.print(f"[dim yellow]Shutdown warning (agent): {exc}[/dim yellow]")
            self._cleanup_step("memory store", self.memory_store.close)
            self._cleanup_step("conversation store", self.conversation_store.close)
            self.console.print("\n[dim]Goodbye.[/dim]\n")
