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

from tools.registry import tool

PLAYLIST_PATH = Path("storage/playlist.json")


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


def _search_youtube(query: str, max_results: int = 10) -> list[dict]:
    try:
        result = subprocess.run(
            ["yt-dlp", f"ytsearch{max_results}:{query}", "--dump-json",
             "--flat-playlist", "--no-download", "--quiet"],
            capture_output=True, text=True, timeout=15,
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
        return entries
    except Exception:
        return []


def _get_full_info(url: str) -> dict:
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-download", "--quiet", url],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout.strip())
        return {
            "like_count": data.get("like_count", 0),
            "view_count": data.get("view_count", 0),
        }
    except Exception:
        return {"like_count": 0, "view_count": 0}


def _rank_results(entries: list[dict]) -> dict | None:
    if not entries:
        return None
    sorted_by_views = sorted(entries, key=lambda x: x["view_count"], reverse=True)
    candidates = sorted_by_views[:3]
    for c in candidates:
        info = _get_full_info(c["url"])
        c["like_count"] = info.get("like_count", 0)

    def score(item):
        return item["view_count"] * 0.7 + item.get("like_count", 0) * 10

    return max(candidates, key=score)


def _play_audio_background(url: str, title: str):
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

    threading.Thread(target=_play, daemon=True).start()


def _normalize_title(title: str) -> str:
    t = title.lower().strip()
    for sep in [" — ", " – ", " - ", " ft. ", " feat. ", " ft ", " feat "]:
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


def _format_playlist(items: list[dict]) -> str:
    if not items:
        return "Playlist is empty"
    lines = []
    for i, item in enumerate(items, 1):
        fav = "⭐ " if item.get("favorite") else "   "
        views = _fmt_count(item.get("view_count", 0))
        lines.append(f"{fav}{i}. {item['title']} — {item['channel']} ({views} views)")
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
    },
)
def youtube_play(query: str) -> str:
    if not query.strip():
        return "Please provide a search query"

    results = _search_youtube(query)
    if not results:
        return "No matching videos found"

    best = _rank_results(results)
    if not best:
        return "No matching videos found"

    _add_to_playlist({
        "title": best["title"],
        "url": best["url"],
        "channel": best["channel"],
        "view_count": best.get("view_count", 0),
        "like_count": best.get("like_count", 0),
        "duration": best.get("duration", 0),
    })

    _play_audio_background(best["url"], best["title"])

    try:
        from memory.brain import Brain
        Brain().commit(
            f"User listened to '{best['title']}' by {best['channel']}",
            importance=0.3,
        )
    except Exception:
        pass

    likes = _fmt_count(best.get("like_count", 0))
    views = _fmt_count(best.get("view_count", 0))
    return (
        f"▶ Playing '{best['title']}' — {best['channel']} "
        f"({views} views, {likes} likes)"
    )


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
                f"{i}. {item['title']} — {item['channel']} "
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
        return f"⭐ Marked '{target['title']}' as favorite"

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
        _play_audio_background(target["url"], target["title"])
        return f"▶ Playing '{target['title']}' — {target['channel']}"

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
        _play_audio_background(target["url"], target["title"])
        return f"▶ Shuffled to '{target['title']}' — {target['channel']}"

    if action == "stop":
        try:
            import pygame
            pygame.mixer.music.stop()
        except Exception:
            pass
        return "Playback stopped"

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
