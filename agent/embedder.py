import json
from pathlib import Path

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


_CACHE_DIR = Path(settings.storage_dir)
_QUERY_CACHE = _CACHE_DIR / "query_embeddings.npy"
_QUERY_TEXTS_CACHE = _CACHE_DIR / "query_texts.json"
_query_cache_texts = None
_query_cache_embeddings = None


def _remote_embed(texts, normalize=True):
    client = _get_client()
    resp = client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
    )
    embs = np.array([r.embedding for r in resp.data])
    if normalize:
        norms = np.linalg.norm(embs, axis=1, keepdims=True)
        embs = np.divide(embs, norms, out=np.zeros_like(embs), where=norms != 0)
    return embs


def _load_query_cache():
    global _query_cache_texts, _query_cache_embeddings
    if _query_cache_texts is not None and _query_cache_embeddings is not None:
        return

    _query_cache_texts = []
    _query_cache_embeddings = None

    if not _QUERY_TEXTS_CACHE.exists() or not _QUERY_CACHE.exists():
        return

    try:
        meta = json.loads(_QUERY_TEXTS_CACHE.read_text(encoding="utf-8"))
        texts = meta.get("texts", [])
        if meta.get("model") != settings.embedding_model:
            return
        embeddings = np.load(_QUERY_CACHE)
        if len(texts) != len(embeddings):
            return
        _query_cache_texts = texts
        _query_cache_embeddings = embeddings
    except Exception:
        _query_cache_texts = []
        _query_cache_embeddings = None


def _save_query_cache():
    if settings.query_embedding_cache_size <= 0:
        return
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta = {"model": settings.embedding_model, "texts": _query_cache_texts}
    _QUERY_TEXTS_CACHE.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    np.save(_QUERY_CACHE, _query_cache_embeddings)


def _embed_single_cached(text: str, normalize=True):
    global _query_cache_texts, _query_cache_embeddings

    if not normalize or settings.query_embedding_cache_size <= 0:
        return _remote_embed([text], normalize=normalize)[0]

    _load_query_cache()
    if _query_cache_embeddings is not None and text in _query_cache_texts:
        idx = _query_cache_texts.index(text)
        return _query_cache_embeddings[idx].copy()

    emb = _remote_embed([text], normalize=normalize)[0]
    _query_cache_texts.append(text)
    if _query_cache_embeddings is None:
        _query_cache_embeddings = emb.reshape(1, -1)
    else:
        _query_cache_embeddings = np.vstack([_query_cache_embeddings, emb.reshape(1, -1)])

    overflow = len(_query_cache_texts) - settings.query_embedding_cache_size
    if overflow > 0:
        _query_cache_texts = _query_cache_texts[overflow:]
        _query_cache_embeddings = _query_cache_embeddings[overflow:]

    _save_query_cache()
    return emb


def embed(texts, normalize=True):
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    texts = [t if t.strip() else "." for t in texts]

    if single:
        return _embed_single_cached(texts[0], normalize=normalize)

    embs = _remote_embed(texts, normalize=normalize)
    return embs
