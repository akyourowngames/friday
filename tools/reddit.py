import time
from datetime import datetime, timezone

import httpx

from tools.registry import tool

_USER_AGENT = "KING/1.0 (AI assistant)"
_CACHE_TTL = 30

_cache = {}
_cache_ts = {}


def _get(path: str, params: dict = None) -> dict | None:
    try:
        r = httpx.get(
            f"https://www.reddit.com{path}.json",
            params=params,
            timeout=15,
            headers={"User-Agent": _USER_AGENT},
        )
        if r.status_code == 429:
            return {"_rate_limited": True}
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        return {"_error": str(e)}
    except Exception:
        return None


def _cache_get(key: str, ttl: int = _CACHE_TTL) -> object | None:
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    return None


def _cache_set(key: str, value: object):
    _cache[key] = value
    _cache_ts[key] = time.time()


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
    return (
        f"[{score} pts] {title} → r/{sub}  (id: {post_id})\n"
        f"   by u/{author} {time_str} | {ratio*100:.0f}% upvoted | {comments} comments"
    )


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


def _fetch_listing(path: str, limit: int, extra_params: dict = None) -> str:
    cache_key = f"listing_{path}_{limit}_{extra_params}"
    cached = _cache_get(cache_key)
    if cached is not None:
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
        return f"Reddit error: {data['_error']}"

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


def _search_reddit(query: str, subreddit: str, limit: int) -> str:
    cache_key = f"search_{query}_{subreddit}_{limit}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    path = f"/r/{subreddit}/search" if subreddit else "/search"
    data = _get(path, {"q": query.strip(), "limit": min(limit, 25), "raw_json": 1, "sort": "relevance"})
    if data is None:
        return "No results found"
    posts = _extract_posts(data, limit)
    if not posts:
        return f"No results for '{query}'"
    lines = [f"{i+1}. {_format_post(p)}" for i, p in enumerate(posts)]
    result = "\n\n".join(lines)
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
    },
)
def reddit(action: str = "front", subreddit: str = "", query: str = "", limit: int = 10, time: str = "week", id: str = "") -> str:
    limit = max(1, min(25, limit))
    action = action.strip().lower()
    if id and not query:
        query = id

    if action == "search":
        if not query:
            return "Provide a search term"
        return _search_reddit(query, subreddit, limit)

    if action == "comments":
        if not subreddit:
            return "Provide a subreddit name"
        if not query:
            return "Provide a post ID"
        return _fetch_comments(subreddit, query, limit)

    if action == "user":
        if not query:
            return "Provide a username"
        return _fetch_user(query, limit)

    if action == "front":
        return _fetch_listing("/", limit, None)

    if action == "hot":
        if not subreddit:
            return "Provide a subreddit name"
        return _fetch_listing(f"/r/{subreddit}/hot", limit, None)

    if action == "new":
        if not subreddit:
            return "Provide a subreddit name"
        return _fetch_listing(f"/r/{subreddit}/new", limit, None)

    if action == "top":
        if not subreddit:
            return "Provide a subreddit name"
        if time not in ("hour", "day", "week", "month", "year", "all"):
            time = "week"
        return _fetch_listing(f"/r/{subreddit}/top", limit, {"t": time})

    valid = "front, hot <subreddit>, new <subreddit>, top <subreddit> [time], comments <subreddit> <post_id>, search [subreddit] <query>, user <username>"
    return f"Unknown action '{action}'. Available: {valid}"
