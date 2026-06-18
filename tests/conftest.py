"""Shared test helpers."""

import hashlib

import numpy as np
import pytest


class FakeEmbeddingProvider:
    """Fast deterministic 384-dim embedding provider for SQLite tests."""

    def embed_bytes(self, text: str) -> bytes:
        vec = np.zeros(384, dtype="float32")
        for token in text.lower().replace("'", " ").split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "little") % 384
            vec[index] += 1.0
        return vec.tobytes()


@pytest.fixture
def fake_embedding_provider():
    return FakeEmbeddingProvider()
