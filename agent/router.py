"""Utterance-based tool router.

The entire routing decision is:
1. Embed every utterance from `tools/TOOL_UTTERANCES.md` (one row per phrase).
2. Embed the user query.
3. Per-tool score = max similarity across that tool's utterances.
4. If the best score is below a single confidence threshold → no tool (chat).
5. Otherwise pick the top-k tools above threshold.

No ranking layers, no winner margins, no relative floors, no small-talk
contrast embedding, no capability routing layers. Collisions are fixed by
editing utterances in the markdown file, not by adding code.
"""

import json
from pathlib import Path

import numpy as np

from config import settings
from tools.registry import get_tools, get_tool
from .embedder import embed

_CACHE_DIR = Path(settings.storage_dir)
_TOOL_CACHE = _CACHE_DIR / "tool_embeddings.npy"
_TOOL_TEXTS_CACHE = _CACHE_DIR / "tool_texts.json"
_TOOL_OWNERS_CACHE = _CACHE_DIR / "tool_owners.json"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "routing_policy.md"
_UTTERANCES_PATH = Path(__file__).resolve().parent.parent / settings.tool_utterances_file


# ─── Exported helpers (consumed by core.py and tests) ───────────────────────

def _load_small_talk_text() -> str:
    """Load the routing contrast text from routing_policy.md."""
    if ROUTING_POLICY_PATH.exists():
        no_memory_small_talk = _load_routing_section("No Memory Small Talk Text")
        text = f"Routing Contrast Text. No Memory Small Talk Text: {no_memory_small_talk}".strip()
        if text:
            return text
    return (
        "Conversational turn with no request for external data, local action, "
        "memory access, file access, search, playback, generation, or side effects."
    )


def _load_capability_rules() -> list[dict]:
    """Load capability rules from tools.capabilities (kept for test imports)."""
    try:
        from tools.capabilities import build_capability_rules
        return build_capability_rules()
    except Exception:
        return []


def _parse_file_generating_tools():
    if not settings.file_generating_tools:
        return set()
    return {t.strip() for t in settings.file_generating_tools.split(",") if t.strip()}

_FILE_GENERATING_TOOLS = _parse_file_generating_tools()


# ─── Internal helpers ───────────────────────────────────────────────────────

def _load_routing_section(heading: str) -> str:
    if not ROUTING_POLICY_PATH.exists():
        return ""
    targets = {f"# {heading}".casefold(), f"## {heading}".casefold()}
    lines = []
    in_section = False
    for raw_line in ROUTING_POLICY_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("# ") or line.startswith("## "):
            in_section = line.casefold() in targets
            continue
        if in_section and line:
            lines.append(line)
    return " ".join(lines).strip()


def _load_tool_utterances() -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Parse the utterance bank from TOOL_UTTERANCES.md.

    Returns:
        (utterances_by_tool, slug_hints_by_phrase)

    slug_hints_by_phrase maps a phrase to {"tool_slug": "SLUG"} when the
    phrase line contains a `[slug:SLUG]` annotation. This lets composio
    utterances carry their resolved slug without a separate capability layer.
    """
    if not _UTTERANCES_PATH.exists():
        return {}, {}
    utterances: dict[str, list[str]] = {}
    slug_hints: dict[str, dict] = {}
    current: str | None = None
    for raw_line in _UTTERANCES_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("## "):
            head = stripped[3:].strip()
            current = head if head else None
            continue
        if current and stripped.startswith("- "):
            phrase = stripped[2:].strip()
            if not phrase:
                continue
            # Check for slug annotation: `- phrase [slug:GOOGLECALENDAR_EVENTS_LIST]`
            slug_tag_start = phrase.find("[slug:")
            if slug_tag_start >= 0:
                slug_tag_end = phrase.find("]", slug_tag_start)
                if slug_tag_end > slug_tag_start:
                    slug = phrase[slug_tag_start + 6:slug_tag_end].strip()
                    clean_phrase = (phrase[:slug_tag_start] + phrase[slug_tag_end + 1:]).strip()
                    if slug and clean_phrase:
                        slug_hints[clean_phrase] = {"tool_slug": slug}
                        phrase = clean_phrase
            if phrase:
                utterances.setdefault(current, []).append(phrase)
    return utterances, slug_hints


# ─── Router ─────────────────────────────────────────────────────────────────

class ToolRouter:
    def __init__(self, top_k=None):
        self.top_k = top_k or settings.tool_top_k
        self.threshold = settings.tool_similarity_threshold
        # Kept for backward-compat with one test that stubs these directly.
        self.winner_margin = 0.0
        self._tool_embeddings = None
        self._tool_names: list[str] = []
        self._tool_texts: list[str] = []
        self._row_owner_idx: list[int] = []
        self._slug_hints: dict[str, dict] = {}
        self._small_talk_emb = None
        self._last_capability_hint: dict[str, dict] = {}
        self._last_generated_file = None
        self._last_decision: dict = {
            "query": "",
            "selected": [],
            "scores": [],
            "reason": "not_run",
        }
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _embed_tools(self):
        """Build the utterance embedding index from the MD file."""
        tools = get_tools()
        self._tool_names = [t["name"] for t in tools]
        if not self._tool_names:
            self._tool_texts = []
            self._row_owner_idx = []
            self._tool_embeddings = np.array([])
            return

        utterance_bank, self._slug_hints = _load_tool_utterances()

        row_texts: list[str] = []
        row_owners: list[int] = []
        for owner_idx, t in enumerate(tools):
            phrases = utterance_bank.get(t["name"]) or []
            if phrases:
                for phrase in phrases:
                    row_texts.append(phrase)
                    row_owners.append(owner_idx)
            else:
                # Fallback: use description + examples for tools not yet in the bank.
                fallback = f"{t['name']}: {t['description']}"
                if t.get("examples"):
                    fallback += " | " + " | ".join(t["examples"])
                row_texts.append(fallback)
                row_owners.append(owner_idx)

        self._tool_texts = row_texts
        self._row_owner_idx = row_owners

        # Cache check: skip re-embedding if texts + owners unchanged.
        cached_texts = None
        if _TOOL_TEXTS_CACHE.exists():
            try:
                cached_texts = json.loads(_TOOL_TEXTS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_texts = None
        cached_owners = None
        if _TOOL_OWNERS_CACHE.exists():
            try:
                cached_owners = json.loads(_TOOL_OWNERS_CACHE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached_owners = None
        owner_names = [self._tool_names[i] for i in row_owners]

        if (
            cached_texts == row_texts
            and cached_owners == owner_names
            and _TOOL_CACHE.exists()
        ):
            self._tool_embeddings = np.load(_TOOL_CACHE)
            return

        self._tool_embeddings = embed(row_texts)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_TOOL_CACHE, self._tool_embeddings)
        _TOOL_TEXTS_CACHE.write_text(
            json.dumps(row_texts, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _TOOL_OWNERS_CACHE.write_text(
            json.dumps(owner_names, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _per_tool_scores(self, row_similarities: np.ndarray) -> tuple[np.ndarray, list[int]]:
        """Max-pool row similarities to per-tool scores.

        Returns (per_tool_scores, per_tool_best_row_idx).
        """
        owners = self._row_owner_idx
        if not owners:
            # Backward-compat: tests may stub _tool_embeddings with one row per
            # tool and not populate _row_owner_idx.
            return row_similarities, list(range(len(row_similarities)))
        n_tools = len(self._tool_names)
        per_tool = np.full(n_tools, -np.inf, dtype=row_similarities.dtype)
        per_tool_row = [-1] * n_tools
        for row_idx, owner in enumerate(owners):
            score = float(row_similarities[row_idx])
            if score > per_tool[owner]:
                per_tool[owner] = score
                per_tool_row[owner] = row_idx
        return per_tool, per_tool_row

    def select_tools(self, query, q_emb=None):
        """Select tools by utterance similarity. Simple argmax above threshold."""
        self._last_capability_hint = {}

        if len(query.strip()) < settings.embedding_min_chars:
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": [],
                "reason": "below_embedding_min_chars",
            }
            return []

        if self._tool_embeddings is None or len(get_tools()) != len(self._tool_names):
            self._embed_tools()

        if not self._tool_names:
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": [],
                "reason": "no_registered_tools",
            }
            return []

        if q_emb is None:
            q_emb = embed(query)

        # Score every row, then collapse to per-tool max.
        row_sims = np.dot(self._tool_embeddings, q_emb)
        per_tool_sims, per_tool_best_row = self._per_tool_scores(row_sims)

        # Rank tools by score.
        ranked = np.argsort(per_tool_sims)[::-1]
        best_score = float(per_tool_sims[ranked[0]])

        scores = [
            {"tool": self._tool_names[int(idx)], "score": round(float(per_tool_sims[int(idx)]), 4)}
            for idx in ranked[: min(5, len(ranked))]
        ]

        # Single gate: if best score is below threshold, it's just chat.
        if best_score < self.threshold:
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": scores,
                "reason": "below_tool_threshold",
            }
            return []

        # Pick top-k tools above threshold.
        selected = []
        for idx in ranked[: self.top_k]:
            if per_tool_sims[idx] < self.threshold:
                break
            tool_name = self._tool_names[int(idx)]
            tool_obj = get_tool(tool_name)
            if tool_obj:
                selected.append(tool_obj)
                # Check if the winning utterance carries a slug hint.
                best_row = per_tool_best_row[int(idx)]
                if 0 <= best_row < len(self._tool_texts):
                    phrase = self._tool_texts[best_row]
                    hint = self._slug_hints.get(phrase)
                    if hint:
                        self._last_capability_hint[tool_name] = {
                            "args": {"action": "execute", "tool_slug": hint["tool_slug"]},
                            "score": round(float(per_tool_sims[idx]), 4),
                        }

        self._last_decision = {
            "query": query,
            "selected": [t["name"] for t in selected],
            "scores": scores,
            "reason": "selected" if selected else "below_tool_threshold",
        }
        return selected

    def capability_hint(self, tool_name: str) -> dict:
        """Return slug hint for a tool from the most recent selection."""
        return dict(self._last_capability_hint.get(tool_name, {}))

    def last_decision(self) -> dict:
        return dict(self._last_decision)
