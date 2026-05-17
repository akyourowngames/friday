import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Union

import numpy as np
from openai import OpenAI, APITimeoutError, APIStatusError

from config import settings

_client = None
_EMBED_DIM = 384  # Standard ONNX embed model dimension
_local_model = None


def _get_client():
    """OpenAI client singleton for API fallback."""
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.nim_base_url,
            api_key=settings.nim_api_key,
            timeout=10.0,
            max_retries=0,
        )
    return _client


def _get_local_model():
    """Lazy load ONNX embedding model on first use."""
    global _local_model
    if _local_model is None:
        try:
            import onnxruntime as rt
            from huggingface_hub import hf_hub_download

            # Download the model if not cached
            model_path = hf_hub_download(
                repo_id="sentence-transformers/all-MiniLM-L6-v2",
                filename="onnx/model.onnx",
                cache_dir=Path(settings.storage_dir) / "onnx_models",
            )
            _local_model = rt.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        except Exception as e:
            import sys
            print(f"[WARNING] Failed to load ONNX model: {e}. Falling back to API.", file=sys.stderr)
            _local_model = False  # Marker for failed init
    return _local_model if _local_model is not False else None


def _normalize(embeddings: np.ndarray) -> np.ndarray:
    """Normalize embeddings to unit vectors."""
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return np.divide(embeddings, norms, out=np.zeros_like(embeddings), where=norms != 0)


def _local_embed(texts: list, normalize: bool = True) -> Union[np.ndarray, None]:
    """Embed texts using local ONNX model (fast, no API calls)."""
    model = _get_local_model()
    if model is None:
        return None

    try:
        import tokenizers

        # Use fast tokenizer
        tokenizer = tokenizers.Tokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
        encoded = tokenizer.encode_batch(texts)

        # Prepare inputs
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encoded], dtype=np.int64)

        # Run inference
        outputs = model.run(
            None,
            {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            },
        )
        embeddings = outputs[0]  # Token embeddings

        # Mean pooling
        mask_expanded = np.expand_dims(attention_mask, -1)
        sum_embeddings = np.sum(embeddings * mask_expanded, axis=1)
        sum_mask = np.clip(np.sum(mask_expanded, axis=1), a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask

        if normalize:
            embeddings = _normalize(embeddings)
        return embeddings
    except Exception as e:
        import sys
        print(f"[WARNING] ONNX embedding failed: {e}. Falling back to API.", file=sys.stderr)
        return None


def _remote_embed(texts: list, normalize: bool = True) -> Union[np.ndarray, None]:
    """Embed texts using remote API (fallback)."""
    client = _get_client()
    try:
        resp = client.embeddings.create(
            model=settings.embedding_model,
            input=texts,
        )
    except (APITimeoutError, APIStatusError, ConnectionError, TimeoutError):
        return None
    embs = np.array([r.embedding for r in resp.data])
    if normalize:
        embs = _normalize(embs)
    return embs


_CACHE_DIR = Path(settings.storage_dir)
_QUERY_CACHE = _CACHE_DIR / "query_embeddings.npy"
_QUERY_TEXTS_CACHE = _CACHE_DIR / "query_texts.json"
_query_cache_hashes = {}  # Hash -> embedding
_query_cache_loaded = False


def _text_hash(text: str) -> str:
    """Fast hash for cache lookup."""
    return hashlib.md5(text.encode()).hexdigest()


def _load_query_cache():
    """Load persistent embedding cache (hash-based for O(1) lookup)."""
    global _query_cache_hashes, _query_cache_loaded

    if _query_cache_loaded:
        return

    _query_cache_loaded = True
    if not _QUERY_TEXTS_CACHE.exists() or not _QUERY_CACHE.exists():
        return

    try:
        meta = json.loads(_QUERY_TEXTS_CACHE.read_text(encoding="utf-8"))
        texts = meta.get("texts", [])
        embeddings = np.load(_QUERY_CACHE)

        if len(texts) != len(embeddings):
            return

        for text, emb in zip(texts, embeddings):
            _query_cache_hashes[_text_hash(text)] = emb
    except Exception:
        pass


def _save_query_cache():
    """Save embedding cache to disk."""
    if settings.query_embedding_cache_size <= 0:
        return

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    texts = list(_query_cache_hashes.keys())
    embeddings = np.array([_query_cache_hashes[h] for h in texts[:settings.query_embedding_cache_size]])

    _QUERY_TEXTS_CACHE.write_text(
        json.dumps({"texts": texts[:settings.query_embedding_cache_size]}, ensure_ascii=False),
        encoding="utf-8",
    )
    np.save(_QUERY_CACHE, embeddings)


@lru_cache(maxsize=512)
def _get_null_embedding(dim: int) -> np.ndarray:
    """Cached null embedding (constant)."""
    return (np.ones(dim) / np.sqrt(dim)).astype(np.float32)


def embed(texts: Union[str, list], normalize: bool = True) -> np.ndarray:
    """
    Embed texts using local ONNX model with LRU + persistent caching.
    Falls back to remote API if local model fails.
    
    Args:
        texts: Single string or list of strings to embed
        normalize: Whether to normalize embeddings
        
    Returns:
        numpy array of embeddings
    """
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    texts = [t if t.strip() else "." for t in texts]

    # Try fast local ONNX embedding first
    embeddings = _local_embed(texts, normalize=normalize)
    if embeddings is not None:
        if single:
            return embeddings[0]
        return embeddings

    # Fallback: check cache, then API
    _load_query_cache()
    results = []

    for text in texts:
        text_hash = _text_hash(text)

        # Check in-memory cache
        if text_hash in _query_cache_hashes:
            results.append(_query_cache_hashes[text_hash].copy())
            continue

        # Try remote API
        emb = _remote_embed([text], normalize=normalize)
        if emb is not None:
            emb = emb[0]
        else:
            # Null embedding fallback
            emb = _get_null_embedding(_EMBED_DIM)

        results.append(emb)

        # Add to cache
        if settings.query_embedding_cache_size > 0:
            _query_cache_hashes[text_hash] = emb
            if len(_query_cache_hashes) > settings.query_embedding_cache_size:
                # Simple eviction: remove oldest (dict order in Python 3.7+)
                oldest_key = next(iter(_query_cache_hashes))
                del _query_cache_hashes[oldest_key]
                _save_query_cache()

    result_array = np.array(results)
    if single:
        return result_array[0]
    return result_array
