import numpy as np

from config import settings
from tools.registry import get_tools, get_tool
from .embedder import embed


class ToolRouter:
    def __init__(self, top_k=None):
        self.top_k = top_k or settings.tool_top_k
        self.threshold = settings.tool_similarity_threshold
        self._tool_embeddings = None
        self._tool_names = []
        self._tool_texts = []

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

        self._tool_embeddings = embed(self._tool_texts)

    def select_tools(self, query):
        if self._tool_embeddings is None or len(get_tools()) != len(self._tool_names):
            self._embed_tools()

        if not self._tool_names:
            return []

        q_emb = embed(query)

        similarities = np.dot(self._tool_embeddings, q_emb)

        top_indices = np.argsort(similarities)[::-1]
        top_indices = [i for i in top_indices if similarities[i] >= self.threshold][:self.top_k]

        selected = []
        for idx in top_indices:
            name = self._tool_names[idx]
            selected.append(get_tool(name))

        return selected
