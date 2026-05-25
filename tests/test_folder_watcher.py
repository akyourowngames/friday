import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from folder_watcher.api import create_app
from folder_watcher.bus import EventBus
from folder_watcher.configuration import load_config
from folder_watcher.index import FolderIndex
from folder_watcher.ingest import IngestPipeline
from folder_watcher.watcher import DebouncedWatcher


def _write_config(root: Path) -> Path:
    config = root / "watcher.md"
    config.write_text(
        "\n".join(
            [
                "# Test Watcher",
                "## Paths And Runtime",
                "- watch_path: watched",
                "- database_path: watcher.sqlite3",
                "- api_host: 127.0.0.1",
                "- api_port: 7474",
                "- debounce_ms: 25",
                "- scan_on_start: false",
                "- max_content_chars: 10000",
                "- hash_chunk_bytes: 4096",
                "- large_file_size: 1KB",
                "- ai_summaries_enabled: false",
                "## Ignore Globs",
                "- ignored/**",
                "- *.tmp",
                "## Text Extensions",
                "- .py",
                "- .md",
                "- .json",
                "## Tag Rules",
                "- extension:.py -> python",
                "- extension:.md -> markdown",
                "- mime-prefix:text/ -> text",
                "- directory:models -> ml-model",
                "- size-over:1KB -> large-file",
            ]
        ),
        encoding="utf-8",
    )
    return config


class FolderWatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.watch = self.root / "watched"
        self.watch.mkdir()
        self.config_path = _write_config(self.root)
        self.config = load_config(self.root, self.config_path)
        self.index = FolderIndex(self.config.database_path)
        self.pipeline = IngestPipeline(self.config, self.index)

    def tearDown(self):
        self.index.close()
        self.tmp.cleanup()

    def test_config_loads_markdown_controls_and_ignore_rules(self):
        ignored = self.watch / "ignored" / "skip.py"
        ignored.parent.mkdir()
        ignored.write_text("print('skip')", encoding="utf-8")

        self.assertEqual(self.config.watch_path, self.watch.resolve())
        self.assertEqual(self.config.api_port, 7474)
        self.assertTrue(self.config.should_ignore(ignored))
        self.assertFalse(self.config.should_ignore(self.watch / "keep.py"))
        self.assertEqual(self.config.large_file_size, 1024)

    def test_ingest_indexes_text_metadata_tags_search_and_stats(self):
        code = self.watch / "agent_loop.py"
        code.write_text(
            "\n".join(
                [
                    "import json",
                    "class AgentLoop:",
                    "    pass",
                    "def run_loop():",
                    "    return 'semantic folder watcher'",
                ]
            ),
            encoding="utf-8",
        )

        result = self.pipeline.ingest_path(code)
        file_item = result["file"]

        self.assertEqual(file_item["filename"], "agent_loop.py")
        self.assertEqual(file_item["metadata"]["language"], "python")
        self.assertIn("AgentLoop", file_item["metadata"]["classes"])
        self.assertIn("python", file_item["tags"])

        search = self.index.search("semantic folder watcher")
        self.assertEqual(len(search), 1)
        self.assertEqual(search[0]["id"], file_item["id"])

        content = self.index.get_content(file_item["id"])
        self.assertIn("run_loop", content)

        stats = self.index.stats()
        self.assertEqual(stats["active_files"], 1)
        self.assertEqual(stats["by_extension"][".py"], 1)
        self.assertGreaterEqual(stats["events"], 1)

    def test_duplicates_and_delete_events_are_grounded_in_hashes(self):
        first = self.watch / "first.md"
        second = self.watch / "second.md"
        first.write_text("same body", encoding="utf-8")
        second.write_text("same body", encoding="utf-8")

        self.pipeline.ingest_path(first)
        self.pipeline.ingest_path(second)
        duplicates = self.index.duplicates()

        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["count"], 2)

        event = self.pipeline.delete_path(first)
        self.assertEqual(event["event_type"], "FILE_DELETED")
        diff = self.index.diff(since=0)
        self.assertTrue(any(item["event_type"] == "FILE_DELETED" for item in diff))

    def test_api_exposes_latest_search_config_patch_and_record_delete(self):
        note = self.watch / "note.md"
        note.write_text("folder watcher api search target", encoding="utf-8")
        file_item = self.pipeline.ingest_path(note)["file"]
        app = create_app(self.config, self.index)
        client = TestClient(app)

        latest = client.get("/files/latest?n=5")
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["files"][0]["id"], file_item["id"])

        search = client.get("/files/search", params={"q": "api search target"})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["files"][0]["id"], file_item["id"])

        query = client.post("/files/query", json={"query": "what file mentions api search target", "limit": 5})
        self.assertEqual(query.status_code, 200)
        self.assertEqual(query.json()["mode"], "local_index_resolution")

        webhook = client.post(
            "/webhooks",
            json={"url": "http://127.0.0.1:9/hook", "events": ["FILE_CREATED"], "filter": {"ext": ["md"]}},
        )
        self.assertEqual(webhook.status_code, 200)
        self.assertEqual(len(client.get("/webhooks").json()["webhooks"]), 1)

        patch = client.patch("/config", json={"debounce_ms": 10, "ai_summaries_enabled": True})
        self.assertEqual(patch.status_code, 200)
        self.assertEqual(patch.json()["config"]["debounce_ms"], 10)
        self.assertTrue(patch.json()["config"]["ai_summaries_enabled"])

        tag = client.post(f"/files/{file_item['id']}/tags", json={"tag": "user-check"})
        self.assertEqual(tag.status_code, 200)
        self.assertIn("user-check", tag.json()["tags"])

        delete = client.delete(f"/files/{file_item['id']}")
        self.assertEqual(delete.status_code, 200)
        missing = client.get(f"/files/{file_item['id']}")
        self.assertEqual(missing.status_code, 404)

    def test_watchdog_live_event_indexes_created_file_when_available(self):
        try:
            import watchdog  # noqa: F401
        except ImportError:
            self.skipTest("watchdog is not installed")

        self.config.debounce_ms = 50
        watcher = DebouncedWatcher(self.config, self.pipeline, EventBus())
        watcher.start()
        try:
            live_file = self.watch / "live.md"
            live_file.write_text("live watchdog signal", encoding="utf-8")
            deadline = time.time() + 5
            found = []
            while time.time() < deadline:
                found = self.index.search("live watchdog signal")
                if found:
                    break
                time.sleep(0.1)
            self.assertTrue(found)
            self.assertEqual(found[0]["filename"], "live.md")
        finally:
            watcher.stop()


if __name__ == "__main__":
    unittest.main()
