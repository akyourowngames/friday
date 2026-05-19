from html.parser import HTMLParser

import httpx
from ddgs import DDGS
from tavily import TavilyClient

from config import settings
from tools.registry import tool

_tavily = None


def _provider_attempt(operation) -> tuple[object | None, str]:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
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
            import time
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


def _ddgs_search(query: str, max_results: int) -> tuple[list[str] | None, str]:
    def run():
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    results, error = _provider_attempt(run)
    if error:
        return None, error
    if not results:
        return None, ""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['body']}\n   {r['href']}")
    return lines, ""


def _tavily_search(query: str, max_results: int) -> tuple[list[str] | None, str]:
    client = _get_tavily()
    if not client:
        return None, ""

    def run():
        resp = client.search(query, max_results=max_results)
        return resp.get("results", [])

    results, error = _provider_attempt(run)
    if error:
        return None, error
    if not results:
        return None, ""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r.get('content', '')}\n   {r['url']}")
    return lines, ""


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
    },
)
def web_search(query: str, max_results: int = 8) -> str:
    max_results = max(1, min(max_results, 10))
    lines, tavily_error = _tavily_search(query, max_results)
    if lines is None:
        lines, ddgs_error = _ddgs_search(query, max_results)
    else:
        ddgs_error = ""
    if not lines:
        detail = ddgs_error or tavily_error
        if detail:
            return f"No results found. Provider status: {detail}"
        return "No results found"
    return "\n\n".join(lines)


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
    },
)
def web_fetch(url: str, max_chars: int = 4000) -> str:
    try:
        max_chars = max(500, min(max_chars, 8000))
        def run():
            resp = httpx.get(
                url,
                timeout=15,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            return resp

        resp, error = _provider_attempt(run)
        if error:
            return f"Error fetching page: {error}"
        parser = _TextExtractor()
        parser.feed(resp.text)
        text = parser.text().strip()
        header = [f"URL: {str(resp.url)}", f"Status: {resp.status_code}"]
        if parser.title:
            header.append(f"Title: {parser.title}")
        if not text:
            return "\n".join(header + ["No readable text found"])
        return "\n".join(header + ["", text[:max_chars]])
    except Exception as e:
        return f"Error fetching page: {e.__class__.__name__}"
