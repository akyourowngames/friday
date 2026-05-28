from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .service import TelegramWatcherService


class CliMessageRequest(BaseModel):
    message: str
    session_id: str = "main_cli"


def create_app(service: TelegramWatcherService) -> FastAPI:
    app = FastAPI(title="KING Telegram Watcher Service", version="1.0.0")

    @app.get("/health")
    def health() -> dict[str, Any]:
        config = service.config
        return {
            "status": "ok",
            "service": "telegram_watcher",
            "config_path": str(config.config_path),
            "token_present": bool(config.token),
            "authorized_user_ids_configured": bool(config.authorized_user_ids),
            "authorized_chat_ids_configured": bool(config.authorized_chat_ids),
            "allowed_zone_count": len(config.enabled_zones()),
            "folder_watcher_base_url": config.folder_watcher_base_url,
            "cli_bridge_enabled": config.cli_bridge_enabled,
        }

    @app.get("/status")
    def status() -> dict[str, Any]:
        config = service.config
        return {
            "status": "ok",
            "locked": bool(service.state.get("locked")),
            "allowed_zones": [
                {"name": zone.name, "path": str(zone.path), "enabled": zone.enabled}
                for zone in config.zones
            ],
            "cli_forward_actions": sorted(config.cli_forward_actions),
            "folder_watcher_base_url": config.folder_watcher_base_url,
            "state_path": str(config.state_path),
            "session_log_path": str(config.session_log_path),
        }

    @app.get("/verify")
    def verify() -> dict[str, Any]:
        return service.verify_runtime()

    @app.post("/cli/message")
    def cli_message(request: CliMessageRequest) -> dict[str, Any]:
        if not service.config.cli_bridge_enabled:
            return {"handled": False, "status": "disabled", "reason": "cli_bridge_disabled"}
        if not request.message.strip():
            raise HTTPException(status_code=400, detail="message must not be empty")
        return service.handle_local_message(request.message, request.session_id)

    @app.post("/push-check")
    def push_check() -> dict[str, Any]:
        return service.check_push_notifications()

    app.state.telegram_watcher_service = service
    return app
