"""Entry point: python -m ares"""

import argparse
import asyncio
import getpass
import os
import threading
from collections.abc import Coroutine
from typing import Any

from ares.cli import AresCLI
from ares.config import load_config, save_config


async def _run_cli() -> None:
    cli = AresCLI()
    await cli.run()


async def _run_voice(
    voice_name: str | None = None,
    stt_backend: str | None = None,
    tts_backend: str | None = None,
    barge_in: bool | None = None,
) -> None:
    from ares.voice.agent import run_voice_agent

    await run_voice_agent(voice_name, stt_backend=stt_backend, tts_backend=tts_backend, barge_in=barge_in)


def _run_coro(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine from sync code, even if this thread already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=False)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


async def _run_server(
    host: str,
    port: int,
    *,
    watcher_host: str | None = None,
    watcher_port: int | None = None,
    workspace_host: str | None = None,
    workspace_port: int | None = None,
) -> None:
    from ares.server import run_server

    await run_server(
        host=host,
        port=port,
        watcher_dashboard_host=watcher_host,
        watcher_dashboard_port=watcher_port,
        workspace_host=workspace_host,
        workspace_port=workspace_port,
    )


async def _run_telegram() -> None:
    from ares.channels.telegram import run_telegram_channel

    await run_telegram_channel()


def _run_telephony_webhook(host: str, port: int) -> None:
    from ares.telephony.webhook import run_twilio_webhook_server

    run_twilio_webhook_server(host=host, port=port)


async def _run_telephony_media_gateway(host: str, port: int) -> None:
    from ares.telephony.media_gateway import run_twilio_media_gateway

    await run_twilio_media_gateway(host=host, port=port)


def _authorize_telegram_chat(chat_id: int, *, revoke: bool = False) -> None:
    """Update the strict local allowlist without ever printing the bot token."""
    config = load_config()
    allowed = {int(value) for value in config.telegram.allowed_chat_ids}
    if revoke:
        allowed.discard(chat_id)
    else:
        allowed.add(chat_id)
        config.telegram.enabled = True
    config.telegram.allowed_chat_ids = sorted(allowed)
    save_config(config)
    if revoke:
        print(f"Telegram chat {chat_id} was removed from Ares' allowlist.")
    else:
        print(
            f"Telegram chat {chat_id} is authorized. Start or restart Ares with "
            "`python -m ares --all` to connect it."
        )


def _setup_telegram() -> None:
    """Store local Telegram settings without echoing a bot token to the terminal."""
    config = load_config()
    token = getpass.getpass("Telegram bot token (leave blank to keep the current/environment token): ").strip()
    if token:
        config.telegram.bot_token = token
    if not config.telegram.bot_token and not os.environ.get("ARES_TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("A Telegram bot token is required. Create one with @BotFather first.")
    config.telegram.enabled = True
    save_config(config)
    print(
        "Telegram is enabled locally. Start `python -m ares --all`, message /start to your bot, "
        "then run `python -m ares --telegram-authorize CHAT_ID`."
    )


def main():
    parser = argparse.ArgumentParser(description="Ares personal AI assistant")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run the unified Ares runtime: power workspace, API, agent tools, MCP, Telegram, and watchers",
    )
    parser.add_argument("--server", action="store_true", help="Legacy alias for the unified --all runtime")
    parser.add_argument("--telegram", action="store_true", help="Legacy alias for the unified --all runtime")
    parser.add_argument("--watcher", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--watcher-host", default=None, help="Override the unified watcher dashboard bind host")
    parser.add_argument("--watcher-port", type=int, default=None, help="Override the unified watcher dashboard port")
    parser.add_argument("--workspace-host", default=None, help="Override the separate power workspace bind host")
    parser.add_argument("--workspace-port", type=int, default=None, help="Override the separate power workspace port")
    parser.add_argument("--telegram-setup", action="store_true", help="Securely save Telegram channel setup")
    parser.add_argument("--telegram-authorize", type=int, metavar="CHAT_ID", help="Allow one Telegram chat ID")
    parser.add_argument("--telegram-revoke", type=int, metavar="CHAT_ID", help="Remove one Telegram chat ID")
    parser.add_argument("--voice", action="store_true", help="Run continuous voice mode (always listening)")
    parser.add_argument(
        "--telephony-webhook",
        action="store_true",
        help="Run the signed Twilio Voice webhook server (place behind public HTTPS)",
    )
    parser.add_argument("--telephony-webhook-host", default="127.0.0.1", help="Bind host for --telephony-webhook")
    parser.add_argument("--telephony-webhook-port", type=int, default=8080, help="Bind port for --telephony-webhook")
    parser.add_argument(
        "--telephony-media-gateway",
        action="store_true",
        help="Run the Twilio Media Streams gateway (publish it through public WSS)",
    )
    parser.add_argument("--telephony-media-host", default="127.0.0.1", help="Bind host for --telephony-media-gateway")
    parser.add_argument("--telephony-media-port", type=int, default=8767, help="Bind port for --telephony-media-gateway")
    parser.add_argument("--voice-name", default=None, help="Edge TTS voice for --voice")
    parser.add_argument("--stt-backend", choices=["auto", "whisper", "sarvam"], default=None, help="STT backend for --voice")
    parser.add_argument("--tts-backend", choices=["auto", "edge", "sarvam"], default=None, help="TTS backend for --voice")
    parser.add_argument(
        "--barge-in",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable microphone interruption of TTS (defaults to the saved voice setting)",
    )
    parser.add_argument("--tts", choices=["edge"], default=None, help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1", help="Server host for --server")
    parser.add_argument("--port", type=int, default=8765, help="Server port for --server")
    args = parser.parse_args()

    try:
        if args.telegram_setup:
            _setup_telegram()
        elif args.telegram_authorize is not None:
            _authorize_telegram_chat(args.telegram_authorize)
        elif args.telegram_revoke is not None:
            _authorize_telegram_chat(args.telegram_revoke, revoke=True)
        elif args.all or args.server or args.telegram or args.watcher:
            if args.voice or args.telephony_webhook or args.telephony_media_gateway:
                parser.error("Voice and telephony gateway processes cannot share the unified runtime process.")
            _run_coro(_run_server(
                args.host,
                args.port,
                watcher_host=args.watcher_host,
                watcher_port=args.watcher_port,
                workspace_host=args.workspace_host,
                workspace_port=args.workspace_port,
            ))
        elif args.voice:
            _run_coro(_run_voice(
                voice_name=args.voice_name,
                stt_backend=args.stt_backend,
                tts_backend=args.tts_backend,
                barge_in=args.barge_in,
            ))
        elif args.telephony_webhook:
            _run_telephony_webhook(args.telephony_webhook_host, args.telephony_webhook_port)
        elif args.telephony_media_gateway:
            _run_coro(_run_telephony_media_gateway(args.telephony_media_host, args.telephony_media_port))
        else:
            _run_coro(_run_cli())
    except asyncio.CancelledError:
        return


if __name__ == "__main__":
    main()
