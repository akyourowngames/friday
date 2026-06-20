"""Entry point for PyInstaller frozen build — starts the Ares WebSocket server."""

import asyncio
import sys


def main():
    host = "127.0.0.1"
    port = 8765

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--host" and i < len(sys.argv) - 1:
            host = sys.argv[i + 1]
        elif arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    from ares.server import run_server
    asyncio.run(run_server(host=host, port=port))


if __name__ == "__main__":
    main()
