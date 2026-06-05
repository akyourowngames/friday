from __future__ import annotations

from typing import Any

import numpy as np
from llama_index.core.base.embeddings.base import BaseEmbedding


class MiniLMEmbedding(BaseEmbedding):
    """LlamaIndex embedding adapter over the repo's local all-MiniLM-L6-v2 ONNX model."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(model_name="sentence-transformers/all-MiniLM-L6-v2", **kwargs)

    def _embed_many(self, texts: list[str]) -> list[list[float]]:
        from agent.embedder import embed

        vectors = embed(texts, normalize=True)
        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        return vectors.tolist()

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed_many([query])[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed_many([text])[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed_many(texts)
