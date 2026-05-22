from pathlib import Path
import contextlib
import io
import logging
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


def embed(texts: Union[str, list], normalize: bool = True) -> np.ndarray:
    single = isinstance(texts, str)
    if single:
        texts = [texts]

    texts = [t if t.strip() else "." for t in texts]

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
