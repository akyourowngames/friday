import json
from pathlib import Path

import numpy as np

from config import settings
from tools.registry import get_tools, get_tool
from .embedder import embed

_CACHE_DIR = Path(settings.storage_dir)
_TOOL_CACHE = _CACHE_DIR / "tool_embeddings.npy"
_TOOL_TEXTS_CACHE = _CACHE_DIR / "tool_texts.json"
_ST_CACHE = _CACHE_DIR / "small_talk_emb.npy"
_ST_TEXT_CACHE = _CACHE_DIR / "small_talk_text.txt"
ROUTING_POLICY_PATH = Path(__file__).resolve().parent.parent / "routing_policy.md"

_SMALL_TALK_MARGIN = 0.10


def _term_set(text: str) -> set[str]:
    terms = set()
    current = []
    for char in str(text or "").casefold():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            if len(token) >= 3:
                terms.add(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 3:
            terms.add(token)
    return terms


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


def _load_small_talk_text() -> str:
    if ROUTING_POLICY_PATH.exists():
        no_memory_small_talk = _load_routing_section("No Memory Small Talk Text")
        text = f"Routing Contrast Text. No Memory Small Talk Text: {no_memory_small_talk}".strip()
        if text:
            return text
    return (
        "Conversational turn with no request for external data, local action, "
        "memory access, file access, search, playback, generation, or side effects."
    )

# Parse file-generating tools from config
def _parse_file_generating_tools():
    """Parse KING_FILE_GENERATING_TOOLS from config format: tool1,tool2,tool3"""
    if not settings.file_generating_tools:
        return set()
    return {t.strip() for t in settings.file_generating_tools.split(",") if t.strip()}

_FILE_GENERATING_TOOLS = _parse_file_generating_tools()


class ToolRouter:
    def __init__(self, top_k=None):
        self.top_k = top_k or settings.tool_top_k
        self.threshold = settings.tool_similarity_threshold
        self.winner_margin = settings.tool_winner_margin
        self._tool_embeddings = None
        self._tool_names = []
        self._tool_texts = []
        self._small_talk_emb = None
        self._last_generated_file = None  # Track generated files for viewing
        self._last_decision = {
            "query": "",
            "selected": [],
            "scores": [],
            "small_talk_score": 0.0,
            "reason": "not_run",
        }
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get_small_talk_emb(self):
        if self._small_talk_emb is None:
            text = _load_small_talk_text()
            cached_text = _ST_TEXT_CACHE.read_text(encoding="utf-8") if _ST_TEXT_CACHE.exists() else None
            if _ST_CACHE.exists() and cached_text == text:
                self._small_talk_emb = np.load(_ST_CACHE)
            else:
                self._small_talk_emb = embed(text)
                np.save(_ST_CACHE, self._small_talk_emb)
                _ST_TEXT_CACHE.write_text(text, encoding="utf-8")
        return self._small_talk_emb

    def _embed_tools(self):
        tools = get_tools()
        self._tool_names = [t["name"] for t in tools]
        self._tool_texts = []
        for t in tools:
            text = f"{t['name']}: {t['description']}"
            if t.get("examples"):
                text += " | " + " | ".join(t["examples"])
            self._tool_texts.append(text)

        if not self._tool_texts:
            self._tool_embeddings = np.array([])
            return

        cached_texts = None
        if _TOOL_TEXTS_CACHE.exists():
            cached_texts = json.loads(_TOOL_TEXTS_CACHE.read_text(encoding="utf-8"))

        if cached_texts == self._tool_texts and _TOOL_CACHE.exists():
            self._tool_embeddings = np.load(_TOOL_CACHE)
            return

        self._tool_embeddings = embed(self._tool_texts)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(_TOOL_CACHE, self._tool_embeddings)
        _TOOL_TEXTS_CACHE.write_text(
            json.dumps(self._tool_texts, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def select_tools(self, query, q_emb=None):
        """Select tools purely based on semantic similarity. No regex, no rules."""
        # Short queries skip embedding cost - just return nothing
        if len(query.strip()) < settings.embedding_min_chars:
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": [],
                "small_talk_score": 0.0,
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
                "small_talk_score": 0.0,
                "reason": "no_registered_tools",
            }
            return []

        if q_emb is None:
            q_emb = embed(query)

        similarities = np.dot(self._tool_embeddings, q_emb)
        max_tool_sim = float(np.max(similarities))
        ranked_all = np.argsort(similarities)[::-1]
        top_index = int(ranked_all[0])
        scores = [
            {"tool": self._tool_names[int(idx)], "score": float(similarities[int(idx)])}
            for idx in ranked_all[: min(5, len(ranked_all))]
        ]

        # Check if this is just casual chat
        small_talk_sim = float(np.dot(self._get_small_talk_emb(), q_emb))
        ambiguous_tool_ceiling = self.threshold + max(self.winner_margin, _SMALL_TALK_MARGIN)
        small_talk_close = small_talk_sim + _SMALL_TALK_MARGIN >= max_tool_sim
        top_tool_text = self._tool_texts[top_index] if top_index < len(self._tool_texts) else self._tool_names[top_index]
        top_tool_grounded = bool(_term_set(query) & _term_set(top_tool_text))
        tool_grounding_wins = top_tool_grounded and max_tool_sim >= self.threshold
        if not tool_grounding_wins and (small_talk_sim > max_tool_sim + _SMALL_TALK_MARGIN or (
            small_talk_close and max_tool_sim < ambiguous_tool_ceiling
        ) or (
            small_talk_sim >= 0.0 and max_tool_sim < ambiguous_tool_ceiling
        )):
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": scores,
                "small_talk_score": small_talk_sim,
                "reason": "small_talk_contrast_won",
            }
            return []  # Just chat, no tools needed

        if max_tool_sim < self.threshold:
            self._last_decision = {
                "query": query,
                "selected": [],
                "scores": scores,
                "small_talk_score": small_talk_sim,
                "reason": "below_tool_threshold",
            }
            return []  # No matching tools

        # Get top matches
        top_indices = ranked_all
        
        # Apply winner margin (pick clear winners)
        if len(top_indices) > 1:
            best = float(similarities[top_indices[0]])
            second = float(similarities[top_indices[1]])
            if self.winner_margin > 0 and best >= self.threshold and best >= second + self.winner_margin:
                top_indices = top_indices[:1]

        # Filter by absolute threshold, then keep candidates close enough to the best match.
        # This keeps low-confidence follow-ups usable while avoiding broad accidental tool dumps.
        absolute_matches = [i for i in top_indices if similarities[i] >= self.threshold]
        relative_floor = max_tool_sim * settings.tool_relative_floor
        relative_matches = [i for i in absolute_matches if similarities[i] >= relative_floor]
        top_indices = (relative_matches or absolute_matches)[:self.top_k]

        selected = []
        for idx in top_indices:
            name = self._tool_names[idx]
            selected.append(get_tool(name))

        self._last_decision = {
            "query": query,
            "selected": [tool["name"] for tool in selected],
            "scores": scores,
            "small_talk_score": small_talk_sim,
            "reason": "selected",
        }
        return selected

    def last_decision(self) -> dict:
        return dict(self._last_decision)
