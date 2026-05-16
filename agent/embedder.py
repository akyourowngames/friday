import numpy as np
from openai import OpenAI

from config import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
        )
    return _client


_DIM = 4096


def embed(texts, normalize=True):
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    texts = [t if t.strip() else "." for t in texts]

    client = _get_client()
    resp = client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
    )
    embs = np.array([r.embedding for r in resp.data])
    if normalize:
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        embs = np.divide(embs, norms, out=np.zeros_like(embs), where=norms != 0)
    return embs[0] if single else embs
