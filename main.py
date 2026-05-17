import json
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agent.core import Agent
from config import settings
from tools.registry import get_tools
from voice.listener import Listener
from voice.speaker import speak

console = Console()


def print_welcome():
    title = Panel.fit(
        "[bold cyan] KING [/bold cyan]  [dim]— your AI assistant[/dim]",
        border_style="cyan",
        padding=(1, 4),
    )
    console.print(title)
    console.print("  [dim]/debug[/dim]  [dim]/tools[/dim]  [dim]/model <name>[/dim]  [dim]/voice[/dim]  [dim]/new[/dim]  [dim]/exit[/dim]")
    console.print()


def cmd_debug():
    settings.debug = not settings.debug
    console.print(f"[green]Debug mode: {'ON' if settings.debug else 'OFF'}[/green]")


def cmd_tools():
    tools = get_tools()
    if not tools:
        console.print("[yellow]No tools registered yet.[/yellow]")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Tool")
    table.add_column("Description")
    for t in tools:
        table.add_row(f"[cyan]{t['name']}[/cyan]", t["description"])
    console.print(table)


def cmd_model(args):
    if not args:
        console.print(f"[yellow]Current model: {settings.model_name}[/yellow]")
        return
    settings.model_name = args
    console.print(f"[green]Model set to: {settings.model_name}[/green]")


def voice_loop(agent: Agent):
    listener = Listener()
    console.print("[cyan]Voice mode active. Speak or type /voice to exit.[/cyan]")
    while settings.voice_enabled:
        console.print("[dim]Listening...[/dim]")
        transcript = listener.listen()
        if transcript:
            console.print(f"[dim]You: {transcript}[/dim]")
            response = agent.process(transcript)
            if response and response.strip():
                speak(response)
        else:
            threading.Event().wait(0.1)


def main():
    print_welcome()
    agent = Agent()

    while True:
        try:
            raw = console.input("[bold yellow]> [/bold yellow] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[cyan]Goodbye![/cyan]")
            sys.exit(0)

        if not raw:
            continue

        if raw.startswith("/"):
            cmd = raw[1:].split()
            base = cmd[0].lower() if cmd else ""

            if base in ("exit", "quit"):
                console.print("[cyan]Goodbye![/cyan]")
                break
            elif base == "debug":
                cmd_debug()
            elif base == "tools":
                cmd_tools()
            elif base == "model":
                cmd_model(" ".join(cmd[1:]) if len(cmd) > 1 else None)
            elif base == "new":
                agent = Agent()
                console.print("[green]New conversation started.[/green]")
            elif base == "voice":
                settings.voice_enabled = not settings.voice_enabled
                if settings.voice_enabled:
                    voice_loop(agent)
                else:
                    console.print("[cyan]Voice mode off.[/cyan]")
            elif base == "playlist":
                playlist_path = Path("storage/playlist.json")
                if not playlist_path.exists():
                    console.print("[yellow]No playlist yet. Play some music first![/yellow]")
                else:
                    try:
                        items = json.loads(playlist_path.read_text(encoding="utf-8"))
                        if not items:
                            console.print("[yellow]Playlist is empty[/yellow]")
                        else:
                            console.print("[bold cyan]Your Playlist:[/bold cyan]")
                            for i, item in enumerate(items, 1):
                                fav = "⭐ " if item.get("favorite") else "   "
                                views = f"{item.get('view_count', 0):,}" if item.get("view_count") else ""
                                title = item.get("title", "?")
                                channel = item.get("channel", "?")
                                console.print(f"  {fav}{i}. {title} — {channel} [dim]({views} views)[/dim]")
                    except Exception as e:
                        console.print(f"[red]Could not load playlist: {e}[/red]")
            elif base == "help":
                console.print("[bold]Commands:[/bold] /debug  /tools  /model <name>  /voice  /playlist  /new  /help  /exit")
            else:
                console.print(f"[red]Unknown command: /{base}. Try /help[/red]")
            continue

        agent.process(raw)


if __name__ == "__main__":
    main()
