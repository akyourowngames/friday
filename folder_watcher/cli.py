from __future__ import annotations

import argparse
import time

import uvicorn

from config import settings

from .api import create_app
from .bus import EventBus
from .configuration import load_config
from .index import FolderIndex
from .ingest import IngestPipeline
from .watcher import DebouncedWatcher


def build_runtime(config_path: str | None = None):
    config = load_config(".", config_path or settings.folder_watcher_config_file)
    config.watch_path.mkdir(parents=True, exist_ok=True)
    index = FolderIndex(config.database_path)
    pipeline = IngestPipeline(config, index)
    bus = EventBus()
    app = create_app(config, index, bus)
    return config, index, pipeline, bus, app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KING folder watcher service")
    parser.add_argument("command", choices=("run", "scan", "stats"), nargs="?", default="run")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    config, index, pipeline, bus, app = build_runtime(args.config)
    try:
        if args.command == "scan":
            result = pipeline.scan_once()
            print(result)
            return 0
        if args.command == "stats":
            print(index.stats())
            return 0

        if config.scan_on_start:
            pipeline.scan_once()
        watcher = DebouncedWatcher(config, pipeline, bus)
        watcher.start()
        host = args.host or config.api_host
        port = args.port or config.api_port
        print(f"KING folder watcher serving {config.watch_path} on http://{host}:{port}")
        uvicorn.run(app, host=host, port=port)
        return 0
    finally:
        time.sleep(0)
        index.close()


if __name__ == "__main__":
    raise SystemExit(main())
