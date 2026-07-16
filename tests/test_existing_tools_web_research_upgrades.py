import json
from pathlib import Path

import pytest

import ares.tools.research_upgrades as upgrade_module
from ares.memory import MemoryStore
from ares.tools import ToolExecutor
from ares.tools.definitions import get_tool_definitions
from ares.tools.research_upgrades import ResearchUpgradeStore, advanced_fetch


ENVELOPE_KEYS = {
    "ok", "status", "summary", "data", "artifacts", "warnings", "errors",
    "next_actions", "provenance", "metrics", "undo_id",
}


def _search_payload(args, fetch_top):
    del fetch_top
    query = args["query"]
    results = [
        {
            "title": "Ares architecture",
            "url": "https://docs.example/ares",
            "snippet": "Ares supports resumable research and evidence tracking.",
            "host": "docs.example",
            "rank_score": 1.2,
        },
        {
            "title": "Ares limitations",
            "url": "https://review.example/ares",
            "snippet": "Ares does not support resumable research and evidence tracking.",
            "host": "review.example",
            "rank_score": 1.0,
        },
    ]
    return {
        "query": query,
        "provider": "fixture",
        "results": results,
        "source_matrix": [
            {
                "url": result["url"],
                "quality_score": 0.8,
                "quality_label": "primary-or-technical",
                "freshness_label": "dated",
            }
            for result in results
        ],
        "fetched": [],
        "errors": [],
    }


def test_research_store_builds_conflict_graph_and_resumes(tmp_path):
    store = ResearchUpgradeStore(tmp_path)
    first = store.search(
        {"query": "Ares research", "search_mode": "deep", "max_subqueries": 2},
        _search_payload,
    )

    assert len(first["subqueries"]) == 2
    assert len(first["sources"]) == 2
    assert len(first["claims"]) == 2
    assert first["conflicts"]
    assert store.get(first["research_id"])["query"] == "Ares research"

    resumed = store.search(
        {
            "research_id": first["research_id"],
            "follow_up": "focus on persistence",
            "search_mode": "deep",
            "max_subqueries": 1,
        },
        _search_payload,
    )

    assert resumed["research_id"] == first["research_id"]
    assert any("persistence" in query for query in resumed["subqueries"])
    store.close()


def test_research_store_preserves_partial_state_when_provider_fails(tmp_path):
    store = ResearchUpgradeStore(tmp_path)

    def unavailable(_args, _fetch_top):
        raise RuntimeError("provider offline")

    state = store.search(
        {"query": "Ares research", "search_mode": "quick"}, unavailable,
    )

    assert state["sources"] == []
    assert "provider offline" in state["errors"][0]
    assert store.get(state["research_id"])["errors"] == state["errors"]
    store.close()


def test_advanced_fetch_extracts_structured_fields_and_detects_change(tmp_path, monkeypatch):
    pages = iter([
        """<html><head><meta name="description" content="First"></head><body>
        <h1 id="overview">Overview</h1><div class="answer">Version 1 costs 10.</div>
        <a href="/details">Details</a><script type="application/ld+json">{"name":"Ares"}</script>
        </body></html>""",
        """<html><head><meta name="description" content="Second"></head><body>
        <h1 id="overview">Overview</h1><div class="answer">Version 2 costs 20.</div>
        <a href="/details">Details</a><script type="application/ld+json">{"name":"Ares"}</script>
        </body></html>""",
    ])

    monkeypatch.setattr(upgrade_module, "validate_public_remote_url", lambda value: value)

    def fake_fetch(url, **_kwargs):
        return {
            "url": url,
            "final_url": url,
            "title": "Ares",
            "content": next(pages),
            "content_type": "text/html",
            "error": "",
        }

    monkeypatch.setattr(upgrade_module, "fetch_url", fake_fetch)
    store = ResearchUpgradeStore(tmp_path)
    args = {
        "url": "https://example.com/page",
        "selector": "div.answer",
        "pattern": r"costs\s+(\d+)",
        "extract": ["links", "meta", "json-ld", "headings"],
    }
    first = advanced_fetch(args, store)
    second = advanced_fetch(args, store)

    assert first["content"] == "Version 1 costs 10."
    assert first["selection"]["pattern_matches"] == ["10"]
    assert first["selection"]["links"][0]["url"] == "https://example.com/details"
    assert first["selection"]["meta"]["description"] == "First"
    assert first["selection"]["json_ld"] == [{"name": "Ares"}]
    assert first["snapshot"]["changed"] is False
    assert second["snapshot"]["changed"] is True
    assert second["snapshot"]["change_summary"]
    store.close()


@pytest.mark.asyncio
async def test_executor_returns_structured_resumable_research_off_event_loop(
    tmp_path, fake_embedding_provider, monkeypatch,
):
    executor = ToolExecutor(
        memory_store=MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider),
    )
    monkeypatch.setattr(executor, "_research_search_payload", _search_payload)

    result = json.loads(await executor.execute_async("web_search", {
        "query": "Ares research",
        "search_mode": "fact-check",
        "max_subqueries": 1,
        "response_format": "structured",
    }))

    assert set(result) == ENVELOPE_KEYS
    assert result["ok"] is True
    assert result["data"]["research_id"]
    assert result["data"]["conflicts"]
    assert result["metrics"]["source_count"] == 2
    executor.close()


def test_executor_compares_documents_and_renders_existing_research(
    tmp_path, fake_embedding_provider, monkeypatch,
):
    left = tmp_path / "left.md"
    right = tmp_path / "right.md"
    left.write_text("# Design\n\nMode: fast\ncontact: ares@example.com\n", encoding="utf-8")
    right.write_text("# Design\n\nMode: safe\ncontact: ares@example.com\n", encoding="utf-8")
    executor = ToolExecutor(
        memory_store=MemoryStore(tmp_path / "memory.db", embedding_provider=fake_embedding_provider),
    )
    monkeypatch.setattr(executor, "_research_search_payload", _search_payload)

    extracted = json.loads(executor.execute("extract_document", {
        "paths": [str(left), str(right)],
        "mode": "compare",
        "response_format": "structured",
    }))
    assert set(extracted) == ENVELOPE_KEYS
    assert extracted["data"]["document_count"] == 2
    assert extracted["data"]["comparison"][0]["diff"]
    assert extracted["data"]["documents"][0]["outline"] == ["# Design"]
    assert extracted["data"]["documents"][0]["entities"]["emails"] == ["ares@example.com"]

    research = json.loads(executor.execute("web_search", {
        "query": "Ares research", "search_mode": "deep", "max_subqueries": 1,
    }))
    report = json.loads(executor.execute("create_research_report", {
        "research_id": research["research_id"],
        "style": "decision",
        "response_format": "structured",
    }))
    report_path = Path(report["artifacts"][0]["path"])
    assert set(report) == ENVELOPE_KEYS
    assert report_path.exists()
    assert "## Recommendation" in report_path.read_text(encoding="utf-8")
    assert report["data"]["research_id"] == research["research_id"]

    json_report = json.loads(executor.execute("create_research_report", {
        "research_id": research["research_id"],
        "style": "technical",
        "output_format": "json",
        "output_path": str(tmp_path / "technical-report"),
        "response_format": "structured",
    }))
    json_path = Path(json_report["artifacts"][0]["path"])
    assert json_path.name == "technical-report.json"
    assert json.loads(json_path.read_text(encoding="utf-8"))["research"]["research_id"] == research["research_id"]
    executor.close()


def test_web_research_definitions_expose_advanced_modes_without_renaming_tools():
    definitions = {
        item["function"]["name"]: item["function"]["parameters"]
        for item in get_tool_definitions()
    }

    assert "deep" in definitions["web_search"]["properties"]["search_mode"]["enum"]
    assert "response_format" in definitions["fetch_url"]["properties"]
    assert "documents" in definitions["extract_document"]["properties"]
    assert "decision" in definitions["create_research_report"]["properties"]["style"]["enum"]
