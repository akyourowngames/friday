from html.parser import HTMLParser
import time
from urllib.parse import urlparse

import httpx
from ddgs import DDGS
from tavily import TavilyClient

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_int,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)

_tavily = None
_WEB_VERSION = "2.0.0"
_SEARCH_PROVIDERS = ("auto", "tavily", "ddgs")


def _provider_attempt(operation, attempts: int | None = None, delay: float | None = None) -> tuple[object | None, str]:
    attempts = max(1, int(attempts if attempts is not None else settings.external_request_attempts))
    delay = max(0.0, float(delay if delay is not None else settings.external_retry_delay))
    last_error = "provider unavailable"
    for attempt in range(attempts):
        try:
            return operation(), ""
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            last_error = f"http {status}"
            if isinstance(status, int) and 400 <= status < 500:
                break
        except httpx.HTTPError as e:
            last_error = e.__class__.__name__
        except Exception as e:
            last_error = e.__class__.__name__
        if attempt < attempts - 1 and delay:
            time.sleep(delay)
    return None, f"{last_error} after {attempts} attempt(s)"


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "title":
            self._in_title = True
        if tag in ("script", "style", "noscript", "svg"):
            self._skip_depth += 1

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        if tag in ("script", "style", "noscript", "svg") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        if not self._skip_depth:
            self.parts.append(text)

    def text(self):
        return " ".join(self.parts)


def _get_tavily():
    global _tavily
    if _tavily is None:
        key = settings.tavily_api_key
        if key:
            _tavily = TavilyClient(api_key=key)
    return _tavily


def _ddgs_client(timeout_seconds: float):
    try:
        return DDGS(timeout=timeout_seconds)
    except TypeError:
        return DDGS()


def _format_search_lines(results: list[dict]) -> list[str]:
    lines = []
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item['title']}\n   {item['body']}\n   {item['url']}")
    return lines


def _result_key(item: dict) -> str:
    url = str(item.get("url", "")).strip().lower()
    if url:
        return url
    return str(item.get("title", "")).strip().lower()


def _merge_results(primary: list[dict] | None, secondary: list[dict] | None, max_results: int) -> list[dict]:
    merged = []
    seen = set()
    for collection in (primary or [], secondary or []):
        for item in collection:
            key = _result_key(item)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            merged.append(item)
            if len(merged) >= max_results:
                return merged
    return merged


def _ddgs_search(query: str, max_results: int, timeout_seconds: float = 10.0) -> tuple[list[dict] | None, str]:
    def run():
        with _ddgs_client(timeout_seconds) as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    results, error = _provider_attempt(run)
    if error:
        return None, error
    if not results:
        return None, ""
    items = []
    for r in results:
        items.append({
            "title": str(r.get("title", "")),
            "body": str(r.get("body", "")),
            "url": str(r.get("href", "")),
            "provider": "ddgs",
        })
    return items, ""


def _tavily_search(query: str, max_results: int, timeout_seconds: float = 10.0) -> tuple[list[dict] | None, str]:
    client = _get_tavily()
    if not client:
        return None, ""

    def run():
        try:
            resp = client.search(query, max_results=max_results, timeout=timeout_seconds)
        except TypeError:
            resp = client.search(query, max_results=max_results)
        return resp.get("results", [])

    results, error = _provider_attempt(run)
    if error:
        return None, error
    if not results:
        return None, ""
    items = []
    for r in results:
        items.append({
            "title": str(r.get("title", "")),
            "body": str(r.get("content", "")),
            "url": str(r.get("url", "")),
            "provider": "tavily",
        })
    return items, ""


def _web_trace(tool_name: str, started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, external_count: int, error_code: str | None = None) -> dict:
    return make_trace(
        tool_name,
        _WEB_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": external_count, "systems": ["web"] if external_count else []},
        error_code,
    )


def _web_error(tool_name: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str = "input_validation", external_count: int = 0):
    trace = _web_trace(tool_name, started_at, started, inputs_received, False, execution_path, "FAILED", 1, external_count, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, _WEB_VERSION, error, started, trace)
    return legacy


def _normalize_timeout_or_error(tool_name: str, timeout_ms, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int):
    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, 10000)
    if timeout_error is None:
        return timeout_value, None
    return None, _web_error(
        tool_name,
        timeout_error,
        response_format,
        trace_enabled,
        started,
        started_at,
        inputs_received,
        f"Error: invalid timeout_ms",
    )


@tool(
    name="web_search",
    description="Search the web for current information, news, weather, or any topic",
    examples=[
        "search for latest AI news",
        "what is the weather in Tokyo",
        "find information about python decorators",
        "who won the super bowl",
    ],
    param_descriptions={
        "query": "Search query",
        "max_results": "Number of results to return, from 1 to 10",
        "provider": "Provider preference: auto, tavily, or ddgs",
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def web_search(
    query: str,
    max_results: int = 8,
    provider: str = "auto",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 6
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    query = str(query or "").strip()
    provider = str(provider or "auto").strip().lower()

    if not query:
        error = error_payload(
            "EMPTY_QUERY",
            "query must not be empty.",
            "query",
            query,
            "non-empty search query",
            False,
            "Pass the search terms to query.",
        )
        return _web_error("web_search", error, response_format, trace_enabled, started, started_at, inputs_received, "No results found. Provider status: empty query")
    if provider not in _SEARCH_PROVIDERS:
        error = error_payload(
            "INVALID_PROVIDER",
            "provider must be auto, tavily, or ddgs.",
            "provider",
            provider,
            "auto, tavily, or ddgs",
            False,
            "Use provider='auto' to keep default fallback behavior.",
        )
        return _web_error("web_search", error, response_format, trace_enabled, started, started_at, inputs_received, "No results found. Provider status: invalid provider")

    max_results, max_error = normalize_int(
        max_results,
        "max_results",
        8,
        1,
        10,
        "Use a max_results value between 1 and 10.",
        "INVALID_RESULT_LIMIT",
    )
    if max_error is not None:
        return _web_error("web_search", max_error, response_format, trace_enabled, started, started_at, inputs_received, "No results found. Provider status: invalid max_results")
    timeout_value, timeout_response = _normalize_timeout_or_error("web_search", timeout_ms, response_format, trace_enabled, started, started_at, inputs_received)
    if timeout_response is not None:
        return timeout_response
    timeout_seconds = timeout_value / 1000

    external_count = 0
    fallback_used = False
    tavily_error = ""
    ddgs_error = ""
    results = None
    tavily_results = None
    ddgs_results = None
    provider_used = ""
    provider_sequence = []
    supplemental_used = False
    if provider in ("auto", "tavily"):
        external_count += 1
        tavily_results, tavily_error = _tavily_search(query, max_results, timeout_seconds)
        if tavily_results:
            results = tavily_results
            provider_used = "tavily"
            provider_sequence.append("tavily")
    if (
        provider == "ddgs"
        or (
            provider == "auto"
            and (not results or len(results) < max_results)
        )
    ):
        if provider == "auto" and tavily_error:
            fallback_used = True
        external_count += 1
        remaining = max_results if not results else max_results - len(results)
        ddgs_results, ddgs_error = _ddgs_search(query, max(1, remaining), timeout_seconds)
        if ddgs_results:
            provider_sequence.append("ddgs")
            supplemental_used = bool(results)
            results = _merge_results(results, ddgs_results, max_results)
            provider_used = "ddgs"
            if supplemental_used:
                provider_used = "+".join(provider_sequence)

    if not results:
        detail = ddgs_error or tavily_error
        result = {
            "query": query,
            "max_results": max_results,
            "provider_requested": provider,
            "provider_used": provider_used,
            "provider_sequence": provider_sequence,
            "fallback_used": fallback_used,
            "supplemental_used": supplemental_used,
            "results": [],
            "result_count": 0,
            "degraded": bool(detail),
            "degraded_reason": detail,
            "provider_status": detail or "no results",
        }
        trace = _web_trace("web_search", started_at, started, inputs_received, True, provider, "PARTIAL", len(result), external_count, "NO_RESULTS")
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("web_search", _WEB_VERSION, result, started, trace)
        if detail:
            return f"No results found. Provider status: {detail}"
        return "No results found"
    result = {
        "query": query,
        "max_results": max_results,
        "provider_requested": provider,
        "provider_used": provider_used,
        "provider_sequence": provider_sequence,
        "fallback_used": fallback_used,
        "supplemental_used": supplemental_used,
        "results": results,
        "result_count": len(results),
        "degraded": fallback_used or bool(tavily_error or ddgs_error),
        "degraded_reason": tavily_error if fallback_used else "",
        "provider_status": "ok",
    }
    trace = _web_trace("web_search", started_at, started, inputs_received, True, provider_used or provider, "SUCCESS", len(result), external_count)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success("web_search", _WEB_VERSION, result, started, trace)
    return "\n\n".join(_format_search_lines(results))


@tool(
    name="web_fetch",
    description="Fetch and read the content of a web page or URL",
    examples=[
        "open https://example.com",
        "read the page at https://en.wikipedia.org/wiki/Python",
    ],
    param_descriptions={
        "url": "Page URL to fetch",
        "max_chars": "Maximum characters of readable page text to return, from 500 to 8000",
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "follow_redirects": "Whether to follow HTTP redirects. Default true preserves existing behavior",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def web_fetch(
    url: str,
    max_chars: int = 4000,
    timeout_ms: int = 0,
    follow_redirects: bool = True,
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 6
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    follow_redirects = coerce_bool(follow_redirects)
    try:
        url = str(url or "").strip()
        if not url:
            error = error_payload(
                "EMPTY_URL",
                "url must not be empty.",
                "url",
                url,
                "http or https URL",
                False,
                "Pass the page URL to fetch.",
            )
            return _web_error("web_fetch", error, response_format, trace_enabled, started, started_at, inputs_received, "Error fetching page: empty URL")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            error = error_payload(
                "INVALID_URL",
                "url must be an absolute http or https URL.",
                "url",
                url,
                "absolute http or https URL",
                False,
                "Include the URL scheme and host, for example https://example.com.",
            )
            return _web_error("web_fetch", error, response_format, trace_enabled, started, started_at, inputs_received, "Error fetching page: invalid URL")
        max_chars, max_error = normalize_int(
            max_chars,
            "max_chars",
            4000,
            500,
            8000,
            "Use a max_chars value between 500 and 8000.",
            "INVALID_MAX_CHARS",
        )
        if max_error is not None:
            return _web_error("web_fetch", max_error, response_format, trace_enabled, started, started_at, inputs_received, "Error fetching page: invalid max_chars")
        timeout_value, timeout_response = _normalize_timeout_or_error("web_fetch", timeout_ms, response_format, trace_enabled, started, started_at, inputs_received)
        if timeout_response is not None:
            return timeout_response
        timeout_seconds = timeout_value / 1000

        def run():
            resp = httpx.get(
                url,
                timeout=timeout_seconds,
                follow_redirects=follow_redirects,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            return resp

        resp, error = _provider_attempt(run)
        if error:
            payload = error_payload(
                "FETCH_FAILED",
                "The page fetch failed after bounded attempts.",
                "url",
                url,
                "reachable http or https page",
                True,
                "Retry with a reachable URL or increase timeout_ms within the allowed range.",
            )
            return _web_error("web_fetch", payload, response_format, trace_enabled, started, started_at, inputs_received, f"Error fetching page: {error}", "fetch", 1)
        final_url = str(resp.url)
        status_code = int(resp.status_code)
        parser = _TextExtractor()
        parser.feed(resp.text)
        text = parser.text().strip()
        truncated = len(text) > max_chars
        display_text = text[:max_chars]
        result = {
            "requested_url": url,
            "final_url": final_url,
            "status_code": status_code,
            "title": parser.title,
            "text": display_text,
            "truncated": truncated,
            "readable_text_found": bool(text),
            "follow_redirects": follow_redirects,
        }
        trace = _web_trace("web_fetch", started_at, started, inputs_received, True, "fetch", "SUCCESS" if text else "PARTIAL", len(result), 1, None if text else "NO_READABLE_TEXT")
        emit_trace(trace, trace_enabled)
        if response_format == "structured":
            return structured_success("web_fetch", _WEB_VERSION, result, started, trace)
        if error:
            return f"Error fetching page: {error}"
        header = [f"URL: {str(resp.url)}", f"Status: {resp.status_code}"]
        if parser.title:
            header.append(f"Title: {parser.title}")
        if not text:
            return "\n".join(header + ["No readable text found"])
        return "\n".join(header + ["", display_text])
    except Exception as e:
        payload = error_payload(
            "FETCH_FAILED",
            "The page fetch failed before completion.",
            "url",
            url,
            "successful page fetch",
            True,
            "Verify the URL and retry if the operation is still needed.",
        )
        return _web_error("web_fetch", payload, response_format, trace_enabled, started, started_at, inputs_received, f"Error fetching page: {e.__class__.__name__}", "fetch", 1)
