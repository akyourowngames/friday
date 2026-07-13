"""Terminal UI using Rich and prompt_toolkit."""

import asyncio
from contextlib import nullcontext, suppress
from difflib import SequenceMatcher
import json
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
from ares.onboarding import OnboardingWizard
from ares.reminders import DesktopNotifier
from ares.tools.renders import get_renderer, render_generic_tool
from ares.soul import SoulManager, SOUL_TEMPLATE
from ares.config import _ensure_mcp_defaults, load_config, save_config
from ares.prompts import WELCOME_MESSAGE, FIRST_RUN_MESSAGE
from ares.llm import FREE_MODELS
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
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            config=self.config,
            mcp_manager=self.mcp_manager,
            session_store=self.session_store,
            session_id=self.session_manager.get_id(),
        )
        self._session_finalized = False
        self.conversation_history: list[dict] = self.conversation_store.get_recent_messages(
            limit=self.config.max_context_messages
        )
        self.notifier = DesktopNotifier(enabled=self.config.enable_desktop_notifications)
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

    def _create_prompt_session(self) -> PromptSession | None:
        """Create an interactive prompt session when attached to a TTY."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        return PromptSession(
            history=FileHistory(history_path()),
            auto_suggest=AutoSuggestFromHistory(),
            completer=COMPLETER,
            complete_while_typing=True,
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
        overview.add_row(Text.assemble(("Commands  ", "dim"), ("/help", "cyan"), ("    Activity detail  ", "dim"), ("/tools", "cyan")))

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

    def _show_model_list(self) -> None:
        table = Table(title="Models", border_style="bright_cyan", box=CLI_BOX)
        table.add_column("Model", style="cyan")
        table.add_column("Status", no_wrap=True)
        for model in FREE_MODELS:
            status = "[green]current[/green]" if model == self.config.model else "available"
            table.add_row(model, status)
        self.console.print(table)

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
        """Identify local tools and MCP calls without exposing protocol noise."""
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            if len(parts) == 3:
                server = self._pretty_activity_name(parts[1])
                action = self._pretty_activity_name(parts[2])
                separator = self._activity_separator()
                return f"MCP{separator}{server}{separator}{action}"
        return f"Tool{self._activity_separator()}{self._pretty_activity_name(self._tool_label(tool_name))}"

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
            live_status.update(self._working_text(label))
        else:
            self.console.print(self._activity_line("start", label, "running"))

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
        kind = "MCP step" if str(event.get("tool", "")).startswith("mcp__") else "Tool step"
        state = "failed" if failed else "completed"
        body = Table.grid(expand=True, padding=(0, 1))
        body.add_column(no_wrap=True)
        body.add_column(ratio=1)
        body.add_row(
            Text(state.upper(), style="bold red" if failed else "bold bright_green"),
            Text(self._clip_tool_detail(detail, 100), style="red" if failed else "default"),
        )
        self.console.print(Panel(
            body,
            title=f"[bold]{kind}[/bold] [dim]{label}[/dim]",
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
            title="[bold bright_cyan]Ares working[/bold bright_cyan]",
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
            table.add_row("/memory [search|edit|delete|clean]", "Review, manage, and clean memories")
            table.add_row("/goals [search|show|due|signals]", "Track goals, evidence, proactive watcher signals, and hierarchy")
            table.add_row("/forget ID", "Delete a memory by ID")
            table.add_row("/model [MODEL]", f"Show or switch model: {self.config.model}")
            table.add_row("/clear", "Clear terminal screen")
            table.add_row("/export", "Export data to JSON")
            table.add_row("/import PATH [--config]", "Import data from an Ares JSON export")
            table.add_row("/reset", "Reset conversation context")
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
                table.add_row("Policy pruned", str(stats.get("policy_pruned", 0)))
                table.add_row("Duplicates merged", str(stats.get("duplicates_merged", 0)))
                table.add_row("Stale pruned", str(stats.get("stale_pruned", 0)))
                table.add_row("Remaining", str(stats.get("total_after", 0)))
                self.console.print("[green]Memory cleaned.[/green]")
                self.console.print(table)
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
            elif arg.startswith("delete "):
                fact_id = int(arg.split(maxsplit=1)[1])
                if self.memory_store.delete(fact_id):
                    self.console.print(f"[yellow]Forgot memory #{fact_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Memory #{fact_id} was not found.[/red]")
            else:
                self.console.print("[red]Usage: /memory [search QUERY|edit ID NEW_TEXT|delete ID|clean][/red]")

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

        elif command == "/model":
            if not arg or arg == "list":
                self._show_model_list()
            else:
                self.config.model = arg
                save_config(self.config)
                self.agent.set_model(arg)
                self.console.print(f"[green]Model switched to {arg}.[/green]")

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
            self.conversation_history = []
            self.console.print("[dim]Conversation reset. Memory preserved.[/dim]")

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
        thinking_label = f"Thinking{self._activity_separator()}{self.config.model}"
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
        self._show_banner()

        try:
            with patch_stdout():
                while True:
                    try:
                        user_input = await self._prompt()
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
