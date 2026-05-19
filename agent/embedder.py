from pathlib import Path
from typing import Union

import numpy as np
from openai import OpenAI, APITimeoutError, APIStatusError

from config import settings

_client = None
_EMBED_DIM = 384
_local_model = None


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
            from huggingface_hub import hf_hub_download

            model_path = hf_hub_download(
                repo_id="sentence-transformers/all-MiniLM-L6-v2",
                filename="onnx/model.onnx",
                cache_dir=Path(settings.storage_dir) / "onnx_models",
            )
            _local_model = rt.InferenceSession(model_path, providers=["CPUExecutionProvider"])
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
        import tokenizers

        tokenizer = tokenizers.Tokenizer.from_pretrained("sentence-transformers/all-MiniLM-L6-v2")
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
