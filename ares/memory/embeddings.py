"""Embedding providers for Ares memory search."""

from __future__ import annotations

import logging
from dataclasses import dataclass
import hashlib
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

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


def _load_onnx_sentence_transformer(
    model_name: str,
    *,
    provider: str,
    model_kwargs: dict[str, Any],
    local_files_only: bool,
) -> Any:
    """Load a SentenceTransformer ONNX model, silencing a misleading log line.

    Sentence Transformers 5.x prints a confusing
    ``expected str, bytes or os.PathLike object, not NoneType`` error (to
    stderr) while it inspects whether the ONNX model is already converted.  The
    load itself succeeds; the message only looks like a failure.  We suppress
    that specific output so operators are not alarmed, while keeping every
    other warning/error visible.
    """
    import contextlib
    import io
    from sentence_transformers import SentenceTransformer

    _NOISE = (
        "could not infer whether the model was already converted",
        "expected str, bytes or os.PathLike object, not NoneType",
    )

    class _FilterStderr(io.StringIO):
        def write(self, s: str) -> int:  # noqa: A003
            if any(fragment in s for fragment in _NOISE):
                return 0
            return super().write(s)

    buffer = _FilterStderr()
    try:
        with contextlib.redirect_stderr(buffer):
            return SentenceTransformer(
                model_name,
                backend="onnx",
                model_kwargs=model_kwargs,
                local_files_only=local_files_only,
            )
    finally:
        stray = buffer.getvalue()
        if stray.strip():
            logger.debug("sentence-transformers load output: %s", stray.strip())


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
                # Pin export explicitly.  Leaving it None makes Sentence
                # Transformers 5.x guess whether the ONNX file is already
                # converted, which logs a misleading "expected str, bytes or
                # os.PathLike object, not NoneType" error and can attempt to
                # re-export the model at load time.
                model_kwargs["export"] = False if self.export is None else self.export
                model = _load_onnx_sentence_transformer(
                    self.model_name,
                    provider=self.provider,
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
        raw = model.encode(text)
        if raw is None:
            # A failed/partial ONNX load can yield None instead of a vector.
            # Surface it clearly so callers skip vector search rather than feed
            # None bytes into sqlite-vec (which raises a cryptic ProgrammingError).
            raise ValueError("embedding model returned None for the query")
        vec = np.asarray(raw, dtype="float32")
        if vec.shape[-1] != EMBEDDING_DIMS:
            raise ValueError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIMS}, got {vec.shape[-1]}"
            )
        if vec.size == 0:
            raise ValueError("embedding model returned an empty vector")
        return vec

    def embed_bytes(self, text: str) -> bytes:
        """Return an embedding as raw float32 bytes for sqlite-vec."""
        return self.embed_vector(text).tobytes()
