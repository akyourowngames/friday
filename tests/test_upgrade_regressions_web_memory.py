"""Regressions for web evidence, durable memory, exports, and time/config."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx
import pytest

from ares.memory import MemoryStore
from ares.models import AppConfig
from ares.tools import exporter, web
from ares.tools.executor import ToolExecutor
from ares.tools.dates import parse_user_datetime
from ares.tools.datetime_tool import get_current_datetime_result
from ares.tools.exporter import export_data
from ares.tools.web import fetch_url, fetch_url_tool, web_search_payload


class _ExecutorMemory:
    db_path = None


@pytest.mark.asyncio
async def test_async_web_search_times_out_without_blocking_other_workspace_tasks(tmp_path, monkeypatch):
    """A synchronous provider must not be able to freeze the WebSocket loop."""
    executor = ToolExecutor(_ExecutorMemory(), config=AppConfig(data_dir=str(tmp_path)))
    started = threading.Event()
    release = threading.Event()
    ticked = asyncio.Event()

    def blocked_provider(_args, *, fetch_top):
        started.set()
        release.wait(timeout=1)
        return {"results": [], "errors": []}

    monkeypatch.setattr(executor, "_research_search_payload", blocked_provider)
    monkeypatch.setattr("ares.tools.executor.WEB_SEARCH_BLOCKING_TIMEOUT_SECONDS", 0.02)
    ticker = asyncio.create_task(_tick(ticked))
    try:
        with pytest.raises(TimeoutError, match="Web search timed out"):
            await executor._web_search_async({"query": "slow provider", "fetch_top": 0})
        assert started.is_set()
        await asyncio.wait_for(ticked.wait(), timeout=0.1)
    finally:
        release.set()
        await ticker
        executor.close()


async def _tick(event):
    await asyncio.sleep(0)
    event.set()


class _StreamResponse:
    def __init__(self, chunks, *, status_code=200, content_type="text/plain; charset=utf-8", url="https://example.test/final"):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url
        self.encoding = "utf-8"
        self.read_chunks = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            self.read_chunks += 1
            yield chunk

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, headers=self.headers, request=httpx.Request("GET", str(self.url)))
            raise httpx.HTTPStatusError("failed", request=response.request, response=response)


class _StreamClient:
    def __init__(self, response, **kwargs):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return self.response


def test_web_preserves_dates_rejects_lookalikes_and_fetches_concurrently(monkeypatch):
    normalized = web._normalize_result(
        {"title": "Official", "href": "https://github.com.example.test/a", "body": "body", "date": "2026-07-01"}
    )
    assert normalized["date"] == "2026-07-01"
    assert web._source_quality("https://github.com.evil.example/a")["label"] == "standard"
    assert web._source_quality("https://api.github.com/a")["label"] == "primary-or-technical"

    monkeypatch.setattr(web, "load_config", lambda: AppConfig())
    monkeypatch.setattr(web, "ddgs_search", lambda *args, **kwargs: ([
        {"title": "one", "url": "https://one.test", "snippet": "one", "date": "2026-07-01"},
        {"title": "two", "url": "https://two.test", "snippet": "two"},
    ], []))

    concurrent_fetches = threading.Barrier(2, timeout=1.0)

    def slow_fetch(url, **kwargs):
        # A barrier proves both fetch workers entered before either completed,
        # without relying on an elapsed-time threshold that is sensitive to
        # full-suite scheduler contention on Windows.
        concurrent_fetches.wait()
        return {"title": url, "content": "evidence", "truncated": False, "status_code": 200, "final_url": url, "retryable": False, "error": "broken" if "two" in url else ""}

    monkeypatch.setattr(web, "fetch_url", slow_fetch)
    payload = web_search_payload("query", provider="ddgs", fetch_top=2)
    assert [item["url"] for item in payload["fetched"]] == ["https://one.test", "https://two.test"]
    assert not any("one.test" in error for error in payload["errors"])
    assert any("two.test" in error for error in payload["errors"])
    assert payload["source_matrix"][0]["freshness_label"] == "dated"


def test_fetch_url_enforces_stream_cap_and_retains_http_failure_metadata(monkeypatch):
    chunks = [b"x" * web.FETCH_CHUNK_BYTES for _ in range(40)]
    response = _StreamResponse(chunks)
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: _StreamClient(response))
    result = fetch_url("https://example.test/large", max_chars=100)
    assert result["truncated"] is True and result["byte_truncated"] is True
    assert len(result["content"]) == 100
    assert response.read_chunks <= web.MAX_FETCH_BYTES // web.FETCH_CHUNK_BYTES + 1

    error_response = _StreamResponse([], status_code=429, url="https://example.test/redirected")
    monkeypatch.setattr(web.httpx, "Client", lambda **kwargs: _StreamClient(error_response))
    failed = fetch_url("https://example.test/rate-limited")
    assert failed["status_code"] == 429
    assert failed["final_url"] == "https://example.test/redirected"
    assert failed["retryable"] is True

    wrapped = fetch_url_tool({"url": "https://example.test/rate-limited"})
    assert "Status: 429" in wrapped
    assert "Final URL: https://example.test/redirected" in wrapped
    assert "Content type: text/plain; charset=utf-8" in wrapped
    assert "Retryable: True" in wrapped


def test_memory_literal_scope_confidence_and_bulk_delete(tmp_path, fake_embedding_provider):
    store = MemoryStore(db_path=tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    try:
        favorite = store.store('user "favorite color" is blue', session_id="current")
        store.vector_enabled = False
        literal = store.search('"favorite', limit=5)
        assert favorite in [row["fact_id"] for row in literal]
        assert store.last_search_diagnostics["fts"].startswith("literal")

        scoped_ids = [store.store(f"session needle {index}", session_id="current") for index in range(4)]
        for index in range(20):
            store.store(f"global needle {index}", session_id=f"other-{index}")
        scoped = store.search("needle", limit=3, scope="session", session_id="current", recent_sessions=0)
        assert len(scoped) == 3
        assert set(row["fact_id"] for row in scoped).issubset(set(scoped_ids) | {favorite})

        high = store.store("same confidence token", confidence=1.0)
        low = store.store("same confidence token", confidence=0.0)
        ranked = store.search("same confidence token", limit=5)
        assert ranked.index(next(row for row in ranked if row["fact_id"] == high)) < ranked.index(next(row for row in ranked if row["fact_id"] == low))
        assert store.bulk_delete([high, 999999, high]) == 1
    finally:
        store.close()


def test_export_config_validation_and_timezone_regressions(tmp_path, fake_embedding_provider, monkeypatch):
    memory = MemoryStore(db_path=tmp_path / "memory.db", embedding_provider=fake_embedding_provider)
    try:
        memory.store("memory")
        config = AppConfig(mcp_servers=[{"name": "private", "env": {"TOKEN": "nested-secret", "NORMAL": "kept"}}])
        output = export_data(memory_store=memory, config=config, path=tmp_path / "export.json", profile="MEMORIES")
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["export_profile"] == "memories"
        assert payload["config"] == {}

        config_output = export_data(memory_store=memory, config=config, path=tmp_path / "config-export.json", profile="CONFIG")
        config_payload = json.loads(config_output.read_text(encoding="utf-8"))
        assert config_payload["config"]["mcp_servers"][0]["env"]["TOKEN"] is None
        assert config_payload["config"]["mcp_servers"][0]["env"]["NORMAL"] == "kept"
        assert exporter.default_export_path() != exporter.default_export_path()

        protected = tmp_path / "protected-export.json"
        protected.write_bytes(b'{"old": true}\n')
        with monkeypatch.context() as scoped:
            scoped.setattr(exporter.os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")))
            with pytest.raises(OSError, match="replace failed"):
                export_data(memory_store=memory, config=config, path=protected)
        assert protected.read_bytes() == b'{"old": true}\n'
    finally:
        memory.close()

    import ares.config as config_module

    config_path = tmp_path / "config.json"
    original = b'{\n  "model": "safe",\n  "phone": {"enabled": false, "adb_path": "old"}\n}\n'
    config_path.write_bytes(original)
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    valid = config_module.update_config_field("phone.enabled", True)
    assert valid["ok"] is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["phone"] == {"enabled": True, "adb_path": "old"}
    before_invalid = config_path.read_bytes()
    assert config_module.update_config_field("phone.unknown", True)["ok"] is False
    assert config_module.update_config_field("phone.enabled", "true")["ok"] is False
    assert config_path.read_bytes() == before_invalid

    invalid_zone = get_current_datetime_result("Mars/Olympus")
    assert invalid_zone["ok"] is False and "Invalid" in invalid_zone["error"]
    parsed = parse_user_datetime("2026-07-10T12:00:00+00:00", timezone_name="America/New_York")
    assert datetime.fromisoformat(parsed).utcoffset() == datetime(2026, 7, 10, tzinfo=ZoneInfo("America/New_York")).utcoffset()
