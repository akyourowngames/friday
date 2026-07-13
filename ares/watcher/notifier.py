"""Multi-channel watcher notification delivery with per-channel audit logs."""

from __future__ import annotations

import asyncio
import os
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from typing import Any
from uuid import uuid4

import httpx

from ares.watcher.database import WatcherDatabase
from ares.watcher.fetchers.base import validate_target_url
from ares.watcher.models import Event, Monitor, Notification, utc_now


def format_alert(event: Event, monitor: Monitor) -> str:
    icon = {"critical": "🚨", "warning": "⚠️"}.get(event.severity, "🔔")
    lines = [f"{icon} Ares Watcher Alert", "", f"Monitor: {monitor.name}", f"Type: {event.event_type.replace('_', ' ').title()}",
        f"Severity: {event.severity.upper()}", f"Time: {event.created_at.isoformat()}", "", event.change_summary or "A monitored value changed."]
    if event.old_value is not None: lines.append(f"Previous: {event.old_value[:700]}")
    if event.new_value is not None: lines.append(f"Current: {event.new_value[:700]}")
    if event.ai_summary: lines.extend(["", "Ares analysis:", event.ai_summary[:1200]])
    return "\n".join(lines)


class NotificationDispatcher:
    def __init__(self, db: WatcherDatabase, settings: dict[str, Any] | None = None) -> None:
        self.db, self.settings = db, settings or {}

    async def dispatch(self, event: Event, monitor: Monitor) -> list[Notification]:
        config = self._merged_config(monitor)
        channels = [name for name in ("telegram", "desktop", "email", "webhook") if (config.get(name) or {}).get("enabled")]
        if not channels:
            self.db.mark_event_notified(event.id)
            event.notified = True
            return []
        results = await asyncio.gather(*(self._deliver(name, event, monitor, config[name]) for name in channels))
        if any(item.status == "sent" for item in results):
            self.db.mark_event_notified(event.id)
            event.notified = True
        return results

    async def retry_failed(self, max_attempts: int = 4) -> list[Notification]:
        """Retry due channel deliveries in place so every attempt stays auditable."""
        completed: list[Notification] = []
        for record in self.db.list_retryable_notifications(max_attempts=max_attempts):
            event = self.db.get_event(record.event_id)
            monitor = self.db.get_monitor(event.monitor_id) if event else None
            if event is None or monitor is None:
                continue
            config = self._merged_config(monitor).get(record.channel) or {}
            if not config.get("enabled"):
                continue
            record.attempts += 1
            try:
                await getattr(self, f"_send_{record.channel}")(format_alert(event, monitor), event, monitor, config)
                record.status, record.sent_at, record.error, record.next_retry_at = "sent", utc_now(), None, None
                self.db.mark_event_notified(event.id)
            except Exception as exc:
                record.error = str(exc)[:1000]
                record.next_retry_at = utc_now() + timedelta(minutes=min(60, 2 ** record.attempts))
            self.db.update_notification(record)
            completed.append(record)
        return completed

    def _merged_config(self, monitor: Monitor) -> dict[str, Any]:
        merged = {key: dict(value) for key, value in self.settings.items() if isinstance(value, dict)}
        for key, value in (monitor.config.get("notifications") or {}).items():
            merged[key] = {**merged.get(key, {}), **(value if isinstance(value, dict) else {"enabled": bool(value)})}
        return merged

    async def _deliver(self, channel: str, event: Event, monitor: Monitor, config: dict[str, Any]) -> Notification:
        record = Notification(id=str(uuid4()), event_id=event.id, channel=channel, attempts=1)
        self.db.insert_notification(record)
        try:
            sender = getattr(self, f"_send_{channel}")
            await sender(format_alert(event, monitor), event, monitor, config)
            record.status, record.sent_at = "sent", utc_now()
        except Exception as exc:
            record.status, record.error = "failed", str(exc)[:1000]
            record.next_retry_at = utc_now() + timedelta(minutes=2)
        self.db.update_notification(record)
        return record

    async def _send_telegram(self, message: str, event: Event, monitor: Monitor, config: dict[str, Any]) -> None:
        token = str(config.get("bot_token") or os.environ.get("ARES_TELEGRAM_BOT_TOKEN") or "")
        chat_id = config.get("chat_id")
        if not token or chat_id in {None, ""}:
            raise RuntimeError("Telegram bot_token and chat_id are required")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":chat_id,"text":message[:4000],"disable_web_page_preview":True})
            response.raise_for_status()

    async def _send_desktop(self, message: str, event: Event, monitor: Monitor, config: dict[str, Any]) -> None:
        def send() -> None:
            from plyer import notification
            notification.notify(title=f"Ares · {monitor.name}"[:64], message=(event.change_summary or message)[:240], app_name="Ares Watcher", timeout=int(config.get("timeout", 8)))
        await asyncio.to_thread(send)

    async def _send_email(self, message: str, event: Event, monitor: Monitor, config: dict[str, Any]) -> None:
        def send() -> None:
            host, port = str(config.get("smtp_host") or ""), int(config.get("smtp_port", 587))
            username = str(config.get("username") or "")
            password = str(config.get("password") or os.environ.get("ARES_WATCHER_SMTP_PASSWORD") or "")
            recipient = str(config.get("to_address") or "")
            if not host or not recipient: raise RuntimeError("Email smtp_host and to_address are required")
            mail = EmailMessage(); mail["Subject"] = f"[{event.severity.upper()}] Ares Watcher · {monitor.name}"; mail["From"] = config.get("from_address") or username; mail["To"] = recipient; mail.set_content(message)
            with smtplib.SMTP_SSL(host, port, timeout=20) if config.get("use_ssl") else smtplib.SMTP(host, port, timeout=20) as smtp:
                if not config.get("use_ssl", False) and config.get("start_tls", True): smtp.starttls()
                if username: smtp.login(username, password)
                smtp.send_message(mail)
        await asyncio.to_thread(send)

    async def _send_webhook(self, message: str, event: Event, monitor: Monitor, config: dict[str, Any]) -> None:
        url = validate_target_url(str(config.get("url") or ""), allow_private_network=bool(config.get("allow_private_network")))
        async with httpx.AsyncClient(timeout=min(float(config.get("timeout", 15)), 30), follow_redirects=False) as client:
            response = await client.post(url, headers=config.get("headers"), json={"text":message,"event":event.to_dict(),"monitor":monitor.public_dict()})
            response.raise_for_status()
