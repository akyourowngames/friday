import json
import os
import shutil
import tempfile
from pathlib import Path

import faiss
import numpy as np


class VectorStore:
    """Persistent vector store backed by FAISS index + metadata JSON.

    Embeds each memory once, stores in FAISS IndexIDMap (IndexFlatIP),
    and provides ANN search over normalized vectors.
    """

    def __init__(self, index_path: Path, metadata_path: Path, dim: int = 384):
        self.index_path = Path(index_path)
        self.metadata_path = Path(metadata_path)
        self.dim = dim
        self.index: faiss.IndexIDMap | None = None
        self.metadata: list[dict] = []
        self._loaded = False

    # --- Public API ---

    def add(self, embedding: np.ndarray, text: str, metadata: dict | None = None) -> int:
        """Add a single embedding vector + metadata. Returns the assigned ID."""
        self._lazy_init()
        emb = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
        if emb.shape[1] != self.dim:
            emb = emb[:, :self.dim] if emb.shape[1] >= self.dim else np.pad(emb, ((0, 0), (0, self.dim - emb.shape[1])))
        faiss.normalize_L2(emb)
        next_id = len(self.metadata)
        ids = np.array([next_id], dtype=np.int64)
        self.index.add_with_ids(emb, ids)
        self.metadata.append({
            "id": next_id,
            "text": str(text),
            **(metadata or {}),
        })
        self.save()
        return next_id

    def add_batch(self, embeddings: np.ndarray, texts: list[str], metadata_list: list[dict] | None = None) -> int:
        """Add multiple embeddings at once. Returns count added."""
        self._lazy_init()
        embs = np.asarray(embeddings, dtype=np.float32)
        if embs.ndim == 1:
            embs = embs.reshape(1, -1)
        if embs.shape[1] != self.dim:
            embs = embs[:, :self.dim] if embs.shape[1] >= self.dim else np.pad(embs, ((0, 0), (0, self.dim - embs.shape[1])))
        faiss.normalize_L2(embs)
        start_id = len(self.metadata)
        n = embs.shape[0]
        ids = np.arange(start_id, start_id + n, dtype=np.int64)
        self.index.add_with_ids(embs, ids)
        meta_list = metadata_list or [{} for _ in range(n)]
        for i in range(n):
            self.metadata.append({
                "id": start_id + i,
                "text": str(texts[i]) if texts and i < len(texts) else "",
                **(meta_list[i] if i < len(meta_list) else {}),
            })
        self.save()
        return n

    def search(self, query_emb: np.ndarray, k: int = 5) -> list[dict]:
        """ANN search. Returns list of {id, text, score, ...metadata}."""
        self._lazy_init()
        n_total = len(self.metadata)
        if n_total == 0:
            return []
        safe_k = min(max(1, int(k)), n_total)
        q = np.asarray(query_emb, dtype=np.float32).reshape(1, -1)
        if q.shape[1] != self.dim:
            q = q[:, :self.dim] if q.shape[1] >= self.dim else np.pad(q, ((0, 0), (0, self.dim - q.shape[1])))
        faiss.normalize_L2(q)
        distances, indices = self.index.search(q, safe_k)
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(self.metadata):
                continue
            meta = self.metadata[int(idx)]
            results.append({
                "id": meta.get("id", int(idx)),
                "text": meta.get("text", ""),
                "score": float(dist),
                **(meta.get("extra", {})),
            })
        return results

    def remove(self, ids: list[int]):
        """Remove vectors by ID. FAISS does not support true deletion from
        IndexFlatIP, so we remove from metadata only. The index is rebuilt."""
        if not ids:
            return
        id_set = set(int(i) for i in ids)
        surviving_meta = [m for m in self.metadata if m["id"] not in id_set]
        if len(surviving_meta) == len(self.metadata):
            return
        self.metadata = surviving_meta
        self._rebuild_index_from_metadata()
        self.save()

    def size(self) -> int:
        return len(self.metadata)

    def get_embeddings(self) -> np.ndarray:
        """Reconstruct a dense numpy array (n_total x dim) from the index,
        for backward-compatibility checks.

        Returns empty array if reconstruction is not supported by the index type."""
        self._lazy_init()
        n = len(self.metadata)
        if n == 0:
            return np.empty((0, 0), dtype=np.float32)
        if not hasattr(self.index, "reconstruct") or not callable(getattr(self.index, "reconstruct", None)):
            return np.empty((0, 0), dtype=np.float32)
        try:
            ids = np.array([m["id"] for m in self.metadata], dtype=np.int64)
            vectors = np.empty((n, self.dim), dtype=np.float32)
            for i, idx in enumerate(ids):
                vec = self.index.reconstruct(int(idx))
                vectors[i] = vec
            return vectors
        except (RuntimeError, AttributeError, NotImplementedError):
            return np.empty((0, 0), dtype=np.float32)

    def save(self):
        self._lazy_init()
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=self.index_path.parent, prefix=f".{self.index_path.name}.", suffix=".tmp")
        os.close(fd)
        try:
            faiss.write_index(self.index, temp_path)
            shutil.move(temp_path, self.index_path)
        finally:
            if Path(temp_path).exists():
                Path(temp_path).unlink()
        self._atomic_write_json(self.metadata_path, self.metadata)

    def load(self) -> bool:
        """Load existing index and metadata from disk. Returns True if loaded."""
        if not self.index_path.exists() or not self.metadata_path.exists():
            return False
        try:
            idx = faiss.read_index(str(self.index_path))
            if idx is None:
                return False
            self.index = idx
            self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            self._loaded = True
            return True
        except (OSError, json.JSONDecodeError, RuntimeError, ValueError):
            return False

    def migrate_from_embeddings(self, embeddings: np.ndarray, metadata: list[dict]) -> int:
        """Migrate legacy .npy embeddings + metadata into the vector store.

        Returns number of entries migrated, or 0 if dimensions mismatch."""
        if embeddings.ndim != 2:
            return 0
        if embeddings.shape[1] != self.dim:
            return 0
        if embeddings.shape[0] != len(metadata):
            return 0
        if embeddings.shape[0] == 0:
            return 0
        self._lazy_init()
        embs = np.asarray(embeddings, dtype=np.float32)
        faiss.normalize_L2(embs)
        start_id = len(self.metadata)
        n = embs.shape[0]
        ids = np.arange(start_id, start_id + n, dtype=np.int64)
        self.index.add_with_ids(embs, ids)
        for i in range(n):
            entry = dict(metadata[i] or {})
            entry["id"] = start_id + i
            if "text" not in entry:
                entry["text"] = str(entry.get("text", ""))
            self.metadata.append(entry)
        self.save()
        return n

    def clear(self):
        self.index = None
        self.metadata = []
        self._loaded = False
        if self.index_path.exists():
            self.index_path.unlink()
        if self.metadata_path.exists():
            self.metadata_path.unlink()

    # --- Internal ---

    def _lazy_init(self):
        if self.index is not None:
            return
        if not self.load() or self.index is None:
            self.index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))
            self.metadata = []

    def _rebuild_index_from_metadata(self):
        self.index = faiss.IndexIDMap(faiss.IndexFlatIP(self.dim))
        if not self.metadata:
            return
        texts = [m.get("text", "") for m in self.metadata]
        from agent.embedder import embed
        embs = embed(texts)
        embs = np.asarray(embs, dtype=np.float32)
        if embs.ndim == 1:
            embs = embs.reshape(1, -1)
        if embs.shape[1] != self.dim:
            embs = embs[:, :self.dim] if embs.shape[1] >= self.dim else np.pad(embs, ((0, 0), (0, self.dim - embs.shape[1])))
        faiss.normalize_L2(embs)
        ids = np.array([m["id"] for m in self.metadata], dtype=np.int64)
        self.index.add_with_ids(embs, ids)

    def _atomic_write_json(self, path: Path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            shutil.move(temp_path, path)
        finally:
            if Path(temp_path).exists():
                Path(temp_path).unlink()
