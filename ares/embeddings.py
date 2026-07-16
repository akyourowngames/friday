"""Embedding providers for Ares memory search."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

import numpy as np

EMBEDDING_DIMS = 384
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_ONNX_FILE_NAME = "onnx/model.onnx"
SUPPORTED_BACKENDS = {"torch", "onnx", "hash"}

_MODEL_CACHE: dict[tuple[str, str, str, str, bool | None, bool], Any] = {}


class HashEmbeddingModel:
    """Tiny deterministic local embedding fallback."""

    def encode(self, text: str) -> np.ndarray:
        vec = np.zeros(EMBEDDING_DIMS, dtype="float32")
        for token in text.lower().replace("'", " ").split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "little") % EMBEDDING_DIMS
            vec[index] += 1.0
        norm = np.linalg.norm(vec)
        if norm:
            vec /= norm
        return vec


@dataclass
class EmbeddingProvider:
    """Small wrapper around Sentence Transformers embedding backends."""

    model_name: str = DEFAULT_EMBEDDING_MODEL
    backend: str = "onnx"
    provider: str = "CPUExecutionProvider"
    file_name: str = ""
    export: bool | None = None
    fallback_to_torch: bool = True
    fallback_to_hash: bool = True
    local_files_only: bool = True

    def __post_init__(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise ValueError(f"Unsupported embedding backend: {self.backend}")
        self.backend_used = self.backend
        self.fallback_error: str | None = None

    def _load_model(self) -> Any:
        effective_file_name = self._effective_file_name()
        cache_key = (
            self.model_name, self.backend, self.provider, effective_file_name,
            self.export, self.local_files_only,
        )
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        if self.backend == "hash":
            return self._load_hash_model()

        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            if self.fallback_to_hash:
                return self._load_hash_model(exc)
            raise

        try:
            if self.backend == "onnx":
                model_kwargs: dict[str, Any] = {"provider": self.provider}
                if effective_file_name:
                    model_kwargs["file_name"] = effective_file_name
                if self.export is not None:
                    model_kwargs["export"] = self.export
                model = SentenceTransformer(
                    self.model_name,
                    backend="onnx",
                    model_kwargs=model_kwargs,
                    local_files_only=self.local_files_only,
                )
            else:
                model = SentenceTransformer(
                    self.model_name, backend="torch", local_files_only=self.local_files_only
                )
        except Exception as exc:
            if self.backend != "onnx" or not self.fallback_to_torch:
                if self.fallback_to_hash:
                    return self._load_hash_model(exc)
                raise
            self.backend_used = "torch"
            self.fallback_error = str(exc)
            cache_key = (self.model_name, "torch", "", "", None, self.local_files_only)
            cached = _MODEL_CACHE.get(cache_key)
            if cached is not None:
                return cached
            try:
                model = SentenceTransformer(
                    self.model_name, backend="torch", local_files_only=self.local_files_only
                )
            except Exception as torch_exc:
                if self.fallback_to_hash:
                    return self._load_hash_model(torch_exc)
                raise

        _MODEL_CACHE[cache_key] = model
        return model

    def _effective_file_name(self) -> str:
        """Return the explicit or known default ONNX file name."""
        if self.file_name:
            return self.file_name
        if self.backend == "onnx" and self.model_name == DEFAULT_EMBEDDING_MODEL:
            return DEFAULT_ONNX_FILE_NAME
        return ""

    def _load_hash_model(self, exc: Exception | None = None) -> HashEmbeddingModel:
        """Load the deterministic hash fallback model."""
        self.backend_used = "hash"
        if exc is not None:
            self.fallback_error = str(exc)
        cache_key = (self.model_name, "hash", "", "", None, self.local_files_only)
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        model = HashEmbeddingModel()
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
