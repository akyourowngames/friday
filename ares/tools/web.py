"""Web search providers and summarization for Ares."""

from __future__ import annotations

import json
import logging
import os
import re
from html import unescape
from urllib.parse import urljoin
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


def _source_quality(url: str) -> dict[str, Any]:
    """Return a lightweight source quality label for transparent research output."""
    host = ""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    score = 0.5
    label = "standard"
    if host.endswith((".gov", ".edu")):
        score = 0.9
        label = "authoritative"
    elif any(part in host for part in ("docs.", "developer.", "support.", "github.com")):
        score = 0.8
        label = "primary-or-technical"
    elif any(part in host for part in ("wikipedia.org", "reuters.com", "apnews.com")):
        score = 0.7
        label = "reference-or-news"
    elif not host:
        score = 0.2
        label = "unknown"
    return {"score": score, "label": label, "host": host}


def _freshness_label(result: dict[str, str]) -> str:
    """Infer source freshness when provider metadata includes a date-like value."""
    raw = str(
        result.get("date")
        or result.get("published_date")
        or result.get("published")
        or result.get("updated")
        or ""
    ).strip()
    if not raw:
        return "undated"
    return "dated"


def source_matrix(results: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Build source quality/freshness metadata without mutating result rows."""
    matrix = []
    for index, result in enumerate(results, 1):
        quality = _source_quality(result.get("url", ""))
        matrix.append({
            "index": index,
            "title": result.get("title", ""),
            "url": result.get("url", ""),
            "host": quality["host"],
            "quality_score": quality["score"],
            "quality_label": quality["label"],
            "freshness_label": _freshness_label(result),
        })
    return matrix


def web_search_payload(
    query: str,
    max_results: int = 5,
    *,
    provider: str | None = None,
    fetch_top: int = 3,
    max_fetch_chars: int = 8000,
) -> dict[str, Any]:
    """Return structured web search payload with summary, results, AND fetched content.

    Automatically fetches the top N results so Ares doesn't need to call fetch_url separately.
    """
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
                    "source_matrix": [],
                    "fetched": [],
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

    # Automatically fetch top results for full content
    fetched: list[dict[str, Any]] = []
    if fetch_top > 0 and results:
        for result in results[:fetch_top]:
            url = result.get("url", "")
            if not url:
                continue
            try:
                page = fetch_url(url, max_chars=max_fetch_chars)
                if not page["error"]:
                    fetched.append({
                        "url": url,
                        "title": page.get("title", result.get("title", "")),
                        "content": page["content"],
                        "truncated": page.get("truncated", False),
                    })
            except Exception as exc:
                errors.append(f"Failed to fetch {url}: {exc}")

    return {
        "query": query,
        "provider": actual_provider,
        "summary": summarize_results(results, answer),
        "answer": answer,
        "results": results,
        "source_matrix": source_matrix(results),
        "fetched": fetched,
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
        if payload.get("source_matrix"):
            labels = [
                f"{item.get('index')}: {item.get('quality_label')}/{item.get('freshness_label')}"
                for item in payload["source_matrix"][:3]
            ]
            lines.append("Source labels: " + "; ".join(labels))
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


# ---------------------------------------------------------------------------
# fetch_url — fetch and extract readable content from a URL
# ---------------------------------------------------------------------------

MAX_FETCH_BYTES = 2 * 1024 * 1024  # 2 MB limit
DEFAULT_FETCH_TIMEOUT = 15.0


def _strip_html_tags(html: str) -> str:
    """Remove HTML tags and return plain text."""
    # Remove script and style elements
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    # Replace br/p/div/li with newlines
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(p|div|li|h[1-6]|tr|blockquote)[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Remove all remaining tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities.
    text = unescape(text)
    # Collapse multiple whitespace/newlines
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_meta_content(html: str, key: str, value: str) -> str:
    """Extract a content value from a meta tag in either attribute order."""
    attr = re.escape(key)
    wanted = re.escape(value)
    match = re.search(
        rf'<meta[^>]+{attr}=["\']{wanted}["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return unescape(match.group(1).strip())
    match = re.search(
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+{attr}=["\']{wanted}["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return unescape(match.group(1).strip())
    return ""


def _extract_meta_description(html: str) -> str:
    """Try to extract a useful page description from HTML metadata."""
    return (
        _extract_meta_content(html, "name", "description")
        or _extract_meta_content(html, "property", "og:description")
        or _extract_meta_content(html, "name", "twitter:description")
    )


def _extract_canonical_url(html: str, base_url: str) -> str:
    """Extract a canonical URL from HTML when present."""
    for match in re.finditer(r"<link[^>]+>", html, re.IGNORECASE):
        tag = match.group(0)
        rel_match = re.search(r'rel=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not rel_match:
            continue
        rel_values = {value.strip().lower() for value in rel_match.group(1).split()}
        if "canonical" not in rel_values:
            continue
        href_match = re.search(r'href=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if href_match:
            return urljoin(base_url, unescape(href_match.group(1).strip()))
    return ""


def _is_retryable_status(status_code: int) -> bool:
    """Return whether a failed HTTP status is worth retrying later."""
    return status_code in {408, 425, 429} or 500 <= status_code <= 599


def _decode_pdf_literal(raw: bytes) -> str:
    """Decode a small subset of PDF literal-string escapes."""
    text = raw.decode("latin-1", errors="replace")
    replacements = {
        r"\n": "\n",
        r"\r": "\r",
        r"\t": "\t",
        r"\b": "\b",
        r"\f": "\f",
        r"\(": "(",
        r"\)": ")",
        r"\\": "\\",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\\([0-7]{1,3})", lambda m: chr(int(m.group(1), 8)), text)
    return text


def _extract_pdf_text(data: bytes, max_chars: int) -> tuple[str, bool]:
    """Best-effort PDF text extraction without adding a hard dependency.

    The preferred path uses pypdf when available.  The fallback covers simple
    text PDFs produced by many fixtures and lightweight generators.
    """
    try:  # pragma: no cover - exercised only when optional dependency exists.
        from pypdf import PdfReader

        import io

        reader = PdfReader(io.BytesIO(data))
        chunks = [(page.extract_text() or "") for page in reader.pages]
        text = "\n".join(chunk for chunk in chunks if chunk.strip())
    except Exception:
        literals = [
            _decode_pdf_literal(match.group(1))
            for match in re.finditer(rb"\(((?:\\.|[^\\)])*)\)\s*Tj", data, re.DOTALL)
        ]
        if not literals:
            literals = [
                _decode_pdf_literal(match)
                for array in re.findall(rb"\[(.*?)\]\s*TJ", data, re.DOTALL)
                for match in re.findall(rb"\((?:\\.|[^\\)])*\)", array, re.DOTALL)
            ]
        text = "\n".join(literals)

    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n[Content truncated at {max_chars} chars]", True
    return text, False


def fetch_url(
    url: str,
    max_chars: int = 15000,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
    extract_text: bool = True,
) -> dict[str, Any]:
    """Fetch a URL and return its content as readable text.

    Returns dict with keys: url, title, content, content_type, truncated, error
    """
    result: dict[str, Any] = {
        "url": url,
        "final_url": url,
        "canonical_url": "",
        "title": "",
        "description": "",
        "content": "",
        "content_type": "",
        "status_code": None,
        "truncated": False,
        "retryable": False,
        "error": "",
    }

    # Validate URL
    if not url.startswith(("http://", "https://")):
        result["error"] = "URL must start with http:// or https://"
        return result

    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        ) as client:
            response = client.get(url)
            result["status_code"] = response.status_code
            result["final_url"] = str(response.url)
            response.raise_for_status()
    except httpx.TimeoutException:
        result["error"] = f"Request timed out after {timeout}s"
        result["retryable"] = True
        return result
    except httpx.HTTPStatusError as exc:
        result["error"] = f"HTTP {exc.response.status_code}: {exc.response.reason_phrase}"
        result["status_code"] = exc.response.status_code
        result["final_url"] = str(exc.response.url)
        result["retryable"] = _is_retryable_status(exc.response.status_code)
        return result
    except Exception as exc:
        result["error"] = f"Request failed: {exc}"
        result["retryable"] = True
        return result

    content_type = response.headers.get("content-type", "")
    result["content_type"] = content_type

    if "application/pdf" in content_type or result["final_url"].lower().endswith(".pdf"):
        data = response.content[:MAX_FETCH_BYTES]
        text, truncated = _extract_pdf_text(data, max_chars)
        if not text:
            result["error"] = "PDF text extraction produced no readable text."
            return result
        result["content"] = text
        result["truncated"] = truncated or len(response.content) > MAX_FETCH_BYTES
        return result

    # Handle non-HTML content
    if "text/html" not in content_type and "text/" in content_type:
        # Plain text or other text type
        text = response.text[:MAX_FETCH_BYTES]
        if len(text) > max_chars:
            text = text[:max_chars]
            result["truncated"] = True
        result["content"] = text
        return result

    if "text/html" not in content_type:
        result["error"] = f"Unsupported content type: {content_type}. Can only fetch text/HTML."
        return result

    # Parse HTML
    html = response.text[:MAX_FETCH_BYTES]

    # Extract title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if title_match:
        result["title"] = unescape(re.sub(r"\s+", " ", title_match.group(1)).strip())
    result["description"] = _extract_meta_description(html)
    result["canonical_url"] = _extract_canonical_url(html, result["final_url"])

    if extract_text:
        text = _strip_html_tags(html)
    else:
        text = html

    # Truncate if needed
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Content truncated at {} chars]".format(max_chars)
        result["truncated"] = True

    result["content"] = text
    return result


def fetch_url_tool(args: dict) -> str:
    """Tool wrapper for fetch_url."""
    url = args.get("url", "")
    max_chars = int(args.get("max_chars", 15000))
    extract_text = bool(args.get("extract_text", True))

    data = fetch_url(url, max_chars=max_chars, extract_text=extract_text)

    if data["error"]:
        return f"Error fetching {url}: {data['error']}"

    parts = []
    if data["title"]:
        parts.append(f"Title: {data['title']}")
    parts.append(f"URL: {data['url']}")
    if data.get("final_url") and data["final_url"] != data["url"]:
        parts.append(f"Final URL: {data['final_url']}")
    if data.get("canonical_url"):
        parts.append(f"Canonical URL: {data['canonical_url']}")
    if data.get("description"):
        parts.append(f"Description: {data['description']}")
    if data.get("status_code") is not None:
        parts.append(f"Status: {data['status_code']}")
    parts.append(f"Content type: {data['content_type']}")
    parts.append(f"Retryable: {bool(data.get('retryable'))}")
    parts.append("")
    parts.append(data["content"])

    if data["truncated"]:
        parts.append("\n[Content was truncated due to length]")

    return "\n".join(parts)
