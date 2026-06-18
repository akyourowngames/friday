"""Tests for embedding backend selection."""

import sys
import types

import numpy as np
import pytest

from ares import embeddings
from ares.embeddings import DEFAULT_EMBEDDING_MODEL, DEFAULT_ONNX_FILE_NAME, EMBEDDING_DIMS, EmbeddingProvider


def test_embedding_provider_uses_onnx_backend(monkeypatch):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, *, backend, model_kwargs=None):
            calls.append((model_name, backend, model_kwargs))

        def encode(self, text):
            return np.ones(384, dtype="float32")

    module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    embeddings._MODEL_CACHE.clear()

    provider = EmbeddingProvider(
        model_name="test-model",
        backend="onnx",
        provider="CPUExecutionProvider",
        file_name="onnx/model.onnx",
    )

    assert len(provider.embed_bytes("hello")) == 384 * 4
    assert calls == [
        (
            "test-model",
            "onnx",
            {"provider": "CPUExecutionProvider", "file_name": "onnx/model.onnx"},
        )
    ]


def test_embedding_cache_includes_onnx_file_name(monkeypatch):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, *, backend, model_kwargs=None):
            calls.append((model_name, backend, model_kwargs))
            self.file_name = (model_kwargs or {}).get("file_name", "")

        def encode(self, text):
            return np.ones(384, dtype="float32")

    module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    embeddings._MODEL_CACHE.clear()

    first = EmbeddingProvider(
        model_name="test-model",
        backend="onnx",
        provider="CPUExecutionProvider",
        file_name="onnx/model.onnx",
    )
    second = EmbeddingProvider(
        model_name="test-model",
        backend="onnx",
        provider="CPUExecutionProvider",
        file_name="onnx/model_quantized.onnx",
    )

    first.embed_bytes("hello")
    second.embed_bytes("hello")

    assert [call[2]["file_name"] for call in calls] == [
        "onnx/model.onnx",
        "onnx/model_quantized.onnx",
    ]


def test_default_onnx_file_name_is_set_for_default_model(monkeypatch):
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, *, backend, model_kwargs=None):
            calls.append((model_name, backend, model_kwargs))

        def encode(self, text):
            return np.ones(384, dtype="float32")

    module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    embeddings._MODEL_CACHE.clear()

    provider = EmbeddingProvider(model_name=DEFAULT_EMBEDDING_MODEL, backend="onnx")
    provider.embed_bytes("hello")

    assert calls[0][2]["file_name"] == DEFAULT_ONNX_FILE_NAME


def test_embedding_provider_falls_back_to_hash_when_sentence_transformers_import_fails(monkeypatch):
    module = types.ModuleType("sentence_transformers")
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    embeddings._MODEL_CACHE.clear()

    provider = EmbeddingProvider(model_name="test-model", backend="onnx")

    vector = provider.embed_vector("Krish is a JEE aspirant")

    assert vector.shape == (EMBEDDING_DIMS,)
    assert provider.backend_used == "hash"
    assert provider.fallback_error


def test_hash_backend_is_available_without_sentence_transformers(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    embeddings._MODEL_CACHE.clear()

    provider = EmbeddingProvider(backend="hash")

    assert len(provider.embed_bytes("hello")) == EMBEDDING_DIMS * 4
    assert provider.backend_used == "hash"


def test_embedding_provider_rejects_unknown_backend():
    with pytest.raises(ValueError, match="Unsupported embedding backend"):
        EmbeddingProvider(backend="made-up")
