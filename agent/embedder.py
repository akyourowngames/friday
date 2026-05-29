import json
from pathlib import Path
import contextlib
import io
import logging
import threading
from typing import Union

import numpy as np
from openai import OpenAI, APITimeoutError, APIStatusError

from config import settings

logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

_client = None
_EMBED_DIM = 384
_local_model = None
_local_tokenizer = None
_MODEL_REPO = "sentence-transformers/all-MiniLM-L6-v2"
_MODEL_CACHE = Path(settings.storage_dir) / "onnx_models"
_QUERY_CACHE_LOCK = threading.RLock()
_QUERY_CACHE_LOADED = False
_QUERY_CACHE_TEXTS = []
_QUERY_CACHE_EMBEDDINGS = None
_QUERY_CACHE_MAP = {}


def _quiet_call(func, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return func(*args, **kwargs)


def _cached_hf_file(filename: str) -> Path | None:
    for path in _MODEL_CACHE.rglob(Path(filename).name):
        if str(path).replace("\\", "/").endswith(filename):
            return path
    return None


def _get_client():
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
    global _local_model
    if _local_model is None:
        try:
            import onnxruntime as rt

            model_path = _cached_hf_file("onnx/model.onnx")
            if model_path is None:
                from huggingface_hub import hf_hub_download

                model_path = Path(_quiet_call(
                    hf_hub_download,
                    repo_id=_MODEL_REPO,
                    filename="onnx/model.onnx",
                    cache_dir=_MODEL_CACHE,
                ))
            _local_model = rt.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        except Exception as e:
            import sys
            print(f"[WARNING] Failed to load ONNX model: {e}. Falling back to API.", file=sys.stderr)
            _local_model = False
    return _local_model if _local_model is not False else None


def _normalize(embeddings: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    return np.divide(embeddings, norms, out=np.zeros_like(embeddings), where=norms != 0)


def _local_embed(texts: list, normalize: bool = True) -> Union[np.ndarray, None]:
    model = _get_local_model()
    if model is None:
        return None

    try:
        global _local_tokenizer
        import tokenizers

        if _local_tokenizer is None:
            tokenizer_path = _cached_hf_file("tokenizer.json")
            if tokenizer_path is None:
                from huggingface_hub import hf_hub_download

                tokenizer_path = Path(_quiet_call(
                    hf_hub_download,
                    repo_id=_MODEL_REPO,
                    filename="tokenizer.json",
                    cache_dir=_MODEL_CACHE,
                ))
            _local_tokenizer = tokenizers.Tokenizer.from_file(
                str(tokenizer_path)
            )
        tokenizer = _local_tokenizer
        encoded = tokenizer.encode_batch(texts)

        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
        token_type_ids = np.array([e.type_ids for e in encoded], dtype=np.int64)

        outputs = model.run(None, {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        })
        embeddings = outputs[0]

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
    client = _get_client()
    try:
        resp = client.embeddings.create(model=settings.embedding_model, input=texts)
    except (APITimeoutError, APIStatusError, ConnectionError, TimeoutError):
        return None
    embs = np.array([r.embedding for r in resp.data])
    if normalize:
        embs = _normalize(embs)
    return embs


def _query_cache_paths() -> tuple[Path, Path]:
    return Path(settings.query_embedding_texts_file), Path(settings.query_embedding_cache_file)


def _query_cache_limit() -> int:
    try:
        return max(0, int(settings.query_embedding_cache_size))
    except (TypeError, ValueError):
        return 0


def _load_query_cache():
    global _QUERY_CACHE_LOADED, _QUERY_CACHE_TEXTS, _QUERY_CACHE_EMBEDDINGS, _QUERY_CACHE_MAP
    if _QUERY_CACHE_LOADED:
        return
    _QUERY_CACHE_LOADED = True
    texts_path, embeddings_path = _query_cache_paths()
    if not texts_path.exists() or not embeddings_path.exists():
        _QUERY_CACHE_TEXTS = []
        _QUERY_CACHE_EMBEDDINGS = None
        _QUERY_CACHE_MAP = {}
        return
    try:
        texts = json.loads(texts_path.read_text(encoding="utf-8"))
        embeddings = np.load(embeddings_path)
    except (OSError, json.JSONDecodeError, ValueError):
        _QUERY_CACHE_TEXTS = []
        _QUERY_CACHE_EMBEDDINGS = None
        _QUERY_CACHE_MAP = {}
        return
    if not isinstance(texts, list) or len(texts) != getattr(embeddings, "shape", (0,))[0]:
        _QUERY_CACHE_TEXTS = []
        _QUERY_CACHE_EMBEDDINGS = None
        _QUERY_CACHE_MAP = {}
        return
    _QUERY_CACHE_TEXTS = [str(text) for text in texts]
    _QUERY_CACHE_EMBEDDINGS = np.asarray(embeddings, dtype=np.float32)
    _QUERY_CACHE_MAP = {text: idx for idx, text in enumerate(_QUERY_CACHE_TEXTS)}


def _save_query_cache():
    texts_path, embeddings_path = _query_cache_paths()
    texts_path.parent.mkdir(parents=True, exist_ok=True)
    embeddings_path.parent.mkdir(parents=True, exist_ok=True)
    texts_path.write_text(json.dumps(_QUERY_CACHE_TEXTS, indent=2, ensure_ascii=False), encoding="utf-8")
    if _QUERY_CACHE_EMBEDDINGS is None:
        np.save(embeddings_path, np.empty((0, _EMBED_DIM), dtype=np.float32))
    else:
        np.save(embeddings_path, _QUERY_CACHE_EMBEDDINGS)


def _cached_many(texts: list[str]) -> tuple[list[np.ndarray | None], list[str]]:
    with _QUERY_CACHE_LOCK:
        _load_query_cache()
        cached = []
        misses = []
        seen_misses = set()
        for text in texts:
            idx = _QUERY_CACHE_MAP.get(text)
            if idx is None or _QUERY_CACHE_EMBEDDINGS is None:
                cached.append(None)
                if text not in seen_misses:
                    seen_misses.add(text)
                    misses.append(text)
                continue
            cached.append(np.asarray(_QUERY_CACHE_EMBEDDINGS[idx], dtype=np.float32))
        return cached, misses


def _remember_many(texts: list[str], embeddings: np.ndarray):
    limit = _query_cache_limit()
    if limit <= 0:
        return
    embeddings = np.asarray(embeddings, dtype=np.float32)
    if embeddings.ndim == 1:
        embeddings = embeddings.reshape(1, -1)
    if embeddings.shape[0] != len(texts):
        return
    with _QUERY_CACHE_LOCK:
        _load_query_cache()
        global _QUERY_CACHE_TEXTS, _QUERY_CACHE_EMBEDDINGS, _QUERY_CACHE_MAP
        rows = []
        row_texts = []
        if _QUERY_CACHE_EMBEDDINGS is not None and len(_QUERY_CACHE_TEXTS):
            for idx, existing_text in enumerate(_QUERY_CACHE_TEXTS):
                if existing_text in texts:
                    continue
                row_texts.append(existing_text)
                rows.append(_QUERY_CACHE_EMBEDDINGS[idx])
        for text, embedding in zip(texts, embeddings):
            row_texts.append(text)
            rows.append(embedding)
        if len(row_texts) > limit:
            row_texts = row_texts[-limit:]
            rows = rows[-limit:]
        _QUERY_CACHE_TEXTS = row_texts
        _QUERY_CACHE_EMBEDDINGS = np.asarray(rows, dtype=np.float32) if rows else None
        _QUERY_CACHE_MAP = {text: idx for idx, text in enumerate(_QUERY_CACHE_TEXTS)}
        _save_query_cache()


def embed(texts: Union[str, list], normalize: bool = True) -> np.ndarray:
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    texts = [t if t.strip() else "." for t in texts]
    if normalize and _query_cache_limit() > 0:
        cached, misses = _cached_many(texts)
        if not misses:
            result = np.asarray(cached, dtype=np.float32)
            if single:
                return result[0]
            return result
        miss_embeddings = _local_embed(misses, normalize=normalize)
        if miss_embeddings is None:
            miss_embeddings = _remote_embed(misses, normalize=normalize)
        if miss_embeddings is None:
            dim = _EMBED_DIM
            norm = 1.0 / np.sqrt(dim)
            miss_embeddings = np.array([np.ones(dim, dtype=np.float32) * norm for _ in misses])
        miss_embeddings = np.asarray(miss_embeddings, dtype=np.float32)
        if miss_embeddings.ndim == 1:
            miss_embeddings = miss_embeddings.reshape(1, -1)
        _remember_many(misses, miss_embeddings)
        miss_map = {text: miss_embeddings[idx] for idx, text in enumerate(misses)}
        rows = []
        for text, cached_row in zip(texts, cached):
            rows.append(cached_row if cached_row is not None else miss_map[text])
        result = np.asarray(rows, dtype=np.float32)
        if single:
            return result[0]
        return result

    embeddings = _local_embed(texts, normalize=normalize)
    if embeddings is not None:
        if single:
            return embeddings[0]
        return embeddings

    embeddings = _remote_embed(texts, normalize=normalize)
    if embeddings is not None:
        if single:
            return embeddings[0]
        return embeddings

    dim = _EMBED_DIM
    norm = 1.0 / np.sqrt(dim)
    fallback = np.array([np.ones(dim, dtype=np.float32) * norm for _ in texts])
    if single:
        return fallback[0]
    return fallback
