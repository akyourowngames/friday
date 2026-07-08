"""Tests for web search module."""

from unittest.mock import MagicMock, patch

from ares.models import AppConfig
from ares.tools.web import (
    format_results,
    summarize_results,
    tavily_search,
    web_search,
    web_search_payload,
)


class TestWebSearch:
    def test_format_results_with_data(self):
        results = [
            {"title": "Python Docs", "url": "https://docs.python.org", "snippet": "Official Python docs"},
            {"title": "Real Python", "url": "https://realpython.com", "snippet": "Python tutorials"},
        ]
        output = format_results(results)
        assert "Python Docs" in output
        assert "https://docs.python.org" in output
        assert "2 result" in output

    def test_format_results_empty(self):
        assert "no results" in format_results([]).lower()

    def test_summarize_results_prefers_answer(self):
        assert summarize_results([], answer="Direct answer") == "Direct answer"

    def test_summarize_results_from_snippets(self):
        summary = summarize_results([
            {"snippet": "First useful sentence. Extra words."},
            {"snippet": "Second useful sentence."},
        ])
        assert "[1] First useful sentence." in summary
        assert "[2] Second useful sentence." in summary

    @patch("ares.tools.web.DDGS")
    def test_web_search_returns_results(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs_cls.return_value = mock_ddgs
        mock_ddgs.text.return_value = [
            {"title": "Test", "href": "https://example.com", "body": "A test result"},
        ]

        results = web_search("test query", max_results=1)

        assert results == [
            {"title": "Test", "url": "https://example.com", "snippet": "A test result"},
        ]

    @patch("ares.tools.web.DDGS")
    def test_web_search_empty_results(self, mock_ddgs_cls):
        mock_ddgs = MagicMock()
        mock_ddgs_cls.return_value = mock_ddgs
        mock_ddgs.text.return_value = []
        assert web_search("nonexistent query") == []

    @patch("ares.tools.web.DDGS")
    def test_web_search_failover_on_ratelimit(self, mock_ddgs_cls):
        from ddgs.exceptions import RatelimitException

        mock_ddgs = MagicMock()
        mock_ddgs_cls.return_value = mock_ddgs
        mock_ddgs.text.side_effect = [
            RatelimitException("rate limited"),
            [{"title": "Fallback", "href": "https://fallback.com", "body": "Found via fallback"}],
        ]

        results = web_search("test query")

        assert len(results) == 1
        assert results[0]["title"] == "Fallback"

    @patch("ares.tools.web.DDGS")
    def test_web_search_all_backends_fail(self, mock_ddgs_cls):
        from ddgs.exceptions import RatelimitException

        mock_ddgs = MagicMock()
        mock_ddgs_cls.return_value = mock_ddgs
        mock_ddgs.text.side_effect = RatelimitException("always fail")
        assert web_search("test query") == []

    @patch("ares.tools.web.ddgs_search")
    @patch("ares.tools.web.tavily_search")
    def test_web_search_payload_auto_without_key_uses_ddgs_quietly(self, mock_tavily, mock_ddgs, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr("ares.tools.web.load_config", lambda: AppConfig())
        mock_ddgs.return_value = ([
            {"title": "Fallback", "url": "https://example.com", "snippet": "Fallback snippet."}
        ], [])

        payload = web_search_payload("test query", provider="auto")

        mock_tavily.assert_not_called()
        assert payload["provider"] == "ddgs"
        assert payload["summary"] == "[1] Fallback snippet."
        assert payload["errors"] == []
        assert payload["results"][0]["title"] == "Fallback"

    @patch("ares.tools.web.ddgs_search")
    @patch("ares.tools.web.tavily_search")
    def test_web_search_payload_auto_falls_back_when_tavily_fails(self, mock_tavily, mock_ddgs, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        monkeypatch.setattr("ares.tools.web.load_config", lambda: AppConfig(tavily_api_key="tvly-key"))
        mock_tavily.return_value = ([], "", ["tavily failed"])
        mock_ddgs.return_value = ([
            {"title": "Fallback", "url": "https://example.com", "snippet": "Fallback snippet."}
        ], [])

        payload = web_search_payload("test query", provider="auto")

        assert payload["provider"] == "ddgs"
        assert "tavily failed" in payload["errors"]
        assert payload["results"][0]["title"] == "Fallback"

    @patch("ares.tools.web.httpx.Client")
    def test_tavily_search_request_shape(self, mock_client_cls, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        response = MagicMock()
        response.json.return_value = {
            "answer": "Tavily answer",
            "results": [
                {"title": "Tavily Result", "url": "https://tavily.com", "content": "Search result content."}
            ],
        }
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.post.return_value = response
        mock_client_cls.return_value.__enter__.return_value = client

        results, answer, errors = tavily_search(
            "test query",
            max_results=2,
            api_key="tvly-test",
            search_depth="advanced",
        )

        assert answer == "Tavily answer"
        assert errors == []
        assert results[0]["title"] == "Tavily Result"
        _url, kwargs = client.post.call_args
        assert kwargs["json"]["query"] == "test query"
        assert kwargs["json"]["include_answer"] is True
        assert kwargs["json"]["include_raw_content"] is False
        assert kwargs["json"]["search_depth"] == "advanced"
        assert kwargs["headers"]["Authorization"] == "Bearer tvly-test"

    @patch("ares.tools.web.httpx.Client")
    def test_fetch_url_plain_text_content(self, mock_client_cls):
        from ares.tools.web import fetch_url

        response = MagicMock()
        response.headers = {"content-type": "text/plain; charset=utf-8"}
        response.text = "plain text body"
        response.raise_for_status.return_value = None
        client = MagicMock()
        client.get.return_value = response
        mock_client_cls.return_value.__enter__.return_value = client

        result = fetch_url("https://example.com/readme.txt")

        assert result["error"] == ""
        assert result["content"] == "plain text body"
