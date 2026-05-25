import time as time_module
from datetime import datetime, timezone

import httpx
from ddgs import DDGS

from config import settings
from tools.registry import tool
from tools.runtime import (
    coerce_bool,
    emit_trace,
    error_payload,
    make_trace,
    normalize_response_format,
    normalize_timeout_ms,
    structured_error,
    structured_success,
    utc_now_iso,
)

_USER_AGENT = "KING/1.0 (AI assistant)"
_CACHE_TTL = 30
_REDDIT_VERSION = "2.0.0"

_cache = {}
_cache_ts = {}


def _record_external(stats: dict | None, system: str) -> None:
    if stats is None:
        return
    stats["external_count"] = stats.get("external_count", 0) + 1
    systems = stats.setdefault("systems", [])
    if system not in systems:
        systems.append(system)


def _get(path: str, params: dict = None, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict | None:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
    last_error = "provider unavailable"
    for attempt in range(attempts):
        _record_external(stats, "reddit")
        try:
            r = httpx.get(
                f"https://www.reddit.com{path}.json",
                params=params,
                timeout=timeout_seconds,
                headers={"User-Agent": _USER_AGENT},
            )
            if r.status_code == 429:
                return {"_rate_limited": True}
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else "unknown"
            if status == 404:
                return None
            last_error = f"http {status}"
            if isinstance(status, int) and 400 <= status < 500:
                break
        except httpx.TimeoutException:
            last_error = "timeout"
        except httpx.HTTPError as e:
            last_error = e.__class__.__name__
        except Exception as e:
            last_error = e.__class__.__name__
        if attempt < attempts - 1 and delay:
            time_module.sleep(delay)
    return {"_error": f"{last_error} after {attempts} attempt(s)"}


def _cache_get(key: str, ttl: int = _CACHE_TTL) -> object | None:
    now = time_module.time()
    if key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    return None


def _cache_set(key: str, value: object):
    _cache[key] = value
    _cache_ts[key] = time_module.time()


def _ddgs_client(timeout_seconds: float):
    try:
        return DDGS(timeout=timeout_seconds)
    except TypeError:
        return DDGS()


def _fallback_search_result(query: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
    results = []
    last_error = ""
    for attempt in range(attempts):
        _record_external(stats, "ddgs")
        try:
            with _ddgs_client(timeout_seconds) as ddgs:
                results = list(ddgs.text(query, max_results=min(limit, 10)))
            last_error = ""
            break
        except Exception as e:
            last_error = e.__class__.__name__
        if attempt < attempts - 1 and delay:
            time_module.sleep(delay)
    if not results:
        return {
            "text": "Reddit is blocked from this network and fallback search returned no results",
            "items": [],
            "error": last_error or "fallback search returned no results",
        }
    lines = ["Reddit API is blocked from this network. Fallback web results:"]
    items = []
    for i, result in enumerate(results, 1):
        title = result.get("title", "Untitled")
        body = result.get("body", "")
        href = result.get("href", "")
        lines.append(f"{i}. {title}\n   {body}\n   {href}")
        items.append({"title": title, "body": body, "url": href, "source": "ddgs"})
    return {"text": "\n\n".join(lines), "items": items, "error": ""}


def _fallback_search(query: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> str:
    return _fallback_search_result(query, limit, timeout_seconds, stats)["text"]


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


def _format_post(data: dict) -> str:
    title = data.get("title", "Untitled")
    score = data.get("score", 0)
    author = data.get("author", "[deleted]")
    comments = data.get("num_comments", 0)
    sub = data.get("subreddit", "?")
    time_str = _relative_time(data.get("created_utc", 0))
    ratio = data.get("upvote_ratio", 0.5)
    post_id = data.get("id", "")
    permalink = data.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
    return (
        f"[{score} pts] {title} → r/{sub}  (id: {post_id})\n"
        f"   by u/{author} {time_str} | {ratio*100:.0f}% upvoted | {comments} comments\n"
        f"   {url}"
    )


def _post_item(data: dict) -> dict:
    permalink = data.get("permalink", "")
    url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
    return {
        "id": data.get("id", ""),
        "title": data.get("title", "Untitled"),
        "subreddit": data.get("subreddit", ""),
        "author": data.get("author", "[deleted]"),
        "score": data.get("score", 0),
        "comments": data.get("num_comments", 0),
        "upvote_ratio": data.get("upvote_ratio", 0.5),
        "created_utc": data.get("created_utc", 0),
        "url": url,
        "permalink": permalink,
    }


def _extract_posts(data: dict, limit: int) -> list[dict]:
    if not data or not isinstance(data, dict):
        return []
    kind = data.get("kind")
    if kind == "Listing":
        children = data.get("data", {}).get("children", [])
    elif kind == "t3":
        return [data.get("data", {})]
    elif kind == "more":
        return []
    else:
        children = data.get("data", {}).get("children", []) if data.get("data") else []
    posts = []
    for child in children:
        if child.get("kind") == "t3":
            posts.append(child.get("data", {}))
        if len(posts) >= limit:
            break
    return posts


def _operation_result(
    action: str,
    text: str,
    items: list[dict] | None = None,
    status: str = "ok",
    source: str = "reddit",
    fallback_used: bool = False,
    degraded: bool = False,
    degraded_reason: str = "",
    error: dict | None = None,
    rate_limited: bool = False,
    not_found: bool = False,
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
        "source": source,
        "fallback_used": fallback_used,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "rate_limited": rate_limited,
        "not_found": not_found,
        "cache_hit": cache_hit,
        "source_status": source_status,
        "error": error,
    }
    if extra:
        result.update(extra)
    return result


def _make_error(code: str, message: str, field: str, value, expected: str, retryable: bool, suggestion: str) -> dict:
    return error_payload(code, message, field, value, expected, retryable, suggestion)


def _normalize_limit(value, default: int = 10, maximum: int = 25):
    if value in (None, ""):
        return default, None
    if isinstance(value, bool):
        return None, _make_error(
            "INVALID_LIMIT",
            "limit must be an integer.",
            "limit",
            value,
            f"integer 1..{maximum}",
            False,
            f"Use a Reddit limit from 1 to {maximum}.",
        )
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None, _make_error(
            "INVALID_LIMIT",
            "limit must be an integer.",
            "limit",
            value,
            f"integer 1..{maximum}",
            False,
            f"Use a Reddit limit from 1 to {maximum}.",
        )
    if normalized < 1:
        return None, _make_error(
            "INVALID_LIMIT",
            "limit is outside the supported range.",
            "limit",
            value,
            f"integer 1..{maximum}",
            False,
            f"Use a Reddit limit from 1 to {maximum}.",
        )
    return min(normalized, maximum), None


def _fetch_listing_result(path: str, limit: int, extra_params: dict = None, action: str = "listing", timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    cache_key = f"listing_{path}_{limit}_{extra_params}"
    cached = _cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, dict):
            cached_result = dict(cached)
            cached_result["cache_hit"] = True
            cached_result["source"] = "cache"
            cached_result["source_status"] = "cache hit"
            return cached_result
        return _operation_result(action, cached, source="cache", cache_hit=True, source_status="cache hit")

    params = {"limit": min(limit, 25), "raw_json": 1}
    if extra_params:
        params.update(extra_params)
    data = _get(path, params, timeout_seconds, stats)
    if data is None:
        error = _make_error(
            "NOT_FOUND",
            "The requested Reddit page or subreddit was not found.",
            "subreddit",
            path,
            "existing Reddit page or subreddit",
            False,
            "Check the subreddit or page path and try again.",
        )
        return _operation_result(action, "Subreddit or page not found (404)", status="not_found", error=error, not_found=True, source_status="not found")
    if isinstance(data, dict) and data.get("_rate_limited"):
        error = _make_error(
            "RATE_LIMITED",
            "Reddit rate limited the request.",
            "reddit",
            path,
            "request accepted by Reddit",
            True,
            "Wait a moment and retry the same request.",
        )
        return _operation_result(action, "Reddit rate limited — try again in a moment", status="rate_limited", error=error, rate_limited=True, source_status="rate limited")
    if isinstance(data, dict) and data.get("_error"):
        fallback = _fallback_search_result(f"site:reddit.com{path} reddit posts", limit, timeout_seconds, stats)
        if fallback["items"]:
            result = _operation_result(
                action,
                fallback["text"],
                fallback["items"],
                source="ddgs",
                fallback_used=True,
                degraded=True,
                degraded_reason=data.get("_error", "reddit api unavailable"),
                source_status="reddit unavailable, fallback ok",
            )
            _cache_set(cache_key, result)
            return result
        error = _make_error(
            "PROVIDER_ERROR",
            "Reddit and fallback search were unavailable for this request.",
            "provider",
            "reddit, ddgs",
            "at least one reachable provider",
            True,
            "Retry later or narrow the request.",
        )
        return _operation_result(action, fallback["text"], status="provider_error", source="ddgs", fallback_used=True, degraded=True, degraded_reason=fallback["error"], error=error, source_status="reddit unavailable, fallback failed")

    posts = _extract_posts(data, limit)
    if not posts:
        error = _make_error(
            "NO_RESULTS",
            "Reddit returned no posts for this request.",
            "query",
            path,
            "posts matching the request",
            False,
            "Try a different subreddit, query, or time filter.",
        )
        return _operation_result(action, "No posts found", status="no_results", error=error, source_status="no results")
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    result = _operation_result(action, "\n\n".join(lines), [_post_item(p) for p in posts], source_status="ok")
    _cache_set(cache_key, result)
    return result


def _fetch_listing(path: str, limit: int, extra_params: dict = None) -> str:
    cache_key = f"listing_{path}_{limit}_{extra_params}"
    cached = _cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, dict):
            return cached.get("text", "")
        return cached

    params = {"limit": min(limit, 25), "raw_json": 1}
    if extra_params:
        params.update(extra_params)
    data = _get(path, params)
    if data is None:
        return "Subreddit or page not found (404)"
    if isinstance(data, dict) and data.get("_rate_limited"):
        return "Reddit rate limited — try again in a moment"
    if isinstance(data, dict) and data.get("_error"):
        return _fallback_search(f"site:reddit.com{path} reddit posts", limit)

    posts = _extract_posts(data, limit)
    if not posts:
        return "No posts found"
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    result = "\n\n".join(lines)
    _cache_set(cache_key, result)
    return result


def _fetch_comments(subreddit: str, post_id: str, limit: int) -> str:
    data = _get(f"/r/{subreddit}/comments/{post_id}", {"limit": min(limit, 25), "raw_json": 1})
    if data is None:
        return "Post not found"
    if isinstance(data, dict) and data.get("_rate_limited"):
        return "Reddit rate limited — try again in a moment"
    if not isinstance(data, list) or len(data) < 2:
        return "No comments found"
    post_data = data[0]
    comment_data = data[1]
    posts = _extract_posts(post_data, 1)
    header = _format_post(posts[0]) if posts else "Post not found"
    children = comment_data.get("data", {}).get("children", []) if isinstance(comment_data, dict) else []
    comment_lines = []
    for child in children[:limit]:
        if child.get("kind") == "t1":
            cd = child.get("data", {})
            if cd.get("body"):
                author = cd.get("author", "?")
                body = cd["body"][:400]
                score = cd.get("score", 0)
                comment_lines.append(f"  [{score}] u/{author}: {body}")
    if comment_lines:
        return header + "\n\nComments:\n" + "\n".join(comment_lines)
    return header + "\n\nNo comments yet"


def _fetch_comments_result(subreddit: str, post_id: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    data = _get(f"/r/{subreddit}/comments/{post_id}", {"limit": min(limit, 25), "raw_json": 1}, timeout_seconds, stats)
    extra = {"subreddit": subreddit, "query": post_id}
    if data is None:
        error = _make_error(
            "POST_NOT_FOUND",
            "The Reddit post was not found.",
            "id",
            post_id,
            "existing Reddit post id",
            False,
            "Check the post id and subreddit, then retry.",
        )
        return _operation_result("comments", "Post not found", status="not_found", error=error, not_found=True, source_status="not found", extra=extra)
    if isinstance(data, dict) and data.get("_rate_limited"):
        error = _make_error(
            "RATE_LIMITED",
            "Reddit rate limited the request.",
            "reddit",
            subreddit,
            "request accepted by Reddit",
            True,
            "Wait a moment and retry the same request.",
        )
        return _operation_result("comments", "Reddit rate limited — try again in a moment", status="rate_limited", error=error, rate_limited=True, source_status="rate limited", extra=extra)
    if not isinstance(data, list) or len(data) < 2:
        error = _make_error(
            "NO_RESULTS",
            "Reddit returned no comments for this post.",
            "id",
            post_id,
            "comments for the Reddit post",
            False,
            "Retry later or inspect the post directly.",
        )
        return _operation_result("comments", "No comments found", status="no_results", error=error, source_status="no comments", extra=extra)
    post_data = data[0]
    comment_data = data[1]
    posts = _extract_posts(post_data, 1)
    header = _format_post(posts[0]) if posts else "Post not found"
    children = comment_data.get("data", {}).get("children", []) if isinstance(comment_data, dict) else []
    comment_lines = []
    comments = []
    for child in children[:limit]:
        if child.get("kind") == "t1":
            cd = child.get("data", {})
            if cd.get("body"):
                author = cd.get("author", "?")
                body = cd["body"][:400]
                score = cd.get("score", 0)
                comment_lines.append(f"  [{score}] u/{author}: {body}")
                comments.append({"author": author, "body": body, "score": score, "id": cd.get("id", "")})
    if comment_lines:
        text = header + "\n\nComments:\n" + "\n".join(comment_lines)
    else:
        text = header + "\n\nNo comments yet"
    result_extra = dict(extra)
    result_extra["comments"] = comments
    result_extra["comment_count"] = len(comments)
    return _operation_result("comments", text, [_post_item(posts[0])] if posts else [], source_status="ok", extra=result_extra)


def _search_reddit(query: str, subreddit: str, limit: int, sort: str) -> str:
    if sort not in ("relevance", "hot", "top", "new", "comments"):
        sort = "relevance"
    cache_key = f"search_{query}_{subreddit}_{limit}_{sort}"
    cached = _cache_get(cache_key)
    if cached is not None:
        if isinstance(cached, dict):
            return cached.get("text", "")
        return cached
    path = f"/r/{subreddit}/search" if subreddit else "/search"
    data = _get(path, {"q": query.strip(), "limit": min(limit, 25), "raw_json": 1, "sort": sort})
    if data is None:
        return "No results found"
    if isinstance(data, dict) and data.get("_error"):
        scope = f"r/{subreddit} " if subreddit else ""
        return _fallback_search(f"site:reddit.com {scope}{query}", limit)
    posts = _extract_posts(data, limit)
    if not posts:
        return f"No results for '{query}'"
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    result = f"Reddit search results for '{query}':\n\n" + "\n\n".join(lines)
    _cache_set(cache_key, result)
    return result


def _search_reddit_result(query: str, subreddit: str, limit: int, sort: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    if sort not in ("relevance", "hot", "top", "new", "comments"):
        sort = "relevance"
    cache_key = f"search_{query}_{subreddit}_{limit}_{sort}"
    cached = _cache_get(cache_key)
    extra = {"subreddit": subreddit, "query": query, "sort": sort}
    if cached is not None:
        if isinstance(cached, dict):
            cached_result = dict(cached)
            cached_result["cache_hit"] = True
            cached_result["source"] = "cache"
            cached_result["source_status"] = "cache hit"
            return cached_result
        return _operation_result("search", cached, source="cache", cache_hit=True, source_status="cache hit", extra=extra)
    path = f"/r/{subreddit}/search" if subreddit else "/search"
    data = _get(path, {"q": query.strip(), "limit": min(limit, 25), "raw_json": 1, "sort": sort}, timeout_seconds, stats)
    if data is None:
        error = _make_error(
            "NO_RESULTS",
            "Reddit returned no search results.",
            "query",
            query,
            "posts matching the search query",
            False,
            "Try a different search term or subreddit.",
        )
        return _operation_result("search", "No results found", status="no_results", error=error, source_status="no results", extra=extra)
    if isinstance(data, dict) and data.get("_rate_limited"):
        error = _make_error(
            "RATE_LIMITED",
            "Reddit rate limited the request.",
            "reddit",
            query,
            "request accepted by Reddit",
            True,
            "Wait a moment and retry the same request.",
        )
        return _operation_result("search", "Reddit rate limited — try again in a moment", status="rate_limited", error=error, rate_limited=True, source_status="rate limited", extra=extra)
    if isinstance(data, dict) and data.get("_error"):
        scope = f"r/{subreddit} " if subreddit else ""
        fallback = _fallback_search_result(f"site:reddit.com {scope}{query}", limit, timeout_seconds, stats)
        if fallback["items"]:
            result = _operation_result(
                "search",
                fallback["text"],
                fallback["items"],
                source="ddgs",
                fallback_used=True,
                degraded=True,
                degraded_reason=data.get("_error", "reddit api unavailable"),
                source_status="reddit unavailable, fallback ok",
                extra=extra,
            )
            _cache_set(cache_key, result)
            return result
        error = _make_error(
            "PROVIDER_ERROR",
            "Reddit and fallback search were unavailable for this query.",
            "provider",
            "reddit, ddgs",
            "at least one reachable provider",
            True,
            "Retry later or narrow the query.",
        )
        return _operation_result("search", fallback["text"], status="provider_error", source="ddgs", fallback_used=True, degraded=True, degraded_reason=fallback["error"], error=error, source_status="reddit unavailable, fallback failed", extra=extra)
    posts = _extract_posts(data, limit)
    if not posts:
        error = _make_error(
            "NO_RESULTS",
            "Reddit returned no search results.",
            "query",
            query,
            "posts matching the search query",
            False,
            "Try a different search term or subreddit.",
        )
        return _operation_result("search", f"No results for '{query}'", status="no_results", error=error, source_status="no results", extra=extra)
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    text = f"Reddit search results for '{query}':\n\n" + "\n\n".join(lines)
    result = _operation_result("search", text, [_post_item(p) for p in posts], source_status="ok", extra=extra)
    _cache_set(cache_key, result)
    return result


def _fetch_user(username: str, limit: int) -> str:
    data = _get(f"/user/{username}/overview", {"limit": min(limit, 25), "raw_json": 1})
    if data is None:
        return f"User u/{username} not found"
    posts = _extract_posts(data, limit)
    if not posts:
        return f"u/{username} has no public posts"
    about = _get(f"/user/{username}/about")
    user_info = ""
    if isinstance(about, dict) and about.get("data"):
        ad = about["data"]
        created = _relative_time(ad.get("created_utc", 0))
        karma = ad.get("total_karma", 0)
        user_info = f"u/{username} — {karma:,} karma, joined {created}\n\n"
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    return user_info + "\n\n".join(lines)


def _fetch_user_result(username: str, limit: int, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    data = _get(f"/user/{username}/overview", {"limit": min(limit, 25), "raw_json": 1}, timeout_seconds, stats)
    extra = {"query": username}
    if data is None:
        error = _make_error(
            "USER_NOT_FOUND",
            "The Reddit user was not found.",
            "query",
            username,
            "existing Reddit username",
            False,
            "Check the username and try again.",
        )
        return _operation_result("user", f"User u/{username} not found", status="not_found", error=error, not_found=True, source_status="not found", extra=extra)
    if isinstance(data, dict) and data.get("_rate_limited"):
        error = _make_error(
            "RATE_LIMITED",
            "Reddit rate limited the request.",
            "reddit",
            username,
            "request accepted by Reddit",
            True,
            "Wait a moment and retry the same request.",
        )
        return _operation_result("user", "Reddit rate limited — try again in a moment", status="rate_limited", error=error, rate_limited=True, source_status="rate limited", extra=extra)
    if isinstance(data, dict) and data.get("_error"):
        error = _make_error(
            "PROVIDER_ERROR",
            "Reddit user lookup failed before completion.",
            "provider",
            "reddit",
            "reachable Reddit user endpoint",
            True,
            "Retry later or check Reddit availability.",
        )
        return _operation_result("user", f"User u/{username} not found", status="provider_error", error=error, degraded=True, degraded_reason=data.get("_error", ""), source_status="provider error", extra=extra)
    posts = _extract_posts(data, limit)
    if not posts:
        error = _make_error(
            "NO_RESULTS",
            "The Reddit user has no public posts in the overview response.",
            "query",
            username,
            "public Reddit posts",
            False,
            "Try a different username or inspect comments directly.",
        )
        return _operation_result("user", f"u/{username} has no public posts", status="no_results", error=error, source_status="no public posts", extra=extra)
    about = _get(f"/user/{username}/about", timeout_seconds=timeout_seconds, stats=stats)
    user_info = ""
    user_meta = {}
    degraded = False
    degraded_reason = ""
    if isinstance(about, dict) and about.get("data"):
        ad = about["data"]
        created = _relative_time(ad.get("created_utc", 0))
        karma = ad.get("total_karma", 0)
        user_info = f"u/{username} — {karma:,} karma, joined {created}\n\n"
        user_meta = {"karma": karma, "joined": created}
    elif isinstance(about, dict) and about.get("_error"):
        degraded = True
        degraded_reason = about.get("_error", "about lookup failed")
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    result_extra = dict(extra)
    result_extra["user"] = user_meta
    return _operation_result("user", user_info + "\n\n".join(lines), [_post_item(p) for p in posts], degraded=degraded, degraded_reason=degraded_reason, source_status="ok", extra=result_extra)


def _normalize_subreddit(subreddit: str) -> str:
    value = str(subreddit or "").strip().strip("/")
    if value.lower().startswith("r/"):
        value = value[2:]
    return value.strip("/")


def _reddit_trace(started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, stats: dict, error_code: str | None = None) -> dict:
    return make_trace(
        "reddit",
        _REDDIT_VERSION,
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


def _reddit_error(error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str, stats: dict):
    trace = _reddit_trace(started_at, started, inputs_received, False, execution_path, "FAILED", 1, stats, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error("reddit", _REDDIT_VERSION, error, started, trace)
    return legacy


def _finalize_reddit(result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, stats: dict, include_source_status: bool):
    error = result.get("error")
    status = "FAILED" if error else ("PARTIAL" if result.get("degraded") else "SUCCESS")
    trace = _reddit_trace(started_at, started, inputs_received, True, result.get("action", "unknown"), status, len(result), stats, error["code"] if error else None)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        public_result = dict(result)
        public_result.pop("error", None)
        if not include_source_status:
            public_result.pop("source_status", None)
        if error:
            return structured_error("reddit", _REDDIT_VERSION, error, started, trace)
        return structured_success("reddit", _REDDIT_VERSION, public_result, started, trace)
    return result.get("text", "")


def _run_reddit(action: str, subreddit: str, query: str, limit: int, time_filter: str, sort: str, timeout_seconds: float, stats: dict) -> dict:
    if action == "search":
        return _search_reddit_result(query, subreddit, limit, sort, timeout_seconds, stats)
    if action == "comments":
        return _fetch_comments_result(subreddit, query, limit, timeout_seconds, stats)
    if action == "user":
        return _fetch_user_result(query, limit, timeout_seconds, stats)
    if action == "front":
        return _fetch_listing_result("/", limit, None, "front", timeout_seconds, stats)
    if action == "hot":
        return _fetch_listing_result(f"/r/{subreddit}/hot", limit, None, "hot", timeout_seconds, stats)
    if action == "new":
        return _fetch_listing_result(f"/r/{subreddit}/new", limit, None, "new", timeout_seconds, stats)
    if action == "top":
        if time_filter not in ("hour", "day", "week", "month", "year", "all"):
            time_filter = "week"
        return _fetch_listing_result(f"/r/{subreddit}/top", limit, {"t": time_filter}, "top", timeout_seconds, stats)
    valid = "front, hot <subreddit>, new <subreddit>, top <subreddit> [time], comments <subreddit> <post_id>, search [subreddit] <query>, user <username>"
    error = _make_error(
        "INVALID_ACTION",
        "The Reddit action is not supported.",
        "action",
        action,
        valid,
        False,
        "Use one of the documented Reddit actions.",
    )
    return _operation_result(action, f"Unknown action '{action}'. Available: {valid}", status="invalid_action", error=error, source_status="invalid action")


@tool(
    name="reddit",
    description="Browse Reddit: front page, subreddit hot/new/top posts, search, get comments, view user profiles. Actions: front, hot, new, top, comments, search, user",
    examples=[
        "show reddit front page",
        "hot posts from r/python",
        "top posts from r/wallstreetbets this week",
        "search reddit for AI news",
        "comments from r/AskReddit for post abc123",
        "deepdive into reddit post by id",
        "show reddit user spez",
    ],
    param_descriptions={
        "action": "front (default), hot, new, top, comments, search, user",
        "subreddit": "Subreddit name (without r/) for hot/new/top/comments",
        "query": "Search term for search, post ID for comments, username for user",
        "id": "Alias for query — post ID to get comments for",
        "limit": "Number of results (1-25, default 10)",
        "time": "Time filter for top: hour, day, week (default), month, year, all",
        "sort": "Search sort: relevance, hot, top, new, or comments",
        "timeout_ms": "External request timeout in milliseconds, from 1 to 60000",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
        "include_source_status": "When true in structured mode, include provider/cache status details",
    },
)
def reddit(
    action: str = "front",
    subreddit: str = "",
    query: str = "",
    limit: int = 10,
    time: str = "week",
    id: str = "",
    sort: str = "relevance",
    timeout_ms: int = 0,
    response_format: str = "legacy",
    trace_enabled: bool = False,
    include_source_status: bool = False,
):
    started = time_module.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 11
    stats = {"external_count": 0, "systems": []}
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    include_source_status = coerce_bool(include_source_status)
    limit, limit_error = _normalize_limit(limit, 10, 25)
    if limit_error is not None:
        return _reddit_error(limit_error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a limit between 1 and 25", "input_validation", stats)
    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, 15000)
    if timeout_error is not None:
        return _reddit_error(timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "Error: invalid timeout_ms", "input_validation", stats)
    timeout_seconds = timeout_value / 1000
    action = str(action or "front").strip().lower()
    subreddit = _normalize_subreddit(subreddit)
    query = str(query or "").strip()
    sort = str(sort or "relevance").strip().lower()
    time_filter = str(time or "week").strip().lower()
    if id and not query:
        query = str(id).strip()
    if action == "front" and query:
        action = "search"
    elif action == "front" and subreddit:
        action = "hot"

    if action == "search":
        if not query:
            error = _make_error("MISSING_QUERY", "A Reddit search needs a search term.", "query", query, "non-empty search term", False, "Pass query with the Reddit search terms.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a search term", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "comments":
        if not subreddit:
            error = _make_error("MISSING_SUBREDDIT", "Reddit comments lookup needs a subreddit.", "subreddit", subreddit, "subreddit name", False, "Pass the subreddit that contains the post.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a subreddit name", "input_validation", stats)
        if not query:
            error = _make_error("MISSING_POST_ID", "Reddit comments lookup needs a post id.", "query", query, "Reddit post id", False, "Pass query or id with the Reddit post id.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a post ID", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "user":
        if not query:
            error = _make_error("MISSING_USERNAME", "Reddit user lookup needs a username.", "query", query, "Reddit username", False, "Pass query with the Reddit username.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a username", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "front":
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "hot":
        if not subreddit:
            error = _make_error("MISSING_SUBREDDIT", "Reddit hot listing needs a subreddit.", "subreddit", subreddit, "subreddit name", False, "Pass a subreddit name.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a subreddit name", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "new":
        if not subreddit:
            error = _make_error("MISSING_SUBREDDIT", "Reddit new listing needs a subreddit.", "subreddit", subreddit, "subreddit name", False, "Pass a subreddit name.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a subreddit name", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    if action == "top":
        if not subreddit:
            error = _make_error("MISSING_SUBREDDIT", "Reddit top listing needs a subreddit.", "subreddit", subreddit, "subreddit name", False, "Pass a subreddit name.")
            return _reddit_error(error, response_format, trace_enabled, started, started_at, inputs_received, "Provide a subreddit name", "input_validation", stats)
        result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
        return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)

    result = _run_reddit(action, subreddit, query, limit, time_filter, sort, timeout_seconds, stats)
    return _finalize_reddit(result, response_format, trace_enabled, started, started_at, inputs_received, stats, include_source_status)
