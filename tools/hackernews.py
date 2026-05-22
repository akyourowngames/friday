import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import httpx

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
_HN_VERSION = "2.0.0"
_ACTION_ALIASES_PATH = Path(__file__).with_name("HACKERNEWS_ACTION_ALIASES.md")

_cache = {}
_cache_ts = {}
_cache_error = {}


@lru_cache(maxsize=1)
def _load_action_aliases() -> dict:
    aliases = {}
    if not _ACTION_ALIASES_PATH.exists():
        return aliases
    for raw_line in _ACTION_ALIASES_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        left, right = line.split("=", 1)
        alias = left.strip().lower()
        target = right.strip().lower()
        if alias and target:
            aliases[alias] = target
    return aliases


def _normalize_action(action: str) -> str:
    normalized = str(action or "top").strip().lower()
    return _load_action_aliases().get(normalized, normalized)


def _record_external(stats: dict | None, system: str) -> None:
    if stats is None:
        return
    stats["external_count"] = stats.get("external_count", 0) + 1
    systems = stats.setdefault("systems", [])
    if system not in systems:
        systems.append(system)


def _request_json(url: str, params: dict | None = None, timeout_seconds: float = 15.0, stats: dict | None = None, system: str = "hackernews") -> tuple[dict | list | None, str]:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
    last_error = "provider unavailable"
    for attempt in range(attempts):
        _record_external(stats, system)
        try:
            r = httpx.get(url, params=params, timeout=timeout_seconds, headers={"User-Agent": _USER_AGENT})
            r.raise_for_status()
            return r.json(), ""
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


def _get(url: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict | list | None:
    data, error = _request_json(url, timeout_seconds=timeout_seconds, stats=stats)
    if error:
        _cache_error[url] = error
    else:
        _cache_error.pop(url, None)
    return data


def _cached_ids(endpoint: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> list[int]:
    now = time.time()
    key = f"ids_{endpoint}"
    if key in _cache and now - _cache_ts.get(key, 0) < _CACHE_TTL:
        return _cache[key]
    fb_endpoint = _ENDPOINT_MAP.get(endpoint, endpoint)
    url = f"{_API_BASE}/{fb_endpoint}.json"
    data = _get(url, timeout_seconds, stats)
    if not isinstance(data, list):
        _cache_error[key] = _cache_error.get(url, "unexpected response")
        return []
    ids = data
    _cache_error.pop(key, None)
    _cache[key] = ids
    _cache_ts[key] = now
    return ids


def _fetch_item(item_id: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict | None:
    data = _get(f"{_API_BASE}/item/{item_id}.json", timeout_seconds, stats)
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
    hn_url = f"https://news.ycombinator.com/item?id={item['id']}"
    return f"[{score} pts] {title} ({domain})\n   id: {item['id']} | by {by} {time_str} | {comments} comments\n   {url}\n   {hn_url}"


def _story_item(item: dict) -> dict:
    url = item.get("url", f"https://news.ycombinator.com/item?id={item['id']}")
    domain = url.split("/")[2] if "//" in url else "news.ycombinator.com"
    return {
        "id": item.get("id"),
        "title": item.get("title", "Untitled"),
        "score": item.get("score", 0),
        "author": item.get("by", "anonymous"),
        "time": item.get("time", 0),
        "comments": item.get("descendants", 0),
        "url": url,
        "domain": domain,
        "hn_url": f"https://news.ycombinator.com/item?id={item['id']}",
    }


def _operation_result(
    action: str,
    text: str,
    items: list[dict] | None = None,
    status: str = "ok",
    endpoint: str = "",
    provider: str = "hackernews",
    degraded: bool = False,
    degraded_reason: str = "",
    error: dict | None = None,
    cache_hit: bool = False,
    source_status: str = "ok",
    extra: dict | None = None,
) -> dict:
    items = items or []
    result = {
        "action": action,
        "text": text,
        "items": items,
        "count": len(items),
        "status": status,
        "endpoint": endpoint,
        "provider": provider,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "cache_hit": cache_hit,
        "source_status": source_status,
        "error": error,
    }
    if extra:
        result.update(extra)
    return result


def _make_error(code: str, message: str, field: str, value, expected: str, retryable: bool, suggestion: str) -> dict:
    return error_payload(code, message, field, value, expected, retryable, suggestion)


def _fetch_stories_result(endpoint: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    key = f"ids_{endpoint}"
    cache_hit = key in _cache and time.time() - _cache_ts.get(key, 0) < _CACHE_TTL
    ids = _cached_ids(endpoint, timeout_seconds, stats)[:limit]
    if not ids:
        error_detail = _cache_error.get(key)
        if error_detail:
            error = _make_error(
                "PROVIDER_ERROR",
                "Hacker News story ids were unavailable.",
                "provider",
                endpoint,
                "reachable Hacker News Firebase endpoint",
                True,
                "Retry later or choose a different Hacker News endpoint.",
            )
            return _operation_result(endpoint, f"Hacker News {endpoint} unavailable: {error_detail}", status="provider_error", endpoint=endpoint, degraded=True, degraded_reason=error_detail, error=error, cache_hit=cache_hit, source_status=error_detail)
    stories = []
    items = []
    for sid in ids:
        item = _fetch_item(sid, timeout_seconds, stats)
        if item:
            stories.append(_format_story(item))
            items.append(_story_item(item))
        if len(stories) >= limit:
            break
    if not stories:
        error = _make_error(
            "NO_RESULTS",
            "Hacker News returned no stories for this endpoint.",
            "action",
            endpoint,
            "stories from the selected endpoint",
            False,
            "Try another endpoint such as top, new, best, ask, or show.",
        )
        return _operation_result(endpoint, "No stories right now", status="no_results", endpoint=endpoint, error=error, cache_hit=cache_hit, source_status="no stories")
    lines = [f"{i+1}. {s}" for i, s in enumerate(stories)]
    return _operation_result(endpoint, "\n\n".join(lines), items, endpoint=endpoint, cache_hit=cache_hit, source_status="ok")


def _fetch_stories(endpoint: str, limit: int) -> str:
    ids = _cached_ids(endpoint)[:limit]
    if not ids:
        key = f"ids_{endpoint}"
        error = _cache_error.get(key)
        if error:
            return f"Hacker News {endpoint} unavailable: {error}"
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


def _fetch_item_detail_result(item_id: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    try:
        sid = int(str(item_id).strip())
    except ValueError:
        error = _make_error(
            "INVALID_STORY_ID",
            "Hacker News story id must be an integer.",
            "query",
            item_id,
            "integer story id",
            False,
            "Pass a numeric Hacker News item id.",
        )
        return _operation_result("comments", f"Invalid story ID: '{item_id}'", status="invalid_id", error=error, source_status="invalid id", extra={"query": item_id})
    item = _fetch_item(sid, timeout_seconds, stats)
    if not item:
        error = _make_error(
            "STORY_NOT_FOUND",
            "The Hacker News story was not found.",
            "query",
            sid,
            "existing Hacker News story id",
            False,
            "Check the story id and retry.",
        )
        return _operation_result("comments", f"Story {sid} not found", status="not_found", error=error, source_status="not found", extra={"query": sid})
    header = _format_story(item)
    text = item.get("text", "")
    if text:
        header += f"\n\n{text}"
    kids = item.get("kids", [])
    comments = []
    if not kids:
        return _operation_result("comments", header + "\n\nNo comments yet", [_story_item(item)], source_status="ok", extra={"query": sid, "comments": comments, "comment_count": 0})
    comment_lines = []
    for cid in kids[:15]:
        c = _fetch_item(cid, timeout_seconds, stats)
        if c and not c.get("deleted"):
            author = c.get("by", "?")
            body = (c.get("text", "") or "")[:300]
            comment_lines.append(f"  [{author}] {body}")
            comments.append({"id": c.get("id"), "author": author, "body": body})
    if comment_lines:
        header += f"\n\nTop comments:\n" + "\n".join(comment_lines)
    return _operation_result("comments", header, [_story_item(item)], source_status="ok", extra={"query": sid, "comments": comments, "comment_count": len(comments)})


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


def _fetch_user_result(username: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    data = _get(f"{_API_BASE}/user/{username}.json", timeout_seconds, stats)
    if not data or not isinstance(data, dict):
        error = _make_error(
            "USER_NOT_FOUND",
            "The Hacker News user was not found.",
            "query",
            username,
            "existing Hacker News username",
            False,
            "Check the username and retry.",
        )
        return _operation_result("user", f"User '{username}' not found", status="not_found", error=error, source_status="not found", extra={"query": username})
    created = _relative_time(data.get("created", 0))
    karma = data.get("karma", 0)
    submitted = data.get("submitted", [])[:5]
    recent = []
    for sid in submitted:
        item = _fetch_item(sid, timeout_seconds, stats)
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
    return _operation_result("user", "\n".join(lines), [], source_status="ok", extra={"query": username, "user": {"id": username, "karma": karma, "joined": created}, "recent_submissions": recent})


def _search_hn(query: str, limit: int) -> str:
    search_url = _ALGOLIA + "_by_date"
    data, error = _request_json(
        search_url,
        params={"query": query.strip(), "hitsPerPage": limit, "tags": "story"},
    )
    if error or not isinstance(data, dict):
        detail = error or "unexpected response"
        return f"Search unavailable: {detail}"
    hits = data.get("hits", [])
    if not hits:
        return f"No results for '{query}'"
    lines = []
    for i, hit in enumerate(hits[:limit], 1):
        title = hit.get("title") or hit.get("story_title") or "Untitled"
        points = hit.get("points", 0)
        author = hit.get("author", "?")
        time_str = _relative_time(hit.get("created_at_i", 0))
        comments = hit.get("num_comments", 0)
        item_id = hit.get("objectID", "")
        url = hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={item_id}"
        hn_url = f"https://news.ycombinator.com/item?id={item_id}" if item_id else ""
        lines.append(f"{i}. [{points} pts] {title}\n   id: {item_id} | by {author} {time_str} | {comments} comments\n   {url}\n   {hn_url}")
    return "\n\n".join(lines)


def _search_hn_result(query: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    search_url = _ALGOLIA + "_by_date"
    data, error_detail = _request_json(
        search_url,
        params={"query": query.strip(), "hitsPerPage": limit, "tags": "story"},
        timeout_seconds=timeout_seconds,
        stats=stats,
        system="algolia",
    )
    extra = {"query": query}
    if error_detail or not isinstance(data, dict):
        detail = error_detail or "unexpected response"
        error = _make_error(
            "PROVIDER_ERROR",
            "Hacker News search provider was unavailable.",
            "provider",
            "algolia",
            "reachable Algolia Hacker News search endpoint",
            True,
            "Retry later or use a listing action instead.",
        )
        return _operation_result("search", f"Search unavailable: {detail}", status="provider_error", provider="algolia", degraded=True, degraded_reason=detail, error=error, source_status=detail, extra=extra)
    hits = data.get("hits", [])
    if not hits:
        error = _make_error(
            "NO_RESULTS",
            "Hacker News search returned no results.",
            "query",
            query,
            "stories matching the search query",
            False,
            "Try a different search term.",
        )
        return _operation_result("search", f"No results for '{query}'", status="no_results", provider="algolia", error=error, source_status="no results", extra=extra)
    lines = []
    items = []
    for i, hit in enumerate(hits[:limit], 1):
        title = hit.get("title") or hit.get("story_title") or "Untitled"
        points = hit.get("points", 0)
        author = hit.get("author", "?")
        time_str = _relative_time(hit.get("created_at_i", 0))
        comments = hit.get("num_comments", 0)
        item_id = hit.get("objectID", "")
        url = hit.get("url") or hit.get("story_url") or f"https://news.ycombinator.com/item?id={item_id}"
        hn_url = f"https://news.ycombinator.com/item?id={item_id}" if item_id else ""
        lines.append(f"{i}. [{points} pts] {title}\n   id: {item_id} | by {author} {time_str} | {comments} comments\n   {url}\n   {hn_url}")
        items.append({
            "id": item_id,
            "title": title,
            "score": points,
            "author": author,
            "comments": comments,
            "url": url,
            "hn_url": hn_url,
            "created_at_i": hit.get("created_at_i", 0),
        })
    return _operation_result("search", "\n\n".join(lines), items, provider="algolia", source_status="ok", extra=extra)


def _hn_trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, stats: dict, error_code: str | None = None) -> dict:
    return make_trace(
        "hackernews",
        _HN_VERSION,
        started_at,
        started,
        inputs_received,
        schema_valid,
        execution_path,
        status,
        output_fields,
        {"count": stats.get("external_count", 0), "systems": stats.get("systems", [])},
        error_code,
    )


def _hn_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str, stats: dict):
    trace = _hn_trace(started_at, started, inputs_received, False, execution_path, "FAILED", 1, stats, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("hackernews", _HN_VERSION, error, started, trace)
    return legacy


def _finalize_hn(result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, stats: dict, include_source_status: bool):
    error = result.get("error")
    status = "FAILED" if error else ("PARTIAL" if result.get("degraded") else "SUCCESS")
    trace = _hn_trace(started_at, started, inputs_received, True, result.get("action", "unknown"), status, len(result), stats, error["code"] if error else None)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        public_result = dict(result)
        public_result.pop("error", None)
        if not include_source_status:
            public_result.pop("source_status", None)
        if error:
            return structured_error("hackernews", _HN_VERSION, error, started, trace)
        return structured_success("hackernews", _HN_VERSION, public_result, started, trace)
    return result.get("text", "")


def _run_hn(action: str, limit: int, query: str, timeout_seconds: float, stats: dict) -> dict:
    if action == "comments":
        return _fetch_item_detail_result(query, timeout_seconds, stats)
    if action == "user":
        return _fetch_user_result(query, timeout_seconds, stats)
    if action == "search":
        return _search_hn_result(query, limit, timeout_seconds, stats)
    if action in ("top", "new", "best", "ask", "show"):
        return _fetch_stories_result(action, limit, timeout_seconds, stats)
    valid = "top, new, best, ask, show, comments <id>, user <username>, search <query>"
    error = _make_error(
        "INVALID_ACTION",
        "The Hacker News action is not supported.",
        "action",
        action,
        valid,
        False,
        "Use one of the documented Hacker News actions.",
    )
    return _operation_result(action, f"Unknown action '{action}'. Available: {valid}", status="invalid_action", error=error, source_status="invalid action")


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
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
        "include_source_status": "When true in structured mode, include provider/cache status details",
        "id": "Alias for query — story ID to get comments for",
    },
)
def hackernews(
    action: str = "top",
    limit: int = 10,
    query: str = "",
    id: str = "",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
    include_source_status: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 8
    stats = {"external_count": 0, "systems": []}
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_source_status = coerce_bool(include_source_status)
    limit, limit_error = normalize_int(
        limit,
        "limit",
        10,
        1,
        30,
        "Use a Hacker News limit from 1 to 30.",
        "INVALID_LIMIT",
    )
    if limit_error is not None:
        return _hn_error(limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a limit between 1 and 30", "input_validation", stats)
    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, 15000)
    if timeout_error is not None:
        return _hn_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid timeout_ms", "input_validation", stats)
    timeout_seconds = timeout_value / 1000
    action = _normalize_action(action)
    query = str(query or "").strip()
    if id and not query:
        query = str(id).strip()

    if action == "comments":
        if not query:
            error = _make_error("MISSING_STORY_ID", "Hacker News comments lookup needs a story id.", "query", query, "Hacker News story id", False, "Pass query or id with the story id.")
            return _hn_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a story ID", "input_validation", stats)
        result = _run_hn(action, limit, query, timeout_seconds, stats)
        return _finalize_hn(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "user":
        if not query:
            error = _make_error("MISSING_USERNAME", "Hacker News user lookup needs a username.", "query", query, "Hacker News username", False, "Pass query with the Hacker News username.")
            return _hn_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a username", "input_validation", stats)
        result = _run_hn(action, limit, query, timeout_seconds, stats)
        return _finalize_hn(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "search":
        if not query:
            error = _make_error("MISSING_QUERY", "Hacker News search needs a search term.", "query", query, "non-empty search term", False, "Pass query with the Hacker News search terms.")
            return _hn_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a search term", "input_validation", stats)
        result = _run_hn(action, limit, query, timeout_seconds, stats)
        return _finalize_hn(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action in ("top", "new", "best", "ask", "show"):
        result = _run_hn(action, limit, query, timeout_seconds, stats)
        return _finalize_hn(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    result = _run_hn(action, limit, query, timeout_seconds, stats)
    return _finalize_hn(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)
