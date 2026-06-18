"""Web search providers and summarization for Ares."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from ddgs import DDGS

from ares.config import load_config

try:
    from ddgs.exceptions import RatelimitException, TimeoutException
except Exception:  # pragma: no cover - defensive across ddgs releases
    RatelimitException = TimeoutException = Exception

logger = logging.getLogger(__name__)

BACKEND_PRIORITY = ["bing", "brave", "mojeek", "duckduckgo"]
TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def _bounded_results(max_results: int) -> int:
    return max(1, min(int(max_results), 10))


def _normalize_result(result: dict, *, tavily: bool = False) -> dict[str, str]:
    """Normalize provider result fields."""
    if tavily:
        title = result.get("title", "")
        url = result.get("url", "")
        snippet = result.get("content", "") or result.get("snippet", "")
    else:
        title = result.get("title", "")
        url = result.get("href", "") or result.get("url", "")
        snippet = result.get("body", "") or result.get("snippet", "")
    return {
        "title": str(title).strip(),
        "url": str(url).strip(),
        "snippet": str(snippet).strip(),
    }


def ddgs_search(
    query: str,
    max_results: int = 5,
    max_retries: int = 2,
) -> tuple[list[dict[str, str]], list[str]]:
    """Search with ddgs backend failover."""
    if not query.strip():
        return [], []

    ddgs = DDGS(timeout=10)
    errors: list[str] = []
    bounded = _bounded_results(max_results)

    for _attempt in range(max_retries + 1):
        for backend in BACKEND_PRIORITY:
            try:
                results = ddgs.text(query, max_results=bounded, backend=backend)
                normalized = [
                    _normalize_result(r)
                    for r in results
                    if r.get("title") or r.get("href") or r.get("body")
                ]
                return normalized, errors
            except (RatelimitException, TimeoutException) as exc:
                message = f"{backend}: {exc}"
                logger.warning("Web search backend failed: %s", message)
                errors.append(message)
                continue
            except Exception as exc:
                message = f"{backend}: {exc}"
                logger.warning("Web search backend failed: %s", message)
                errors.append(message)
                continue

    return [], errors


def _tavily_api_key(config_key: str = "") -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip() or config_key.strip()


def tavily_search(
    query: str,
    max_results: int = 5,
    *,
    api_key: str = "",
    search_depth: str = "basic",
    timeout: float = 20.0,
) -> tuple[list[dict[str, str]], str, list[str]]:
    """Search Tavily's official search endpoint."""
    key = _tavily_api_key(api_key)
    if not key:
        return [], "", ["Tavily API key is not configured."]

    payload = {
        "query": query,
        "max_results": _bounded_results(max_results),
        "search_depth": search_depth,
        "include_answer": True,
        "include_raw_content": False,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(TAVILY_SEARCH_URL, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning("Tavily search failed: %s", exc)
        return [], "", [f"Tavily search failed: {exc}"]

    results = [
        _normalize_result(result, tavily=True)
        for result in data.get("results", [])
        if result.get("title") or result.get("url") or result.get("content")
    ]
    answer = str(data.get("answer") or "").strip()
    return results, answer, []


def _first_sentence(text: str, max_chars: int = 180) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    match = re.search(r"(.+?[.!?])(?:\s|$)", cleaned)
    sentence = match.group(1) if match else cleaned
    if len(sentence) > max_chars:
        sentence = sentence[: max_chars - 3].rstrip() + "..."
    return sentence


def summarize_results(results: list[dict[str, str]], answer: str = "") -> str:
    """Create a concise, citation-friendly summary from provider data."""
    if answer.strip():
        return answer.strip()
    bullets = []
    for index, result in enumerate(results[:3], 1):
        sentence = _first_sentence(result.get("snippet", ""))
        if sentence:
            bullets.append(f"[{index}] {sentence}")
    return "\n".join(bullets) if bullets else "No summary available."


def web_search_payload(
    query: str,
    max_results: int = 5,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return structured web search payload with summary and results."""
    config = load_config()
    selected_provider = (provider or config.web_search_provider or "auto").lower()
    errors: list[str] = []
    answer = ""
    actual_provider = selected_provider
    has_tavily_key = bool(_tavily_api_key(config.tavily_api_key))

    if selected_provider == "tavily" or (selected_provider == "auto" and has_tavily_key):
        results, answer, tavily_errors = tavily_search(
            query,
            max_results=max_results,
            api_key=config.tavily_api_key,
            search_depth=config.tavily_search_depth,
        )
        if results or answer:
            actual_provider = "tavily"
        else:
            errors.extend(tavily_errors)
            if selected_provider == "tavily":
                return {
                    "query": query,
                    "provider": "tavily",
                    "summary": "No summary available.",
                    "answer": "",
                    "results": [],
                    "errors": errors,
                }
            results, ddgs_errors = ddgs_search(query, max_results=max_results)
            errors.extend(ddgs_errors)
            actual_provider = "ddgs"
    elif selected_provider == "auto":
        results, ddgs_errors = ddgs_search(query, max_results=max_results)
        errors.extend(ddgs_errors)
        actual_provider = "ddgs"
    elif selected_provider == "ddgs":
        results, ddgs_errors = ddgs_search(query, max_results=max_results)
        errors.extend(ddgs_errors)
        actual_provider = "ddgs"
    else:
        results, ddgs_errors = ddgs_search(query, max_results=max_results)
        errors.append(f"Unknown web search provider '{selected_provider}', used ddgs.")
        errors.extend(ddgs_errors)
        actual_provider = "ddgs"

    return {
        "query": query,
        "provider": actual_provider,
        "summary": summarize_results(results, answer),
        "answer": answer,
        "results": results,
        "errors": errors,
    }


def web_search(
    query: str,
    max_results: int = 5,
    max_retries: int = 2,
) -> list[dict[str, str]]:
    """Compatibility wrapper returning only result rows."""
    results, _errors = ddgs_search(query, max_results=max_results, max_retries=max_retries)
    return results


def format_results(results_or_payload: list[dict] | dict) -> str:
    """Format search payload/results for plain-text fallback use."""
    if isinstance(results_or_payload, dict):
        payload = results_or_payload
        results = payload.get("results", [])
        lines = [
            f"Query: {payload.get('query', '')}",
            f"Provider: {payload.get('provider', '')}",
            f"Summary: {payload.get('summary', 'No summary available.')}",
        ]
        if payload.get("errors"):
            lines.append("Warnings: " + "; ".join(payload["errors"]))
    else:
        results = results_or_payload
        lines = []

    if not results:
        lines.append("No results found.")
        return "\n".join(line for line in lines if line)

    lines.append(f"Found {len(results)} result(s):")
    for index, result in enumerate(results, 1):
        title = result.get("title", "Untitled")
        url = result.get("url", "")
        snippet = result.get("snippet", "")
        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   {snippet[:500]}")
    return "\n".join(lines)


def payload_to_json(payload: dict[str, Any]) -> str:
    """Serialize a web search payload for tool transport."""
    return json.dumps(payload, ensure_ascii=False)
