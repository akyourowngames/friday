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


def _load_small_talk_text() -> str:
    if ROUTING_POLICY_PATH.exists():
        return ROUTING_POLICY_PATH.read_text(encoding="utf-8").strip()
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
            return []

        if self._tool_embeddings is None or len(get_tools()) != len(self._tool_names):
            self._embed_tools()

        if not self._tool_names:
            return []

        if q_emb is None:
            q_emb = embed(query)

        similarities = np.dot(self._tool_embeddings, q_emb)
        max_tool_sim = float(np.max(similarities))

        # Check if this is just casual chat
        small_talk_sim = float(np.dot(self._get_small_talk_emb(), q_emb))
        if small_talk_sim > 0.65 and small_talk_sim > max_tool_sim + _SMALL_TALK_MARGIN:
            return []  # Just chat, no tools needed

        if max_tool_sim < self.threshold:
            return []  # No matching tools

        # Get top matches
        top_indices = np.argsort(similarities)[::-1]
        
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

        return selected
