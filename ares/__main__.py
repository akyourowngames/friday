"""Entry point: python -m ares"""

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any

from ares.cli import AresCLI


async def _run_cli() -> None:
    cli = AresCLI()
    await cli.run()


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


def main():
    _run_coro(_run_cli())


if __name__ == "__main__":
    main()
