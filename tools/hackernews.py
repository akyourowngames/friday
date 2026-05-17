import time
from datetime import datetime, timezone

import httpx

from tools.registry import tool

_API_BASE = "https://hacker-news.firebaseio.com/v0"
_ENDPOINT_MAP = {
    "top": "topstories",
    "new": "newstories",
    "best": "beststories",
    "ask": "askstories",
    "show": "showstories",
}
_ALGOLIA = "https://hn.algolia.com/api/v1/search"
_CACHE_TTL = 60
_USER_AGENT = "KING/1.0 (AI assistant)"

_cache = {}
_cache_ts = {}


def _get(url: str) -> dict | list | None:
    try:
        r = httpx.get(url, timeout=15, headers={"User-Agent": _USER_AGENT})
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _cached_ids(endpoint: str) -> list[int]:
    now = time.time()
    key = f"ids_{endpoint}"
    if key in _cache and now - _cache_ts.get(key, 0) < _CACHE_TTL:
        return _cache[key]
    fb_endpoint = _ENDPOINT_MAP.get(endpoint, endpoint)
    data = _get(f"{_API_BASE}/{fb_endpoint}.json")
    ids = data if isinstance(data, list) else []
    _cache[key] = ids
    _cache_ts[key] = now
    return ids


def _fetch_item(item_id: int) -> dict | None:
    data = _get(f"{_API_BASE}/item/{item_id}.json")
    if isinstance(data, dict) and data.get("type") in ("story", "comment") and not data.get("deleted"):
        return data
    return None


def _relative_time(timestamp: int) -> str:
    delta = int(datetime.now(timezone.utc).timestamp()) - timestamp
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    if delta < 2592000:
        return f"{delta // 86400}d ago"
    return f"{delta // 2592000}mo ago"


def _format_story(item: dict) -> str:
    title = item.get("title", "Untitled")
    score = item.get("score", 0)
    by = item.get("by", "anonymous")
    time_str = _relative_time(item.get("time", 0))
    comments = item.get("descendants", 0)
    url = item.get("url", f"https://news.ycombinator.com/item?id={item['id']}")
    domain = url.split("/")[2] if "//" in url else "news.ycombinator.com"
    return f"[{score} pts] {title} ({domain})\n   by {by} {time_str} | {comments} comments"


def _fetch_stories(endpoint: str, limit: int) -> str:
    ids = _cached_ids(endpoint)[:limit]
    stories = []
    for sid in ids:
        item = _fetch_item(sid)
        if item:
            stories.append(_format_story(item))
        if len(stories) >= limit:
            break
    if not stories:
        return "No stories right now"
    lines = [f"{i+1}. {s}" for i, s in enumerate(stories)]
    return "\n\n".join(lines)


def _fetch_item_detail(item_id: str) -> str:
    try:
        sid = int(item_id.strip())
    except ValueError:
        return f"Invalid story ID: '{item_id}'"
    item = _fetch_item(sid)
    if not item:
        return f"Story {sid} not found"
    header = _format_story(item)
    text = item.get("text", "")
    if text:
        header += f"\n\n{text}"
    kids = item.get("kids", [])
    if not kids:
        return header + "\n\nNo comments yet"
    comment_lines = []
    for cid in kids[:15]:
        c = _fetch_item(cid)
        if c and not c.get("deleted"):
            author = c.get("by", "?")
            body = (c.get("text", "") or "")[:300]
            comment_lines.append(f"  [{author}] {body}")
    if comment_lines:
        header += f"\n\nTop comments:\n" + "\n".join(comment_lines)
    return header


def _fetch_user(username: str) -> str:
    data = _get(f"{_API_BASE}/user/{username}.json")
    if not data or not isinstance(data, dict):
        return f"User '{username}' not found"
    created = _relative_time(data.get("created", 0))
    karma = data.get("karma", 0)
    submitted = data.get("submitted", [])[:5]
    recent = []
    for sid in submitted:
        item = _fetch_item(sid)
        if item:
            recent.append(f"  - {item.get('title', '(untitled)')}")
    lines = [
        f"User: {username}",
        f"Karma: {karma:,}",
        f"Joined: {created}",
    ]
    if recent:
        lines.append("Recent submissions:")
        lines.extend(recent)
    return "\n".join(lines)


def _search_hn(query: str, limit: int) -> str:
    search_url = _ALGOLIA + "_by_date"
    try:
        r = httpx.get(
            search_url,
            params={"query": query.strip(), "hitsPerPage": limit},
            timeout=15,
            headers={"User-Agent": _USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return "Search unavailable"
    hits = data.get("hits", [])
    if not hits:
        return f"No results for '{query}'"
    lines = []
    for i, hit in enumerate(hits[:limit], 1):
        title = hit.get("title", "Untitled")
        points = hit.get("points", 0)
        author = hit.get("author", "?")
        time_str = _relative_time(hit.get("created_at_i", 0))
        comments = hit.get("num_comments", 0)
        lines.append(f"{i}. [{points} pts] {title}\n   by {author} {time_str} | {comments} comments")
    return "\n\n".join(lines)


@tool(
    name="hackernews",
    description="Browse Hacker News: top/new/best/ask/show stories, view comments, search posts, look up users. Actions: top, new, best, ask, show, comments <story_id>, user <username>, search <query>",
    examples=[
        "show top HN stories",
        "what's new on hacker news",
        "show me Ask HN posts",
        "search hacker news for rust",
        "get comments for story 432423",
        "deepdive into hn story by id",
        "show hn user whoishiring",
    ],
    param_descriptions={
        "action": "top (default), new, best, ask, show, comments, user, search",
        "limit": "Number of results (1-30, default 10)",
        "query": "Story ID for comments, username for user, or search term for search",
        "id": "Alias for query — story ID to get comments for",
    },
)
def hackernews(action: str = "top", limit: int = 10, query: str = "", id: str = "") -> str:
    limit = max(1, min(30, limit))
    if id and not query:
        query = id

    if action == "comments":
        if not query:
            return "Provide a story ID"
        return _fetch_item_detail(query)

    if action == "user":
        if not query:
            return "Provide a username"
        return _fetch_user(query)

    if action == "search":
        if not query:
            return "Provide a search term"
        return _search_hn(query, limit)

    if action in ("top", "new", "best", "ask", "show"):
        return _fetch_stories(action, limit)

    valid = "top, new, best, ask, show, comments <id>, user <username>, search <query>"
    return f"Unknown action '{action}'. Available: {valid}"
