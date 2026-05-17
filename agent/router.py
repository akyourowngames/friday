import json
from pathlib import Path

import numpy as np

from config import settings
from tools.registry import get_tools, get_tool
from .embedder import embed

_CACHE_DIR = Path("storage")
_TOOL_CACHE = _CACHE_DIR / "tool_embeddings.npy"
_TOOL_TEXTS_CACHE = _CACHE_DIR / "tool_texts.json"
_ST_CACHE = _CACHE_DIR / "small_talk_emb.npy"

_SMALL_TALK_TEXT = (
    "just chatting, casual conversation, greetings, small talk, "
    "saying hello, pleasantries, how are you, checking in"
)
_SMALL_TALK_MARGIN = 0.10


class ToolRouter:
    def __init__(self, top_k=None):
        self.top_k = top_k or settings.tool_top_k
        self.threshold = settings.tool_similarity_threshold
        self._tool_embeddings = None
        self._tool_names = []
        self._tool_texts = []
        self._small_talk_emb = None
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get_small_talk_emb(self):
        if self._small_talk_emb is None:
            if _ST_CACHE.exists():
                self._small_talk_emb = np.load(_ST_CACHE)
            else:
                self._small_talk_emb = embed(_SMALL_TALK_TEXT)
                np.save(_ST_CACHE, self._small_talk_emb)
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
        if len(query.strip()) <= 6:
            return []

        if self._tool_embeddings is None or len(get_tools()) != len(self._tool_names):
            self._embed_tools()

        if not self._tool_names:
            return []

        if q_emb is None:
            q_emb = embed(query)

        similarities = np.dot(self._tool_embeddings, q_emb)
        max_tool_sim = float(np.max(similarities))

        small_talk_sim = float(np.dot(self._get_small_talk_emb(), q_emb))
        if small_talk_sim > 0.65 and small_talk_sim > max_tool_sim + _SMALL_TALK_MARGIN:
            return []

        if max_tool_sim < self.threshold:
            return []

        top_indices = np.argsort(similarities)[::-1]
        top_indices = [i for i in top_indices if similarities[i] >= self.threshold][:self.top_k]

        selected = []
        for idx in top_indices:
            name = self._tool_names[idx]
            selected.append(get_tool(name))

        return selected
