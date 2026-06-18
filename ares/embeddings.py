"""Embedding providers for Ares memory search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

EMBEDDING_DIMS = 384
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SUPPORTED_BACKENDS = {"torch", "onnx"}

_MODEL_CACHE: dict[tuple[str, str, str, str, bool | None], Any] = {}


@dataclass
class EmbeddingProvider:
    """Small wrapper around Sentence Transformers embedding backends."""

    model_name: str = DEFAULT_EMBEDDING_MODEL
    backend: str = "onnx"
    provider: str = "CPUExecutionProvider"
    file_name: str = ""
    export: bool | None = None
    fallback_to_torch: bool = True

    def __post_init__(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise ValueError(f"Unsupported embedding backend: {self.backend}")
        self.backend_used = self.backend
        self.fallback_error: str | None = None

    def _load_model(self) -> Any:
        cache_key = (self.model_name, self.backend, self.provider, self.file_name, self.export)
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        from sentence_transformers import SentenceTransformer

        try:
            if self.backend == "onnx":
                model_kwargs: dict[str, Any] = {"provider": self.provider}
                if self.file_name:
                    model_kwargs["file_name"] = self.file_name
                if self.export is not None:
                    model_kwargs["export"] = self.export
                model = SentenceTransformer(
                    self.model_name,
                    backend="onnx",
                    model_kwargs=model_kwargs,
                )
            else:
                model = SentenceTransformer(self.model_name, backend="torch")
        except Exception as exc:
            if self.backend != "onnx" or not self.fallback_to_torch:
                raise
            self.backend_used = "torch"
            self.fallback_error = str(exc)
            cache_key = (self.model_name, "torch", "", "", None)
            cached = _MODEL_CACHE.get(cache_key)
            if cached is not None:
                return cached
            model = SentenceTransformer(self.model_name, backend="torch")

        _MODEL_CACHE[cache_key] = model
        return model

    def embed_vector(self, text: str) -> np.ndarray:
        """Return a float32 sentence embedding vector."""
        model = self._load_model()
        vec = np.asarray(model.encode(text), dtype="float32")
        if vec.shape[-1] != EMBEDDING_DIMS:
            raise ValueError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIMS}, got {vec.shape[-1]}"
            )
        return vec

    def embed_bytes(self, text: str) -> bytes:
        """Return an embedding as raw float32 bytes for sqlite-vec."""
        return self.embed_vector(text).tobytes()
