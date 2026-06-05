from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from openai import APIConnectionError, APIStatusError, AuthenticationError, RateLimitError
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from assistant_cli.config import load_settings
from assistant_cli.langchain_memory import JsonlChatMessageHistory
from assistant_cli.memory import MemoryManager
from assistant_cli.nvidia_chat import NvidiaChat
from assistant_cli.tools import ToolRegistry, build_default_registry


console = Console(legacy_windows=False)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


configure_stdio()


HELP_TEXT = """/help       show commands
/model      show active model
/session    show JSONL session file
/memory     show memory status
/memory rebuild
/memory files
/memory search <query>
/remember <bucket> <fact>
/tools      list registered tools
/tool <name> <json|key=value args>
/voice      show voice status
/voice on   enable voice for this session
/voice off  disable voice for this session
/clear      reset chat memory
/ping       test NVIDIA connection
/exit       quit
"""


def render_header(model: str, voice_mode: bool = False) -> None:
    extra = "\n[dim]Sarvam voice mode | Ctrl+Space toggles listening[/dim]" if voice_mode else ""
    console.print(
        Panel.fit(
            f"[bold]Friday CLI[/bold]\n[dim]NVIDIA streaming chat | {model}[/dim]{extra}",
            border_style="cyan",
        )
    )


def print_error(exc: Exception) -> None:
    if isinstance(exc, AuthenticationError):
        console.print("[red]Authentication failed.[/red] Check NVIDIA_API_KEY in .env.")
    elif isinstance(exc, RateLimitError):
        console.print("[red]Rate limited by NVIDIA.[/red] Try again shortly or switch models.")
    elif isinstance(exc, APIStatusError):
        console.print(f"[red]NVIDIA API error {exc.status_code}:[/red] {exc.message}")
    elif isinstance(exc, APIConnectionError):
        console.print("[red]Connection failed.[/red] Check internet access and NVIDIA_BASE_URL.")
    else:
        console.print(f"[red]Error:[/red] {exc}")


def pop_voice_chunks(buffer: str, force: bool = False) -> tuple[list[str], str]:
    text = str(buffer or "")
    chunks: list[str] = []
    while True:
        cut = -1
        for index, char in enumerate(text):
            if char in ".?!\n" and index >= 8:
                cut = index + 1
                break
        if cut == -1 and len(text) >= 180:
            cut = text.rfind(" ", 0, 180)
            if cut < 80:
                cut = -1
        if cut == -1:
            break
        chunk = text[:cut].strip()
        text = text[cut:].strip()
        if chunk:
            chunks.append(chunk)
    if force and text.strip():
        chunks.append(text.strip())
        text = ""
    return chunks, text


def parse_tool_args(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Tool JSON args must be an object.")
        return data

    args: dict[str, object] = {}
    free_parts: list[str] = []
    for part in shlex.split(text):
        if "=" not in part:
            free_parts.append(part)
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            continue
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    if free_parts and not args:
        args["query"] = " ".join(free_parts)
    return args


def render_tools(registry: ToolRegistry) -> None:
    lines: list[str] = []
    for spec in registry.specs():
        example = f"\n  example: /tool {spec.examples[0]}" if spec.examples else ""
        lines.append(f"[bold]{spec.name}[/bold]\n  {spec.description}{example}")
    console.print(Panel("\n\n".join(lines), title=f"Tools ({len(lines)})", border_style="cyan"))


def render_tool_result(result) -> None:
    style = "green" if result.ok else "red"
    title = f"{result.tool} | {result.latency_ms} ms"
    console.print(Panel(result.text or json.dumps(result.data, indent=2), title=title, border_style=style))


def run_tool(name: str, raw_args: str = "{}") -> int:
    registry: ToolRegistry | None = None
    try:
        settings = load_settings()
        registry = build_default_registry(settings)
        args = parse_tool_args(raw_args)
        result = registry.execute(name, args)
        render_tool_result(result)
        return 0 if result.ok else 1
    except Exception as exc:
        print_error(exc)
        return 1
    finally:
        if registry is not None:
            registry.close()


def run_ping(chat: NvidiaChat) -> int:
    try:
        console.print(chat.ping())
        return 0
    except Exception as exc:
        print_error(exc)
        return 1


def run_once(user_text: str, voice_mode: bool = False) -> int:
    try:
        settings = load_settings()
        chat = NvidiaChat(settings)
        memory = MemoryManager(settings)
        from assistant_cli.voice import SarvamVoice

        voice = SarvamVoice(settings, enabled=voice_mode)
        history = JsonlChatMessageHistory(settings.session_dir)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    try:
        memory_context = memory.prompt_context()
    except Exception as exc:
        print_error(exc)
        memory_context = ""

    history.add_user_message(user_text)
    answer = ""
    try:
        for token in chat.stream_reply(memory_context, history.recent_openai_messages(settings.last_messages)):
            print(token, end="", flush=True)
            answer += token
        print()
    except Exception as exc:
        print_error(exc)
        return 1

    history.add_ai_message(answer)
    memory.capture_user_facts(user_text, answer)
    voice.speak(answer, wait=True)
    return 0


def run_voice_test(text: str) -> int:
    try:
        settings = load_settings()
        from assistant_cli.voice import SarvamVoice

        voice = SarvamVoice(settings, enabled=True)
        result = voice.synthesize(text)
        voice.play(result.path, wait=True)
        console.print(f"[green]Voice OK[/green] {result.path} ({result.byte_count} bytes, {result.speaker})")
        return 0
    except Exception as exc:
        print_error(exc)
        return 1


def run_transcribe_test(path: str) -> int:
    try:
        settings = load_settings()
        from assistant_cli.listen import SarvamTranscriber

        result = SarvamTranscriber(settings).transcribe_file(Path(path))
        console.print(f"[green]STT OK[/green] {result.text}")
        console.print(f"[dim]{result.path} ({result.byte_count} bytes, {result.language_code})[/dim]")
        return 0
    except Exception as exc:
        print_error(exc)
        return 1


def run_voice_roundtrip_test(text: str) -> int:
    try:
        settings = load_settings()
        from assistant_cli.listen import SarvamTranscriber
        from assistant_cli.voice import SarvamVoice

        voice = SarvamVoice(settings, enabled=True)
        audio = voice.synthesize(text)
        transcript = SarvamTranscriber(settings).transcribe_file(audio.path)
        console.print(f"[green]Roundtrip OK[/green] {transcript.text}")
        console.print(f"[dim]tts={audio.path} ({audio.byte_count} bytes) stt={transcript.language_code}[/dim]")
        return 0
    except Exception as exc:
        print_error(exc)
        return 1


def run_chat(voice_mode: bool = False) -> int:
    try:
        settings = load_settings()
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from rich.live import Live
    from rich.markdown import Markdown

    from assistant_cli.listen import SpaceHoldToTalk
    from assistant_cli.voice import SarvamVoice

    chat = NvidiaChat(settings)
    memory = MemoryManager(settings)
    voice = SarvamVoice(settings, enabled=voice_mode)
    tools = build_default_registry(settings)
    history = JsonlChatMessageHistory(settings.session_dir)
    session = PromptSession(history=FileHistory(".friday_history"))
    render_header(settings.model, voice_mode=voice.enabled)
    console.print("[dim]Type /help for commands.[/dim]\n")

    voice_input: SpaceHoldToTalk | None = None

    def voice_status(message: str) -> None:
        console.print(f"\n[dim]{message}[/dim]")

    def submit_voice_text(text: str) -> None:
        console.print(f"\n[cyan]voice >[/cyan] {text}")
        try:
            import keyboard

            keyboard.write(text)
            keyboard.press_and_release("enter")
        except Exception as exc:
            console.print(f"[red]Voice transcript ready but could not submit it: {exc}[/red]")

    def start_voice_input() -> bool:
        nonlocal voice_input
        if voice_input is not None:
            return True
        voice_input = SpaceHoldToTalk(settings, submit_voice_text, voice_status)
        started = voice_input.start()
        if not started:
            voice_input = None
        return started

    def stop_voice_input() -> None:
        nonlocal voice_input
        if voice_input is not None:
            voice_input.stop()
            voice_input = None

    if voice.enabled:
        if start_voice_input():
            console.print("[dim]Ctrl+Space starts listening; Ctrl+Space again transcribes and sends.[/dim]\n")

    while True:
        try:
            user_text = session.prompt("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            stop_voice_input()
            return 0

        if not user_text:
            continue

        command = user_text.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            console.print("[dim]bye[/dim]")
            stop_voice_input()
            return 0
        if command == "/help":
            console.print(Panel(HELP_TEXT, title="Commands", border_style="cyan"))
            continue
        if command == "/model":
            console.print(f"[cyan]{settings.model}[/cyan]")
            continue
        if command == "/session":
            console.print(f"session: [cyan]{history.session_id}[/cyan]\njsonl: {history.path}")
            continue
        if command == "/memory":
            status = "needs rebuild" if memory.needs_rebuild() else "ready"
            console.print(
                Panel(
                    f"Directory: {memory.memory_dir}\n"
                    f"Index: {memory.index_dir}\n"
                    f"Sessions: {history.session_dir}\n"
                    f"Current JSONL: {history.path}\n"
                    f"Last messages sent: {settings.last_messages}\n"
                    f"Embedding model: {settings.embed_model}\n"
                    f"Status: {status}",
                    title="Memory",
                    border_style="cyan",
                )
            )
            continue
        if command == "/voice":
            status = voice.status()
            status["input"] = "on" if voice_input is not None else "off"
            status["hotkey"] = settings.voice_hotkey
            status["stt_model"] = settings.stt_model
            status["stt_language"] = settings.stt_language
            if not voice.enabled:
                status["tip"] = "Run python chat.py --voice to start in voice mode."
            console.print(
                Panel(
                    "\n".join(f"{key}: {value}" for key, value in status.items() if value),
                    title="Voice",
                    border_style="cyan",
                )
            )
            continue
        if command == "/voice on":
            voice.set_enabled(True)
            start_voice_input()
            console.print("[green]Voice enabled for this session.[/green]")
            continue
        if command == "/voice off":
            voice.set_enabled(False)
            stop_voice_input()
            console.print("[green]Voice disabled for this session.[/green]")
            continue
        if command == "/memory rebuild":
            try:
                memory.rebuild()
                console.print("[green]Memory index rebuilt.[/green]")
            except Exception as exc:
                print_error(exc)
            continue
        if command == "/memory files":
            for path in memory.files():
                console.print(path)
            continue
        if command.startswith("/memory search "):
            query = user_text[len("/memory search ") :].strip()
            try:
                hits = memory.search(query)
            except Exception as exc:
                print_error(exc)
                continue
            if not hits:
                console.print("[yellow]No memory hits.[/yellow]")
                continue
            for hit in hits:
                score = f"{hit.score:.3f}" if hit.score is not None else "n/a"
                console.print(Panel(hit.text, title=f"{hit.source} | {score}", border_style="cyan"))
            continue
        if command.startswith("/remember "):
            parts = user_text.split(maxsplit=2)
            if len(parts) < 3:
                console.print("[yellow]Use: /remember <project|user|preferences|personal> <fact>[/yellow]")
                continue
            try:
                path = memory.append(parts[1], parts[2])
                console.print(f"[green]Remembered in {path}.[/green]")
            except Exception as exc:
                print_error(exc)
            continue
        if command == "/tools":
            render_tools(tools)
            continue
        if command.startswith("/tool "):
            parts = user_text.split(maxsplit=2)
            if len(parts) < 2:
                console.print("[yellow]Use: /tool <name> <json|key=value args>[/yellow]")
                continue
            if not settings.tools_enabled:
                console.print("[yellow]Tools are disabled. Set FRIDAY_TOOLS_ENABLED=true in .env.[/yellow]")
                continue
            name = parts[1].strip()
            raw_args = parts[2] if len(parts) > 2 else ""
            try:
                result = tools.execute(name, parse_tool_args(raw_args))
            except Exception as exc:
                print_error(exc)
                continue
            render_tool_result(result)
            continue
        if command == "/clear":
            chat.reset()
            history = JsonlChatMessageHistory(settings.session_dir)
            console.print("[green]Started a fresh JSONL session.[/green]")
            continue
        if command == "/ping":
            run_ping(chat)
            continue

        try:
            memory_context = memory.prompt_context()
        except Exception as exc:
            print_error(exc)
            memory_context = ""

        chat.add_user_message(user_text)
        history.add_user_message(user_text)
        answer = ""
        voice_buffer = ""
        console.print(Text("friday >", style="bold cyan"))
        try:
            with Live(Markdown(""), console=console, refresh_per_second=24, transient=False) as live:
                conversation = history.recent_openai_messages(settings.last_messages)
                for token in chat.stream_reply(memory_context, conversation):
                    answer += token
                    if voice.enabled:
                        voice_buffer += token
                        chunks, voice_buffer = pop_voice_chunks(voice_buffer)
                        for chunk in chunks:
                            voice.speak(chunk, wait=False)
                    live.update(Markdown(answer))
        except Exception as exc:
            print_error(exc)
            chat.messages.pop()
            continue

        chat.add_assistant_message(answer)
        history.add_ai_message(answer)
        memory.capture_user_facts(user_text, answer)
        if voice.enabled:
            for chunk in pop_voice_chunks(voice_buffer, force=True)[0]:
                voice.speak(chunk, wait=False)
        console.print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast NVIDIA-powered streaming assistant CLI.")
    parser.add_argument("--ping", action="store_true", help="test the NVIDIA chat endpoint and exit")
    parser.add_argument("--once", help="send one message and exit")
    parser.add_argument("--tool", help="run a registered tool by name and exit")
    parser.add_argument("--tool-args", default="{}", help="tool args as JSON or key=value pairs")
    parser.add_argument("--voice", action="store_true", help="enable Sarvam voice mode for this chat session")
    parser.add_argument("--voice-test", nargs="?", const="Friday voice is online.", help="synthesize and play Sarvam voice")
    parser.add_argument("--transcribe-test", help="transcribe an audio file with Sarvam STT")
    parser.add_argument(
        "--voice-roundtrip-test",
        nargs="?",
        const="hello friday voice transcription test",
        help="synthesize a line with Sarvam TTS, then transcribe it with Sarvam STT",
    )
    args = parser.parse_args()

    if args.ping:
        try:
            return run_ping(NvidiaChat(load_settings()))
        except Exception as exc:
            print_error(exc)
            return 1
    if args.once:
        return run_once(args.once, voice_mode=args.voice)
    if args.tool:
        return run_tool(args.tool, args.tool_args)
    if args.voice_test:
        return run_voice_test(args.voice_test)
    if args.transcribe_test:
        return run_transcribe_test(args.transcribe_test)
    if args.voice_roundtrip_test:
        return run_voice_roundtrip_test(args.voice_roundtrip_test)

    return run_chat(voice_mode=args.voice)


if __name__ == "__main__":
    sys.exit(main())
