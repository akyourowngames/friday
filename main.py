import json
import argparse
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

Panel = None
Table = None


class _PlainConsole:
    def print(self, *values, end="\n", **_kwargs):
        print(" ".join(str(value) for value in values), end=end)

    def input(self, prompt: str = "") -> str:
        return input(prompt)

    def print_json(self, text: str):
        print(text)


console = _PlainConsole()
_gesture_detector = None
_gesture_enabled = False
DEFAULT_API_BASE = "http://127.0.0.1:8000"


def _enable_rich_console():
    global console, Panel, Table
    from rich.console import Console
    from rich.panel import Panel as RichPanel
    from rich.table import Table as RichTable

    console = Console()
    Panel = RichPanel
    Table = RichTable


def _configure_output_encoding():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def print_welcome():
    if not Panel:
        console.print("KING - your AI assistant")
        console.print("  /debug  /tools  /model <name>  /memory [recall|extract|sync]  /remember <fact>  /forget <fact>  /voice  /new  /exit")
        console.print()
        return
    title = Panel.fit(
        "[bold cyan] KING [/bold cyan]  [dim]— your AI assistant[/dim]",
        border_style="cyan",
        padding=(1, 4),
    )
    console.print(title)
    console.print("  [dim]/debug[/dim]  [dim]/tools[/dim]  [dim]/model <name>[/dim]  [dim]/memory [recall|extract|sync][/dim]  [dim]/remember <fact>[/dim]  [dim]/forget <fact>[/dim]  [dim]/voice[/dim]  [dim]/new[/dim]  [dim]/exit[/dim]")
    console.print()


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="KING CLI, API client, or local API server.")
    parser.add_argument("message", nargs="*", help="Optional one-shot message for local or --api mode.")
    parser.add_argument("--message", dest="message_text", default="", help="Run one local or --api message and exit.")
    parser.add_argument(
        "--api",
        nargs="?",
        const=DEFAULT_API_BASE,
        default="",
        help="Connect this CLI to a running KING API. Optional value: base URL.",
    )
    parser.add_argument("--server", action="store_true", help="Run the KING FastAPI server instead of the local CLI agent.")
    parser.add_argument("--host", default="127.0.0.1", help="API server host for --server mode.")
    parser.add_argument("--port", type=int, default=8000, help="API server port for --server mode.")
    return parser.parse_args(argv)


def _message_from_args(args) -> str:
    explicit = str(getattr(args, "message_text", "") or "").strip()
    if explicit:
        return explicit
    return " ".join(getattr(args, "message", []) or []).strip()


def _normalize_api_base(base_url: str) -> str:
    base = str(base_url or DEFAULT_API_BASE).strip()
    if not base:
        base = DEFAULT_API_BASE
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    return base.rstrip("/")


def _looks_like_api_base(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if any(ch.isspace() for ch in text):
        return False
    has_scheme = "://" in text
    parsed = urlparse(text if has_scheme else "http://" + text)
    if parsed.hostname and parsed.port:
        return True
    if has_scheme and parsed.scheme in ("http", "https") and parsed.hostname:
        return True
    host = str(parsed.hostname or "")
    if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return True
    if "." in host:
        return True
    return False


def _resolve_api_cli_inputs(api_arg: str, message_parts: list[str]) -> tuple[str, str]:
    api_text = str(api_arg or "").strip()
    if api_text and not message_parts and not _looks_like_api_base(api_text):
        return DEFAULT_API_BASE, api_text
    return _normalize_api_base(api_text or DEFAULT_API_BASE), " ".join(message_parts).strip()


def _iter_sse_events(response):
    source = response.iter_lines() if hasattr(response, "iter_lines") else response
    for raw_line in source:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _print_table(columns: list[str], rows: list[list[str]], right_aligned: set[int] | None = None):
    right_aligned = right_aligned or set()
    if Table:
        table = Table(show_header=True, header_style="bold cyan")
        for index, column in enumerate(columns):
            table.add_column(column, justify="right" if index in right_aligned else "left")
        for row in rows:
            table.add_row(*[str(value) for value in row])
        console.print(table)
        return
    widths = [len(str(column)) for column in columns]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))
    header = " | ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns))
    console.print(header)
    console.print("-+-".join("-" * width for width in widths))
    for row in rows:
        cells = []
        for index, value in enumerate(row):
            text = str(value)
            cells.append(text.rjust(widths[index]) if index in right_aligned else text.ljust(widths[index]))
        console.print(" | ".join(cells))


def _print_folder_panel(payload: dict):
    answer = str(payload.get("answer") or "").strip()
    if answer:
        console.print(answer)
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    if stats:
        console.print(
            f"Folder watcher: {stats.get('active_files', payload.get('count', '?'))} active file(s), "
            f"{stats.get('total_size_bytes', '?')} bytes"
        )
        details = stats.get("by_extension_details") if isinstance(stats.get("by_extension_details"), dict) else {}
        if details:
            rows = []
            for extension, item in list(details.items())[:10]:
                if isinstance(item, dict):
                    rows.append([str(extension), str(item.get("count", "")), str(item.get("size_bytes", ""))])
            _print_table(["Type", "Files", "Bytes"], rows, right_aligned={1, 2})
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    if files:
        rows = []
        for item in files[:10]:
            if isinstance(item, dict):
                rows.append([
                    str(item.get("filename") or item.get("id") or ""),
                    str(item.get("extension") or item.get("mime_type") or ""),
                    str(item.get("size_bytes") or ""),
                ])
        _print_table(["File", "Type", "Bytes"], rows, right_aligned={2})


def _api_folder_request(base_url: str, action: str = "ask", query: str = "", **extra) -> dict:
    payload = {"action": action, "query": query}
    payload.update(extra)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _normalize_api_base(base_url) + "/folder-watcher",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    return data if isinstance(data, dict) else {"data": data}


def _api_chat_once(base_url: str, message: str, session_id: str | None = None, show_panels: bool = False) -> tuple[str, str | None]:
    text_parts = []
    next_session_id = session_id
    payload = {"message": message, "session_id": session_id, "tts": False}
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _normalize_api_base(base_url) + "/chat/jarvis/stream",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        for event in _iter_sse_events(response):
            if event.get("session_id"):
                next_session_id = str(event["session_id"])
            if event.get("chunk"):
                chunk = str(event["chunk"])
                text_parts.append(chunk)
                console.print(chunk, end="")
            if show_panels and event.get("folder_watcher_result"):
                console.print()
                _print_folder_panel(event["folder_watcher_result"])
            if show_panels and event.get("navigator_result"):
                console.print()
                console.print("Navigator result received from API.")
            if show_panels and event.get("vision_result"):
                console.print()
                console.print(str(event["vision_result"].get("description") or ""))
            if event.get("error"):
                console.print(f"\n{event['error']}")
        if text_parts:
            console.print()
    return "".join(text_parts).strip(), next_session_id


def _handle_api_command(base_url: str, raw: str) -> str:
    if raw == "/help":
        console.print("Type naturally. Slash commands are optional: /health  /folder <question>  /folder-stats  /exit")
        return "handled"
    if raw == "/health":
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=10) as response:
                console.print_json(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:
            console.print(f"API health failed: {exc.__class__.__name__}")
        return "handled"
    if raw == "/folder-stats":
        try:
            _print_folder_panel(_api_folder_request(base_url, action="stats"))
        except Exception as exc:
            console.print(f"Folder watcher request failed: {exc.__class__.__name__}")
        return "handled"
    if raw.startswith("/folder "):
        try:
            _print_folder_panel(_api_folder_request(base_url, action="ask", query=raw[len("/folder "):].strip()))
        except Exception as exc:
            console.print(f"Folder watcher request failed: {exc.__class__.__name__}")
        return "handled"
    if raw.startswith("/"):
        console.print(f"Unknown API command: {raw}. Try /help")
        return "handled"
    return "unhandled"


def run_api_client(base_url: str, initial_message: str = ""):
    base_url = _normalize_api_base(base_url)
    console.print("+-----------------------+")
    console.print("| KING API CLI          |")
    console.print(f"| {base_url.ljust(21)} |")
    console.print("+-----------------------+")
    console.print("Connected mode: type naturally like the frontend. Slash commands are optional.")
    console.print("  examples: how many python files are there? | what's in this folder?")
    console.print("  commands: /health  /folder <question>  /folder-stats  /exit")
    session_id = None
    if initial_message:
        if _handle_api_command(base_url, initial_message) == "handled":
            return
        _, session_id = _api_chat_once(base_url, initial_message, session_id)
        return
    while True:
        try:
            raw = console.input("api> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nGoodbye!")
            return
        if not raw:
            continue
        if raw in ("/exit", "/quit"):
            console.print("Goodbye!")
            return
        if raw == "/help":
            _handle_api_command(base_url, raw)
            continue
        if raw.startswith("/"):
            _handle_api_command(base_url, raw)
            continue
        try:
            _, session_id = _api_chat_once(base_url, raw, session_id)
        except Exception as exc:
            console.print(f"API chat failed: {exc.__class__.__name__}")


def run_api_server(host: str, port: int):
    try:
        import uvicorn
    except ImportError:
        console.print("uvicorn is not installed, so API server mode cannot start.")
        return
    console.print(f"Starting KING API on http://{host}:{port}")
    uvicorn.run("api_server:app", host=host, port=port)


def cmd_debug():
    from config import settings

    settings.debug = not settings.debug
    console.print(f"[green]Debug mode: {'ON' if settings.debug else 'OFF'}[/green]")


def cmd_tools():
    from tools.registry import get_tools

    tools = get_tools()
    if not tools:
        console.print("[yellow]No tools registered yet.[/yellow]")
        return
    rows = [[t["name"], t["description"]] for t in tools]
    _print_table(["Tool", "Description"], rows)


def cmd_model(args):
    from config import settings

    if not args:
        console.print(f"[yellow]Current model: {settings.model_name}[/yellow]")
        return
    settings.model_name = args
    console.print(f"[green]Model set to: {settings.model_name}[/green]")


def cmd_memory(agent, args: str = ""):
    text = str(args or "").strip()
    command, _, rest = text.partition(" ")
    command = command.casefold()
    if command in {"recall", "search"}:
        query = rest.strip()
        if not query:
            console.print("[yellow]Usage: /memory recall <query>[/yellow]")
            return
        hits = agent.brain.recall_unified(query, k=10)
        if not hits:
            console.print("[yellow]No matching memories.[/yellow]")
            return
        rows = []
        for idx, item in enumerate(hits, 1):
            rows.append([str(idx), item.get("text", ""), str(item.get("score", ""))])
        _print_table(["#", "Fact", "Score"], rows, right_aligned={0, 2})
        return
    if command in {"extract", "ingest"}:
        from memory.worker import ingest_user_files

        result = ingest_user_files(agent.brain)
        console.print(
            "[cyan]Memory extract:[/cyan] "
            + f"status={result.get('status')} files={result.get('user_files_found', 0)} "
            + f"facts={result.get('facts_ingested', result.get('ingested', 0))}"
        )
        return
    if command in {"sync", "graph"}:
        report = agent.brain.maintain(rebuild=True, backup=True)
        agent.brain._sync_obsidian_graph()
        status = agent.brain.obsidian_graph_status()
        console.print(
            "[cyan]Memory sync:[/cyan] "
            + f"tier={report.get('after', {}).get('tier')} graph_rebuilt={report.get('graph_rebuilt')} "
            + f"vault={status.get('status')}"
        )
        return
    try:
        limit = int(text) if text else 25
    except ValueError:
        limit = 25
    memories = agent.brain.list_memories(limit)
    assessment = agent.brain.system_assessment()
    graph = assessment.get("graph", {})
    console.print(
        f"[cyan]Memory:[/cyan] {assessment['entry_count']} stored, "
        f"{assessment['indexed_count']} indexed, index {assessment['index_state']}, "
        f"graph {graph.get('active_edge_count', 0)} active edges"
    )
    if not memories:
        console.print("[yellow]No memories stored.[/yellow]")
        return
    rows = []
    for item in memories:
        when = " ".join(part for part in (item.get("date", ""), item.get("time", "")) if part)
        rows.append([str(item["index"]), item["text"], when])
    _print_table(["#", "Fact", "When"], rows, right_aligned={0})


def cmd_remember(agent, fact: str):
    result = agent.brain.remember(fact)
    if result["stored"]:
        console.print(f"[green]Remembered:[/green] {result['text']}")
    else:
        console.print(f"[yellow]Memory unchanged:[/yellow] {result['text'] or result['reason']}")


def cmd_forget(agent, query: str):
    result = agent.brain.forget(query)
    status = result.get("status")
    if status == "removed":
        for fact in result.get("removed", []):
            console.print(f"[green]Forgot:[/green] {fact}")
        return
    if status == "ambiguous":
        console.print("[yellow]That matched more than one memory. Use a more exact phrase.[/yellow]")
        for candidate in result.get("candidates", []):
            console.print(f"  - {candidate}")
        return
    console.print(f"[yellow]No memory removed:[/yellow] {result.get('reason', 'not found')}")


def voice_loop(agent):
    from config import settings
    from voice.listener import Listener
    from voice.speaker import speak

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


def gesture_loop(agent):
    from gesture.controller import execute as gesture_execute

    global _gesture_detector, _gesture_enabled
    mode = _gesture_detector.mode
    console.print(f"[cyan]Gesture control active. Mode: {mode}. ESC on camera or /gesture stop to exit.[/cyan]")
    if mode == "nav":
        console.print("  [point=down] [thumbs_down=up] [pinch=enter] [peace=open]")
        console.print("  [fist=delete] [thumbs_up=parent] [open_palm=refresh]")
    while _gesture_enabled:
        action = _gesture_detector.get_action(timeout=0.1)
        if action:
            result = gesture_execute(action)
            if result:
                console.print(f"[green]{result}[/green]")


def main(argv: list[str] | None = None):
    _configure_output_encoding()
    args = _parse_args(argv)
    if args.server:
        run_api_server(args.host, args.port)
        return
    initial_message = _message_from_args(args)
    if args.api:
        base_url, initial_message = _resolve_api_cli_inputs(args.api, [initial_message] if initial_message else [])
        run_api_client(base_url, initial_message)
        return
    _enable_rich_console()
    if initial_message:
        from agent.core import Agent

        Agent().process(initial_message)
        return

    print_welcome()
    agent = None

    def ensure_agent():
        nonlocal agent
        if agent is None:
            from agent.core import Agent

            agent = Agent()
        return agent

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
            elif base == "memory":
                cmd_memory(ensure_agent(), " ".join(cmd[1:]) if len(cmd) > 1 else "")
            elif base == "remember":
                cmd_remember(ensure_agent(), " ".join(cmd[1:]) if len(cmd) > 1 else "")
            elif base == "forget":
                cmd_forget(ensure_agent(), " ".join(cmd[1:]) if len(cmd) > 1 else "")
            elif base == "new":
                from agent.core import Agent

                agent = Agent()
                console.print("[green]New conversation started.[/green]")
            elif base == "voice":
                from config import settings

                settings.voice_enabled = not settings.voice_enabled
                if settings.voice_enabled:
                    voice_loop(ensure_agent())
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
            elif base == "gesture":
                from gesture.collector import collect as gesture_collect
                from gesture.detector import GestureDetector
                from gesture.trainer import train as gesture_train

                global _gesture_detector, _gesture_enabled
                sub = cmd[1].lower() if len(cmd) > 1 else "status"
                if sub == "status":
                    if _gesture_enabled:
                        mode = _gesture_detector.mode if _gesture_detector else "?"
                        console.print(f"[green]Gesture control: ON[/green] — mode: {mode}")
                    else:
                        console.print("[yellow]Gesture control: OFF[/yellow]")
                    console.print("  /gesture nav      — file list navigation mode (default)")
                    console.print("  /gesture mouse    — virtual mouse cursor mode")
                    console.print("  /gesture collect  — record training data")
                    console.print("  /gesture train    — train classifier")
                    console.print("  /gesture start    — begin live detection")
                    console.print("  /gesture stop     — stop live detection")
                elif sub == "collect":
                    gesture_collect()
                elif sub == "train":
                    gesture_train()
                elif sub in ("start", "nav", "mouse"):
                    if _gesture_detector is None:
                        _gesture_detector = GestureDetector()
                    _gesture_detector.mode = "mouse" if sub == "mouse" else "nav"
                    _gesture_detector.start()
                    _gesture_enabled = True
                    gesture_thread = threading.Thread(target=gesture_loop, args=(ensure_agent(),), daemon=True)
                    gesture_thread.start()
                elif sub == "stop":
                    _gesture_enabled = False
                    if _gesture_detector:
                        _gesture_detector.stop()
                    console.print("[cyan]Gesture control off.[/cyan]")
                else:
                    console.print(f"[red]Unknown gesture subcommand: {sub}. Try /gesture[/red]")
            elif base == "help":
                console.print("[bold]Commands:[/bold] /debug  /tools  /model <name>  /memory [limit|recall <query>|extract|sync]  /remember <fact>  /forget <fact>  /voice  /gesture  /playlist  /new  /help  /exit")
            else:
                console.print(f"[red]Unknown command: /{base}. Try /help[/red]")
            continue

        ensure_agent().process(raw)


if __name__ == "__main__":
    main()
