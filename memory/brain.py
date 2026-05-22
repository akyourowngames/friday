import hashlib
import json
import os
import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path
from time import perf_counter

import numpy as np

from agent.embedder import embed
from config import settings

MEMORY_DIR = Path(settings.memory_dir)
BACKUP_DIR = Path(settings.memory_backup_dir)
MEMORY_FILTER_POLICY_PATH = Path(settings.memory_filter_policy_file)
_INDEX_SCHEMA_VERSION = 2

_VAGUE_PATTERNS = [
    "medical records",
    "doctor's offices",
    "urgent care",
    "area with available",
    "listened to",
    "playlist contains",
]


def _load_policy_reject_phrases() -> list[str]:
    path = MEMORY_FILTER_POLICY_PATH
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    if not path.exists():
        return []

    phrases = []
    in_reject_section = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_reject_section = line.lower() == "## reject facts containing"
            continue
        if not in_reject_section or not line.startswith("- "):
            continue
        phrase = line[2:].strip().lower()
        if phrase:
            phrases.append(phrase)
    return phrases

_CONTRADICTION_CATEGORIES = {
    "name": ["name is", "name :", "full name", "called "],
    "location": ["lives in", "live in", "lives at", "is from", "living in"],
    "age": ["year", "years old", "age is", " yrs"],
    "health": [
        "not feeling",
        "feeling sick",
        "feeling bad",
        "feels sick",
        "feels bad",
        "recovered",
        "feeling better",
        "feeling well",
        "is well",
        "is better",
        "is not well",
        "is sick",
        "is healthy",
    ],
}


def _is_vague(text: str) -> bool:
    lower = text.lower()
    if len(text) < 15:
        return True
    for pat in _VAGUE_PATTERNS + _load_policy_reject_phrases():
        if pat in lower:
            return True
    return False


def _contradiction_category(text: str) -> str | None:
    lower = text.lower()
    for category, keywords in _CONTRADICTION_CATEGORIES.items():
        for kw in keywords:
            if kw in lower:
                return category
    return None


def _normalize_fact(text: str) -> str:
    words = text.strip().split()
    normalized = []
    i = 0
    while i < len(words):
        current = words[i]
        current_key = current.strip(".,;:!?").casefold()
        next_key = words[i + 1].strip(".,;:!?").casefold() if i + 1 < len(words) else ""
        if current_key == "now" and next_key in {"lives", "is", "has"}:
            i += 1
            continue
        if current_key in {"actually", "currently"}:
            i += 1
            continue
        normalized.append(current)
        i += 1
    return " ".join(normalized).strip()


def _safe_float(value, default: float = 0.5) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(result):
        return default
    return result


def _importance_bounds() -> tuple[float, float]:
    lower = _safe_float(settings.memory_importance_min, 0.0)
    upper = _safe_float(settings.memory_importance_max, 1.0)
    if upper < lower:
        lower, upper = upper, lower
    return lower, upper


def _normalize_importance(value) -> float:
    lower, upper = _importance_bounds()
    default = min(max(0.5, lower), upper)
    score = _safe_float(value, default)
    return min(max(score, lower), upper)


def _memory_capacity_limit() -> int:
    try:
        return max(0, int(settings.memory_max_entries))
    except (TypeError, ValueError):
        return 0


def _daily_date_from_memory_file(file_path: Path) -> str | None:
    stem = file_path.stem
    prefix = "memory_"
    if not stem.startswith(prefix):
        return None
    date_part = stem[len(prefix):]
    try:
        date.fromisoformat(date_part)
    except ValueError:
        return None
    return date_part


def _memory_id(item: dict) -> str:
    payload = "|".join(
        [
            str(item.get("_date", "")),
            str(item.get("ts", "")),
            str(item.get("text", "")),
        ]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _memory_signature(memories: list[dict]) -> str:
    payload = []
    for item in memories:
        payload.append(
            "|".join(
                [
                    _memory_id(item),
                    str(item.get("importance", 0.5)),
                    str(item.get("text", "")),
                ]
            )
        )
    digest = "\n".join(payload)
    return hashlib.sha1(digest.encode("utf-8")).hexdigest()


def _timestamp_key(item: dict) -> tuple[str, str, str]:
    return (
        str(item.get("_date", "")),
        str(item.get("ts", "")),
        str(item.get("text", "")),
    )


def _ensure_2d(array: np.ndarray) -> np.ndarray:
    if array.ndim == 1:
        return array.reshape(1, -1)
    return array


class Brain:
    def __init__(self):
        self.memories = []
        self._embeddings = None
        self._index_state = "cold"
        self._last_backup = None
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._load_all()
        self._load_or_build_index()

    def _index_meta_path(self) -> Path:
        return MEMORY_DIR / settings.memory_index_file

    def _index_embeddings_path(self) -> Path:
        return MEMORY_DIR / settings.memory_embeddings_file

    def _archive_path(self) -> Path:
        return MEMORY_DIR / settings.memory_archive_file

    def _memory_files(self) -> list[Path]:
        files = []
        for file_path in MEMORY_DIR.glob("memory_*.json"):
            if _daily_date_from_memory_file(file_path):
                files.append(file_path)
        return sorted(files)

    def _normalize_loaded_item(self, item: dict, date_str: str) -> dict | None:
        text = str(item.get("text", "")).strip()
        if not text:
            return None
        text = _normalize_fact(text)
        if _is_vague(text):
            return None
        return {
            "text": text,
            "importance": _normalize_importance(item.get("importance")),
            "ts": str(item.get("ts") or "00:00:00"),
            "_date": date_str,
        }

    def _load_all(self):
        self.memories = []
        seen = set()
        for file_path in self._memory_files():
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, list):
                continue
            date_str = _daily_date_from_memory_file(file_path)
            if date_str is None:
                continue
            for item in data:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_loaded_item(item, date_str)
                if not normalized:
                    continue
                key = normalized["text"].strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                self.memories.append(normalized)
        self.memories.sort(key=_timestamp_key)

    def _today_path(self):
        return MEMORY_DIR / f"memory_{date.today().isoformat()}.json"

    def _atomic_write_text(self, path: Path, content: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _atomic_write_json(self, path: Path, payload):
        self._atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))

    def _atomic_write_npy(self, path: Path, array: np.ndarray):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(fd)
        try:
            with open(temp_path, "wb") as handle:
                np.save(handle, array)
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _index_payload(self) -> dict:
        return {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "entry_count": len(self.memories),
            "signature": _memory_signature(self.memories),
            "source_files": [path.name for path in self._memory_files()],
            "last_backup": self._last_backup,
        }

    def create_backup(self, label: str = "manual") -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"{stamp}_{label}"
        backup_path.mkdir(parents=True, exist_ok=True)

        copied = 0
        for file_path in self._memory_files():
            shutil.copy2(file_path, backup_path / file_path.name)
            copied += 1

        for extra in (self._index_meta_path(), self._index_embeddings_path(), self._archive_path()):
            if extra.exists():
                shutil.copy2(extra, backup_path / extra.name)
                copied += 1

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "copied_files": copied,
            "entry_count": len(self.memories),
        }
        self._atomic_write_json(backup_path / "backup_manifest.json", manifest)
        self._last_backup = str(backup_path)
        return str(backup_path)

    def _load_or_build_index(self):
        if not self.memories:
            self._embeddings = np.empty((0, 0), dtype=np.float32)
            self._index_state = "empty"
            self._persist_index()
            return

        meta_path = self._index_meta_path()
        emb_path = self._index_embeddings_path()
        signature = _memory_signature(self.memories)

        if meta_path.exists() and emb_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                embeddings = np.load(emb_path)
                embeddings = _ensure_2d(embeddings)
                if (
                    meta.get("schema_version") == _INDEX_SCHEMA_VERSION
                    and meta.get("signature") == signature
                    and int(meta.get("entry_count", -1)) == len(self.memories)
                    and embeddings.shape[0] == len(self.memories)
                    and len(embeddings.shape) == 2
                    and embeddings.shape[1] > 0
                ):
                    self._embeddings = embeddings.astype(np.float32, copy=False)
                    self._index_state = "warm"
                    self._last_backup = meta.get("last_backup")
                    return
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        if self._memory_files():
            self.create_backup("index-migration")
        self._rebuild_index()
        self._index_state = "rebuilt"

    def _rebuild_index(self):
        if not self.memories:
            self._embeddings = np.empty((0, 0), dtype=np.float32)
        else:
            embeddings = embed([item["text"] for item in self.memories])
            self._embeddings = _ensure_2d(np.asarray(embeddings, dtype=np.float32))
            if self._embeddings.shape[0] != len(self.memories):
                self._embeddings = np.empty((0, 0), dtype=np.float32)
        self._persist_index()

    def _persist_index(self):
        payload = self._index_payload()
        self._atomic_write_json(self._index_meta_path(), payload)
        if self._embeddings is None:
            self._embeddings = np.empty((0, 0), dtype=np.float32)
        self._atomic_write_npy(self._index_embeddings_path(), self._embeddings)

    def _save_dates(self, dirty_dates: set[str]):
        if not dirty_dates:
            return

        grouped = {}
        for item in self.memories:
            date_str = item.get("_date", date.today().isoformat())
            grouped.setdefault(date_str, []).append(
                {
                    "text": item["text"],
                    "importance": item["importance"],
                    "ts": item["ts"],
                }
            )

        for date_str in dirty_dates:
            try:
                date.fromisoformat(str(date_str))
            except ValueError:
                continue
            path = MEMORY_DIR / f"memory_{date_str}.json"
            payload = grouped.get(date_str, [])
            if payload:
                self._atomic_write_json(path, payload)
            elif path.exists():
                path.unlink()

    def _archive_entries(self, entries: list[dict], reason: str):
        if not entries:
            return
        archive_path = self._archive_path()
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as handle:
            for item in entries:
                payload = {
                    "archived_at": datetime.now().isoformat(timespec="seconds"),
                    "reason": reason,
                    "entry": {
                        "text": item["text"],
                        "importance": item.get("importance", 0.5),
                        "ts": item.get("ts", "00:00:00"),
                        "_date": item.get("_date", date.today().isoformat()),
                    },
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _remove_indices(self, indices: list[int], reason: str | None = None) -> set[str]:
        if not indices:
            return set()

        valid_indices = sorted({idx for idx in indices if 0 <= idx < len(self.memories)}, reverse=True)
        if not valid_indices:
            return set()

        removed = []
        dirty_dates = set()
        for idx in valid_indices:
            item = self.memories.pop(idx)
            removed.append(item)
            dirty_dates.add(item.get("_date", date.today().isoformat()))

        if reason:
            self._archive_entries(list(reversed(removed)), reason)

        if self._embeddings is not None and getattr(self._embeddings, "shape", (0,))[0] == len(self.memories) + len(removed):
            self._embeddings = np.delete(self._embeddings, sorted(valid_indices), axis=0)
        else:
            self._embeddings = None

        return dirty_dates

    def _trim_capacity(self) -> set[str]:
        limit = _memory_capacity_limit()
        if limit <= 0 or len(self.memories) <= limit:
            return set()

        indexed = list(enumerate(self.memories))
        indexed.sort(
            key=lambda pair: (
                pair[1].get("importance", 0.5),
                pair[1].get("_date", ""),
                pair[1].get("ts", ""),
            )
        )
        remove_count = len(self.memories) - limit
        indices = [idx for idx, _ in indexed[:remove_count]]
        return self._remove_indices(indices, reason="capacity")

    def _persist_changes(self, dirty_dates: set[str]):
        self._save_dates(dirty_dates)
        if self._embeddings is None or getattr(self._embeddings, "shape", (0,))[0] != len(self.memories):
            self._rebuild_index()
        else:
            self._persist_index()

    def _append_embedding(self, text: str):
        try:
            new_embedding = np.asarray(embed(text), dtype=np.float32)
        except Exception:
            self._embeddings = None
            return
        new_embedding = _ensure_2d(new_embedding)
        if self._embeddings is None or self._embeddings.size == 0:
            self._embeddings = new_embedding
            return
        if self._embeddings.shape[1] != new_embedding.shape[1]:
            self._embeddings = None
            return
        self._embeddings = np.vstack([self._embeddings, new_embedding])

    def _is_duplicate(self, text):
        lower = text.lower()
        words = set(lower.split())
        for memory in self.memories:
            existing = memory["text"].lower()
            if existing == lower:
                return True
            if len(text) > 25 and len(existing) > 25:
                if lower in existing or existing in lower:
                    return True
            existing_words = set(existing.split())
            overlap = words & existing_words
            if len(overlap) >= min(len(words), len(existing_words)) * 0.85:
                return True
        return False

    def _is_exact_duplicate(self, text):
        lower = text.lower()
        for memory in self.memories:
            if memory["text"].lower() == lower:
                return True
        return False

    def _remove_contradictions(self, text) -> set[str]:
        category = _contradiction_category(text)
        if not category:
            return set()
        keywords = _CONTRADICTION_CATEGORIES[category]
        to_remove = []
        for idx, memory in enumerate(self.memories):
            lower_existing = memory["text"].lower()
            if any(keyword in lower_existing for keyword in keywords):
                to_remove.append(idx)
        return self._remove_indices(to_remove, reason="contradiction")

    def commit(self, text: str, importance: float = 0.5):
        if _is_vague(text):
            return False
        text = _normalize_fact(text)
        if self._is_exact_duplicate(text):
            return False

        dirty_dates = self._remove_contradictions(text)
        if self._is_duplicate(text):
            if dirty_dates:
                self._persist_changes(dirty_dates)
            return False

        item = {
            "text": text,
            "importance": _normalize_importance(importance),
            "ts": datetime.now().strftime("%H:%M:%S"),
            "_date": date.today().isoformat(),
        }
        self.memories.append(item)
        self.memories.sort(key=_timestamp_key)
        dirty_dates.add(item["_date"])
        self._append_embedding(item["text"])
        dirty_dates.update(self._trim_capacity())
        self._persist_changes(dirty_dates)
        return True

    def remember(self, text: str, importance: float = 0.8) -> dict:
        normalized = _normalize_fact(str(text or "").strip())
        if not normalized:
            return {"status": "blocked", "reason": "empty", "stored": False, "text": ""}
        stored = self.commit(normalized, importance=importance)
        return {
            "status": "stored" if stored else "unchanged",
            "stored": stored,
            "text": normalized,
            "entry_count": len(self.memories),
        }

    def list_memories(self, limit: int = 25) -> list[dict]:
        try:
            safe_limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            safe_limit = 25
        items = self.memories[-safe_limit:]
        return [
            {
                "index": idx + 1,
                "text": item["text"],
                "importance": item.get("importance", 0.5),
                "date": item.get("_date", ""),
                "time": item.get("ts", ""),
            }
            for idx, item in enumerate(items)
        ]

    def forget(self, query: str, reason: str = "user_forget") -> dict:
        query = str(query or "").strip()
        if not query:
            return {"status": "blocked", "reason": "empty_query", "removed": []}

        lower_query = query.lower()
        exact_indices = []
        for idx, item in enumerate(self.memories):
            lower_text = item["text"].lower()
            if lower_query == lower_text or lower_query in lower_text:
                exact_indices.append(idx)

        if exact_indices:
            removed = [self.memories[idx]["text"] for idx in exact_indices]
            dirty_dates = self._remove_indices(exact_indices, reason=reason)
            self._persist_changes(dirty_dates)
            return {"status": "removed", "reason": "text_match", "removed": removed}

        if not self.memories:
            return {"status": "not_found", "reason": "empty_memory", "removed": []}

        try:
            q_emb = np.asarray(embed(query), dtype=np.float32)
            if q_emb.ndim != 1:
                q_emb = q_emb.reshape(-1)
            if self._embeddings is None or getattr(self._embeddings, "shape", (0,))[0] != len(self.memories):
                self._rebuild_index()
            mem_embs = self._embeddings
            if mem_embs.size == 0 or len(mem_embs.shape) != 2 or mem_embs.shape[1] != q_emb.shape[0]:
                return {"status": "not_found", "reason": "index_unavailable", "removed": []}
            sims = np.dot(mem_embs, q_emb)
            ranked = np.argsort(sims)[::-1]
            best_idx = int(ranked[0])
            best_score = float(sims[best_idx])
            second_score = float(sims[int(ranked[1])]) if len(ranked) > 1 else 0.0
        except Exception:
            return {"status": "not_found", "reason": "semantic_lookup_failed", "removed": []}

        if best_score < settings.memory_similarity_threshold:
            return {"status": "not_found", "reason": "below_threshold", "removed": []}
        if len(self.memories) > 1 and best_score < second_score + settings.memory_winner_margin:
            candidates = [
                self.memories[int(idx)]["text"]
                for idx in ranked[:3]
                if float(sims[int(idx)]) >= settings.memory_similarity_threshold
            ]
            return {"status": "ambiguous", "reason": "multiple_matches", "removed": [], "candidates": candidates}

        removed = [self.memories[best_idx]["text"]]
        dirty_dates = self._remove_indices([best_idx], reason=reason)
        self._persist_changes(dirty_dates)
        return {"status": "removed", "reason": "semantic_match", "removed": removed}

    def system_assessment(self) -> dict:
        texts = [item["text"].strip().lower() for item in self.memories]
        duplicate_count = len(texts) - len(set(texts))
        indexed_count = int(getattr(self._embeddings, "shape", (0,))[0]) if self._embeddings is not None else 0
        return {
            "schema_version": _INDEX_SCHEMA_VERSION,
            "entry_count": len(self.memories),
            "daily_file_count": len(self._memory_files()),
            "duplicate_count": duplicate_count,
            "index_state": self._index_state,
            "index_present": self._index_meta_path().exists() and self._index_embeddings_path().exists(),
            "indexed_count": indexed_count,
            "embedding_dimension": int(getattr(self._embeddings, "shape", (0, 0))[1]) if self._embeddings is not None and len(getattr(self._embeddings, "shape", ())) == 2 else 0,
            "index_coverage_ratio": (indexed_count / len(self.memories)) if self.memories else 1.0,
            "capacity_limit": _memory_capacity_limit(),
            "capacity_remaining": max(0, _memory_capacity_limit() - len(self.memories)),
            "last_backup": self._last_backup,
        }

    def benchmark_recall(self, query: str, runs: int = 25, k: int = 5) -> dict:
        runs = max(1, int(runs))
        q_emb = embed(query)
        started = perf_counter()
        last_result = ""
        for _ in range(runs):
            last_result = self.recall(query, k=k, q_emb=q_emb)
        elapsed = perf_counter() - started
        return {
            "query": query,
            "runs": runs,
            "avg_ms": round((elapsed * 1000.0) / runs, 3),
            "result_count": 0 if not last_result else len(last_result.split(" | ")),
            "indexed_count": int(getattr(self._embeddings, "shape", (0,))[0]) if self._embeddings is not None else 0,
        }

    def recall(self, query: str, k: int = 5, q_emb=None) -> str:
        if not self.memories:
            return ""

        if q_emb is None:
            q_emb = embed(query)
        q_emb = np.asarray(q_emb, dtype=np.float32)
        if q_emb.ndim != 1:
            q_emb = q_emb.reshape(-1)

        if self._embeddings is None or getattr(self._embeddings, "shape", (0,))[0] != len(self.memories):
            self._rebuild_index()

        mem_embs = self._embeddings
        if mem_embs.size == 0:
            return ""
        if len(mem_embs.shape) != 2:
            self._rebuild_index()
            mem_embs = self._embeddings
        if mem_embs.size == 0 or len(mem_embs.shape) != 2:
            return ""
        if mem_embs.shape[1] != q_emb.shape[0]:
            self._rebuild_index()
            mem_embs = self._embeddings
            if mem_embs.size == 0 or len(mem_embs.shape) != 2 or mem_embs.shape[1] != q_emb.shape[0]:
                return ""

        sims = np.dot(mem_embs, q_emb)
        weighted = sims * np.array([memory.get("importance", 0.5) for memory in self.memories], dtype=np.float32)

        top_idx = np.argsort(weighted)[-k:][::-1]
        top_idx = [idx for idx in top_idx if sims[idx] >= settings.memory_similarity_threshold]

        if not top_idx:
            return ""

        unique_idx = []
        seen = set()
        for idx in top_idx:
            key = self.memories[idx]["text"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            unique_idx.append(idx)
        top_idx = unique_idx

        if len(top_idx) > 1:
            best = top_idx[0]
            second = top_idx[1]
            if sims[best] >= sims[second] + settings.memory_winner_margin:
                top_idx = [best]

        return " | ".join(self.memories[idx]["text"] for idx in top_idx)
