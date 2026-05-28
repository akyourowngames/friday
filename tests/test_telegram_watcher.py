import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from telegram_watcher.api import create_app
from telegram_watcher.configuration import load_config
from telegram_watcher.service import TelegramWatcherService, _score_action_terms


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.documents = []

    def send_message(self, chat_id, text):
        self.messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True}

    def send_document(self, chat_id, path, caption=""):
        self.documents.append({"chat_id": chat_id, "path": Path(path), "caption": caption})
        return {"ok": True, "document": str(path)}

    def get_me(self):
        return {
            "ok": True,
            "result": {
                "id": 123,
                "username": "king_test_bot",
                "first_name": "KING",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            },
        }


class FakeFolderClient:
    def __init__(self, files=None, events=None, answer=""):
        self.files = files or []
        self.events = events or []
        self.answer = answer
        self.available = True

    def status(self):
        if not self.available:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE"}}
        return {"ok": True, "data": {"runtime": {"watch_path": "fake-watch"}}}

    def stats(self):
        if not self.available:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE"}}
        return {"ok": True, "data": {"active_files": len(self.files), "total_size_bytes": 123}}

    def latest(self, limit):
        if not self.available:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE"}}
        return {"ok": True, "data": {"files": self.files[:limit]}}

    def search(self, query, limit):
        if not self.available:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE"}}
        return {"ok": True, "data": {"files": self.files[:limit]}}

    def chat(self, message, limit):
        if self.answer:
            return {"ok": True, "data": {"answer": self.answer, "mode": "fake"}}
        return {"ok": False, "error": {"code": "CHAT_UNAVAILABLE"}}

    def diff(self, since, limit):
        if not self.available:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE"}}
        return {"ok": True, "data": {"events": [event for event in self.events if event["timestamp"] > since][:limit]}}


def _update(text, user_id=42, chat_id=100, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": user_id},
            "text": text,
        },
    }


def _write_config(root: Path, zone: Path) -> Path:
    path = root / "telegram.md"
    path.write_text(
        "\n".join(
            [
                "# Test Telegram Watcher",
                "## Runtime",
                "- token_env: TEST_TELEGRAM_TOKEN",
                "- authorized_user_ids_env: TEST_TELEGRAM_USERS",
                "- authorized_chat_ids_env: TEST_TELEGRAM_CHATS",
                "- unlock_pin_env: TEST_TELEGRAM_PIN",
                "- state_path: state.json",
                "- session_log_path: session.jsonl",
                "- api_host: 127.0.0.1",
                "- api_port: 7499",
                "- service_base_url: http://127.0.0.1:7499",
                "- main_cli_autostart: true",
                "- cli_bridge_enabled: true",
                "- api_startup_wait_ms: 1200",
                "- local_cli_chat_id: -9011",
                "- local_cli_user_id: -9011",
                "- max_file_size: 2KB",
                "- max_results: 5",
                "- max_scan_files: 50",
                "- rate_limit_queries_per_minute: 30",
                "- rate_limit_sends_per_minute: 30",
                "- semantic_min_score: 0.2",
                "- semantic_min_margin: 0.1",
                "- push_check_interval_seconds: 1",
                "- startup_notice_enabled: true",
                "- startup_notice_text: KING test watcher online",
                "## Allowed Zones",
                "- test: " + str(zone) + " | enabled: true",
                "## Blocked Suffixes",
                "- .env",
                "## Blocked Name Fragments",
                "- secret",
                "## Blocked Path Parts",
                "- .git",
                "## Command Aliases",
                "- status: status",
                "- health: health",
                "- latest: latest",
                "- send: send",
                "- watch: watch_on",
                "- unlock: unlock",
                "- lockdown: lockdown",
                "## Action Semantics",
                "- status: service visibility and allowed zones",
                "- health: runtime diagnostics and watcher health",
                "- latest: recently modified files inside allowed zones",
                "- send: deliver the requested safe file",
                "- find: find matching files",
                "- stats: aggregate file counts and extension totals",
                "- ask: natural file conversation",
                "- watch_on: enable push notifications",
                "- watch_off: disable push notifications",
                "- lockdown: lock the service",
                "- unlock: unlock with pin",
                "## CLI Forward Actions",
                "- status",
                "- health",
                "- latest",
                "- find",
                "- search",
                "- send",
                "- info",
                "- new",
                "- list",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _scorer(winning_action):
    def score(_text, candidates):
        scored = []
        for action, _semantic in candidates:
            scored.append((action, 1.0 if action == winning_action else 0.0))
        return scored

    return score


class TelegramWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.zone = self.root / "zone"
        self.zone.mkdir()
        self.config_path = _write_config(self.root, self.zone)
        self.env = patch.dict(
            os.environ,
            {
                "TEST_TELEGRAM_TOKEN": "token",
                "TEST_TELEGRAM_USERS": "42",
                "TEST_TELEGRAM_CHATS": "",
                "TEST_TELEGRAM_PIN": "1234",
            },
        )
        self.env.start()
        self.config = load_config(self.root, self.config_path)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_config_loads_markdown_controls_without_credentials_in_file(self):
        self.assertEqual(self.config.token, "token")
        self.assertEqual(self.config.authorized_user_ids, {42})
        self.assertEqual(self.config.authorized_chat_ids, set())
        self.assertEqual(self.config.enabled_zones()[0].path, self.zone.resolve())
        self.assertEqual(self.config.api_port, 7499)
        self.assertEqual(self.config.service_base_url, "http://127.0.0.1:7499")
        self.assertTrue(self.config.main_cli_autostart)
        self.assertTrue(self.config.cli_bridge_enabled)
        self.assertIn("send", self.config.cli_forward_actions)
        self.assertNotIn("ask", self.config.cli_forward_actions)
        self.assertNotIn("stats", self.config.cli_forward_actions)
        self.assertIn(".env", self.config.blocked_suffixes)
        self.assertEqual(self.config.command_aliases["send"], "send")
        self.assertIn("deliver the requested", self.config.action_semantics["send"])

    def test_folder_watcher_base_url_can_follow_client_active_target(self):
        tools_dir = self.root / "tools"
        tools_dir.mkdir(exist_ok=True)
        (tools_dir / "FOLDER_WATCHER_CLIENT.md").write_text(
            "\n".join(
                [
                    "# Client",
                    "## Runtime",
                    "- active_target: demo",
                    "## Targets",
                    "- demo: http://127.0.0.1:7475 | auth_env: TEST_AUTH",
                    "- local: http://127.0.0.1:7474 | auth_env: TEST_AUTH",
                ]
            ),
            encoding="utf-8",
        )

        config = load_config(self.root, self.config_path)

        self.assertEqual(config.folder_watcher_base_url, "http://127.0.0.1:7475")

    def test_local_semantic_fallback_scores_markdown_actions_without_phrase_table(self):
        scores = _score_action_terms(
            "show me watcher health",
            [
                ("health", self.config.action_semantics["health"]),
                ("send", self.config.action_semantics["send"]),
                ("ask", self.config.action_semantics["ask"]),
            ],
        )

        self.assertEqual(max(scores, key=lambda item: item[1])[0], "health")

    def test_unauthorized_user_is_silent_and_authorized_status_reports_fields(self):
        telegram = FakeTelegram()
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=FakeFolderClient())

        unauthorized = service.handle_update(_update("/status", user_id=7))
        self.assertEqual(unauthorized["reason"], "unauthorized")
        self.assertEqual(telegram.messages, [])
        self.assertEqual(service.state["last_unauthorized"]["user_id"], 7)
        log_entry = json.loads(self.config.session_log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(log_entry["text"], "")

        authorized = service.handle_update(_update("/status", user_id=42))
        self.assertEqual(authorized["action"], "status")
        self.assertIn("Allowed zones: 1", telegram.messages[-1]["text"])
        self.assertIn("Folder watcher: reachable", telegram.messages[-1]["text"])

    def test_authorized_chat_id_can_allow_chat_scoped_updates(self):
        with patch.dict(os.environ, {"TEST_TELEGRAM_USERS": "", "TEST_TELEGRAM_CHATS": "100"}):
            config = load_config(self.root, self.config_path)
            telegram = FakeTelegram()
            service = TelegramWatcherService(config, telegram=telegram, folder_client=FakeFolderClient())

            result = service.handle_update(_update("/status", user_id=7, chat_id=100))

        self.assertEqual(result["action"], "status")
        self.assertIn("Allowed zones: 1", telegram.messages[-1]["text"])

    def test_verify_runtime_and_startup_notice_do_not_expose_token(self):
        with patch.dict(os.environ, {"TEST_TELEGRAM_CHATS": "100"}):
            config = load_config(self.root, self.config_path)
            telegram = FakeTelegram()
            service = TelegramWatcherService(config, telegram=telegram, folder_client=FakeFolderClient())

            verify = service.verify_runtime()
            startup = service.announce_startup()

        self.assertTrue(verify["telegram_api_ok"])
        self.assertEqual(verify["bot"]["username"], "king_test_bot")
        self.assertNotIn("token", str(verify["bot"]))
        self.assertEqual(startup["sent"], 1)
        self.assertEqual(telegram.messages[-1]["text"], "KING test watcher online")

    def test_natural_send_delivers_single_file_from_folder_watcher_result(self):
        target = self.zone / "config_v3.json"
        target.write_text('{"ok": true}', encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient(files=[{"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}])
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        result = service.handle_update(_update("send me the config I was editing"))

        self.assertEqual(result["action"], "send")
        self.assertEqual(telegram.documents[0]["path"], target.resolve())
        self.assertEqual(telegram.documents[0]["caption"], "config_v3.json")

    def test_multiple_matches_create_pick_list_and_numeric_reply_sends_choice(self):
        first = self.zone / "one.md"
        second = self.zone / "two.md"
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient(
            files=[
                {"path": str(first), "filename": first.name, "size_bytes": first.stat().st_size},
                {"path": str(second), "filename": second.name, "size_bytes": second.stat().st_size},
            ]
        )
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        result = service.handle_update(_update("send me notes"))
        self.assertEqual(result["status"], "needs_selection")
        self.assertIn("Reply with a number", telegram.messages[-1]["text"])

        pick = service.handle_update(_update("2", update_id=2))
        self.assertEqual(pick["action"], "pick")
        self.assertEqual(telegram.documents[0]["path"], second.resolve())

    def test_natural_pick_phrase_sends_existing_selection(self):
        first = self.zone / "one.md"
        second = self.zone / "two.md"
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient(
            files=[
                {"path": str(first), "filename": first.name, "size_bytes": first.stat().st_size},
                {"path": str(second), "filename": second.name, "size_bytes": second.stat().st_size},
            ]
        )
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        service.handle_update(_update("send me notes"))
        pick = service.handle_update(_update("send me the 2", update_id=2))

        self.assertEqual(pick["action"], "pick")
        self.assertEqual(telegram.documents[0]["path"], second.resolve())

    def test_latest_respects_requested_zone_scope(self):
        first = self.zone / "inside.md"
        first.write_text("inside", encoding="utf-8")
        deep_dir = self.zone / "nested"
        deep_dir.mkdir()
        deep = deep_dir / "deep.md"
        deep.write_text("deep", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        telegram = FakeTelegram()
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=FakeFolderClient(), action_scorer=_scorer("latest"))

        result = service.handle_update(_update("latest test files"))

        self.assertEqual(result["action"], "latest")
        self.assertIn("test/inside.md", telegram.messages[-1]["text"])
        self.assertNotIn("nested/deep.md", telegram.messages[-1]["text"])
        self.assertNotIn("outside.md", telegram.messages[-1]["text"])

    def test_cli_stats_action_is_not_forwarded_to_telegram_bridge(self):
        telegram = FakeTelegram()
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=FakeFolderClient(), action_scorer=_scorer("stats"))

        result = service.handle_local_message("how many files are there in this folder")

        self.assertFalse(result["handled"])
        self.assertEqual(result["action"], "stats")

    def test_blocked_file_policy_prevents_delivery_even_when_index_matches(self):
        target = self.zone / ".env"
        target.write_text("TOKEN=value", encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient(files=[{"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}])
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        result = service.handle_update(_update("send env file"))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(telegram.documents, [])
        self.assertIn("blocked by the Telegram watcher file policy", telegram.messages[-1]["text"])

    def test_local_scan_fallback_sends_allowed_file_when_folder_watcher_unavailable(self):
        target = self.zone / "local_report.md"
        target.write_text("local fallback", encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient()
        folder.available = False
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        result = service.handle_update(_update("send local_report"))

        self.assertEqual(result["action"], "send")
        self.assertEqual(telegram.documents[0]["path"], target.resolve())

    def test_watch_mode_pushes_new_allowed_events_without_user_command_loop(self):
        target = self.zone / "arrival.md"
        target.write_text("new file", encoding="utf-8")
        event = {
            "timestamp": 105.0,
            "event_type": "FILE_CREATED",
            "new_path": str(target),
            "payload": {"file": {"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}},
        }
        now = [100.0]
        telegram = FakeTelegram()
        folder = FakeFolderClient(events=[event])
        service = TelegramWatcherService(
            self.config,
            telegram=telegram,
            folder_client=folder,
            action_scorer=_scorer("watch_on"),
            clock=lambda: now[0],
        )

        result = service.handle_update(_update("watch this folder for me"))
        self.assertEqual(result["action"], "watch_on")
        now[0] = 106.0
        push = service.check_push_notifications()

        self.assertEqual(push["pushed"], 1)
        self.assertIn("New file: test/arrival.md", telegram.messages[-1]["text"])

    def test_lockdown_blocks_file_requests_until_pin_unlocks(self):
        target = self.zone / "note.md"
        target.write_text("note", encoding="utf-8")
        telegram = FakeTelegram()
        folder = FakeFolderClient(files=[{"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}])
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

        service.handle_update(_update("/lockdown"))
        blocked = service.handle_update(_update("send note", update_id=2))
        self.assertEqual(blocked["reason"], "locked")
        self.assertEqual(telegram.documents, [])

        unlock = service.handle_update(_update("/unlock 1234", update_id=3))
        self.assertEqual(unlock["action"], "unlock")
        sent = service.handle_update(_update("send note", update_id=4))
        self.assertEqual(sent["action"], "send")
        self.assertEqual(telegram.documents[0]["path"], target.resolve())

    def test_local_cli_message_sends_file_through_configured_telegram_targets(self):
        with patch.dict(os.environ, {"TEST_TELEGRAM_USERS": "", "TEST_TELEGRAM_CHATS": "100"}):
            config = load_config(self.root, self.config_path)
            target = self.zone / "cli_report.md"
            target.write_text("cli report", encoding="utf-8")
            telegram = FakeTelegram()
            folder = FakeFolderClient(files=[{"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}])
            service = TelegramWatcherService(config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))

            result = service.handle_local_message("send me the cli report")

        self.assertTrue(result["handled"])
        self.assertEqual(result["action"], "send")
        self.assertIn("Sent cli_report.md", result["text"])
        self.assertEqual(telegram.documents[0]["chat_id"], 100)
        self.assertEqual(telegram.documents[0]["path"], target.resolve())

    def test_local_cli_message_leaves_broad_chat_for_main_king_agent(self):
        telegram = FakeTelegram()
        service = TelegramWatcherService(self.config, telegram=telegram, folder_client=FakeFolderClient(), action_scorer=_scorer("ask"))

        result = service.handle_local_message("how are you today")

        self.assertFalse(result["handled"])
        self.assertEqual(result["action"], "ask")
        self.assertEqual(telegram.messages, [])

    def test_api_exposes_health_and_cli_message_endpoint(self):
        with patch.dict(os.environ, {"TEST_TELEGRAM_USERS": "", "TEST_TELEGRAM_CHATS": "100"}):
            config = load_config(self.root, self.config_path)
            target = self.zone / "api_report.md"
            target.write_text("api report", encoding="utf-8")
            telegram = FakeTelegram()
            folder = FakeFolderClient(files=[{"path": str(target), "filename": target.name, "size_bytes": target.stat().st_size}])
            service = TelegramWatcherService(config, telegram=telegram, folder_client=folder, action_scorer=_scorer("send"))
            client = TestClient(create_app(service))

            health = client.get("/health")
            response = client.post("/cli/message", json={"message": "send api report"})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["service"], "telegram_watcher")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["handled"])
        self.assertEqual(telegram.documents[0]["chat_id"], 100)


if __name__ == "__main__":
    unittest.main()
