from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from assistant_cli.config import load_settings
from assistant_cli.listen import SpaceHoldToTalk
from assistant_cli.runtime import FridayRuntime, format_error


console = Console()


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


configure_stdio()


HELP_TEXT = """/help                         show commands
/model                        show active model
/session                      show session id and JSONL path
/memory                       show memory/RAG status
/memory rebuild               rebuild LlamaIndex MiniLM index
/memory files                 list indexed memory/knowledge files
/memory search <query>        search knowledge RAG
/db search <query>            search SQLite chat history
/remember <bucket> <fact>     save fact to project/user/preferences/personal
/voice                        show Sarvam voice status
/voice on                     auto-speak Friday replies
/voice off                    stop auto-speaking replies
/voice test                   synthesize and play a short test line
/voice speaker <name>         change Bulbul voice, default priya
/voice input on               enable Ctrl+Space voice input
/voice input off              disable Ctrl+Space voice input
/clear                        start a fresh session
/ping                         test NVIDIA NIM
/exit                         quit
"""


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


def run_once(message: str, stream: bool, voice_mode: bool = False) -> int:
    try:
        runtime = FridayRuntime(load_settings())
        runtime.voice.set_enabled(voice_mode)
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


def run_voice_test(text: str) -> int:
    try:
        runtime = FridayRuntime(load_settings())
        result = runtime.voice_test(text)
        console.print(f"[green]Voice OK[/green] {result.path} ({result.byte_count} bytes, {result.speaker})")
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_transcribe_test(path: str) -> int:
    try:
        runtime = FridayRuntime(load_settings())
        result = runtime.transcribe_file(Path(path))
        console.print(f"[green]STT OK[/green] {result.text}")
        console.print(f"[dim]{result.path} ({result.byte_count} bytes, {result.language_code})[/dim]")
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_voice_roundtrip_test(text: str) -> int:
    try:
        runtime = FridayRuntime(load_settings())
        audio, transcript = runtime.voice_roundtrip_test(text)
        console.print(f"[green]Roundtrip OK[/green] {transcript.text}")
        console.print(f"[dim]tts={audio.path} ({audio.byte_count} bytes) stt={transcript.language_code}[/dim]")
        runtime.close()
        return 0
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1


def run_chat(voice_mode: bool = False) -> int:
    try:
        settings = load_settings()
        runtime = FridayRuntime(settings)
        runtime.voice.set_enabled(voice_mode)
    except Exception as exc:
        console.print(f"[red]{format_error(exc)}[/red]")
        return 1

    session = PromptSession(history=FileHistory(str(settings.storage_dir / "friday_cli_history.txt")))
    render_header(runtime)
    console.print("[dim]Type /help for commands. Enter sends.[/dim]\n")

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

    if runtime.voice.enabled and start_voice_input():
        console.print("[dim]Ctrl+Space starts listening; Ctrl+Space again transcribes and sends.[/dim]\n")

    while True:
        try:
            user_text = session.prompt("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            stop_voice_input()
            runtime.close()
            return 0

        if not user_text:
            continue
        command = user_text.lower()

        if command in {"/exit", "/quit", "exit", "quit"}:
            console.print("[dim]bye[/dim]")
            stop_voice_input()
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
        if command == "/voice":
            status = runtime.voice.status()
            status["input_hotkey"] = settings.voice_hotkey
            status["input_enabled"] = "yes" if voice_input is not None else "no"
            status["stt_model"] = settings.stt_model
            status["stt_language"] = settings.stt_language
            console.print(
                Panel(
                    "\n".join(f"{key}: {value}" for key, value in status.items() if value),
                    title="Sarvam Voice",
                    border_style="cyan",
                )
            )
            continue
        if command == "/voice on":
            runtime.voice.set_enabled(True)
            start_voice_input()
            console.print("[green]Voice auto-speak is on.[/green]")
            continue
        if command == "/voice off":
            runtime.voice.set_enabled(False)
            console.print("[green]Voice auto-speak is off.[/green]")
            continue
        if command == "/voice input on":
            if start_voice_input():
                console.print("[green]Ctrl+Space voice input is on.[/green]")
            continue
        if command == "/voice input off":
            stop_voice_input()
            console.print("[green]Ctrl+Space voice input is off.[/green]")
            continue
        if command == "/voice test":
            try:
                result = runtime.voice_test()
                console.print(f"[green]Voice OK[/green] {result.path} ({result.byte_count} bytes, {result.speaker})")
            except Exception as exc:
                console.print(f"[red]{format_error(exc)}[/red]")
            continue
        if command.startswith("/voice speaker "):
            speaker = user_text[len("/voice speaker ") :].strip()
            try:
                runtime.voice.set_speaker(speaker)
                console.print(f"[green]Voice speaker set to {runtime.voice.speaker}.[/green]")
            except Exception as exc:
                console.print(f"[red]{format_error(exc)}[/red]")
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
        voice_buffer = ""
        try:
            rag_context = runtime.agentic_rag.retrieve(user_text)
            recent = runtime.store.recent_messages(settings.last_messages)
            messages = runtime.chat.build_messages(recent, rag_context)
            with Live(Markdown(""), console=console, refresh_per_second=24, transient=False) as live:
                for token in runtime.chat.stream(messages):
                    answer += token
                    if runtime.voice.enabled:
                        voice_buffer += token
                        chunks, voice_buffer = pop_voice_chunks(voice_buffer)
                        for chunk in chunks:
                            runtime.speak_answer(chunk, wait=False)
                    live.update(Markdown(answer))
            runtime.store.append_message("assistant", answer)
            runtime._write_auto_memory(user_text, answer, user_message_id)
            if runtime.voice.enabled:
                for chunk in pop_voice_chunks(voice_buffer, force=True)[0]:
                    runtime.speak_answer(chunk, wait=False)
        except Exception as exc:
            console.print(f"[red]{format_error(exc)}[/red]")
        console.print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Friday local assistant CLI.")
    parser.add_argument("--ping", action="store_true", help="test NVIDIA NIM and exit")
    parser.add_argument("--once", help="send one message and exit")
    parser.add_argument("--no-stream", action="store_true", help="disable token streaming for --once")
    parser.add_argument("--voice", action="store_true", help="enable Sarvam voice mode for this chat session")
    parser.add_argument("--rebuild-memory", action="store_true", help="rebuild the local LlamaIndex MiniLM index")
    parser.add_argument("--voice-test", nargs="?", const="Friday voice is online.", help="synthesize and play Sarvam voice")
    parser.add_argument("--transcribe-test", help="transcribe a WAV/MP3/etc file with Sarvam STT")
    parser.add_argument(
        "--voice-roundtrip-test",
        nargs="?",
        const="hello friday voice transcription test",
        help="synthesize a line with Sarvam TTS, then transcribe it with Sarvam STT",
    )
    args = parser.parse_args()

    if args.ping:
        return run_ping()
    if args.rebuild_memory:
        return run_rebuild()
    if args.voice_test:
        return run_voice_test(args.voice_test)
    if args.transcribe_test:
        return run_transcribe_test(args.transcribe_test)
    if args.voice_roundtrip_test:
        return run_voice_roundtrip_test(args.voice_roundtrip_test)
    if args.once:
        return run_once(args.once, stream=not args.no_stream, voice_mode=args.voice)
    return run_chat(voice_mode=args.voice)


if __name__ == "__main__":
    sys.exit(main())
