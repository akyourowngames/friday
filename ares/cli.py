"""Terminal UI using Rich and prompt_toolkit."""

import asyncio
from contextlib import suppress
import re
import sqlite3
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.styles import Style

from rich.console import Console
from rich.panel import Panel
from rich.markdown import Markdown
from rich.live import Live
from rich.text import Text
from rich.table import Table

from ares.agent import Agent
from ares.context import ProjectContext
from ares.context_blend import build_context_prompt
from ares.conversations import ConversationStore
from ares.tools.exporter import export_data, import_data
from ares.memory import MemoryStore
from ares.profile import ProfileManager, PROFILE_TEMPLATE
from ares.reminders import DesktopNotifier, ReminderService
from ares.tools.renders import get_renderer, render_generic_tool
from ares.soul import SoulManager, SOUL_TEMPLATE
from ares.tools.tasks import TaskStore
from ares.task_executor import TaskExecutor
from ares.config import load_config, save_config
from ares.prompts import WELCOME_MESSAGE, FIRST_RUN_MESSAGE
from ares.llm import FREE_MODELS

# ── Styles ────────────────────────────────────────────────────
STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
})

COMPLETER = WordCompleter([
    "/help", "/tasks", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context",
    "/tasks auto on", "/tasks auto off", "/tasks auto list",
    "/tasks history", "/tasks executor", "/tasks detail",
    "/tasks events", "/tasks artifacts", "/tasks resume",
], ignore_case=True)


def _history_path() -> str:
    """Return an expanded prompt history path and ensure its directory exists."""
    path = Path("~/.ares_history").expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _supports_unicode_output() -> bool:
    """Return whether stdout is likely to handle emoji and symbols."""
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding


class AresCLI:
    """The main CLI application for Ares."""

    def __init__(self):
        self.console = Console()
        self.unicode_output = _supports_unicode_output()
        self.icons = {
            "fire": "🔥" if self.unicode_output else "*",
            "tasks": "📋" if self.unicode_output else "Tasks",
            "thinking": "🤔" if self.unicode_output else "...",
            "tool": "⚙️" if self.unicode_output else "*",
            "bot": "🤖" if self.unicode_output else "Ares",
            "bye": "👋" if self.unicode_output else "",
            "prompt": "❯ " if self.unicode_output else "> ",
            "current": " ← current" if self.unicode_output else " < current",
        }
        self.config = load_config()
        self.memory_store = MemoryStore()
        self.task_store = TaskStore()
        data_dir = Path(self.config.data_dir).expanduser()
        self.soul_manager = SoulManager(data_dir=data_dir, soul_path=self.config.soul_path)
        self.profile_manager = ProfileManager(data_dir=data_dir, profile_path=self.config.profile_path)
        self.project_context = ProjectContext(
            enabled=self.config.project_context_enabled,
            max_files=self.config.project_context_max_files,
        )
        self.soul_manager.ensure_exists()
        self.profile_manager.ensure_exists()
        self.conversation_store = ConversationStore()
        self.conversation_id = self.conversation_store.start_conversation()
        self.conversation_store.summarize_ended_without_summary(
            min_messages=self.config.session_summary_messages
        )
        self.agent = Agent(
            memory_store=self.memory_store,
            task_store=self.task_store,
            conversation_store=self.conversation_store,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
            config=self.config,
        )
        self.conversation_history: list[dict] = self.conversation_store.get_recent_messages(
            limit=self.config.max_context_messages
        )
        self.reminder_service = ReminderService(
            self.task_store,
            self._notify_reminder,
            poll_seconds=self.config.reminder_poll_seconds,
            notifier=DesktopNotifier(enabled=self.config.enable_desktop_notifications),
        )
        self.task_executor = TaskExecutor(
            self.task_store,
            self._execute_task_in_background,
            self._notify_auto_complete,
            poll_seconds=self.config.task_executor_poll_seconds,
            max_turns=self.config.task_executor_max_turns,
            enabled=self.config.task_executor_enabled,
        )
        self.agent.tool_executor.task_executor = self.task_executor
        self._reminder_task: asyncio.Task | None = None
        self._executor_task: asyncio.Task | None = None
        self.session = None
        if sys.stdin.isatty() and sys.stdout.isatty():
            self.session = PromptSession(
                history=FileHistory(_history_path()),
                auto_suggest=AutoSuggestFromHistory(),
                completer=COMPLETER,
                complete_while_typing=True,
                style=STYLE,
            )

    async def _prompt(self) -> str:
        """Read one prompt line from an interactive session or plain stdin."""
        if self.session is not None:
            return await self.session.prompt_async(self.icons["prompt"])
        return await asyncio.to_thread(input, self.icons["prompt"])

    def _show_banner(self):
        """Display the welcome banner."""
        memory_count = len(self.memory_store.list_all())
        pending_tasks = self.task_store.list_pending()

        self.console.print()
        self.console.print(Panel(
            f"[bold]{self.icons['fire']} Ares[/bold]\n"
            f"[dim]v0.1.0[/dim] | "
            f"[cyan]Model: {self.config.model}[/cyan] | "
            f"[green]Memory: {memory_count} facts[/green] | "
            f"[yellow]Tasks: {len(pending_tasks)} pending[/yellow]",
            border_style="bright_cyan",
            padding=(0, 1),
        ))
        self.console.print()

        # Show pending tasks if any
        if pending_tasks:
            self.console.print(f"[bold yellow]{self.icons['tasks']} Pending tasks:[/bold yellow]")
            for t in pending_tasks[:5]:
                due = f" (due: {t['due']})" if t.get("due") else ""
                self.console.print(f"  • {t['title']}{due}")
            self.console.print()

    def _notify_reminder(self, task: dict) -> None:
        """Render an in-terminal reminder notification."""
        due = f"\n[dim]Due: {task['due']}[/dim]" if task.get("due") else ""
        self.console.print()
        self.console.print(Panel(
            f"[bold yellow]Reminder[/bold yellow]\n{task['title']}{due}",
            border_style="yellow",
            padding=(0, 1),
        ))
        self.console.print()

    async def _execute_task_in_background(self, prompt: str, max_turns: int) -> dict:
        """Run an isolated agent loop for background task execution."""
        from ares.llm import LLMClient

        llm = LLMClient(
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            model=self.config.model,
        )
        try:
            from ares.tools import get_tool_definitions, ToolExecutor

            tools = get_tool_definitions()
            from ares.task_executor import ALLOWED_TOOLS
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
                        args = __import__("json").loads(fn.get("arguments") or "{}")
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

    def _notify_auto_complete(self, task_info: dict) -> None:
        """Render an in-terminal auto-completion notification."""
        status = task_info.get("status", "done")
        icon = "✅" if status == "done" else "⚠️"
        label = "Fully completed" if status == "done" else "Partially completed"
        notes = task_info.get("notes", "")
        notes_str = f"\n[dim]Notes: {notes}[/dim]" if notes else ""
        self.console.print()
        self.console.print(Panel(
            f"[bold yellow]⚡ Auto-completed: {task_info['title']}[/bold yellow]\n"
            f"{icon} {label}{notes_str}",
            border_style="yellow",
            padding=(0, 1),
        ))
        self.console.print()

    def _print_tasks(self, tasks: list[dict], title: str = "Tasks") -> None:
        if not tasks:
            self.console.print("[dim]No tasks found.[/dim]")
            return
        table = Table(title=title, border_style="yellow")
        table.add_column("ID", style="dim")
        table.add_column("Title")
        table.add_column("Priority", style="bold")
        table.add_column("Status")
        table.add_column("Due")
        table.add_column("Reminder")
        for task in tasks:
            table.add_row(
                str(task["id"]),
                task["title"],
                task.get("priority", "medium"),
                task.get("status", "pending"),
                task.get("due") or "—",
                task.get("reminder_at") or "—",
            )
        self.console.print(table)

    def _print_memories(self, memories: list[dict], title: str = "Memories") -> None:
        if not memories:
            self.console.print("[dim]No memories found.[/dim]")
            return
        table = Table(title=title, border_style="green")
        table.add_column("ID", style="dim")
        table.add_column("Category", style="cyan")
        table.add_column("Importance")
        table.add_column("Fact")
        table.add_column("Updated", style="dim")
        for memory in memories:
            table.add_row(
                str(memory["fact_id"]),
                memory.get("category", "note"),
                str(memory.get("importance", 0.5)),
                memory["fact_text"],
                memory.get("updated_at") or "—",
            )
        self.console.print(table)

    def _show_model_list(self) -> None:
        self.console.print(f"[bold]Current model:[/bold] {self.config.model}")
        self.console.print("[dim]Known free models:[/dim]")
        for model in FREE_MODELS:
            marker = self.icons["current"] if model == self.config.model else ""
            self.console.print(f"  • {model}{marker}")

    def _tool_status(self, tool_name: str) -> tuple[str, str]:
        """Return status text and border style for a running tool."""
        statuses = {
            "web_search": ("Searching web...", "bright_green"),
            "read_file": ("Reading file...", "bright_blue"),
            "search_files": ("Searching files...", "bright_yellow"),
            "list_directory": ("Listing directory...", "bright_magenta"),
        }
        return statuses.get(tool_name, ("Running tool...", "dim"))

    def _parse_tool_token(self, token: str) -> tuple[str, str]:
        """Parse [tool:name:content] tokens with a fallback for legacy tokens."""
        inner = token[6:-1]
        parts = inner.split(":", 1)
        if len(parts) == 2 and re.match(r"^[a-z][a-z0-9_]*$", parts[0]):
            return parts[0] or "unknown", parts[1]
        return "unknown", inner

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
            table = Table(title="Commands", border_style="cyan")
            table.add_column("Command", style="cyan")
            table.add_column("Description")
            table.add_row("/help", "Show available commands")
            table.add_row("/tasks [all|search|complete|cancel]", "Review and manage tasks")
            table.add_row("/tasks auto on ID", "Mark task for auto-execution")
            table.add_row("/tasks auto off ID", "Remove task from auto-execution")
            table.add_row("/tasks auto list", "Show auto-executable tasks")
            table.add_row("/tasks history", "Show recently auto-executed tasks")
            table.add_row("/tasks executor", "Show executor status and stats")
            table.add_row("/tasks detail ID", "Show full task detail with plan and report")
            table.add_row("/tasks events ID", "Show execution log for a task")
            table.add_row("/tasks artifacts ID", "Show files created/modified by a task")
            table.add_row("/tasks resume ID", "Resume a failed task from where it left off")
            table.add_row("/memory [search|edit|delete]", "Review and manage memories")
            table.add_row("/forget ID", "Delete a memory by ID")
            table.add_row("/model [MODEL]", f"Show or switch model: {self.config.model}")
            table.add_row("/clear", "Clear terminal screen")
            table.add_row("/export", "Export data to JSON")
            table.add_row("/import PATH [--config]", "Import data from an Ares JSON export")
            table.add_row("/reset", "Reset conversation context")
            table.add_row("/soul [show|edit]", "View or edit Ares' personality")
            table.add_row("/profile [show|edit]", "View or edit your profile")
            table.add_row("/context", "Show active context for this session")
            table.add_row("/exit", "Exit Ares")
            self.console.print(table)

        elif command == "/tasks":
            if not arg:
                self._print_tasks(self.task_store.list_pending(), "Pending Tasks")
            elif arg == "all":
                self._print_tasks(self.task_store.list_all(include_done=True), "All Tasks")
            elif arg.startswith("search "):
                self._print_tasks(self.task_store.search(arg[7:].strip(), include_done=True), "Task Search")
            elif arg.startswith("complete "):
                task_id = int(arg.split(maxsplit=1)[1])
                if self.task_store.complete(task_id):
                    self.console.print(f"[green]Completed task #{task_id}.[/green]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found or is not pending.[/red]")
            elif arg.startswith("cancel "):
                task_id = int(arg.split(maxsplit=1)[1])
                if self.task_store.cancel(task_id):
                    self.console.print(f"[yellow]Cancelled task #{task_id}.[/yellow]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg.startswith("auto on "):
                task_id = int(arg.split(maxsplit=2)[2])
                if self.task_store.update(task_id, auto_executable="yes"):
                    self.console.print(f"[green]Task #{task_id} marked for auto-execution.[/green]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg.startswith("auto off "):
                task_id = int(arg.split(maxsplit=2)[2])
                if self.task_store.update(task_id, auto_executable="no"):
                    self.console.print(f"[yellow]Task #{task_id} removed from auto-execution.[/yellow]")
                else:
                    self.console.print(f"[red]Task #{task_id} was not found.[/red]")
            elif arg == "auto list":
                tasks = self.task_store.get_auto_executable()
                if not tasks:
                    self.console.print("[dim]No tasks marked for auto-execution.[/dim]")
                else:
                    self._print_tasks(tasks, "Auto-Executable Tasks")
            elif arg == "executor":
                stats = self.task_executor.stats
                panel = Panel(
                    f"[bold]Task Executor[/bold]\n"
                    f"[cyan]State:[/cyan] {stats['state']}\n"
                    f"[cyan]Enabled:[/cyan] {'yes' if stats['enabled'] else 'no'}\n"
                    f"[cyan]Poll interval:[/cyan] {stats['poll_seconds']}s\n"
                    f"[cyan]Tasks completed:[/cyan] {stats['tasks_completed']}\n"
                    f"[cyan]Tasks failed:[/cyan] {stats['tasks_failed']}\n"
                    + (f"[cyan]Current task:[/cyan] #{stats['current_task_id']} \"{stats['current_task_title']}\"\n" if stats['current_task_id'] else "")
                    + (f"[cyan]Last error:[/cyan] [red]{stats['last_error']}[/red]\n" if stats['last_error'] else "")
                    + (f"[cyan]Started at:[/cyan] {stats['started_at']}\n" if stats['started_at'] else ""),
                    border_style="yellow",
                    padding=(0, 1),
                )
                self.console.print(panel)
            elif arg == "history":
                tasks = self.task_store.get_recently_executed()
                if not tasks:
                    self.console.print("[dim]No tasks have been auto-executed yet.[/dim]")
                else:
                    table = Table(title="Execution History", border_style="yellow")
                    table.add_column("ID", style="dim")
                    table.add_column("Title")
                    table.add_column("Status")
                    table.add_column("Notes")
                    table.add_column("Executed At", style="dim")
                    for t in tasks:
                        table.add_row(
                            str(t["id"]),
                            t["title"],
                            t.get("status", "pending"),
                            (t.get("execution_notes") or "")[:60],
                            t.get("executed_at") or "—",
                        )
                    self.console.print(table)
            elif arg.startswith("detail "):
                task_id = int(arg.split(maxsplit=1)[1])
                task = self.task_store.get(task_id)
                if not task:
                    self.console.print(f"[red]Task #{task_id} not found.[/red]")
                else:
                    state = task.get("state") or task.get("status", "pending")
                    parts = [
                        f"[bold]Task #{task_id}[/bold]: {task['title']}",
                        f"[cyan]State:[/cyan] {state}",
                        f"[cyan]Priority:[/cyan] {task.get('priority', 'medium')}",
                    ]
                    if task.get("due"):
                        parts.append(f"[cyan]Due:[/cyan] {task['due']}")
                    if task.get("auto_executable") == "yes":
                        parts.append(f"[cyan]Auto-executable:[/cyan] yes")
                    if task.get("plan"):
                        import json
                        try:
                            plan = json.loads(task["plan"])
                            steps_str = " → ".join(f"{s['step']}. {s['title']}" for s in plan)
                            parts.append(f"[cyan]Plan ({len(plan)} steps):[/cyan] {steps_str}")
                            if task.get("current_step"):
                                parts.append(f"[cyan]Current step:[/cyan] {task['current_step']}/{task.get('total_steps', '?')}")
                            if task.get("completed_steps"):
                                completed = json.loads(task["completed_steps"])
                                parts.append(f"[cyan]Completed steps:[/cyan] {completed}")
                        except Exception:
                            pass
                    if task.get("attempt"):
                        parts.append(f"[cyan]Attempt:[/cyan] {task['attempt']}/{task.get('max_attempts', 3)}")
                    if task.get("retry_reason"):
                        parts.append(f"[cyan]Retry reason:[/cyan] {task['retry_reason']}")
                    if task.get("completion_report"):
                        import json
                        try:
                            report = json.loads(task["completion_report"])
                            parts.append(f"\n[bold green]Completion Report[/bold green]")
                            if report.get("summary"):
                                parts.append(report["summary"])
                            if report.get("key_results"):
                                for r in report["key_results"]:
                                    parts.append(f"  • {r}")
                            if report.get("files_created"):
                                parts.append(f"  Files created: {', '.join(report['files_created'])}")
                        except Exception:
                            pass
                    self.console.print(Panel("\n".join(parts), border_style="yellow", padding=(0, 1)))
            elif arg.startswith("events "):
                task_id = int(arg.split(maxsplit=1)[1])
                events = self.task_store.get_events(task_id)
                if not events:
                    self.console.print(f"[dim]No events for task #{task_id}.[/dim]")
                else:
                    table = Table(title=f"Events — Task #{task_id}", border_style="yellow")
                    table.add_column("Step", style="dim", width=6)
                    table.add_column("Level", width=8)
                    table.add_column("Message")
                    table.add_column("Time", style="dim")
                    for ev in events:
                        level_color = {"success": "green", "error": "red", "warning": "yellow"}.get(ev.get("level", ""), "cyan")
                        table.add_row(
                            str(ev.get("step") or "—"),
                            f"[{level_color}]{ev.get('level', 'info')}[/{level_color}]",
                            ev.get("message", ""),
                            ev.get("created_at") or "—",
                        )
                    self.console.print(table)
            elif arg.startswith("artifacts "):
                task_id = int(arg.split(maxsplit=1)[1])
                artifacts = self.task_store.get_artifacts(task_id)
                if not artifacts:
                    self.console.print(f"[dim]No artifacts for task #{task_id}.[/dim]")
                else:
                    table = Table(title=f"Artifacts — Task #{task_id}", border_style="yellow")
                    table.add_column("Step", style="dim", width=6)
                    table.add_column("Type", width=14)
                    table.add_column("Path")
                    table.add_column("Size", style="dim")
                    table.add_column("Description", style="dim")
                    for art in artifacts:
                        table.add_row(
                            str(art.get("step") or "—"),
                            art.get("artifact_type", "unknown"),
                            art.get("path", ""),
                            art.get("size_human", "—"),
                            (art.get("description") or "")[:50],
                        )
                    self.console.print(table)
            elif arg.startswith("resume "):
                task_id = int(arg.split(maxsplit=1)[1])
                task = self.task_store.get(task_id)
                if not task:
                    self.console.print(f"[red]Task #{task_id} not found.[/red]")
                else:
                    state = task.get("state") or task.get("status", "pending")
                    if state != "failed":
                        self.console.print(f"[red]Task #{task_id} is not in failed state (current: {state}).[/red]")
                    else:
                        self.task_executor.enqueue_resume(task_id)
                        self.console.print(f"[green]Task #{task_id} queued for resume on next poll cycle.[/green]")
            else:
                self.console.print("[red]Usage: /tasks [all|search QUERY|complete ID|cancel ID|auto on|off|list|history|executor|detail ID|events ID|artifacts ID|resume ID][/red]")

        elif command == "/memory":
            if not arg:
                self._print_memories(self.memory_store.get_recent(limit=10), "Recent Memories")
            elif arg.startswith("search "):
                self._print_memories(self.memory_store.search(arg[7:].strip(), limit=10), "Memory Search")
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
                self.console.print("[red]Usage: /memory [search QUERY|edit ID NEW_TEXT|delete ID][/red]")

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

        elif command == "/export":
            path = export_data(
                memory_store=self.memory_store,
                task_store=self.task_store,
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
                    task_store=self.task_store,
                    conversation_store=self.conversation_store,
                    import_config=import_config,
                )
                if import_config:
                    self.config = load_config()
                    self.agent.set_model(self.config.model)
                self.console.print(
                    "[green]Imported "
                    f"{counts['memories']} memories, {counts['tasks']} tasks, "
                    f"{counts['conversations']} conversations.[/green]"
                )

        elif command == "/reset":
            self.conversation_history = []
            self.console.print("[dim]Conversation reset. Memory preserved.[/dim]")

        elif command == "/soul":
            if not arg or arg == "show":
                content = self.soul_manager.read()
                if content:
                    self.console.print(Panel(
                        Markdown(content),
                        title="Soul - Ares Personality",
                        border_style="bright_magenta",
                        padding=(0, 1),
                    ))
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
                    self.console.print(Panel(
                        Markdown(content),
                        title="Profile - User Identity",
                        border_style="bright_green",
                        padding=(0, 1),
                    ))
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
                tasks=self.task_store.list_pending(),
                token_budget=self.config.context_token_budget,
            )
            if context_str:
                self.console.print(Panel(
                    Markdown(context_str),
                    title="Active Context",
                    border_style="bright_cyan",
                    padding=(0, 1),
                ))
            else:
                self.console.print("[dim]No context active.[/dim]")

        elif command == "/exit":
            return False

        else:
            self.console.print(f"[red]Unknown command: {command}. Type /help for available commands.[/red]")

        return True

    async def _process_input(self, user_input: str):
        """Process a user message through the agent and display response."""
        tool_renderables = []

        self.console.print()

        with Live(console=self.console, refresh_per_second=10, transient=True) as live:
            live.update(Panel(
                Text(f"{self.icons['thinking']} Thinking...", style="bold italic dim"),
                border_style="dim blue",
            ))

            full_response = ""
            try:
                async for token in self.agent.run_stream(user_input, self.conversation_history):
                    if token.startswith("[tool:"):
                        tool_name, tool_content = self._parse_tool_token(token)
                        status, color = self._tool_status(tool_name)
                        live.update(Panel(
                            Text(status, style=f"bold {color}"),
                            border_style=color,
                        ))
                        try:
                            renderer = get_renderer(tool_name)
                            tool_renderables.append(renderer(tool_content))
                        except Exception:
                            tool_renderables.append(render_generic_tool(tool_content))
                    else:
                        full_response += token
                        if full_response.strip():
                            live.update(Markdown(full_response))
            except Exception as e:
                full_response = f"Error: {e}"

        # Show rendered tool results before the final assistant response.
        for renderable in tool_renderables:
            self.console.print(renderable)

        # Show final response
        if full_response.strip():
            self.console.print(Panel(
                Markdown(full_response),
                title=f"{self.icons['bot']} Ares",
                border_style="bright_blue",
                padding=(0, 1),
            ))

        # Update conversation history with full message exchange (including tool calls)
        self.conversation_history.append({"role": "user", "content": user_input})

        # Use the agent's internal messages which include tool calls and results
        if self.agent.last_messages:
            # Extract only the new messages (skip system prompt and prior history)
            built_messages = self.agent.last_messages
            # Find messages after the last user message (the one we just sent)
            user_msg_idx = len(built_messages) - 1
            for i in range(len(built_messages) - 1, -1, -1):
                if built_messages[i].get("role") == "user" and built_messages[i].get("content") == user_input:
                    user_msg_idx = i
                    break
            # Save assistant + tool messages that followed the user message
            for msg in built_messages[user_msg_idx + 1:]:
                clean_msg = {"role": msg["role"]}
                if msg.get("content"):
                    clean_msg["content"] = msg["content"]
                if msg.get("tool_calls"):
                    clean_msg["tool_calls"] = msg["tool_calls"]
                if msg.get("tool_call_id"):
                    clean_msg["tool_call_id"] = msg["tool_call_id"]
                self.conversation_history.append(clean_msg)
        else:
            # Fallback: save just the text response
            self.conversation_history.append({"role": "assistant", "content": full_response})

        self.conversation_store.add_exchange(self.conversation_id, user_input, full_response)

        # Trim conversation history
        max_msgs = self.config.max_context_messages
        if len(self.conversation_history) > max_msgs:
            self.conversation_history = self.conversation_history[-max_msgs:]

        self.console.print()

    async def run(self):
        """Main CLI loop."""
        self._reminder_task = asyncio.create_task(self.reminder_service.run())
        self._executor_task = asyncio.create_task(self.task_executor.run())
        self._show_banner()

        try:
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
        finally:
            if self._reminder_task is not None:
                self._reminder_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reminder_task
            if self._executor_task is not None:
                self._executor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._executor_task

            # Cleanup
            try:
                await self.agent.close()
            except Exception as exc:
                self.console.print(f"[dim yellow]Shutdown warning (agent): {exc}[/dim yellow]")
            self._cleanup_step("memory store", self.memory_store.close)
            self._cleanup_step("task store", self.task_store.close)
            self._cleanup_step(
                "end conversation",
                lambda: self.conversation_store.end_conversation(self.conversation_id),
            )
            self._cleanup_step(
                "summarize conversation",
                lambda: self.conversation_store.summarize_conversation(self.conversation_id),
            )
            self._cleanup_step("conversation store", self.conversation_store.close)
            self.console.print(f"\n[dim]Goodbye! {self.icons['bye']}[/dim]\n")
