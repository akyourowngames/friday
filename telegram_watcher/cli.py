from __future__ import annotations

import argparse
import json
import threading

from config import settings

from .api import create_app
from .configuration import load_config
from .service import TelegramWatcherService


def build_service(config_path: str | None = None) -> TelegramWatcherService:
    config = load_config(".", config_path or settings.telegram_watcher_config_file)
    return TelegramWatcherService(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KING Telegram watcher service")
    parser.add_argument("command", choices=("run", "poll", "status", "verify", "push-check"), nargs="?", default="run")
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    service = build_service(args.config)
    if args.command == "status":
        config = service.config
        print(
            json.dumps(
                {
                    "config_path": str(config.config_path),
                    "token_present": bool(config.token),
                    "authorized_user_ids_configured": bool(config.authorized_user_ids),
                    "authorized_chat_ids_configured": bool(config.authorized_chat_ids),
                    "startup_notice_enabled": config.startup_notice_enabled,
                    "api_host": config.api_host,
                    "api_port": config.api_port,
                    "service_base_url": config.service_base_url,
                    "main_cli_autostart": config.main_cli_autostart,
                    "cli_bridge_enabled": config.cli_bridge_enabled,
                    "cli_forward_actions": sorted(config.cli_forward_actions),
                    "allowed_zones": [
                        {"name": zone.name, "path": str(zone.path), "enabled": zone.enabled}
                        for zone in config.zones
                    ],
                    "folder_watcher_base_url": config.folder_watcher_base_url,
                    "state_path": str(config.state_path),
                    "session_log_path": str(config.session_log_path),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.command == "verify":
        result = service.verify_runtime()
        print(json.dumps(result, indent=2, sort_keys=True))
        if not result.get("token_present") or not result.get("telegram_api_ok"):
            return 2
        if not result.get("authorized_user_ids_configured") and not result.get("authorized_chat_ids_configured"):
            return 2
        return 0
    if args.command == "push-check":
        print(json.dumps(service.check_push_notifications(), indent=2, sort_keys=True))
        return 0
    if not service.config.token:
        print("Telegram bot token is not configured. Set " + service.config.token_env + ".")
        return 2
    if not service.config.authorized_user_ids and not service.config.authorized_chat_ids:
        print(
            "Authorized Telegram ids are not configured. Set "
            + service.config.authorized_user_ids_env
            + " or "
            + service.config.authorized_chat_ids_env
            + "."
        )
        return 2
    if args.command == "poll":
        service.poll_forever()
        return 0

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed, so the Telegram watcher endpoint service cannot start.")
        return 2

    app = create_app(service)
    polling_thread = threading.Thread(target=service.poll_forever, daemon=True)
    polling_thread.start()
    daily_scheduler = _start_daily_thread(service)
    host = args.host or service.config.api_host
    port = args.port or service.config.api_port
    print("KING Telegram watcher serving endpoints on http://" + host + ":" + str(port))
    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        if daily_scheduler is not None:
            daily_scheduler.stop()
    return 0


def _start_daily_thread(service: TelegramWatcherService):
    from maintenance.config import load_config as load_maintenance_config
    from maintenance.engine import MaintenanceEngine
    from maintenance.scheduler_thread import DailyScheduler
    from maintenance.steps import register_default_steps

    maint_config = load_maintenance_config(str(service.config.repo_root))
    if not maint_config.enabled:
        return None

    chat_ids = list(service.config.authorized_chat_ids or [])
    sender = service.telegram.send_message if chat_ids else None

    def fire(triggered_by: str) -> None:
        engine = MaintenanceEngine(maint_config)
        register_default_steps(engine)
        engine.run(
            triggered_by=triggered_by,
            dry_run=False,
            force=False,
            context={
                "telegram_sender": sender,
                "telegram_chat_ids": chat_ids,
            },
        )

    scheduler = DailyScheduler(
        callback=fire,
        cutoff_time=maint_config.cutoff_time,
        check_interval_seconds=settings.scheduler_check_interval_seconds,
    )
    scheduler.start()
    return scheduler
