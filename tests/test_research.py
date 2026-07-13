import hashlib
import json
import socket
from pathlib import Path

import pytest

import ares.tools.research as research_module
from ares.tools.research import ResearchWorkspace, validate_public_remote_url


class _FakeResponse:
    status_code = 200

    def __init__(self, body: bytes, *, url: str = "https://reports.example/report.pdf") -> None:
        self.headers = {
            "content-type": "application/pdf",
            "content-length": str(len(body)),
        }
        self.url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self, chunk_size=65536):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index:index + chunk_size]


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, _method, _url):
        return self.response


def test_validate_public_remote_url_rejects_private_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("93.184.216.34", 0))],
    )
    assert validate_public_remote_url("https://example.com/report.pdf") == "https://example.com/report.pdf"

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(ValueError, match="public internet"):
        validate_public_remote_url("https://example.com/report.pdf")


def test_download_saves_hashed_file_in_research_workspace(tmp_path, monkeypatch):
    body = b"%PDF-1.4\nBT (Research report) Tj ET\n%%EOF"
    response = _FakeResponse(body)
    monkeypatch.setattr(research_module, "validate_public_remote_url", lambda value: value)
    monkeypatch.setattr(research_module.httpx, "Client", lambda **_kwargs: _FakeClient(response))

    result = ResearchWorkspace(tmp_path).download("https://reports.example/report.pdf")

    assert result["name"].endswith(".pdf")
    assert result["bytes"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert (tmp_path / "research" / "downloads").exists()
    assert Path(result["path"]).read_bytes() == body


def test_extract_document_reads_local_text(tmp_path):
    document = tmp_path / "notes.md"
    document.write_text("# Findings\n\nAres extracted this report.", encoding="utf-8")

    result = ResearchWorkspace(tmp_path).extract_document(path=str(document))

    assert result["kind"] == "text"
    assert "Ares extracted this report." in result["content"]
    assert result["download"] is None


def test_create_report_persists_cited_evidence(tmp_path, monkeypatch):
    payload = {
        "provider": "ddgs",
        "summary": "Primary evidence supports the finding.",
        "results": [{"title": "Public source", "url": "https://example.gov/report", "snippet": "A finding."}],
        "source_matrix": [{
            "index": 1,
            "title": "Public source",
            "url": "https://example.gov/report",
            "quality_label": "authoritative",
            "freshness_label": "dated",
        }],
        "fetched": [{
            "title": "Public source",
            "url": "https://example.gov/report",
            "content": "The complete evidence extracted from the source.",
        }],
        "errors": [],
    }
    monkeypatch.setattr(research_module, "web_search_payload", lambda *_args, **_kwargs: payload)

    result = ResearchWorkspace(tmp_path).create_report("public research", title="Public brief")
    content = Path(result["path"]).read_text(encoding="utf-8")

    assert result["sources"] == 1
    assert "# Public brief" in content
    assert "[Public source](https://example.gov/report)" in content
    assert "complete evidence" in content


def test_executor_exposes_local_document_extraction(tmp_path, fake_embedding_provider):
    from ares.memory import MemoryStore
    from ares.tools import ToolExecutor

    document = tmp_path / "report.txt"
    document.write_text("The source material is ready.", encoding="utf-8")
    executor = ToolExecutor(memory_store=MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider))

    payload = json.loads(executor.execute("extract_document", {"path": str(document)}))

    assert payload["name"] == "report.txt"
    assert payload["content"] == "The source material is ready."
    executor.close()


@pytest.mark.asyncio
async def test_telegram_file_tool_requires_confirmation_and_uses_attached_channel(tmp_path, fake_embedding_provider):
    from ares.memory import MemoryStore
    from ares.tools import ToolExecutor

    class FakeTelegramChannel:
        def __init__(self) -> None:
            self.calls = []

        async def deliver_file(self, *, path, chat_id=None, caption=""):
            self.calls.append((path, chat_id, caption))
            return {"ok": True, "path": path, "chat_id": chat_id or 123}

    executor = ToolExecutor(memory_store=MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider))
    channel = FakeTelegramChannel()
    executor.set_telegram_channel(channel)

    denied = await executor.execute_async("telegram_send_file", {"path": "report.md", "confirm": False})
    delivered = json.loads(await executor.execute_async("telegram_send_file", {
        "path": "report.md", "caption": "Research brief", "confirm": True,
    }))

    assert "Confirm required" in denied
    assert delivered["ok"] is True
    assert channel.calls == [("report.md", None, "Research brief")]
    executor.close()
