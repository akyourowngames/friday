from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llama_index.core.base.embeddings.base import BaseEmbedding
from llama_index.core.bridge.pydantic import Field, PrivateAttr
from openai import OpenAI


class NvidiaEmbedding(BaseEmbedding):
    api_key: str = Field(exclude=True)
    api_base: str
    timeout: float = 60.0

    _client: OpenAI = PrivateAttr()

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.api_base,
            timeout=self.timeout,
        )

    def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.model_name,
            input=list(texts),
            extra_body={"input_type": input_type},
        )
        return [item.embedding for item in response.data]

    def _get_query_embedding(self, query: str) -> list[float]:
        return self._embed([query], "query")[0]

    async def _aget_query_embedding(self, query: str) -> list[float]:
        return self._get_query_embedding(query)

    def _get_text_embedding(self, text: str) -> list[float]:
        return self._embed([text], "passage")[0]

    def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "passage")
