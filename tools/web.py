import re

import httpx
from ddgs import DDGS
from tavily import TavilyClient

from config import settings
from tools.registry import tool

_tavily = None


def _get_tavily():
    global _tavily
    if _tavily is None:
        key = settings.tavily_api_key
        if key:
            _tavily = TavilyClient(api_key=key)
    return _tavily


def _ddgs_search(query: str) -> list[str] | None:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=8))
        if not results:
            return None
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['body']}\n   {r['href']}")
        return lines
    except Exception:
        return None


def _tavily_search(query: str) -> list[str] | None:
    client = _get_tavily()
    if not client:
        return None
    try:
        resp = client.search(query, max_results=8)
        results = resp.get("results", [])
        if not results:
            return None
        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r.get('content', '')}\n   {r['url']}")
        return lines
    except Exception:
        return None


@tool(
    name="web_search",
    description="Search the web for current information, news, weather, or any topic",
    examples=[
        "search for latest AI news",
        "what is the weather in Tokyo",
        "find information about python decorators",
        "who won the super bowl",
    ],
)
def web_search(query: str) -> str:
    lines = _tavily_search(query)
    if lines is None:
        lines = _ddgs_search(query)
    if not lines:
        return "No results found"
    return "\n\n".join(lines)


@tool(
    name="web_fetch",
    description="Fetch and read the content of a web page or URL",
    examples=[
        "open https://example.com",
        "read the page at https://en.wikipedia.org/wiki/Python",
    ],
)
def web_fetch(url: str) -> str:
    try:
        resp = httpx.get(
            url,
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:4000]
    except Exception:
        return "Error fetching page"
