import json
import os
import random
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

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

PLAYLIST_PATH = Path("storage/playlist.json")
_YOUTUBE_VERSION = "2.0.0"
_PLAYBACK_MODES = ("auto", "open", "skip")


def _get_ffmpeg():
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except Exception:
        return None


def _fmt_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _record_external(stats: dict | None, system: str) -> None:
    if stats is None:
        return
    stats["external_count"] = stats.get("external_count", 0) + 1
    systems = stats.setdefault("systems", [])
    if system not in systems:
        systems.append(system)


def _set_stat(stats: dict | None, key: str, value) -> None:
    if stats is not None:
        stats[key] = value


def _search_youtube(query: str, max_results: int = 10, timeout_seconds: float = 15.0, stats: dict | None = None) -> list[dict]:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
    last_error = ""
    for attempt in range(attempts):
        _record_external(stats, "yt-dlp")
        try:
            result = subprocess.run(
                ["yt-dlp", f"ytsearch{max_results}:{query}", "--dump-json",
                 "--flat-playlist", "--no-download", "--quiet"],
                capture_output=True, text=True, timeout=timeout_seconds,
            )
            entries = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                entries.append({
                    "id": data["id"],
                    "title": data["title"],
                    "url": data["webpage_url"],
                    "view_count": data.get("view_count", 0),
                    "channel": data.get("channel", "Unknown"),
                    "duration": data.get("duration", 0),
                })
            _set_stat(stats, "search_error", "")
            return entries
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        except Exception as e:
            last_error = e.__class__.__name__
        if attempt < attempts - 1 and delay:
            time.sleep(delay)
    _set_stat(stats, "search_error", f"{last_error} after {attempts} attempt(s)" if last_error else "no results")
    return []


def _get_full_info(url: str, timeout_seconds: float = 15.0, stats: dict | None = None) -> dict:
    attempts = max(1, settings.external_request_attempts)
    delay = max(0.0, settings.external_retry_delay)
    last_error = ""
    for attempt in range(attempts):
        _record_external(stats, "yt-dlp")
        try:
            result = subprocess.run(
                ["yt-dlp", "--dump-json", "--no-download", "--quiet", url],
                capture_output=True, text=True, timeout=timeout_seconds,
            )
            data = json.loads(result.stdout.strip())
            return {
                "like_count": data.get("like_count", 0),
                "view_count": data.get("view_count", 0),
            }
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        except Exception as e:
            last_error = e.__class__.__name__
        if attempt < attempts - 1 and delay:
            time.sleep(delay)
    _set_stat(stats, "rank_error", f"{last_error} after {attempts} attempt(s)" if last_error else "metadata unavailable")
    return {"like_count": 0, "view_count": 0}


def _rank_results(entries: list[dict], timeout_seconds: float = 15.0, stats: dict | None = None) -> dict | None:
    if not entries:
        return None
    sorted_by_views = sorted(entries, key=lambda x: x["view_count"], reverse=True)
    candidates = sorted_by_views[:3]
    for c in candidates:
        info = _get_full_info(c["url"], timeout_seconds, stats)
        c["like_count"] = info.get("like_count", 0)

    def score(item):
        return item["view_count"] * 0.7 + item.get("like_count", 0) * 10

    return max(candidates, key=score)


def _start_playback_attempt(url: str, title: str, playback_mode: str = "auto") -> str:
    playback_mode = str(playback_mode or "auto").strip().lower()
    if playback_mode == "skip":
        return f"Playback skipped for '{title}': {url}"

    if playback_mode == "open":
        import webbrowser
        opened = webbrowser.open(url)
        if opened:
            return f"Opened YouTube page for '{title}': {url}"
        return f"Could not open YouTube page for '{title}': {url}"

    def _play():
        ffmpeg = _get_ffmpeg()
        if not ffmpeg:
            import webbrowser
            webbrowser.open(url)
            return

        fd, tmp = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

        try:
            subprocess.run(
                ["yt-dlp", "-f", "bestaudio", "-o", tmp,
                 "--ffmpeg-location", ffmpeg,
                 "-x", "--audio-format", "mp3", "--quiet", url],
                capture_output=True, timeout=120,
            )
            if not os.path.exists(tmp) or os.path.getsize(tmp) < 1000:
                raise RuntimeError("Download failed")

            import pygame
            pygame.mixer.init()
            pygame.mixer.music.load(tmp)
            pygame.mixer.music.play()

            def _cleanup():
                import pygame
                try:
                    pygame.mixer.music.load(tmp)
                except Exception:
                    pass
                time.sleep(3)
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

            threading.Thread(target=_cleanup, daemon=True).start()

        except Exception:
            import webbrowser
            try:
                webbrowser.open(url)
            except Exception:
                pass
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        import webbrowser
        opened = webbrowser.open(url)
        if opened:
            return f"Opened YouTube page for '{title}': {url}"
        return f"Could not open YouTube page for '{title}': {url}"

    threading.Thread(target=_play, daemon=True).start()
    return f"Started playback attempt for '{title}'. Local audio is handled in the background; if it fails, the YouTube page will be opened."


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    for sep in [" â€” ", " â€“ ", " - ", " ft. ", " feat. ", " ft ", " feat "]:
        t = t.split(sep)[0]
    return t.strip()


def _deduplicate_playlist(items: list[dict]) -> list[dict]:
    seen = {}
    deduped = []
    for item in items:
        key = _normalize_title(item["title"])
        if key in seen:
            existing = seen[key]
            existing["play_count"] = existing.get("play_count", 0) + item.get("play_count", 0)
            if item.get("view_count", 0) > existing.get("view_count", 0):
                existing["view_count"] = item["view_count"]
                existing["like_count"] = item.get("like_count", 0)
                existing["channel"] = item.get("channel", existing["channel"])
            existing["played_at"] = max(
                existing.get("played_at", ""), item.get("played_at", "")
            )
        else:
            copy = dict(item)
            seen[key] = copy
            deduped.append(copy)
    return deduped


def _load_playlist() -> list[dict]:
    if PLAYLIST_PATH.exists():
        return json.loads(PLAYLIST_PATH.read_text(encoding="utf-8"))
    return []


def _save_playlist(items: list[dict]):
    PLAYLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAYLIST_PATH.write_text(
        json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _add_to_playlist(entry: dict) -> bool:
    items = _load_playlist()
    norm = _normalize_title(entry["title"])
    for item in items:
        if item["url"] == entry["url"] or _normalize_title(item["title"]) == norm:
            item["play_count"] = item.get("play_count", 0) + 1
            item["played_at"] = datetime.now().isoformat()
            _save_playlist(items)
            return False
    entry["favorite"] = False
    entry["play_count"] = 1
    entry["played_at"] = datetime.now().isoformat()
    items.insert(0, entry)
    _save_playlist(items)
    return True


def _playlist_item(entry: dict) -> dict:
    return {
        "title": entry.get("title", ""),
        "url": entry.get("url", ""),
        "channel": entry.get("channel", "Unknown"),
        "view_count": entry.get("view_count", 0),
        "like_count": entry.get("like_count", 0),
        "duration": entry.get("duration", 0),
        "favorite": entry.get("favorite", False),
        "play_count": entry.get("play_count", 0),
        "played_at": entry.get("played_at", ""),
    }


def _youtube_error(code: str, message: str, field: str, value, expected: str, retryable: bool, suggestion: str) -> dict:
    return error_payload(code, message, field, value, expected, retryable, suggestion)


def _youtube_trace(tool_name: str, started_at: str, started: float, inputs_received: int, schema_valid: bool, execution_path: str, status: str, output_fields: int, stats: dict, error_code: str | None = None) -> dict:
    return make_trace(
        tool_name,
        _YOUTUBE_VERSION,
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


def _tool_error(tool_name: str, error: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, legacy: str, execution_path: str, stats: dict):
    trace = _youtube_trace(tool_name, started_at, started, inputs_received, False, execution_path, "FAILED", 1, stats, error["code"])
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_error(tool_name, _YOUTUBE_VERSION, error, started, trace)
    return legacy


def _tool_success(tool_name: str, result: dict, response_format: str, trace_enabled: bool, started: float, started_at: str, inputs_received: int, stats: dict):
    status = "PARTIAL" if result.get("degraded") else "SUCCESS"
    trace = _youtube_trace(tool_name, started_at, started, inputs_received, True, result.get("action", tool_name), status, len(result), stats)
    emit_trace(trace, trace_enabled)
    if response_format == "structured":
        return structured_success(tool_name, _YOUTUBE_VERSION, result, started, trace)
    return result.get("text", "")


def _format_playlist(items: list[dict]) -> str:
    if not items:
        return "Playlist is empty"
    lines = []
    for i, item in enumerate(items, 1):
        fav = "â­ " if item.get("favorite") else "   "
        views = _fmt_count(item.get("view_count", 0))
        lines.append(f"{fav}{i}. {item['title']} â€” {item['channel']} ({views} views)")
    return "\n".join(lines)


@tool(
    name="youtube_play",
    description="Search YouTube and play the best matching music or video. Use when user says play/search/find a song, music, artist, or video",
    examples=[
        "play despacito",
        "play some lofi beats",
        "play taylor swift anti-hero",
        "play classical music for studying",
        "play the weeknd",
        "search for a song",
        "find me some music",
        "play a video",
    ],
    param_descriptions={
        "query": "Song name, artist, or video to search and play on YouTube",
        "max_results": "Number of YouTube search results to consider, from 1 to 25",
        "timeout_ms": "yt-dlp subprocess timeout in milliseconds, from 1 to 60000",
        "playback_mode": "auto, open, or skip. Default auto preserves existing playback behavior",
        "response_format": "legacy or structured. Default legacy preserves existing output",
        "trace_enabled": "When true, emit a machine-readable trace entry",
    },
)
def youtube_play(
    query: str,
    max_results: int = 10,
    timeout_ms: int = 0,
    playback_mode: str = "auto",
    response_format: str = "legacy",
    trace_enabled: bool = False,
):
    started = time.perf_counter()
    started_at = utc_now_iso()
    inputs_received = 6
    stats = {"external_count": 0, "systems": []}
    response_format = normalize_response_format(response_format)
    trace_enabled = coerce_bool(trace_enabled)
    query = str(query or "").strip()
    playback_mode = str(playback_mode or "auto").strip().lower()

    if not query:
        error = _youtube_error(
            "EMPTY_QUERY",
            "YouTube playback needs a non-empty search query.",
            "query",
            query,
            "song, artist, or video query",
            False,
            "Pass the song, artist, or video to search for.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "Please provide a search query", "input_validation", stats)
    max_results, limit_error = normalize_int(
        max_results,
        "max_results",
        10,
        1,
        25,
        "Use a max_results value between 1 and 25.",
        "INVALID_RESULT_LIMIT",
    )
    if limit_error is not None:
        return _tool_error("youtube_play", limit_error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "input_validation", stats)
    timeout_value, timeout_error = normalize_timeout_ms(timeout_ms, 15000)
    if timeout_error is not None:
        return _tool_error("youtube_play", timeout_error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "input_validation", stats)
    if playback_mode not in _PLAYBACK_MODES:
        error = _youtube_error(
            "INVALID_PLAYBACK_MODE",
            "playback_mode must be auto, open, or skip.",
            "playback_mode",
            playback_mode,
            "auto, open, or skip",
            False,
            "Use playback_mode='auto' to preserve existing behavior.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "input_validation", stats)

    timeout_seconds = timeout_value / 1000

    results = _search_youtube(query, max_results, timeout_seconds, stats)
    if not results:
        code = "PROVIDER_ERROR" if stats.get("search_error") else "NO_RESULTS"
        error = _youtube_error(
            code,
            "YouTube search returned no usable videos.",
            "query",
            query,
            "search results from yt-dlp",
            code == "PROVIDER_ERROR",
            "Retry later, increase timeout_ms, or try a more specific query.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "search", stats)

    best = _rank_results(results, timeout_seconds, stats)
    if not best:
        error = _youtube_error(
            "RANKING_FAILED",
            "YouTube results were found but no best result could be selected.",
            "query",
            query,
            "rankable YouTube search results",
            True,
            "Retry with a more specific query.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "ranking", stats)

    entry = {
        "title": best["title"],
        "url": best["url"],
        "channel": best["channel"],
        "view_count": best.get("view_count", 0),
        "like_count": best.get("like_count", 0),
        "duration": best.get("duration", 0),
    }
    try:
        playlist_added = _add_to_playlist(entry)
    except Exception:
        error = _youtube_error(
            "PLAYLIST_WRITE_FAILED",
            "The selected YouTube item could not be saved to the playlist.",
            "playlist",
            str(PLAYLIST_PATH),
            "writable playlist storage",
            True,
            "Check playlist storage permissions and retry if the operation is still wanted.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "playlist_write", stats)

    try:
        playback_status = _start_playback_attempt(best["url"], best["title"], playback_mode)
    except Exception:
        error = _youtube_error(
            "PLAYBACK_FAILED",
            "The selected YouTube item was saved but playback launch failed.",
            "playback_mode",
            playback_mode,
            "successful playback attempt or page open",
            True,
            "Retry with playback_mode='open' or playback_mode='skip'.",
        )
        return _tool_error("youtube_play", error, response_format, trace_enabled, started, started_at, inputs_received, "No matching videos found", "playback", stats)

    likes = _fmt_count(best.get("like_count", 0))
    views = _fmt_count(best.get("view_count", 0))
    text = (
        f"{playback_status}\n"
        f"Selected: '{best['title']}' Ã¢â‚¬â€ {best['channel']} "
        f"({views} views, {likes} likes)"
    )
    result = {
        "action": "youtube_play",
        "query": query,
        "text": text,
        "selected": _playlist_item(entry),
        "search_count": len(results),
        "playlist_added": playlist_added,
        "playlist_path": str(PLAYLIST_PATH),
        "playback_mode": playback_mode,
        "playback_status": playback_status,
        "playback_attempted": playback_mode != "skip",
        "degraded": playback_status.startswith("Could not"),
        "degraded_reason": playback_status if playback_status.startswith("Could not") else "",
    }
    return _tool_success("youtube_play", result, response_format, trace_enabled, started, started_at, inputs_received, stats)



@tool(
    name="playlist",
    description="Manage your saved music playlist: list, search, remove, favorite/unfavorite songs, play/pause/shuffle saved tracks, show top played, clear all. Actions: list, top, favorite, unfavorite, play, stop, remove, clear, search, shuffle",
    examples=[
        "list my playlist",
        "show my songs",
        "list my saved music",
        "show my playlist",
        "list my saved songs",
        "favorite despacito",
        "play my favorite song",
        "show my top songs",
        "stop the music",
        "remove despacito from playlist",
        "delete song 3 from my playlist",
        "clear my playlist",
        "search saved songs for taylor",
        "shuffle my music",
        "play something random",
    ],
    param_descriptions={
        "action": "What to do: list (show all songs), top (show most played), favorite (mark a song), unfavorite (unmark), play (play a saved song by name or number), stop (stop playback), remove (delete a song by name or number), clear (wipe entire playlist), search (find songs matching query), shuffle (play a random saved song)",
        "query": "Song name, number, or search term for actions that need it",
    },
)
def playlist_manage(action: str, query: str = "") -> str:
    action = action.strip().lower()

    if action == "list":
        items = _deduplicate_playlist(_load_playlist())
        if not items:
            return "Your playlist is empty. Play some music first!"
        return _format_playlist(items)

    if action == "top":
        items = sorted(
            _load_playlist(),
            key=lambda x: x.get("play_count", 0),
            reverse=True,
        )[:5]
        if not items:
            return "No songs played yet"
        lines = []
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {item['title']} â€” {item['channel']} "
                f"(played {item.get('play_count', 1)}x)"
            )
        return "\n".join(lines)

    if action == "favorite":
        if not query:
            return "Specify a song title or number to favorite"
        items = _load_playlist()
        target = _find_playlist_item(items, query)
        if target is None:
            return f"Song '{query}' not found in playlist"
        target["favorite"] = True
        _save_playlist(items)
        return f"â­ Marked '{target['title']}' as favorite"

    if action == "unfavorite":
        if not query:
            return "Specify a song title or number to unfavorite"
        items = _load_playlist()
        target = _find_playlist_item(items, query)
        if target is None:
            return f"Song '{query}' not found in playlist"
        target["favorite"] = False
        _save_playlist(items)
        return f"Removed '{target['title']}' from favorites"

    if action == "play":
        items = _load_playlist()
        if not items:
            return "No saved songs to play"
        if not query:
            favs = [i for i in items if i.get("favorite")]
            target = favs[0] if favs else items[0]
        else:
            target = _find_playlist_item(items, query)
        if target is None:
            return "No saved songs to play"
        target["play_count"] = target.get("play_count", 0) + 1
        target["played_at"] = datetime.now().isoformat()
        _save_playlist(items)
        playback_status = _start_playback_attempt(target["url"], target["title"])
        return (
            f"{playback_status}\n"
            f"Selected saved track: '{target['title']}' â€” {target['channel']}"
        )

    if action == "remove":
        if not query:
            return "Specify a song title or number to remove"
        items = _load_playlist()
        target = _find_playlist_item(items, query)
        if target is None:
            return f"Song '{query}' not found in playlist"
        items.remove(target)
        _save_playlist(items)
        return f"Removed '{target['title']}' from playlist"

    if action == "clear":
        items = _load_playlist()
        if not items:
            return "Playlist is already empty"
        count = len(items)
        _save_playlist([])
        return f"Cleared {count} song{'s' if count != 1 else ''} from playlist"

    if action == "search":
        if not query:
            return "Provide a search term to find songs"
        items = _load_playlist()
        q = query.strip().lower()
        matches = [
            item for item in items
            if q in item["title"].lower() or q in item.get("channel", "").lower()
        ]
        if not matches:
            return f"No songs match '{query}'"
        return _format_playlist(matches)

    if action == "shuffle":
        items = _load_playlist()
        if not items:
            return "Playlist is empty, nothing to shuffle"
        target = random.choice(items)
        target["play_count"] = target.get("play_count", 0) + 1
        target["played_at"] = datetime.now().isoformat()
        _save_playlist(items)
        playback_status = _start_playback_attempt(target["url"], target["title"])
        return (
            f"{playback_status}\n"
            f"Shuffled selected track: '{target['title']}' â€” {target['channel']}"
        )

    if action == "stop":
        try:
            import pygame
            pygame.mixer.music.stop()
        except Exception:
            pass
        return "Playback stop signal sent"

    available = "list, top, favorite <title>, unfavorite <title>, play <title>, stop, remove <title>, clear, search <term>, shuffle"
    return f"Unknown action '{action}'. Available: {available}"


def _find_playlist_item(items: list[dict], query: str) -> dict | None:
    query = query.strip().lower()
    if query.isdigit():
        idx = int(query) - 1
        if 0 <= idx < len(items):
            return items[idx]

    best = None
    best_score = 0
    for item in items:
        title = item["title"].lower()
        if query in title:
            return item
        if query in item.get("channel", "").lower():
            return item

        title_words = title.split()
        query_words = [w for w in query.split() if len(w) > 2]
        if query_words:
            hits = sum(1 for qw in query_words for tw in title_words if qw in tw)
            if hits > best_score:
                best_score = hits
                best = item

    if best_score >= len([w for w in query.split() if len(w) > 2]):
        return best
    return None
