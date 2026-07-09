"""Terminal UI using Rich and prompt_toolkit."""

import asyncio
from contextlib import suppress
from difflib import SequenceMatcher
import json
import re
import shutil
import sqlite3
import sys
import textwrap
import unicodedata
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style
from prompt_toolkit.patch_stdout import patch_stdout

from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from ares.agent import Agent
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
from ares.config import load_config, save_config
from ares.prompts import WELCOME_MESSAGE, FIRST_RUN_MESSAGE
from ares.llm import FREE_MODELS
from ares.skills import SkillManager
from ares.tools.mcp_client import MCPClientManager
from ares.tools.adb_bridge import phone_status as get_phone_status
from ares.cron import CronScheduler, CronStore
from ares.cron.toast import CronToastManager
from ares.session import SessionManager
from ares.sessions import SessionStore

# Styles
STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
})

COMPLETER = WordCompleter([
    "/help", "/memory", "/memory clean", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context",
    "/skills", "/skills search", "/skills categories", "/skills load",
    "/setup", "/phone", "/phone status",
    "/tools", "/tools summary", "/tools details", "/tools hidden",
], ignore_case=True)

TOOL_OUTPUT_MODES = {"summary", "details", "hidden"}

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
    "phone_status": "phone",
    "phone_get_notifications": "phone",
    "phone_search_contact": "phone",
    "phone_send_sms": "phone",
    "phone_call_number": "phone",
    "phone_launch_app": "phone",
    "phone_open_url": "phone",
    "update_config": "config",
    "get_current_datetime": "clock",
}


def _history_path() -> str:
    """Return an expanded prompt history path and ensure its directory exists."""
    path = Path("~/.ares_history").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _supports_unicode_output() -> bool:
    """Return whether stdout is likely to handle emoji and symbols."""
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding


def _clear_current_task_cancellation() -> None:
    """Clear asyncio cancellation state after a cancellation was intentionally handled."""
    current_task = asyncio.current_task()
    if current_task is None or not hasattr(current_task, "uncancel"):
        return
    while current_task.cancelling():
        current_task.uncancel()


CLI_BOX = box.ROUNDED


class AresCLI:
    """The main CLI application for Ares."""

    def __init__(self):
        self.console = Console(color_system="auto", highlight=True)
        self.unicode_output = _supports_unicode_output()
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
        if sys.stdin.isatty() and sys.stdout.isatty() and not self.profile_manager.is_populated():
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
            history=FileHistory(_history_path()),
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

    async def _prompt(self) -> str:
        """Read one prompt line from an interactive session or plain stdin."""
        if self.session is not None:
            return await self.session.prompt_async(self.icons["prompt"])
        return await asyncio.to_thread(input, self.icons["prompt"])

    def _show_banner(self):
        """Display the welcome banner."""
        memory_count = len(self.memory_store.list_all())

        self.console.print()
        self.console.print("[bold cyan]Ares[/bold cyan] [dim]v0.1.0[/dim]")
        self.console.print(
            f"[dim]model[/dim] {self.config.model}  "
            f"[dim]memory[/dim] {memory_count} facts  "
            "[dim]help[/dim] /help"
        )
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

    def _tool_status(self, tool_name: str) -> tuple[str, str]:
        """Return status text and border style for a running tool."""
        statuses = {
            "web_search": ("Searching the web", "bright_green"),
            "read_file": ("Reading a file", "bright_blue"),
            "search_files": ("Searching files", "bright_yellow"),
            "list_directory": ("Scanning a directory", "bright_magenta"),
            "store_memory": ("Saving memory", "green"),
        }
        label = self._tool_label(tool_name)
        return statuses.get(tool_name, (f"Using {label}", "dim"))

    def _tool_label(self, tool_name: str) -> str:
        """Return a friendly tool name for compact CLI status text."""
        if tool_name in TOOL_LABELS:
            return TOOL_LABELS[tool_name]
        if tool_name.startswith("mcp__"):
            parts = [part for part in tool_name.split("__") if part and part != "mcp"]
            if parts:
                return " ".join(parts).replace("_", " ")
        return tool_name.replace("_", " ") or "tool"

    def _parse_tool_token(self, token: str) -> tuple[str, str]:
        """Parse [tool:name:content] tokens with a fallback for legacy tokens."""
        inner = token[6:-1]
        parts = inner.split(":", 1)
        if len(parts) == 2 and re.match(r"^[a-z][a-z0-9_]*$", parts[0]):
            return parts[0] or "unknown", parts[1]
        return "unknown", inner

    def _parse_tool_start_token(self, token: str) -> str:
        """Parse [tool_start:name] tokens."""
        inner = token.removeprefix("[tool_start:").removesuffix("]")
        return inner if re.match(r"^[a-z][a-z0-9_]*$", inner) else "unknown"

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
        elif tool_name.startswith("mcp__"):
            event["detail"] = "external tool completed"
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

    def _print_tool_start(self, tool_name: str) -> None:
        """Show a tool call as soon as the model asks for it."""
        if getattr(self, "tool_output_mode", "summary") != "details":
            return
        self.console.print(f"[dim]Using {self._tool_label(tool_name)}...[/dim]")

    def _print_tool_done(self, event: dict[str, str]) -> None:
        """Show a compact tool completion line."""
        if getattr(self, "tool_output_mode", "summary") != "details":
            return
        label = event.get("label", "tool")
        detail = event.get("detail", "completed")
        if event.get("state") == "failed":
            self.console.print(f"[red]Failed {label}: {detail}[/red]")
        else:
            self.console.print(f"[dim]Done {label}: {detail}[/dim]")

    def _clean_assistant_text(self, text: str) -> str:
        """Remove accidental tool protocol tokens from assistant-facing text."""
        clean = re.sub(r"\[tool:[^\]]+\]", "", text)
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
        return max(48, min(columns, 110) - 4)

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

    def _wrapped_response_lines(self, text: str) -> list[str]:
        """Format assistant text with a stable gutter and hanging bullet indents."""
        width = self._response_width()
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = self._plain_response_line(raw_line)
            if not line:
                lines.append("")
                continue

            bullet = re.match(r"^-\s+(.*)$", line)
            numbered = re.match(r"^(\d+[.)])\s+(.*)$", line)
            if bullet:
                wrapped = textwrap.wrap(
                    bullet.group(1),
                    width=width,
                    initial_indent="  - ",
                    subsequent_indent="    ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            elif numbered:
                marker = numbered.group(1)
                wrapped = textwrap.wrap(
                    numbered.group(2),
                    width=width,
                    initial_indent=f"  {marker} ",
                    subsequent_indent=" " * (len(marker) + 3),
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            else:
                wrapped = textwrap.wrap(
                    line,
                    width=width,
                    initial_indent="  ",
                    subsequent_indent="  ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
            lines.extend(wrapped or ["  "])
        return lines

    def _print_assistant_response(self, text: str) -> None:
        """Render final assistant output with Rich Markdown, tables, and color."""
        self.console.print()
        clean = self._clean_assistant_text(text)
        self.console.print(Panel(
            Markdown(clean, code_theme="monokai"),
            title="[bold bright_cyan]Ares[/bold bright_cyan]",
            border_style="bright_cyan",
            box=CLI_BOX,
            padding=(0, 1),
        ))

    def _cleanup_step(self, label: str, func) -> None:
        """Run one shutdown step without letting cleanup errors crash Ares."""
        try:
            func()
        except sqlite3.OperationalError as exc:
            self.console.print(f"[dim yellow]Shutdown warning ({label}): {exc}[/dim yellow]")
        except Exception as exc:
            self.console.print(f"[dim yellow]Shutdown warning ({label}): {exc}[/dim yellow]")

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
            table.add_row("/forget ID", "Delete a memory by ID")
            table.add_row("/model [MODEL]", f"Show or switch model: {self.config.model}")
            table.add_row("/clear", "Clear terminal screen")
            table.add_row("/export", "Export data to JSON")
            table.add_row("/import PATH [--config]", "Import data from an Ares JSON export")
            table.add_row("/reset", "Reset conversation context")
            table.add_row("/soul [show|edit]", "View or edit Ares' personality")
            table.add_row("/profile [show|edit]", "View or edit your profile")
            table.add_row("/context", "Show active context for this session")
            table.add_row("/skills [search|load|categories]", "List, search, and load reusable skills")
            table.add_row("/setup", "Run the onboarding wizard again")
            table.add_row("/phone status", "Check Android phone bridge pairing health")
            table.add_row("/tools [summary|details|hidden]", "Control tool activity display")
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

        elif command == "/export":
            path = export_data(
                memory_store=self.memory_store,
                conversation_store=self.conversation_store,
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
                    import_config=import_config,
                )
                if import_config:
                    self.config = load_config()
                    self.agent.set_model(self.config.model)
                self.console.print(
                    "[green]Imported "
                    f"{counts['memories']} memories, "
                    f"{counts['conversations']} conversations.[/green]"
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

        elif command == "/tools":
            if not arg:
                mode = getattr(self, "tool_output_mode", "summary")
                descriptions = {
                    "summary": "Compact activity trail after each answer.",
                    "details": "Full rendered tool result tables and panels.",
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
                    "summary": "Tool results stay hidden; Ares shows a compact activity trail.",
                    "details": "Tool result panels are shown for debugging.",
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
        tool_events = []
        tool_renderables = []

        self.console.print()
        self.console.print("[dim]Thinking...[/dim]")
        full_response = ""
        try:
            async for token in self.agent.run_stream(user_input, self.conversation_history):
                if token.startswith("[tool_start:"):
                    tool_name = self._parse_tool_start_token(token)
                    self._print_tool_start(tool_name)
                elif token.startswith("[tool:"):
                    tool_name, tool_content = self._parse_tool_token(token)
                    event = self._summarize_tool_result(tool_name, tool_content)
                    tool_events.append(event)
                    self._print_tool_done(event)
                    if getattr(self, "tool_output_mode", "summary") == "details":
                        try:
                            renderer = get_renderer(tool_name)
                            tool_renderables.append(renderer(tool_content))
                        except Exception:
                            tool_renderables.append(render_generic_tool(tool_content))
                else:
                    full_response += token
        except Exception as e:
            full_response = f"Error: {e}"

        tool_activity = self._render_tool_activity(tool_events)
        if tool_activity is not None:
            self.console.print(tool_activity)

        if getattr(self, "tool_output_mode", "summary") == "details":
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
        if self.mcp_manager is not None:
            try:
                await self.mcp_manager.start()
            except BaseException:
                _clear_current_task_cancellation()
            self.agent.refresh_tools()
        if self.cron_scheduler is not None:
            await self.cron_scheduler.start()
        self._show_banner()

        try:
            with patch_stdout():
                while True:
                    try:
                        user_input = await self._prompt()

                        if not user_input.strip():
                            continue

                        if user_input.strip().startswith("/"):
                            try:
                                should_continue = self._handle_command(user_input.strip())
                            except Exception as e:
                                self.console.print(f"[red]Command error: {e}[/red]")
                                should_continue = True
                            if not should_continue:
                                break
                            continue

                        await self._process_input(user_input)

                    except KeyboardInterrupt:
                        self.console.print("\n[dim]Interrupted. Press /exit to quit.[/dim]\n")
                        continue
                    except EOFError:
                        break
                    except asyncio.CancelledError:
                        _clear_current_task_cancellation()
                        self._reset_prompt_session()
                        self.console.print(
                            "\n[dim yellow]A background operation cancelled the prompt; recovered. Try again or use /exit to quit.[/dim yellow]\n"
                        )
                        continue
        finally:
            # Cleanup
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
            try:
                await self.agent.close()
            except Exception as exc:
                self.console.print(f"[dim yellow]Shutdown warning (agent): {exc}[/dim yellow]")
            self._cleanup_step("memory store", self.memory_store.close)
            # Generate session summary, write to JSONL
            try:
                summary = self.conversation_store.summarize_conversation(
                    self.conversation_id
                )
                if summary:
                    self.session_store.write_summary(
                        self.session_manager.get_id(), summary
                    )
            except Exception as exc:
                self.console.print(f"[dim yellow]Shutdown warning (summary): {exc}[/dim yellow]")
            self._cleanup_step(
                "end conversation",
                lambda: self.conversation_store.end_conversation(self.conversation_id),
            )
            self._cleanup_step("conversation store", self.conversation_store.close)
            self.console.print("\n[dim]Goodbye.[/dim]\n")
