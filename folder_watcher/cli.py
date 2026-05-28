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


def _start_daily_thread(config, pipeline):
    from maintenance.config import load_config as load_maintenance_config
    from maintenance.engine import MaintenanceEngine
    from maintenance.scheduler_thread import DailyScheduler
    from maintenance.steps import register_default_steps

    maint_config = load_maintenance_config(str(config.repo_root))
    if not maint_config.enabled:
        return None

    def fire(triggered_by: str) -> None:
        engine = MaintenanceEngine(maint_config)
        register_default_steps(engine)
        engine.run(triggered_by=triggered_by, dry_run=False, force=False, context={"folder_pipeline": pipeline})

    scheduler = DailyScheduler(
        callback=fire,
        cutoff_time=maint_config.cutoff_time,
        check_interval_seconds=settings.scheduler_check_interval_seconds,
    )
    scheduler.start()
    return scheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KING folder watcher service")
    parser.add_argument("command", choices=("run", "scan", "stats"), nargs="?", default="run")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    config, index, pipeline, bus, app = build_runtime(args.config)
    daily_scheduler = None
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
        daily_scheduler = _start_daily_thread(config, pipeline)
        host = args.host or config.api_host
        port = args.port or config.api_port
        print(f"KING folder watcher serving {config.watch_path} on http://{host}:{port}")
        uvicorn.run(app, host=host, port=port)
        return 0
    finally:
        if daily_scheduler is not None:
            daily_scheduler.stop()
        time.sleep(0)
        index.close()


if __name__ == "__main__":
    raise SystemExit(main())
