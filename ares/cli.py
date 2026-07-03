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
from prompt_toolkit.patch_stdout import patch_stdout

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
from ares.onboarding import OnboardingWizard
from ares.reminders import DesktopNotifier
from ares.tools.renders import get_renderer, render_generic_tool
from ares.soul import SoulManager, SOUL_TEMPLATE
from ares.config import load_config, save_config
from ares.prompts import WELCOME_MESSAGE, FIRST_RUN_MESSAGE
from ares.llm import FREE_MODELS
from ares.skills import SkillManager
from ares.tools.mcp_client import MCPClientManager
from ares.cron import CronScheduler, CronStore
from ares.cron.toast import CronToastManager
from ares.session import SessionManager
from ares.sessions import SessionStore

# ── Styles ────────────────────────────────────────────────────
STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
})

COMPLETER = WordCompleter([
    "/help", "/memory", "/model", "/clear",
    "/forget", "/export", "/import", "/reset", "/exit",
    "/soul", "/profile", "/context",
    "/skills", "/skills search", "/skills categories", "/skills load",
    "/setup",
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


def _clear_current_task_cancellation() -> None:
    """Clear asyncio cancellation state after a cancellation was intentionally handled."""
    current_task = asyncio.current_task()
    if current_task is None or not hasattr(current_task, "uncancel"):
        return
    while current_task.cancelling():
        current_task.uncancel()



class AresCLI:
    """The main CLI application for Ares."""

    def __init__(self):
        self.console = Console()
        self.unicode_output = _supports_unicode_output()
        self.icons = {
            "fire": "🔥" if self.unicode_output else "*",
            "thinking": "🤔" if self.unicode_output else "...",
            "tool": "⚙️" if self.unicode_output else "*",
            "bot": "🤖" if self.unicode_output else "Ares",
            "bye": "👋" if self.unicode_output else "",
            "prompt": "❯ " if self.unicode_output else "> ",
            "current": " ← current" if self.unicode_output else " < current",
        }
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
        self.console.print(Panel(
            f"[bold]{self.icons['fire']} Ares[/bold]\n"
            f"[dim]v0.1.0[/dim] | "
            f"[cyan]Model: {self.config.model}[/cyan] | "
            f"[green]Memory: {memory_count} facts[/green]",
            border_style="bright_cyan",
            padding=(0, 1),
        ))
        self.console.print()

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
            table.add_row("/skills [search|load|categories]", "List, search, and load reusable skills")
            table.add_row("/setup", "Run the onboarding wizard again")
            table.add_row("/skill-name", "Load a skill directly by slash command")
            table.add_row("/exit", "Exit Ares")
            self.console.print(table)

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


        elif command == "/skills":
            if not arg:
                skills = self.skill_manager.list_all()
                table = Table(title="Skills", border_style="magenta")
                table.add_column("Name", style="cyan")
                table.add_column("Category")
                table.add_column("Description")
                for skill in skills:
                    table.add_row(skill.name, skill.category, skill.description)
                self.console.print(table if skills else "[dim]No skills installed.[/dim]")
            elif arg == "categories":
                cats = self.skill_manager.list_categories()
                for category, count in cats.items():
                    self.console.print(f"[cyan]{category}[/cyan]: {count}")
            elif arg.startswith("search "):
                query = arg.split(maxsplit=1)[1]
                skills = self.skill_manager.search(query=query)
                if not skills:
                    self.console.print("[dim]No matching skills found.[/dim]")
                else:
                    for skill in skills:
                        self.console.print(f"[cyan]{skill.name}[/cyan] [{skill.category}] — {skill.description}")
            elif arg.startswith("load "):
                name = arg.split(maxsplit=1)[1]
                skill = self.skill_manager.get_skill(name)
                if skill is None:
                    self.console.print(f"[red]Skill '{name}' not found.[/red]")
                else:
                    self.console.print(Panel(
                        Markdown(skill.content),
                        title=f"Skill: {skill.name}",
                        subtitle=skill.description,
                        border_style="magenta",
                        padding=(0, 1),
                    ))
            else:
                self.console.print("[red]Usage: /skills [search QUERY|load NAME|categories][/red]")

        elif command == "/exit":
            return False

        else:
            skill = self.skill_manager.get_skill(command[1:]) if command.startswith("/") else None
            if skill is not None:
                self.console.print(Panel(
                    Markdown(skill.content),
                    title=f"Skill: {skill.name}",
                    subtitle=skill.description,
                    border_style="magenta",
                    padding=(0, 1),
                ))
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
            self.console.print(f"\n[dim]Goodbye! {self.icons['bye']}[/dim]\n")
