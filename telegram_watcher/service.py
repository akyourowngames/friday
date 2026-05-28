from __future__ import annotations

import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .configuration import AllowedZone, TelegramWatcherConfig


ActionScorer = Callable[[str, list[tuple[str, str]]], list[tuple[str, float]]]


@dataclass
class IncomingMessage:
    update_id: int
    chat_id: int
    user_id: int
    text: str


class TelegramAPI:
    def __init__(self, token: str, timeout_ms: int = 15000):
        self.token = token
        self.timeout_ms = timeout_ms
        self.base_url = "https://api.telegram.org/bot" + token

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            params["offset"] = offset
        response = httpx.get(
            self.base_url + "/getUpdates",
            params=params,
            timeout=max(timeout_seconds + 5, int(self.timeout_ms / 1000)),
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("ok") and isinstance(payload.get("result"), list):
            return payload["result"]
        return []

    def get_me(self) -> dict[str, Any]:
        response = httpx.get(
            self.base_url + "/getMe",
            timeout=int(self.timeout_ms / 1000),
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False}

    def send_message(self, chat_id: int, text: str) -> dict[str, Any]:
        response = httpx.post(
            self.base_url + "/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096]},
            timeout=int(self.timeout_ms / 1000),
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False}

    def send_document(self, chat_id: int, path: Path, caption: str = "") -> dict[str, Any]:
        mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as handle:
            response = httpx.post(
                self.base_url + "/sendDocument",
                data={"chat_id": str(chat_id), "caption": caption[:1024]},
                files={"document": (path.name, handle, mime_type)},
                timeout=max(60, int(self.timeout_ms / 1000)),
            )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {"ok": False}


class FolderWatcherClient:
    def __init__(self, config: TelegramWatcherConfig):
        self.base_url = config.folder_watcher_base_url.rstrip("/")
        self.auth_env = config.folder_watcher_auth_env
        self.timeout_ms = config.folder_watcher_timeout_ms

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def stats(self) -> dict[str, Any]:
        return self._request("GET", "/files/stats")

    def latest(self, limit: int) -> dict[str, Any]:
        return self._request("GET", "/files/latest", params={"n": limit})

    def search(self, query: str, limit: int) -> dict[str, Any]:
        return self._request("GET", "/files/search", params={"q": query, "limit": limit})

    def chat(self, message: str, limit: int) -> dict[str, Any]:
        return self._request("POST", "/chat", json_body={"message": message, "limit": limit})

    def diff(self, since: float, limit: int) -> dict[str, Any]:
        return self._request("GET", "/files/diff", params={"since": since, "limit": limit})

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        token = ""
        if self.auth_env:
            import os

            token = str(os.getenv(self.auth_env, "") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            response = httpx.request(
                method,
                self.base_url + endpoint,
                params=params,
                json=json_body,
                headers=headers,
                timeout=int(self.timeout_ms) / 1000,
            )
            payload = response.json()
        except (httpx.RequestError, ValueError):
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE", "endpoint": endpoint}}
        if response.status_code >= 400:
            return {
                "ok": False,
                "error": {
                    "code": "UPSTREAM_ERROR",
                    "endpoint": endpoint,
                    "status_code": response.status_code,
                    "detail": payload.get("detail") if isinstance(payload, dict) else "",
                },
            }
        return {"ok": True, "data": payload if isinstance(payload, dict) else {"items": payload}}


class LocalTelegramBridge:
    def __init__(self, telegram: Any, target_chat_ids: list[int]):
        self.telegram = telegram
        self.target_chat_ids = target_chat_ids
        self.messages: list[str] = []
        self.documents: list[dict[str, Any]] = []

    def send_message(self, _chat_id: int, text: str) -> dict[str, Any]:
        self.messages.append(str(text or ""))
        return {"ok": True, "local_only": True}

    def send_document(self, _chat_id: int, path: Path, caption: str = "") -> dict[str, Any]:
        if not self.target_chat_ids:
            self.messages.append("No Telegram chat targets are configured for CLI delivery.")
            return {"ok": False, "sent": 0, "failed": 0, "targets": 0}
        sent = 0
        failed = 0
        failures: list[str] = []
        for chat_id in self.target_chat_ids:
            try:
                payload = self.telegram.send_document(chat_id, path, caption=caption)
                if isinstance(payload, dict) and payload.get("ok", True):
                    sent += 1
                else:
                    failed += 1
                    failures.append(str(chat_id))
            except httpx.HTTPError:
                failed += 1
                failures.append(str(chat_id))
        document = {
            "path": str(Path(path).resolve()),
            "caption": caption,
            "sent": sent,
            "failed": failed,
            "targets": len(self.target_chat_ids),
        }
        self.documents.append(document)
        if sent:
            self.messages.append("Sent " + Path(path).name + " to " + str(sent) + " Telegram chat target(s).")
        elif failures:
            self.messages.append(
                "I found " + Path(path).name + " at " + str(Path(path).resolve())
                + ", but Telegram delivery failed. Send the bot one real Telegram message so KING can learn the reachable chat id."
            )
        return {"ok": sent > 0, **document}


class TelegramWatcherService:
    def __init__(
        self,
        config: TelegramWatcherConfig,
        telegram: Any | None = None,
        folder_client: Any | None = None,
        action_scorer: ActionScorer | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.config = config
        self.telegram = telegram or TelegramAPI(config.token, config.request_timeout_ms)
        self.folder_client = folder_client or FolderWatcherClient(config)
        self.action_scorer = action_scorer or score_action_semantics
        self.clock = clock or time.time
        self.state = self._load_state()
        self.query_hits: dict[int, list[float]] = {}
        self.send_hits: dict[int, list[float]] = {}
        self.prepare_local_paths()

    def poll_forever(self) -> None:
        self.announce_startup()
        offset = self.state.get("telegram_update_offset")
        while True:
            updates = self.telegram.get_updates(offset, self.config.polling_timeout_seconds)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                    self.state["telegram_update_offset"] = offset
                self.handle_update(update)
            self.check_push_notifications()
            self._save_state()

    def handle_update(self, update: dict[str, Any]) -> dict[str, Any]:
        incoming = _incoming_message(update)
        if incoming is None:
            return {"status": "ignored", "reason": "no_text_message"}
        if not self._authorized(incoming):
            self._record_unauthorized(incoming)
            self._log("ignored_unauthorized", incoming, {"user_id": incoming.user_id, "chat_id": incoming.chat_id}, include_text=False)
            return {"status": "ignored", "reason": "unauthorized"}
        if not self._rate_allowed(incoming.user_id, "query"):
            self.telegram.send_message(incoming.chat_id, "Rate limit is active for this Telegram watcher.")
            self._log("rate_limited", incoming, {"kind": "query"})
            return {"status": "blocked", "reason": "rate_limited"}

        text = incoming.text.strip()
        if not text:
            return self._handle_status(incoming)

        pending = self._pending_selection(incoming.chat_id)
        selected_number = _selection_number(text)
        if pending and selected_number is not None:
            return self._handle_pick(incoming, selected_number)

        action, query = self._plan_action(text)
        if self.state.get("locked") and action != "unlock":
            self.telegram.send_message(incoming.chat_id, "Telegram watcher is locked.")
            self._log("locked_request", incoming, {"action": action})
            return {"status": "blocked", "reason": "locked", "action": action}

        handler = self._action_handlers().get(action, self._handle_ask)
        return handler(incoming, query)

    def handle_local_message(self, text: str, session_id: str = "main_cli") -> dict[str, Any]:
        clean = str(text or "").strip()
        if not clean:
            return {"handled": False, "status": "empty", "reason": "empty_message"}

        incoming = IncomingMessage(
            update_id=0,
            chat_id=self.config.local_cli_chat_id,
            user_id=self.config.local_cli_user_id,
            text=clean,
        )
        pending = self._pending_selection(incoming.chat_id)
        selected_number = _selection_number(clean)
        if pending and selected_number is not None:
            action = "pick"
            query = str(selected_number)
        else:
            action, query = self._plan_action(clean)
            if action not in self.config.cli_forward_actions:
                return {
                    "handled": False,
                    "status": "not_forwarded",
                    "action": action,
                    "session_id": session_id,
                }

        if self.state.get("locked") and action != "unlock":
            return {
                "handled": True,
                "status": "blocked",
                "action": action,
                "text": "Telegram watcher is locked.",
                "session_id": session_id,
            }

        target_chat_ids = sorted(self.config.authorized_chat_ids or self.config.authorized_user_ids)
        bridge = LocalTelegramBridge(self.telegram, target_chat_ids)
        local_service = TelegramWatcherService(
            self.config,
            telegram=bridge,
            folder_client=self.folder_client,
            action_scorer=self.action_scorer,
            clock=self.clock,
        )
        local_service.state = self.state
        local_service.query_hits = self.query_hits
        local_service.send_hits = self.send_hits

        if action == "pick":
            result = local_service._handle_pick(incoming, int(query))
        else:
            handler = local_service._action_handlers().get(action, local_service._handle_ask)
            result = handler(incoming, query)

        self.state = local_service.state
        response_text = "\n\n".join(message for message in bridge.messages if message).strip()
        return {
            "handled": True,
            "status": result.get("status", "ok") if isinstance(result, dict) else "ok",
            "action": result.get("action", action) if isinstance(result, dict) else action,
            "text": response_text,
            "documents": bridge.documents,
            "telegram_target_count": len(target_chat_ids),
            "session_id": session_id,
            "result": result,
        }

    def _action_handlers(self) -> dict[str, Callable[[IncomingMessage, str], dict[str, Any]]]:
        return {
            "status": self._handle_status,
            "health": self._handle_health,
            "latest": self._handle_latest,
            "find": self._handle_find,
            "search": self._handle_find,
            "send": self._handle_send,
            "sendfile": self._handle_sendfile,
            "info": self._handle_info,
            "new": self._handle_new,
            "list": self._handle_list,
            "stats": self._handle_stats,
            "ask": self._handle_ask,
            "lockdown": self._handle_lockdown,
            "unlock": self._handle_unlock,
            "watch_on": self._handle_watch_on,
            "watch_off": self._handle_watch_off,
        }

    def check_push_notifications(self) -> dict[str, Any]:
        if self.state.get("locked"):
            return {"status": "skipped", "reason": "locked"}
        now = self.clock()
        pushed = 0
        for chat_id, chat_state in self.state.setdefault("chats", {}).items():
            if not isinstance(chat_state, dict) or not chat_state.get("watch_enabled"):
                continue
            last_check = float(chat_state.get("last_push_check_ts") or 0)
            if now - last_check < self.config.push_check_interval_seconds:
                continue
            since = float(chat_state.get("last_push_ts") or now)
            chat_state["last_push_check_ts"] = now
            events = self._watcher_events_since(since)
            if not events:
                chat_state["last_push_ts"] = now
                continue
            max_ts = since
            for event in events[: self.config.push_event_limit]:
                timestamp = float(event.get("timestamp") or now)
                max_ts = max(max_ts, timestamp)
                candidate = self._candidate_from_event(event)
                if candidate is None:
                    continue
                try:
                    numeric_chat_id = int(chat_id)
                except ValueError:
                    continue
                if self.config.push_auto_send:
                    self._send_candidate(numeric_chat_id, candidate)
                else:
                    self.telegram.send_message(numeric_chat_id, "New file: " + self._candidate_line(candidate, 1, include_number=False))
                pushed += 1
            chat_state["last_push_ts"] = max_ts
        self._save_state()
        return {"status": "ok", "pushed": pushed}

    def prepare_local_paths(self) -> None:
        for zone in self.config.enabled_zones():
            try:
                zone.path.relative_to(self.config.repo_root)
            except ValueError:
                continue
            zone.path.mkdir(parents=True, exist_ok=True)
        if self.config.state_path is not None:
            self.config.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config.session_log_path is not None:
            self.config.session_log_path.parent.mkdir(parents=True, exist_ok=True)

    def verify_runtime(self) -> dict[str, Any]:
        result = {
            "token_present": bool(self.config.token),
            "authorized_user_ids_configured": bool(self.config.authorized_user_ids),
            "authorized_chat_ids_configured": bool(self.config.authorized_chat_ids),
            "allowed_zone_count": len(self.config.enabled_zones()),
            "telegram_api_ok": False,
            "bot": {},
        }
        if not self.config.token:
            result["error"] = {"code": "MISSING_TOKEN", "message": "Telegram bot token is not configured."}
            return result
        try:
            payload = self.telegram.get_me()
        except (httpx.HTTPError, ValueError):
            result["error"] = {"code": "TELEGRAM_VERIFY_FAILED", "message": "Telegram getMe failed."}
            return result
        if not payload.get("ok"):
            result["error"] = {"code": "TELEGRAM_VERIFY_FAILED", "message": "Telegram getMe did not return ok."}
            return result
        bot = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        result["telegram_api_ok"] = True
        result["bot"] = {
            "id": bot.get("id"),
            "username": bot.get("username"),
            "first_name": bot.get("first_name"),
            "can_join_groups": bot.get("can_join_groups"),
            "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
            "supports_inline_queries": bot.get("supports_inline_queries"),
        }
        return result

    def announce_startup(self) -> dict[str, Any]:
        if not self.config.startup_notice_enabled:
            return {"status": "skipped", "reason": "startup_notice_disabled"}
        chat_ids = sorted(self.config.authorized_chat_ids or self.config.authorized_user_ids)
        if not chat_ids:
            return {"status": "skipped", "reason": "no_authorized_chat"}
        sent = 0
        failed = 0
        for chat_id in chat_ids:
            try:
                payload = self.telegram.send_message(chat_id, self.config.startup_notice_text)
                if payload.get("ok", True):
                    sent += 1
                else:
                    failed += 1
            except httpx.HTTPError:
                failed += 1
        return {"status": "ok" if sent else "failed", "sent": sent, "failed": failed}

    def _handle_status(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        zones = self.config.enabled_zones()
        lines = [
            "Telegram watcher status",
            "Locked: " + ("yes" if self.state.get("locked") else "no"),
            "Allowed zones: " + str(len(zones)),
        ]
        for zone in zones:
            lines.append("- " + zone.name + ": " + str(zone.path))
        folder_status = self.folder_client.status()
        if isinstance(folder_status, dict) and folder_status.get("ok"):
            data = folder_status.get("data") if isinstance(folder_status.get("data"), dict) else {}
            runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
            lines.append("Folder watcher: reachable")
            if runtime.get("watch_path"):
                lines.append("Index path: " + str(runtime["watch_path"]))
        else:
            lines.append("Folder watcher: unavailable, local scan fallback ready")
        self.telegram.send_message(incoming.chat_id, "\n".join(lines))
        self._log("status", incoming, {"zones": len(zones)})
        return {"status": "ok", "action": "status"}

    def _handle_health(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        result = {
            "token_present": bool(self.config.token),
            "authorized_ids_configured": bool(self.config.authorized_user_ids),
            "authorized_chats_configured": bool(self.config.authorized_chat_ids),
            "zones": len(self.config.enabled_zones()),
            "state_path": str(self.config.state_path),
            "session_log_path": str(self.config.session_log_path),
            "last_unauthorized": self.state.get("last_unauthorized"),
        }
        lines = ["Telegram watcher health"]
        for key, value in result.items():
            lines.append(str(key) + ": " + str(value))
        self.telegram.send_message(incoming.chat_id, "\n".join(lines))
        self._log("health", incoming, result)
        return {"status": "ok", "action": "health", "result": result}

    def _handle_latest(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        files = self._latest_files(self.config.max_results, query.strip() or incoming.text.strip())
        return self._reply_with_candidates(incoming, files, "latest")

    def _handle_find(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        clean_query = query.strip() or incoming.text.strip()
        files = self._search_files(clean_query, self.config.max_results)
        return self._reply_with_candidates(incoming, files, "search")

    def _handle_send(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        clean_query = query.strip() or incoming.text.strip()
        all_files = self._search_files(clean_query, self.config.max_results, include_blocked=True)
        files = [candidate for candidate in all_files if not candidate.get("blocked")]
        blocked_files = [candidate for candidate in all_files if candidate.get("blocked")]
        if not files:
            if blocked_files:
                result = self._send_candidate(incoming.chat_id, blocked_files[0])
                self._log("send_blocked", incoming, {"result": result})
                return {"status": result.get("status", "blocked"), "action": "send"}
            self.telegram.send_message(incoming.chat_id, "No deliverable file matched inside the allowed zones.")
            self._log("send_no_match", incoming, {"query": clean_query})
            return {"status": "empty", "action": "send"}
        if len(files) == 1:
            result = self._send_candidate(incoming.chat_id, files[0])
            self._log("send_single", incoming, {"file": files[0], "result": result})
            return {"status": result.get("status", "ok"), "action": "send", "file": files[0]}
        self._store_candidates(incoming.chat_id, files)
        self.telegram.send_message(incoming.chat_id, self._candidate_list_text(files, "I found multiple files. Reply with a number to receive one."))
        self._log("send_candidates", incoming, {"count": len(files)})
        return {"status": "needs_selection", "action": "send", "count": len(files)}

    def _handle_sendfile(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        clean_path = query.strip() or incoming.text.strip()
        if not clean_path:
            self.telegram.send_message(incoming.chat_id, "Provide a file path to send.")
            return {"status": "empty", "action": "sendfile"}
        path = Path(clean_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            self.telegram.send_message(incoming.chat_id, "File not found: " + str(path))
            self._log("sendfile_missing", incoming, {"path": str(path)})
            return {"status": "blocked", "action": "sendfile", "reason": "missing_file"}
        size = path.stat().st_size
        if size > self.config.max_file_size_bytes:
            self.telegram.send_message(
                incoming.chat_id,
                "File is " + _format_bytes(size) + ", above the send limit of " + _format_bytes(self.config.max_file_size_bytes) + ".",
            )
            return {"status": "blocked", "action": "sendfile", "reason": "file_too_large"}
        if not self._rate_allowed(incoming.chat_id, "send"):
            self.telegram.send_message(incoming.chat_id, "Send rate limit is active.")
            return {"status": "blocked", "action": "sendfile", "reason": "rate_limited"}
        result = self.telegram.send_document(incoming.chat_id, path, caption=path.name)
        self._log("sendfile", incoming, {"path": str(path), "ok": result.get("ok", True)})
        return {"status": "ok" if result.get("ok", True) else "failed", "action": "sendfile"}

    def _handle_info(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        clean_query = query.strip() or incoming.text.strip()
        files = self._search_files(clean_query, self.config.max_results)
        if not files:
            self.telegram.send_message(incoming.chat_id, "No file metadata matched inside the allowed zones.")
            self._log("info_empty", incoming, {"query": clean_query})
            return {"status": "empty", "action": "info"}
        lines = ["File info"]
        for index, candidate in enumerate(files[: self.config.max_results], start=1):
            lines.append(self._candidate_line(candidate, index))
            if candidate.get("modified_ts"):
                lines.append("  modified_ts: " + str(candidate["modified_ts"]))
            if candidate.get("snippet"):
                lines.append("  snippet: " + str(candidate["snippet"])[:240])
        self._store_candidates(incoming.chat_id, files)
        self.telegram.send_message(incoming.chat_id, "\n".join(lines))
        self._log("info", incoming, {"count": len(files)})
        return {"status": "ok", "action": "info", "count": len(files)}

    def _handle_new(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        files = self._new_files(self.config.default_new_window_seconds)
        return self._reply_with_candidates(incoming, files, "new")

    def _handle_list(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        lines = ["Allowed zones"]
        for zone in self.config.enabled_zones():
            count = self._safe_zone_count(zone)
            lines.append("- " + zone.name + ": " + str(zone.path) + " | visible files: " + str(count))
        self.telegram.send_message(incoming.chat_id, "\n".join(lines))
        self._log("list", incoming, {"zones": len(self.config.enabled_zones())})
        return {"status": "ok", "action": "list"}

    def _handle_stats(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        stats = self.folder_client.stats()
        lines = ["File intelligence stats"]
        if isinstance(stats, dict) and stats.get("ok"):
            data = stats.get("data") if isinstance(stats.get("data"), dict) else {}
            lines.append("Folder watcher reachable: yes")
            if "active_files" in data:
                lines.append("Indexed active files: " + str(data["active_files"]))
            if "total_size_bytes" in data:
                lines.append("Indexed total bytes: " + str(data["total_size_bytes"]))
        else:
            lines.append("Folder watcher reachable: no")
        lines.append("Allowed zones: " + str(len(self.config.enabled_zones())))
        self.telegram.send_message(incoming.chat_id, "\n".join(lines))
        self._log("stats", incoming, {"folder_ok": bool(isinstance(stats, dict) and stats.get("ok"))})
        return {"status": "ok", "action": "stats"}

    def _handle_ask(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        clean_query = query.strip() or incoming.text.strip()
        agent_result = self._agent_chat(clean_query)
        if agent_result is not None:
            self.telegram.send_message(incoming.chat_id, agent_result)
            self._log("ask_agent", incoming, {"chars": len(agent_result)})
            return {"status": "ok", "action": "ask", "mode": "agent"}
        chat = self.folder_client.chat(clean_query, self.config.max_results)
        if isinstance(chat, dict) and chat.get("ok"):
            data = chat.get("data") if isinstance(chat.get("data"), dict) else {}
            answer = str(data.get("answer") or "").strip()
            if answer:
                self.telegram.send_message(incoming.chat_id, answer)
                self._log("ask", incoming, {"mode": data.get("mode", "")})
                return {"status": "ok", "action": "ask", "mode": data.get("mode", "")}
        files = self._search_files(clean_query, self.config.max_results)
        if files:
            return self._reply_with_candidates(incoming, files, "ask_search")
        self.telegram.send_message(incoming.chat_id, "I could not ground that in the allowed Telegram watcher zones.")
        self._log("ask_empty", incoming, {"query": clean_query})
        return {"status": "empty", "action": "ask"}

    def _agent_chat(self, text: str) -> str | None:
        try:
            from agent.core import Agent

            if not hasattr(self, "_agent") or self._agent is None:
                self._agent = Agent()
            chunks: list[str] = []

            def collect(chunk: str):
                chunks.append(chunk)

            self._agent.process(text, emit_chunk=collect)
            response = "".join(chunks).strip()
            if not response:
                return None
            if len(response) > 4000:
                response = response[:3997] + "..."
            return response
        except Exception:
            return None

    def _handle_lockdown(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        self.state["locked"] = True
        self.state["locked_at"] = self.clock()
        self._save_state()
        self.telegram.send_message(incoming.chat_id, "Telegram watcher locked.")
        self._log("lockdown", incoming, {})
        return {"status": "ok", "action": "lockdown"}

    def _handle_unlock(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        configured_pin = self.config.unlock_pin
        if not configured_pin:
            self.telegram.send_message(incoming.chat_id, "Unlock PIN is not configured.")
            self._log("unlock_blocked", incoming, {"reason": "pin_missing"})
            return {"status": "blocked", "action": "unlock", "reason": "pin_missing"}
        if query.strip() != configured_pin:
            self.telegram.send_message(incoming.chat_id, "Unlock PIN did not match.")
            self._log("unlock_failed", incoming, {})
            return {"status": "blocked", "action": "unlock", "reason": "pin_mismatch"}
        self.state["locked"] = False
        self.state["unlocked_at"] = self.clock()
        self._save_state()
        self.telegram.send_message(incoming.chat_id, "Telegram watcher unlocked.")
        self._log("unlock", incoming, {})
        return {"status": "ok", "action": "unlock"}

    def _handle_watch_on(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        chat_state = self._chat_state(incoming.chat_id)
        chat_state["watch_enabled"] = True
        chat_state["last_push_ts"] = self.clock()
        chat_state["last_push_check_ts"] = 0
        self._save_state()
        self.telegram.send_message(incoming.chat_id, "Watch mode is on for allowed zones.")
        self._log("watch_on", incoming, {})
        return {"status": "ok", "action": "watch_on"}

    def _handle_watch_off(self, incoming: IncomingMessage, query: str = "") -> dict[str, Any]:
        chat_state = self._chat_state(incoming.chat_id)
        chat_state["watch_enabled"] = False
        self._save_state()
        self.telegram.send_message(incoming.chat_id, "Watch mode is off.")
        self._log("watch_off", incoming, {})
        return {"status": "ok", "action": "watch_off"}

    def _handle_pick(self, incoming: IncomingMessage, number: int) -> dict[str, Any]:
        pending = self._pending_selection(incoming.chat_id)
        if not pending:
            self.telegram.send_message(incoming.chat_id, "There is no active file selection.")
            return {"status": "empty", "action": "pick"}
        index = number - 1
        if index < 0 or index >= len(pending):
            self.telegram.send_message(incoming.chat_id, "That selection number is outside the current list.")
            return {"status": "blocked", "action": "pick", "reason": "out_of_range"}
        candidate = pending[index]
        result = self._send_candidate(incoming.chat_id, candidate)
        self._chat_state(incoming.chat_id)["pending_candidates"] = []
        self._save_state()
        self._log("pick", incoming, {"file": candidate, "result": result})
        return {"status": result.get("status", "ok"), "action": "pick"}

    def _plan_action(self, text: str) -> tuple[str, str]:
        command_action = self._command_action(text)
        if command_action is not None:
            return command_action
        action = self._semantic_action(text)
        return action, text

    def _command_action(self, text: str) -> tuple[str, str] | None:
        clean = text.strip()
        if not clean.startswith("/"):
            return None
        command_text = clean[1:].strip()
        if not command_text:
            return "status", ""
        pieces = command_text.split(None, 1)
        command = pieces[0].split("@", 1)[0].casefold()
        query = pieces[1].strip() if len(pieces) > 1 else ""
        action = self.config.command_aliases.get(command)
        if not action:
            return "ask", clean
        if action == "watch_on":
            lowered = query.casefold()
            if lowered == "off" and "watch_off" in self.config.action_semantics:
                return "watch_off", ""
            if lowered == "on":
                return "watch_on", ""
        return action, query

    def _semantic_action(self, text: str) -> str:
        candidates = [
            (action, semantic)
            for action, semantic in self.config.action_semantics.items()
            if semantic.strip()
        ]
        if not candidates:
            return self.config.fallback_action
        scored = self.action_scorer(text, candidates)
        if not scored:
            return self.config.fallback_action
        ordered = sorted(scored, key=lambda item: item[1], reverse=True)
        best_action, best_score = ordered[0]
        next_score = ordered[1][1] if len(ordered) > 1 else -1.0
        if best_score < self.config.semantic_min_score:
            return self.config.fallback_action
        if best_score - next_score < self.config.semantic_min_margin:
            return self.config.fallback_action
        return best_action

    def _reply_with_candidates(self, incoming: IncomingMessage, files: list[dict[str, Any]], action: str) -> dict[str, Any]:
        if not files:
            self.telegram.send_message(incoming.chat_id, "No matching files were found inside the allowed zones.")
            self._log(action + "_empty", incoming, {})
            return {"status": "empty", "action": action}
        self._store_candidates(incoming.chat_id, files)
        self.telegram.send_message(incoming.chat_id, self._candidate_list_text(files, "Found files. Reply with a number to receive one."))
        self._log(action, incoming, {"count": len(files)})
        return {"status": "ok", "action": action, "count": len(files)}

    def _candidate_list_text(self, files: list[dict[str, Any]], title: str) -> str:
        lines = [title]
        for index, candidate in enumerate(files[: self.config.max_results], start=1):
            lines.append(self._candidate_line(candidate, index))
        return "\n".join(lines)

    def _candidate_line(self, candidate: dict[str, Any], index: int, include_number: bool = True) -> str:
        prefix = str(index) + ". " if include_number else ""
        zone = str(candidate.get("zone") or "zone")
        relative = str(candidate.get("relative_path") or candidate.get("filename") or "file")
        size = _format_bytes(int(candidate.get("size_bytes") or 0))
        return prefix + zone + "/" + relative + " | " + size

    def _send_candidate(self, chat_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(candidate.get("path") or "")).expanduser()
        if not self._allowed_path(path):
            self.telegram.send_message(chat_id, "That file is outside the allowed Telegram watcher zones.")
            return {"status": "blocked", "reason": "outside_allowed_zones"}
        if self._blocked_path(path):
            self.telegram.send_message(chat_id, "That file is blocked by the Telegram watcher file policy.")
            return {"status": "blocked", "reason": "blocked_file_policy"}
        if not path.exists() or not path.is_file():
            self.telegram.send_message(chat_id, "That file is indexed but no longer available on disk.")
            return {"status": "blocked", "reason": "missing_file"}
        size = path.stat().st_size
        if size > self.config.max_file_size_bytes:
            self.telegram.send_message(
                chat_id,
                "File is " + _format_bytes(size) + ", above the Telegram watcher send limit of " + _format_bytes(self.config.max_file_size_bytes) + ".",
            )
            return {"status": "blocked", "reason": "file_too_large", "size_bytes": size}
        if not self._rate_allowed(chat_id, "send"):
            self.telegram.send_message(chat_id, "Send rate limit is active for this Telegram watcher.")
            return {"status": "blocked", "reason": "send_rate_limited"}
        result = self.telegram.send_document(chat_id, path.resolve(), caption=str(candidate.get("relative_path") or path.name))
        return {"status": "ok" if result.get("ok", True) else "failed", "telegram": result}

    def _latest_files(self, limit: int, query: str = "") -> list[dict[str, Any]]:
        response = self.folder_client.latest(max(limit, self.config.max_results * 3))
        files = self._files_from_response(response)
        if files:
            filtered = self._filter_candidates_for_request(files, query)
            return (filtered or files)[:limit]
        scanned = self._scan_local_files()
        scanned.sort(key=lambda item: float(item.get("modified_ts") or 0), reverse=True)
        filtered = self._filter_candidates_for_request(scanned, query)
        return (filtered or scanned)[:limit]

    def _search_files(self, query: str, limit: int, include_blocked: bool = False) -> list[dict[str, Any]]:
        response = self.folder_client.search(query, limit)
        files = self._files_from_response(response, include_blocked=include_blocked)
        if files:
            return files[:limit]
        terms = _plain_terms(query)
        matches = []
        fallback_matches = []
        for candidate in self._scan_local_files(include_blocked=include_blocked):
            haystack = " ".join(
                [
                    str(candidate.get("filename") or ""),
                    str(candidate.get("relative_path") or ""),
                    str(candidate.get("extension") or ""),
                ]
            ).casefold()
            if terms and all(term.casefold() in haystack for term in terms):
                matches.append(candidate)
            elif terms:
                score = sum(1 for term in terms if term.casefold() in haystack)
                if score > 0:
                    fallback_matches.append((score, candidate))
            if len(matches) >= limit:
                break
        if matches:
            return matches
        fallback_matches.sort(key=lambda item: item[0], reverse=True)
        return [candidate for _, candidate in fallback_matches[:limit]]

    def _filter_candidates_for_request(self, candidates: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in _plain_terms(query)}
        if not terms:
            return []
        zone_names = {zone.name.casefold() for zone in self.config.enabled_zones()}
        requested_zones = terms.intersection(zone_names)
        if requested_zones:
            zone_matches = [
                candidate
                for candidate in candidates
                if str(candidate.get("zone") or "").casefold() in requested_zones
            ]
            root_matches = [
                candidate
                for candidate in zone_matches
                if "/" not in str(candidate.get("relative_path") or "")
            ]
            return root_matches or zone_matches
        return []

    def _new_files(self, window_seconds: int) -> list[dict[str, Any]]:
        since = self.clock() - max(1, window_seconds)
        response = self.folder_client.diff(since, self.config.max_results)
        events = response.get("data", {}).get("events", []) if isinstance(response, dict) and response.get("ok") else []
        files = []
        for event in events:
            candidate = self._candidate_from_event(event)
            if candidate is not None:
                files.append(candidate)
        if files:
            return files[: self.config.max_results]
        return [
            candidate
            for candidate in self._scan_local_files()
            if float(candidate.get("modified_ts") or 0) >= since
        ][: self.config.max_results]

    def _watcher_events_since(self, since: float) -> list[dict[str, Any]]:
        response = self.folder_client.diff(since, self.config.push_event_limit)
        if isinstance(response, dict) and response.get("ok"):
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            events = data.get("events")
            if isinstance(events, list):
                return [event for event in events if isinstance(event, dict)]
        return []

    def _files_from_response(self, response: dict[str, Any], include_blocked: bool = False) -> list[dict[str, Any]]:
        if not isinstance(response, dict) or not response.get("ok"):
            return []
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        raw_files = data.get("files")
        if not isinstance(raw_files, list):
            return []
        files = []
        for item in raw_files:
            candidate = self._candidate_from_record(item, include_blocked=include_blocked)
            if candidate is not None:
                files.append(candidate)
        return files

    def _candidate_from_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        raw_file = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        if raw_file:
            candidate = self._candidate_from_record(raw_file)
            if candidate is not None:
                return candidate
        path_value = event.get("new_path") or event.get("old_path")
        if path_value:
            return self._candidate_from_path(Path(str(path_value)))
        return None

    def _candidate_from_record(self, item: Any, include_blocked: bool = False) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            return None
        candidate = self._candidate_from_path(Path(path_value), include_blocked=include_blocked)
        if candidate is None:
            return None
        for key in ("id", "filename", "extension", "mime_type", "size_bytes", "modified_ts", "indexed_ts", "summary", "snippet"):
            if key in item and item.get(key) not in (None, ""):
                candidate[key] = item[key]
        return candidate

    def _candidate_from_path(self, path: Path, include_blocked: bool = False) -> dict[str, Any] | None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return None
        zone = self._zone_for_path(resolved)
        if zone is None:
            return None
        blocked = self._blocked_path(resolved)
        if blocked and not include_blocked:
            return None
        relative = resolved.relative_to(zone.path)
        size = 0
        modified_ts = 0.0
        if resolved.exists() and resolved.is_file():
            stat = resolved.stat()
            size = stat.st_size
            modified_ts = stat.st_mtime
        return {
            "path": str(resolved),
            "filename": resolved.name,
            "extension": resolved.suffix.casefold(),
            "size_bytes": size,
            "modified_ts": modified_ts,
            "zone": zone.name,
            "relative_path": relative.as_posix(),
            "blocked": blocked,
        }

    def _scan_local_files(self, include_blocked: bool = False) -> list[dict[str, Any]]:
        files = []
        seen = 0
        for zone in self.config.enabled_zones():
            if not zone.path.exists() or not zone.path.is_dir():
                continue
            for path in zone.path.rglob("*"):
                seen += 1
                if seen > self.config.max_scan_files:
                    return files
                if not path.is_file():
                    continue
                candidate = self._candidate_from_path(path, include_blocked=include_blocked)
                if candidate is not None:
                    files.append(candidate)
        return files

    def _zone_for_path(self, path: Path) -> AllowedZone | None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return None
        for zone in self.config.enabled_zones():
            try:
                resolved.relative_to(zone.path)
                return zone
            except ValueError:
                continue
        return None

    def _allowed_path(self, path: Path) -> bool:
        return self._zone_for_path(path) is not None

    def _blocked_path(self, path: Path) -> bool:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return True
        name = resolved.name.casefold()
        for suffix in self.config.blocked_suffixes:
            if name.endswith(suffix):
                return True
        for fragment in self.config.blocked_name_fragments:
            if fragment and fragment in name:
                return True
        for part in resolved.parts:
            if part.casefold() in self.config.blocked_path_parts:
                return True
        return False

    def _safe_zone_count(self, zone: AllowedZone) -> int:
        if not zone.path.exists() or not zone.path.is_dir():
            return 0
        count = 0
        for path in zone.path.rglob("*"):
            if path.is_file() and not self._blocked_path(path):
                count += 1
            if count >= self.config.max_scan_files:
                return count
        return count

    def _authorized(self, incoming: IncomingMessage) -> bool:
        allowed_users = self.config.authorized_user_ids
        allowed_chats = self.config.authorized_chat_ids
        if not allowed_users and not allowed_chats:
            return False
        return int(incoming.user_id) in allowed_users or int(incoming.chat_id) in allowed_chats

    def _record_unauthorized(self, incoming: IncomingMessage) -> None:
        self.state["last_unauthorized"] = {
            "chat_id": incoming.chat_id,
            "user_id": incoming.user_id,
            "update_id": incoming.update_id,
            "ts": self.clock(),
        }
        self._save_state()

    def _rate_allowed(self, user_id: int, kind: str) -> bool:
        now = self.clock()
        if kind == "send":
            limit = max(1, self.config.rate_limit_sends_per_minute)
            bucket = self.send_hits.setdefault(user_id, [])
        else:
            limit = max(1, self.config.rate_limit_queries_per_minute)
            bucket = self.query_hits.setdefault(user_id, [])
        bucket[:] = [item for item in bucket if now - item < 60]
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def _chat_state(self, chat_id: int) -> dict[str, Any]:
        chats = self.state.setdefault("chats", {})
        key = str(chat_id)
        item = chats.get(key)
        if not isinstance(item, dict):
            item = {}
            chats[key] = item
        return item

    def _pending_selection(self, chat_id: int) -> list[dict[str, Any]]:
        candidates = self._chat_state(chat_id).get("pending_candidates")
        if isinstance(candidates, list):
            return [item for item in candidates if isinstance(item, dict)]
        return []

    def _store_candidates(self, chat_id: int, candidates: list[dict[str, Any]]) -> None:
        self._chat_state(chat_id)["pending_candidates"] = [
            _public_candidate(candidate)
            for candidate in candidates[: self.config.max_results]
        ]
        self._save_state()

    def _load_state(self) -> dict[str, Any]:
        path = self.config.state_path
        if path is None or not path.exists():
            return {"locked": False, "chats": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"locked": False, "chats": {}}
        if isinstance(payload, dict):
            payload.setdefault("locked", False)
            payload.setdefault("chats", {})
            return payload
        return {"locked": False, "chats": {}}

    def _save_state(self) -> None:
        path = self.config.state_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.state, indent=2, sort_keys=True), encoding="utf-8")

    def _log(self, event: str, incoming: IncomingMessage, payload: dict[str, Any], include_text: bool = True) -> None:
        path = self.config.session_log_path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": self.clock(),
            "event": event,
            "chat_id": incoming.chat_id,
            "user_id": incoming.user_id,
            "text": incoming.text[:500] if include_text else "",
            "payload": payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def score_action_semantics(user_input: str, candidates: list[tuple[str, str]]) -> list[tuple[str, float]]:
    if not str(user_input or "").strip() or not candidates:
        return []
    try:
        import numpy as np
        from agent.embedder import embed

        query_emb = embed(str(user_input or ""))
        candidate_embs = embed([action + ": " + text for action, text in candidates])
        if getattr(candidate_embs, "ndim", 1) == 1:
            candidate_embs = candidate_embs.reshape(1, -1)
        scores = np.dot(candidate_embs, query_emb)
        scored = [
            (action, float(scores[index]))
            for index, (action, _) in enumerate(candidates)
        ]
        term_scores = dict(_score_action_terms(user_input, candidates))
        if term_scores:
            scored = [
                (action, max(score, term_scores.get(action, 0.0)))
                for action, score in scored
            ]
        rounded_scores = {round(score, 6) for _, score in scored}
        if len(rounded_scores) > 1:
            return scored
    except Exception:
        pass
    return _score_action_terms(user_input, candidates)


def _score_action_terms(user_input: str, candidates: list[tuple[str, str]]) -> list[tuple[str, float]]:
    input_terms = {term.casefold() for term in _plain_terms(user_input)}
    if not input_terms:
        return []
    scored: list[tuple[str, float]] = []
    for action, semantic in candidates:
        action_terms = {term.casefold() for term in _plain_terms(action)}
        semantic_terms = {term.casefold() for term in _plain_terms(semantic)}
        direct_hits = len(input_terms.intersection(action_terms))
        semantic_hits = len(input_terms.intersection(semantic_terms))
        score = 0.0
        if direct_hits:
            score += 0.7
        if semantic_hits:
            score += min(0.25, semantic_hits / max(1, len(input_terms)))
            score += min(0.2, semantic_hits / max(1, len(semantic_terms)))
        scored.append((action, min(1.0, score)))
    return scored


def _incoming_message(update: dict[str, Any]) -> IncomingMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        message = update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    text = message.get("text") or message.get("caption") or ""
    update_id = update.get("update_id") if isinstance(update.get("update_id"), int) else 0
    if not isinstance(chat_id, int) or not isinstance(user_id, int):
        return None
    return IncomingMessage(update_id=update_id, chat_id=chat_id, user_id=user_id, text=str(text or ""))


def _plain_terms(value: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for char in str(value or ""):
        if char.isalnum() or char in ("_", "-", "."):
            current.append(char)
        elif current:
            terms.append("".join(current))
            current = []
    if current:
        terms.append("".join(current))
    return terms[:12]


def _selection_number(value: str) -> int | None:
    for term in _plain_terms(value):
        if not term.isdigit():
            continue
        try:
            number = int(term)
        except ValueError:
            continue
        if number > 0:
            return number
    return None


def _format_bytes(size: int) -> str:
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return str(int(value)) + " B"
            return f"{value:.1f} {unit}"
        value = value / 1024
    return str(size) + " B"


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = (
        "id",
        "path",
        "filename",
        "extension",
        "mime_type",
        "size_bytes",
        "modified_ts",
        "zone",
        "relative_path",
        "snippet",
    )
    return {
        key: candidate[key]
        for key in allowed_keys
        if key in candidate
    }
