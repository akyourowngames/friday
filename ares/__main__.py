"""Entry point: python -m ares"""

import argparse
import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

from ares.cli import AresCLI


async def _run_cli() -> None:
    cli = AresCLI()
    await cli.run()


async def _run_voice() -> None:
    from ares.voice.agent import run_voice_agent

    await run_voice_agent()


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


async def _run_server(host: str, port: int) -> None:
    from ares.server import run_server

    await run_server(host=host, port=port)


def main():
    parser = argparse.ArgumentParser(description="Ares personal AI assistant")
    parser.add_argument("--server", action="store_true", help="Run the desktop WebSocket server")
    parser.add_argument("--voice", action="store_true", help="Run continuous LiveKit voice mode")
    parser.add_argument("--host", default="127.0.0.1", help="Server host for --server")
    parser.add_argument("--port", type=int, default=8765, help="Server port for --server")
    args = parser.parse_args()

    try:
        if args.server:
            _run_coro(_run_server(args.host, args.port))
        elif args.voice:
            _run_coro(_run_voice())
        else:
            _run_coro(_run_cli())
    except asyncio.CancelledError:
        return


if __name__ == "__main__":
    main()
