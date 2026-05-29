"""Episode Stitching.

Turns a flat list of memories into narrative episodes by single-link clustering
over two signals already present in the system:

1. temporal proximity  (memory `_date` + `ts`)
2. embedding similarity (cosine over adjacent memories)

Two consecutive memories (in time order) join the same episode when the time
gap is small enough OR their embeddings are similar enough. This is intentionally
cheap: O(n) adjacency, no full pairwise matrix, no ML training.

The episode title defaults to a trimmed concatenation of member facts. Callers
that have an LLM available can pass a `titler` callable to generate a nicer
one-line title; this module never hardcodes phrasing.

No regex, no keyword logic.
"""

from __future__ import annotations

import hashlib

from .config import section_values
from .util import combine_date_time, cosine

_DEFAULTS = {
    "time_gap_minutes": 180,
    "similarity_link_threshold": 0.55,
    "min_episode_size": 2,
    "max_episode_size": 40,
    "max_episodes": 60,
    "title_max_chars": 80,
}


def _episode_id(member_ids: list[str]) -> str:
    payload = "|".join(member_ids)
    return "episode_" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _default_title(texts: list[str], max_chars: int) -> str:
    joined = " / ".join(text.strip() for text in texts if text.strip())
    if len(joined) <= max_chars:
        return joined
    return joined[: max(0, max_chars - 1)].rstrip() + "\u2026"


def stitch_episodes(memories: list[dict], embed_fn=None, config: dict | None = None, titler=None) -> list[dict]:
    """Cluster memories into episodes.

    `embed_fn` maps a list of texts to a 2D array of row embeddings. When omitted
    or it fails, clustering falls back to time-gap-only linking. `titler` maps a
    list of member texts to a title string when provided.
    """
    cfg = section_values("episodes", _DEFAULTS) if config is None else {**_DEFAULTS, **config}

    ordered = [m for m in memories if str(m.get("text", "")).strip()]
    ordered.sort(key=lambda m: (str(m.get("_date", "")), str(m.get("ts", ""))))
    if len(ordered) < int(cfg["min_episode_size"]):
        return []

    embeddings = None
    if embed_fn is not None:
        try:
            import numpy as np

            raw = embed_fn([m["text"] for m in ordered])
            arr = np.asarray(raw, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            if arr.shape[0] == len(ordered):
                embeddings = arr
        except Exception:
            embeddings = None

    gap_seconds = max(0.0, float(cfg["time_gap_minutes"]) * 60.0)
    sim_threshold = float(cfg["similarity_link_threshold"])
    max_size = int(cfg["max_episode_size"])

    clusters: list[list[int]] = [[0]]
    for index in range(1, len(ordered)):
        prev = ordered[index - 1]
        curr = ordered[index]
        link = False

        prev_time = combine_date_time(prev.get("_date", ""), prev.get("ts", ""))
        curr_time = combine_date_time(curr.get("_date", ""), curr.get("ts", ""))
        if prev_time is not None and curr_time is not None:
            if abs((curr_time - prev_time).total_seconds()) <= gap_seconds:
                link = True

        if not link and embeddings is not None:
            if cosine(embeddings[index - 1], embeddings[index]) >= sim_threshold:
                link = True

        if link and len(clusters[-1]) < max_size:
            clusters[-1].append(index)
        else:
            clusters.append([index])

    episodes: list[dict] = []
    min_size = int(cfg["min_episode_size"])
    title_max = int(cfg["title_max_chars"])
    for cluster in clusters:
        if len(cluster) < min_size:
            continue
        members = [ordered[i] for i in cluster]
        member_ids = [str(m.get("id") or m.get("text")) for m in members]
        texts = [m["text"] for m in members]
        title = ""
        if titler is not None:
            try:
                title = str(titler(texts) or "").strip()
            except Exception:
                title = ""
        if not title:
            title = _default_title(texts, title_max)
        episodes.append(
            {
                "id": _episode_id(member_ids),
                "title": title,
                "member_ids": member_ids,
                "size": len(members),
                "start_date": members[0].get("_date", ""),
                "start_time": members[0].get("ts", ""),
                "end_date": members[-1].get("_date", ""),
                "end_time": members[-1].get("ts", ""),
            }
        )

    episodes.sort(key=lambda e: (e.get("end_date", ""), e.get("end_time", "")), reverse=True)
    return episodes[: int(cfg["max_episodes"])]
