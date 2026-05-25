import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from folder_watcher.api import create_app
from folder_watcher.bus import EventBus
from folder_watcher.configuration import load_config
from folder_watcher.index import FolderIndex
from folder_watcher.ingest import IngestPipeline
from folder_watcher.llm import load_llm_policy
from folder_watcher.watcher import DebouncedWatcher


class FakeLLMPolicy:
    allowed_tables = ["files", "tags", "events", "file_contents"]
    allowed_functions = ["count", "max", "min", "like"]


class FakeLLM:
    policy = FakeLLMPolicy()

    def status(self):
        return {
            "provider": "fake",
            "provider_ready": True,
            "model": "fake-model",
            "summaries_enabled": True,
            "queries_enabled": True,
            "chat_enabled": True,
            "deep_dive_enabled": True,
            "policy": {"allowed_tables": self.policy.allowed_tables},
        }

    def query_available(self):
        return True

    def summaries_available(self):
        return True

    def chat_available(self):
        return True

    def deep_dive_available(self):
        return True

    def generate_sql(self, query: str, limit: int | None = None):
        return {
            "sql": "SELECT id, path, filename, extension, mime_type, summary, tags_json FROM files WHERE status = 'active' ORDER BY indexed_ts DESC LIMIT 5",
            "explanation": "Return active indexed files.",
            "row_limit": 5,
        }

    def summarize_file(self, file_record: dict, content: str):
        return {
            "summary": "This file is summarized by the fake LLM for deterministic tests.",
            "tags": ["llm-summary", "test-evidence"],
            "provider": "fake",
        }

    def chat(self, message: str, history: list[dict] | None = None, file_id: str | None = None, limit: int | None = None):
        return {
            "answer": "Fake natural chat grounded in folder watcher evidence: " + message,
            "provider": "fake",
            "selected_file": {"id": file_id} if file_id else None,
            "files": [],
            "stats": {"active_files": 1},
        }

    def deep_dive_file(self, file_id: str):
        return {
            "answer": "Fake deep dive for selected file " + file_id,
            "provider": "fake",
            "file": {"id": file_id, "filename": "fake.md"},
            "dependencies": [],
            "dependents": [],
            "events": [],
        }


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
                "- llm_queries_enabled: true",
                "- llm_policy_file: watcher-llm.md",
                "- hot_file_event_threshold: 2",
                "- hot_file_window_seconds: 3600",
                "- anomaly_events_enabled: true",
                "- ocr_enabled: false",
                "- transcription_enabled: false",
                "- subscriber_rate_limit_per_sec: 30",
                "- webhook_rate_limit_per_sec: 10",
                "- playlist_path: new-arrivals.m3u",
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
                "- mime-prefix:audio/ -> audio",
                "- directory:models -> ml-model",
                "- size-over:1KB -> large-file",
                "## Directory Intent Rules",
                "- prompts: .txt,.md,.json",
                "- audio: .mp3,.wav,.flac",
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
        self.assertEqual(self.config.hot_file_event_threshold, 2)
        self.assertEqual(self.config.playlist_path, (self.root / "new-arrivals.m3u").resolve())
        self.assertEqual(self.config.directory_intents[0].directory, "prompts")

        self.config.llm_policy_path.write_text(
            "\n".join(
                [
                    "# Test LLM Policy",
                    "## Runtime",
                    "- chat_enabled: true",
                    "- deep_dive_enabled: true",
                    "- max_chat_chars: 900",
                    "- chat_context_files: 4",
                    "## Allowed SQL Tables",
                    "- files",
                    "## Allowed SQL Functions",
                    "- count",
                    "## Chat System Prompt",
                    "Chat from evidence.",
                    "## Deep Dive System Prompt",
                    "Deep dive from evidence.",
                ]
            ),
            encoding="utf-8",
        )
        policy = load_llm_policy(self.root, self.config.llm_policy_path)
        self.assertTrue(policy.chat_enabled)
        self.assertTrue(policy.deep_dive_enabled)
        self.assertEqual(policy.max_chat_chars, 900)
        self.assertEqual(policy.chat_context_files, 4)
        self.assertIn("Chat from evidence", policy.chat_prompt)

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
        self.config.llm_queries_enabled = False
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
        self.assertEqual(query.json()["mode"], "local_fallback")

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

    def test_status_and_dashboard_make_runtime_visible(self):
        note = self.watch / "visible.md"
        note.write_text("visible dashboard target", encoding="utf-8")
        self.pipeline.ingest_path(note)
        app = create_app(self.config, self.index)
        client = TestClient(app)

        status = client.get("/status")
        self.assertEqual(status.status_code, 200)
        payload = status.json()
        self.assertIn("implemented", payload)
        self.assertIn("planned", payload)
        self.assertEqual(payload["runtime"]["watch_path"], str(self.config.watch_path))
        self.assertIn("llm", payload["runtime"])

        dashboard = client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("KING Folder Watcher", dashboard.text)
        self.assertIn("/watch", dashboard.text)
        self.assertIn("/chat", dashboard.text)
        self.assertIn("Deep Dive", dashboard.text)

    def test_llm_query_summary_and_sql_guard_are_structural(self):
        note = self.watch / "llm-note.md"
        note.write_text("semantic model driven watcher target", encoding="utf-8")
        file_item = self.pipeline.ingest_path(note)["file"]
        app = create_app(self.config, self.index, llm_service=FakeLLM())
        client = TestClient(app)

        query = client.post("/files/query", json={"query": "show the latest active file", "limit": 5})
        self.assertEqual(query.status_code, 200)
        self.assertEqual(query.json()["mode"], "llm_sql")
        self.assertEqual(query.json()["provider_sql_generation"], "active")
        self.assertTrue(query.json()["rows"])

        summary = client.get(f"/files/{file_item['id']}/summary")
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["status"], "ready")
        self.assertIn("llm-summary", summary.json()["tags"])

        blocked = self.index.readonly_query(
            "DELETE FROM files",
            FakeLLM.policy.allowed_tables,
            FakeLLM.policy.allowed_functions,
            5,
        )
        self.assertEqual(blocked["status"], "blocked")

    def test_chat_and_deep_dive_are_provider_backed_and_grounded(self):
        note = self.watch / "chat-target.md"
        note.write_text("natural chat deep dive evidence", encoding="utf-8")
        file_item = self.pipeline.ingest_path(note)["file"]
        app = create_app(self.config, self.index, llm_service=FakeLLM())
        client = TestClient(app)

        chat = client.post(
            "/chat",
            json={
                "message": "what is here and what matters",
                "history": [{"role": "user", "content": "hello"}],
                "file_id": file_item["id"],
                "limit": 4,
            },
        )
        self.assertEqual(chat.status_code, 200)
        self.assertEqual(chat.json()["mode"], "llm_chat")
        self.assertIn("Fake natural chat", chat.json()["answer"])
        self.assertEqual(chat.json()["selected_file"]["id"], file_item["id"])

        deep_dive = client.get(f"/files/{file_item['id']}/deep-dive")
        self.assertEqual(deep_dive.status_code, 200)
        self.assertEqual(deep_dive.json()["mode"], "llm_deep_dive")
        self.assertIn(file_item["id"], deep_dive.json()["answer"])

    def test_document_and_media_extractors_store_real_metadata(self):
        try:
            from docx import Document
            from PIL import Image
            from pypdf import PdfWriter
        except ImportError as exc:
            self.skipTest(f"optional extractor dependency missing: {exc}")

        pdf_path = self.watch / "brief.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.add_metadata({"/Title": "Watcher Brief"})
        with pdf_path.open("wb") as handle:
            writer.write(handle)

        docx_path = self.watch / "notes.docx"
        document = Document()
        document.add_paragraph("docx semantic evidence paragraph")
        document.save(docx_path)

        image_path = self.watch / "pixel.png"
        Image.new("RGB", (12, 8), color=(40, 80, 120)).save(image_path)

        pdf_item = self.pipeline.ingest_path(pdf_path)["file"]
        docx_item = self.pipeline.ingest_path(docx_path)["file"]
        image_item = self.pipeline.ingest_path(image_path)["file"]

        self.assertEqual(pdf_item["metadata"]["document_kind"], "pdf")
        self.assertEqual(pdf_item["metadata"]["page_count"], 1)
        self.assertEqual(pdf_item["metadata"]["title"], "Watcher Brief")
        self.assertEqual(docx_item["metadata"]["document_kind"], "docx")
        self.assertIn("docx semantic evidence", self.index.get_content(docx_item["id"]))
        self.assertEqual(image_item["metadata"]["media_kind"], "image")
        self.assertEqual(image_item["metadata"]["width"], 12)
        self.assertEqual(image_item["metadata"]["height"], 8)

    def test_graph_hot_anomaly_snapshot_duplicate_and_playlist_surfaces(self):
        prompts = self.watch / "prompts"
        prompts.mkdir()
        bad_prompt = prompts / "unexpected.exe"
        bad_prompt.write_bytes(b"MZnot really executable")

        helper = self.watch / "helper.py"
        helper.write_text("def answer():\n    return 42\n", encoding="utf-8")
        main = self.watch / "main.py"
        main.write_text("import helper\nprint(helper.answer())\n", encoding="utf-8-sig")

        duplicate_one = self.watch / "copy-one.md"
        duplicate_two = self.watch / "copy-two.md"
        duplicate_one.write_text("duplicate body", encoding="utf-8")
        duplicate_two.write_text("duplicate body", encoding="utf-8")

        audio_dir = self.watch / "audio"
        audio_dir.mkdir()
        audio_path = audio_dir / "arrival.wav"
        with wave.open(str(audio_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 800)

        bad_item = self.pipeline.ingest_path(bad_prompt)["file"]
        helper_item = self.pipeline.ingest_path(helper)["file"]
        main_item = self.pipeline.ingest_path(main)["file"]
        self.pipeline.ingest_path(main, "FILE_MODIFIED")
        self.pipeline.ingest_path(duplicate_one)["file"]
        self.pipeline.ingest_path(duplicate_two)["file"]
        audio_item = self.pipeline.ingest_path(audio_path)["file"]

        app = create_app(self.config, self.index)
        client = TestClient(app)

        dependencies = client.get(f"/files/{main_item['id']}/dependencies")
        self.assertEqual(dependencies.status_code, 200)
        self.assertEqual(dependencies.json()["dependencies"][0]["target_file_id"], helper_item["id"])

        dependents = client.get(f"/files/{helper_item['id']}/dependents")
        self.assertEqual(dependents.status_code, 200)
        self.assertEqual(dependents.json()["dependents"][0]["source_file_id"], main_item["id"])

        hot = client.get("/files/hot")
        self.assertEqual(hot.status_code, 200)
        self.assertTrue(any(item["id"] == main_item["id"] for item in hot.json()["files"]))

        anomalies = client.get("/files/anomalies")
        self.assertEqual(anomalies.status_code, 200)
        self.assertEqual(anomalies.json()["events"][0]["file_id"], bad_item["id"])
        self.assertIn("anomaly", bad_item["tags"])

        snapshot = client.get("/files/snapshot")
        self.assertEqual(snapshot.status_code, 200)
        self.assertGreaterEqual(snapshot.json()["count"], 6)

        suggestions = client.get("/files/duplicates/symlink-suggestions")
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(len(suggestions.json()["suggestions"]), 1)

        playlist = client.get("/playlist/new-arrivals")
        self.assertEqual(playlist.status_code, 200)
        self.assertEqual(playlist.json()["playlist"]["count"], 1)
        self.assertEqual(playlist.json()["files"][0]["id"], audio_item["id"])
        self.assertTrue(self.config.playlist_path.exists())

        m3u = client.get("/playlist/new-arrivals", params={"format": "m3u"})
        self.assertEqual(m3u.status_code, 200)
        self.assertIn("#EXTM3U", m3u.text)
        self.assertIn("arrival.wav", m3u.text)

    def test_config_refresh_applies_runtime_markdown_changes(self):
        updated_path = self.root / "updated.m3u"
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                "- playlist_path: new-arrivals.m3u",
                f"- playlist_path: {updated_path.name}",
            ),
            encoding="utf-8",
        )
        fresh = load_config(self.root, self.config_path)
        self.config.refresh_from(fresh)

        self.assertEqual(self.config.playlist_path, updated_path.resolve())
        self.assertEqual(self.config.hot_file_event_threshold, 2)

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
