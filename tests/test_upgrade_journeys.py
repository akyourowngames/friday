"""System-level journeys for the established Ares tool surface.

External providers and a real phone are represented by deterministic local
fixtures; all stateful Ares paths (HTTP parsing, files, SQLite, shells, cron
leases, image encoders, and export/import) run for real.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import sys
import threading
from pathlib import Path

from PIL import Image

from ares.conversations import ConversationStore
from ares.cron.store import CronStore
from ares.cron.tools import CronToolHandlers
from ares.exporter import export_data, import_data
from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools.filesystem import read_file, search_files
from ares.tools.filesystem_write import batch_edit, edit_file, undo_last_edit, write_file
from ares.tools.image_edit import convert_image, crop_image, resize_image
from ares.tools.repl import PersistentREPL
from ares.tools.web import fetch_url


class _ResearchHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>Local Research</title></head><body><h1>Evidence</h1><p>Verified local evidence.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _local_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ResearchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_journey_1_fetch_evidence_and_save_note(tmp_path):
    server, thread = _local_server()
    try:
        page = fetch_url(f"http://127.0.0.1:{server.server_port}/research")
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert page["error"] == "" and "Verified local evidence" in page["content"]
    note = tmp_path / "research.md"
    assert "Created" in write_file(str(note), page["content"])
    assert "Evidence" in note.read_text(encoding="utf-8")


def test_journey_2_memory_update_and_recall(tmp_path, fake_embedding_provider):
    memory = MemoryStore(db_path=tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    try:
        fact_id = memory.store("User prefers tea", confidence=1.0)
        assert memory.update(fact_id, fact_text="User prefers coffee", confidence=0.8)
        recalled = memory.search("prefers coffee", limit=1)
        assert recalled[0]["fact_id"] == fact_id and "coffee" in recalled[0]["fact_text"]
    finally:
        memory.close()


def test_journey_3_search_read_edit_run_and_undo(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("value = 'old'\nprint(value)\n", encoding="utf-8")
    assert "sample.py" in search_files("old", str(tmp_path))
    assert "value = 'old'" in read_file(str(target))
    assert "Edited" in edit_file(str(target), "old", "new")
    repl = PersistentREPL()
    try:
        assert "new" in repl.execute_python(f"exec(open(r'{target}').read())")
    finally:
        repl.close()
    assert "Restored" in undo_last_edit(str(target))
    assert "old" in target.read_text(encoding="utf-8")


def test_journey_4_batch_failure_restores_exact_state(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"first-before")
    second.write_bytes(b"second-before")
    result = batch_edit(
        [
            {"action": "write", "path": str(first), "content": "changed"},
            {"action": "edit", "path": str(second), "old_text": "missing", "new_text": "nope"},
        ],
        confirm=True,
    )
    assert "rolled back" in result.lower()
    assert first.read_bytes() == b"first-before" and second.read_bytes() == b"second-before"


def test_journey_5_child_process_output_is_observable():
    repl = PersistentREPL()
    try:
        result = repl.execute_python("import subprocess, sys; subprocess.run([sys.executable, '-c', \"print('journey-child')\"])")
    finally:
        repl.close()
    assert "journey-child" in result


def test_journey_6_terminal_state_persists_across_commands(tmp_path):
    repl = PersistentREPL()
    try:
        if sys.platform == "win32":
            repl.execute_shell("set JOURNEY_STATE=kept")
            repl.execute_shell(f'cd /d "{tmp_path}"')
            assert "kept" in repl.execute_shell("echo %JOURNEY_STATE%")
            assert str(tmp_path).casefold() in repl.execute_shell("cd").casefold()
        else:
            repl.execute_shell("export JOURNEY_STATE=kept")
            repl.execute_shell(f"cd '{tmp_path}'")
            assert "kept" in repl.execute_shell("echo $JOURNEY_STATE")
            assert str(tmp_path) in repl.execute_shell("pwd")
    finally:
        repl.close()


def test_journey_7_cron_manual_run_has_one_log_and_terminal_state(tmp_path):
    store = CronStore(tmp_path / "ares")
    job = store.create_job("Journey", "do", "* * * * *")

    class Runner:
        def __init__(self, store):
            self.store = store

        async def run_job(self, job_id, *, lease_id=None):
            path = self.store.log_dir(job_id) / "journey.md"
            path.write_text("done", encoding="utf-8")
            self.store.complete_job(job_id, lease_id, status="completed", log_path=path)
            return path

    handlers = CronToolHandlers(store, runner_factory=lambda *, store: Runner(store))
    async def run():
        handlers.run_cron_job_now({"job_id": job["id"]})
        await asyncio.gather(*handlers._run_tasks.values())
    asyncio.run(run())
    completed = store.get_job(job["id"])
    assert completed["state"] == "completed" and completed["run_count"] == 1
    assert Path(completed["last_log_path"]).read_text(encoding="utf-8") == "done"


def test_journey_8_phone_fixture_keeps_capabilities_explicit():
    # The phone bridge’s fixture coverage exercises Unicode and unavailable
    # device paths; this journey checks that no real device action is required
    # for a safe capability read.
    from ares.tools import adb_bridge
    status = json.loads(adb_bridge.phone_status())
    assert "capability_matrix" in status and "permission_preflight" in status


def test_journey_9_resize_crop_convert_preserves_source(tmp_path, monkeypatch):
    monkeypatch.setenv("ARES_ASSET_MANIFEST", str(tmp_path / "manifest.jsonl"))
    source = tmp_path / "source.png"
    Image.new("RGB", (40, 20), "purple").save(source)
    original = source.read_bytes()
    resized = tmp_path / "resized.png"
    cropped = tmp_path / "cropped.png"
    converted = tmp_path / "converted.webp"
    assert "Resized" in resize_image(str(source), width=20, output=str(resized))
    assert "Cropped" in crop_image(str(resized), left=0, top=0, right=10, bottom=10, output=str(cropped))
    assert "Converted" in convert_image(str(cropped), "webp", output=str(converted))
    assert source.read_bytes() == original and converted.exists()


def test_journey_10_export_import_redacts_and_round_trips(tmp_path, fake_embedding_provider):
    source_memory = MemoryStore(db_path=tmp_path / "source.db", embedding_provider=fake_embedding_provider)
    source_conversations = ConversationStore(db_path=tmp_path / "source.db")
    target_memory = MemoryStore(db_path=tmp_path / "target.db", embedding_provider=fake_embedding_provider)
    target_conversations = ConversationStore(db_path=tmp_path / "target.db")
    try:
        source_memory.store("portable fact")
        conversation_id = source_conversations.start_conversation()
        source_conversations.add_exchange(conversation_id, "hello", "world")
        export_path = export_data(
            memory_store=source_memory,
            conversation_store=source_conversations,
            config=AppConfig(api_key="secret"),
            path=tmp_path / "backup.json",
        )
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        assert payload["config"]["api_key"] is None
        counts = import_data(export_path, memory_store=target_memory, conversation_store=target_conversations)
        assert counts["memories"] == 1 and counts["conversations"] == 1
    finally:
        source_memory.close()
        source_conversations.close()
        target_memory.close()
        target_conversations.close()
