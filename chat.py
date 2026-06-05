from __future__ import annotations

import argparse
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from assistant_cli.config import load_settings
from assistant_cli.runtime import FridayRuntime, format_error


console = Console()


HELP_TEXT = """/help                         show commands
/model                        show active model
/session                      show session id and JSONL path
/memory                       show memory/RAG status
/memory rebuild               rebuild LlamaIndex MiniLM index
/memory files                 list indexed memory/knowledge files
/memory search <query>        search knowledge RAG
/db search <query>            search SQLite chat history
/remember <bucket> <fact>     save fact to project/user/preferences/personal
/clear                        start a fresh session
/ping                         test NVIDIA NIM
/exit                         quit
"""


def render_header(runtime: FridayRuntime) -> None:
    console.print(
        Panel.fit(
            f"[bold]Friday CLI[/bold]\n"
            f"[dim]NVIDIA NIM streaming chat | {runtime.settings.model}\n"
            f"LlamaIndex + local MiniLM RAG | SQLite + JSONL sessions[/dim]",
            border_style="cyan",
        )
    )


def run_ping() -> int:
    try:
        runtime = FridayRuntime(load_settings())
        console.print(runtime.chat.ping())
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_once(message: str, stream: bool) -> int:
    try:
        runtime = FridayRuntime(load_settings())
        answer = runtime.answer_once(message, stream=stream)
        if not stream:
            console.print(answer)
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_rebuild() -> int:
    try:
        runtime = FridayRuntime(load_settings())
        runtime.rag.rebuild()
        console.print("[green]Memory index rebuilt.[/green]")
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_chat() -> int:
    try:
        settings = load_settings()
        runtime = FridayRuntime(settings)
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1

    session = PromptSession(history=FileHistory(str(settings.storage_dir / "friday_cli_history.txt")))
    render_header(runtime)
    console.print("[dim]Type /help for commands. Enter sends.[/dim]\n")

    while True:
        try:
            user_text = session.prompt("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            runtime.close()
            return 0

        if not user_text:
            continue
        command = user_text.lower()

        if command in {"/exit", "/quit", "exit", "quit"}:
            console.print("[dim]bye[/dim]")
            runtime.close()
            return 0
        if command == "/help":
            console.print(Panel(HELP_TEXT, title="Commands", border_style="cyan"))
            continue
        if command == "/model":
            console.print(f"[cyan]{settings.model}[/cyan]")
            continue
        if command == "/session":
            console.print(f"session: [cyan]{runtime.store.session_id}[/cyan]\njsonl: {runtime.store.jsonl_path}")
            continue
        if command == "/memory":
            status = "needs rebuild" if runtime.rag.needs_rebuild() else "ready"
            console.print(
                Panel(
                    f"memory dir: {settings.memory_dir}\n"
                    f"knowledge dir: {settings.knowledge_dir}\n"
                    f"index dir: {settings.rag_index_dir}\n"
                    f"db: {settings.db_path}\n"
                    f"status: {status}",
                    title="Memory",
                    border_style="cyan",
                )
            )
            continue
        if command == "/memory rebuild":
            runtime.rag.rebuild()
            console.print("[green]Memory index rebuilt.[/green]")
            continue
        if command == "/memory files":
            for path in runtime.rag.source_files():
                console.print(path)
            continue
        if command.startswith("/memory search "):
            query = user_text[len("/memory search ") :].strip()
            for hit in runtime.rag.search(query):
                score = f"{hit.score:.3f}" if hit.score is not None else "n/a"
                console.print(Panel(hit.text, title=f"{hit.source} | {score}", border_style="cyan"))
            continue
        if command.startswith("/db search "):
            query = user_text[len("/db search ") :].strip()
            for hit in runtime.store.search_messages(query):
                console.print(Panel(hit.content, title=f"{hit.session_id} | {hit.role} | {hit.at}", border_style="cyan"))
            continue
        if command.startswith("/remember "):
            parts = user_text.split(maxsplit=2)
            if len(parts) < 3:
                console.print("[yellow]Use: /remember <project|user|preferences|personal> <fact>[/yellow]")
                continue
            path = runtime.rag.append_fact(parts[1], parts[2])
            runtime.store.append_fact(parts[1], parts[2])
            console.print(f"[green]Remembered in {path}.[/green]")
            continue
        if command == "/clear":
            runtime.close()
            runtime = FridayRuntime(settings)
            console.print("[green]Started a fresh session.[/green]")
            continue
        if command == "/ping":
            try:
                console.print(runtime.chat.ping())
            except Exception as exc:
                console.print(f"[red]{format_error(exc)}[/red]")
            continue

        user_message_id = runtime.store.append_message("user", user_text)
        console.print(Text("friday >", style="bold cyan"))
        answer = ""
        try:
            rag_context = runtime.agentic_rag.retrieve(user_text)
            recent = runtime.store.recent_messages(settings.last_messages)
            messages = runtime.chat.build_messages(recent, rag_context)
            with Live(Markdown(""), console=console, refresh_per_second=24, transient=False) as live:
                for token in runtime.chat.stream(messages):
                    answer += token
                    live.update(Markdown(answer))
            runtime.store.append_message("assistant", answer)
            runtime._write_auto_memory(user_text, answer, user_message_id)
        except Exception as exc:
            console.print(f"[red]{format_error(exc)}[/red]")
        console.print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Friday local assistant CLI.")
    parser.add_argument("--ping", action="store_true", help="test NVIDIA NIM and exit")
    parser.add_argument("--once", help="send one message and exit")
    parser.add_argument("--no-stream", action="store_true", help="disable token streaming for --once")
    parser.add_argument("--rebuild-memory", action="store_true", help="rebuild the local LlamaIndex MiniLM index")
    args = parser.parse_args()

    if args.ping:
        return run_ping()
    if args.rebuild_memory:
        return run_rebuild()
    if args.once:
        return run_once(args.once, stream=not args.no_stream)
    return run_chat()


if __name__ == "__main__":
    sys.exit(main())
