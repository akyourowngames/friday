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
MEMORY_GRAPH_RELATIONS_PATH = Path(settings.memory_graph_relations_file)
_INDEX_SCHEMA_VERSION = 2
_GRAPH_SCHEMA_VERSION = 1

_VAGUE_PATTERNS = [
    "medical records",
    "doctor's offices",
    "urgent care",
    "area with available",
    "listened to",
    "playlist contains",
]


def _resolve_project_path(path: Path) -> Path:
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _load_policy_reject_phrases() -> list[str]:
    path = _resolve_project_path(MEMORY_FILTER_POLICY_PATH)
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


def _load_graph_relation_rules(path: Path | None = None) -> list[dict]:
    rule_path = _resolve_project_path(path or MEMORY_GRAPH_RELATIONS_PATH)
    if not rule_path.exists():
        return []

    rules = []
    in_rules = False
    for raw_line in rule_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            in_rules = line.lower() == "## rules"
            continue
        if not in_rules or not line.startswith("- ") or "=>" not in line:
            continue
        left, right = line[2:].split("=>", 1)
        output = [part.strip() for part in right.strip().split("|")]
        if len(output) < 5:
            continue
        rules.append(
            {
                "pattern": left.strip(),
                "source": output[0],
                "relation": output[1],
                "target": output[2],
                "tier": output[3],
                "mode": output[4],
            }
        )
    return rules

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
    if _looks_like_internal_score_artifact(text):
        return True
    for pat in _VAGUE_PATTERNS + _load_policy_reject_phrases():
        if pat in lower:
            return True
    return False


def _looks_like_internal_score_artifact(text: str) -> bool:
    lower = str(text or "").casefold()
    if "confidence" not in lower:
        return False
    has_digit = any(char.isdigit() for char in lower)
    has_score_word = "level" in lower or "score" in lower or "statement" in lower
    return has_digit or has_score_word


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


def _clean_graph_value(value: str) -> str:
    cleaned = str(value or "").strip()
    while cleaned and cleaned[-1] in ".,;:!?":
        cleaned = cleaned[:-1].strip()
    return cleaned


def _node_id(name: str) -> str:
    cleaned = _clean_graph_value(name)
    lowered = cleaned.casefold()
    if lowered == "user":
        return "user"

    parts = []
    last_was_sep = False
    for char in lowered:
        if char.isalnum():
            parts.append(char)
            last_was_sep = False
        elif not last_was_sep:
            parts.append("_")
            last_was_sep = True
    node = "".join(parts).strip("_")
    return node or hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:12]


def _render_template(template: str, values: dict) -> str:
    output = template
    for key, value in values.items():
        output = output.replace("{" + key + "}", value)
    return _clean_graph_value(output)


def _match_graph_pattern(pattern: str, text: str) -> dict | None:
    pattern = pattern.strip()
    text = text.strip()
    if not pattern or not text:
        return None

    tokens = []
    cursor = 0
    while cursor < len(pattern):
        start = pattern.find("{", cursor)
        if start < 0:
            tokens.append(("literal", pattern[cursor:]))
            break
        end = pattern.find("}", start + 1)
        if end < 0:
            return None
        if start > cursor:
            tokens.append(("literal", pattern[cursor:start]))
        tokens.append(("placeholder", pattern[start + 1:end].strip()))
        cursor = end + 1

    lowered_text = text.casefold()
    values = {}
    pos = 0
    idx = 0
    while idx < len(tokens):
        kind, value = tokens[idx]
        if kind == "literal":
            literal = value
            if not literal:
                idx += 1
                continue
            found = lowered_text.find(literal.casefold(), pos)
            if found < 0:
                return None
            if idx == 0 and found != 0:
                return None
            pos = found + len(literal)
            idx += 1
            continue

        placeholder = value
        next_literal = ""
        for next_kind, next_value in tokens[idx + 1:]:
            if next_kind == "literal" and next_value:
                next_literal = next_value
                break
        if next_literal:
            next_found = lowered_text.find(next_literal.casefold(), pos)
            if next_found < 0:
                return None
            captured = text[pos:next_found]
            pos = next_found
        else:
            captured = text[pos:]
            pos = len(text)
        captured = _clean_graph_value(captured)
        if not captured:
            return None
        values[placeholder] = captured
        idx += 1

    if pos < len(text) and tokens and tokens[-1][0] == "literal":
        remainder = text[pos:].strip()
        if remainder:
            return None
    return values


def _node_type(name: str, relation: str = "") -> str:
    node = _node_id(name)
    if node == "user":
        return "person"
    if relation in {"building", "working_on"}:
        return "project"
    return "concept"


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


def _clamp01(value) -> float:
    return min(max(_safe_float(value, 0.0), 0.0), 1.0)


def _term_set(text: str, min_length: int = 2) -> set[str]:
    terms = set()

    def add_token(token: str):
        if len(token) < min_length:
            return
        terms.add(token)
        if len(token) > min_length + 1 and token.endswith("s"):
            terms.add(token[:-1])

    current = []
    for char in str(text or "").casefold():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            add_token(token)
            current = []
    if current:
        token = "".join(current)
        add_token(token)
    return terms


def _term_overlap(query_terms: set[str], text: str) -> tuple[int, float]:
    if not query_terms:
        return 0, 0.0
    text_terms = _term_set(text)
    if not text_terms:
        return 0, 0.0
    overlap_count = len(query_terms & text_terms)
    return overlap_count, overlap_count / max(len(query_terms), 1)


def _configured_relation_set(value: str) -> set[str]:
    relations = set()
    for item in str(value or "").split(","):
        relation = item.strip().casefold()
        if relation:
            relations.add(relation)
    return relations


def _weighted_confidence(values: list[tuple[float, float]]) -> float:
    weighted_total = 0.0
    total_weight = 0.0
    for value, weight in values:
        safe_weight = max(0.0, _safe_float(weight, 0.0))
        if safe_weight <= 0:
            continue
        weighted_total += _clamp01(value) * safe_weight
        total_weight += safe_weight
    if total_weight <= 0:
        return 0.0
    return round(weighted_total / total_weight, 3)


class Brain:
    def __init__(self):
        self.memories = []
        self._embeddings = None
        self._index_state = "cold"
        self._last_backup = None
        self._graph = self._empty_graph()
        self._graph_rules = _load_graph_relation_rules()
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        self._load_graph()
        self._load_all()
        self._backfill_graph_from_memories()
        self._load_or_build_index()

    def _index_meta_path(self) -> Path:
        return MEMORY_DIR / settings.memory_index_file

    def _index_embeddings_path(self) -> Path:
        return MEMORY_DIR / settings.memory_embeddings_file

    def _archive_path(self) -> Path:
        return MEMORY_DIR / settings.memory_archive_file

    def _graph_path(self) -> Path:
        return MEMORY_DIR / settings.memory_graph_file

    def _empty_graph(self) -> dict:
        return {
            "schema_version": _GRAPH_SCHEMA_VERSION,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "nodes": {},
            "edges": [],
            "reflections": [],
            "procedures": [],
        }

    def _load_graph(self):
        path = self._graph_path()
        if not path.exists():
            self._graph = self._empty_graph()
            self._ensure_graph_node("User", "person", 0.8)
            self._persist_graph()
            return
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            graph = self._empty_graph()
        if not isinstance(graph, dict):
            graph = self._empty_graph()
        graph.setdefault("schema_version", _GRAPH_SCHEMA_VERSION)
        graph.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
        graph.setdefault("nodes", {})
        graph.setdefault("edges", [])
        graph.setdefault("reflections", [])
        graph.setdefault("procedures", [])
        self._graph = graph
        self._ensure_graph_node("User", "person", 0.8)

    def _persist_graph(self):
        self._graph["generated_at"] = datetime.now().isoformat(timespec="seconds")
        self._atomic_write_json(self._graph_path(), self._graph)

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
        normalized = {
            "text": text,
            "importance": _normalize_importance(item.get("importance")),
            "ts": str(item.get("ts") or "00:00:00"),
            "_date": date_str,
        }
        normalized["id"] = str(item.get("id") or _memory_id(normalized))
        normalized["tier"] = str(item.get("tier") or "semantic")
        return normalized

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

    def _backfill_graph_from_memories(self):
        active_memory_ids = {item.get("id") or _memory_id(item) for item in self.memories}
        expected_edge_ids = set()
        for item in self.memories:
            text = item.get("text", "")
            for rule in self._graph_rules:
                edge_info = self._rule_to_edge(rule, text)
                if not edge_info:
                    continue
                source_id = _node_id(edge_info["source_name"])
                target_id = _node_id(edge_info["target_name"])
                expected_edge_ids.add(self._edge_id(source_id, edge_info["relation"], target_id))
                break

        existing_edges = self._graph.setdefault("edges", [])
        kept_edges = [
            edge
            for edge in existing_edges
            if edge.get("memory_id") not in active_memory_ids or edge.get("id") in expected_edge_ids
        ]
        if len(kept_edges) != len(existing_edges):
            self._graph["edges"] = kept_edges
            self._persist_graph()

        for item in self.memories:
            self._ingest_graph_memory(item)

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

        for extra in (self._index_meta_path(), self._index_embeddings_path(), self._archive_path(), self._graph_path()):
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
                    "id": item.get("id") or _memory_id(item),
                    "tier": item.get("tier", "semantic"),
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
                        "id": item.get("id") or _memory_id(item),
                        "tier": item.get("tier", "semantic"),
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
            self._deactivate_memory_edges(item.get("id") or _memory_id(item), reason or "removed")
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

    def _append_embedding(self, text: str, insert_idx: int | None = None):
        try:
            new_embedding = np.asarray(embed(text), dtype=np.float32)
        except Exception:
            self._embeddings = None
            return
        new_embedding = _ensure_2d(new_embedding)
        if self._embeddings is None or self._embeddings.size == 0:
            self._embeddings = new_embedding
            return
        expected_existing = len(self.memories) - 1
        if self._embeddings.shape[0] != expected_existing:
            self._embeddings = None
            return
        if self._embeddings.shape[1] != new_embedding.shape[1]:
            self._embeddings = None
            return
        if insert_idx is None or insert_idx >= self._embeddings.shape[0]:
            self._embeddings = np.vstack([self._embeddings, new_embedding])
            return
        self._embeddings = np.insert(self._embeddings, insert_idx, new_embedding[0], axis=0)

    def _ensure_graph_node(self, name: str, node_type: str = "concept", importance: float = 0.5) -> str:
        name = _clean_graph_value(name)
        node_id = _node_id(name)
        now = datetime.now().isoformat(timespec="seconds")
        nodes = self._graph.setdefault("nodes", {})
        existing = nodes.get(node_id)
        if existing:
            existing["updated_at"] = now
            existing["importance"] = max(_safe_float(existing.get("importance"), 0.5), _normalize_importance(importance))
            if name and name not in existing.get("aliases", []) and existing.get("name", "") != name:
                existing.setdefault("aliases", []).append(name)
            return node_id

        nodes[node_id] = {
            "id": node_id,
            "name": name or node_id,
            "type": node_type,
            "importance": _normalize_importance(importance),
            "created_at": now,
            "updated_at": now,
            "aliases": [],
        }
        return node_id

    def _edge_id(self, source_id: str, relation: str, target_id: str) -> str:
        payload = f"{source_id}|{relation}|{target_id}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _rule_to_edge(self, rule: dict, text: str) -> dict | None:
        values = _match_graph_pattern(rule["pattern"], text)
        if values is None:
            return None
        source_name = _render_template(rule["source"], values)
        target_name = _render_template(rule["target"], values)
        relation = _clean_graph_value(rule["relation"]).casefold()
        if not source_name or not target_name or not relation:
            return None
        return {
            "source_name": source_name,
            "target_name": target_name,
            "relation": relation,
            "tier": rule.get("tier", "semantic"),
            "mode": rule.get("mode", "multi"),
        }

    def _deactivate_memory_edges(self, memory_id: str, reason: str):
        changed = False
        now = datetime.now().isoformat(timespec="seconds")
        for edge in self._graph.setdefault("edges", []):
            if edge.get("memory_id") != memory_id or not edge.get("active", True):
                continue
            edge["active"] = False
            edge["valid_to"] = now
            edge["inactive_reason"] = reason
            changed = True
        if changed:
            self._persist_graph()

    def _ingest_graph_memory(self, item: dict) -> bool:
        text = item.get("text", "")
        importance = item.get("importance", 0.5)
        memory_id = item.get("id") or _memory_id(item)
        now = datetime.now().isoformat(timespec="seconds")
        changed = False

        for rule in self._graph_rules:
            edge_info = self._rule_to_edge(rule, text)
            if not edge_info:
                continue
            source_id = self._ensure_graph_node(
                edge_info["source_name"],
                _node_type(edge_info["source_name"]),
                importance,
            )
            target_id = self._ensure_graph_node(
                edge_info["target_name"],
                _node_type(edge_info["target_name"], edge_info["relation"]),
                importance,
            )
            relation = edge_info["relation"]
            mode = edge_info["mode"]

            if mode == "temporal":
                superseded = []
                for edge in self._graph.setdefault("edges", []):
                    if (
                        edge.get("source") == source_id
                        and edge.get("relation") == relation
                        and edge.get("active", True)
                        and edge.get("target") != target_id
                    ):
                        edge["active"] = False
                        edge["valid_to"] = now
                        edge["inactive_reason"] = "superseded"
                        superseded.append(edge.get("id"))
                        changed = True
            else:
                superseded = []

            edge_id = self._edge_id(source_id, relation, target_id)
            existing = None
            for edge in self._graph.setdefault("edges", []):
                if edge.get("id") == edge_id:
                    existing = edge
                    break

            if existing:
                existing_changed = False
                target_strength = max(_safe_float(existing.get("strength"), 0.5), _normalize_importance(importance))
                target_confidence = max(_safe_float(existing.get("confidence"), 0.5), 0.75)
                if not existing.get("active", True):
                    existing["active"] = True
                    existing_changed = True
                if existing.get("valid_to") is not None:
                    existing["valid_to"] = None
                    existing_changed = True
                if existing.get("memory_id") != memory_id:
                    existing["memory_id"] = memory_id
                    existing_changed = True
                if _safe_float(existing.get("strength"), 0.5) != target_strength:
                    existing["strength"] = target_strength
                    existing_changed = True
                if _safe_float(existing.get("confidence"), 0.5) != target_confidence:
                    existing["confidence"] = target_confidence
                    existing_changed = True
                if superseded:
                    existing.setdefault("supersedes", [])
                    for edge_id_value in superseded:
                        if edge_id_value and edge_id_value not in existing["supersedes"]:
                            existing["supersedes"].append(edge_id_value)
                            existing_changed = True
                if existing_changed:
                    existing["updated_at"] = now
                    changed = True
                break

            self._graph.setdefault("edges", []).append(
                {
                    "id": edge_id,
                    "source": source_id,
                    "target": target_id,
                    "relation": relation,
                    "strength": _normalize_importance(importance),
                    "confidence": 0.75,
                    "memory_id": memory_id,
                    "created_at": now,
                    "updated_at": now,
                    "valid_from": now,
                    "valid_to": None,
                    "active": True,
                    "tier": edge_info["tier"],
                    "mode": mode,
                    "evidence": text,
                    "supersedes": [edge_id_value for edge_id_value in superseded if edge_id_value],
                }
            )
            changed = True
            break

        if changed:
            self._persist_graph()
        return changed

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

    def _temporal_edge_info(self, text: str) -> dict | None:
        for rule in self._graph_rules:
            if rule.get("mode") != "temporal":
                continue
            edge_info = self._rule_to_edge(rule, text)
            if edge_info:
                return edge_info
        return None

    def _graph_contradiction_indices(self, text: str) -> list[int] | None:
        new_edge = self._temporal_edge_info(text)
        if not new_edge:
            return None

        source_id = _node_id(new_edge["source_name"])
        target_id = _node_id(new_edge["target_name"])
        relation = new_edge["relation"]
        to_remove = []

        for idx, memory in enumerate(self.memories):
            existing_edge = self._temporal_edge_info(memory["text"])
            if not existing_edge:
                continue
            if _node_id(existing_edge["source_name"]) != source_id:
                continue
            if existing_edge["relation"] != relation:
                continue
            if _node_id(existing_edge["target_name"]) == target_id:
                continue
            to_remove.append(idx)

        return to_remove

    def _remove_contradictions(self, text) -> set[str]:
        graph_indices = self._graph_contradiction_indices(text)
        if graph_indices is not None:
            return self._remove_indices(graph_indices, reason="contradiction")

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
            "tier": "semantic",
        }
        item["id"] = _memory_id(item)
        self.memories.append(item)
        self.memories.sort(key=_timestamp_key)
        insert_idx = 0
        for idx, memory in enumerate(self.memories):
            if memory.get("id") == item["id"]:
                insert_idx = idx
                break
        dirty_dates.add(item["_date"])
        self._ingest_graph_memory(item)
        self._append_embedding(item["text"], insert_idx=insert_idx)
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
                "id": item.get("id") or _memory_id(item),
                "index": idx + 1,
                "text": item["text"],
                "importance": item.get("importance", 0.5),
                "tier": item.get("tier", "semantic"),
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

    def _edge_to_text(self, edge: dict) -> str:
        nodes = self._graph.get("nodes", {})
        source = nodes.get(edge.get("source"), {}).get("name", edge.get("source", ""))
        target = nodes.get(edge.get("target"), {}).get("name", edge.get("target", ""))
        relation = str(edge.get("relation", "")).replace("_", " ")
        return f"{source} {relation} {target}".strip()

    def _graph_edge_rank(self, edge: dict, query_terms: set[str]) -> dict | None:
        text = self._edge_to_text(edge)
        overlap_count, overlap_ratio = _term_overlap(query_terms, text)
        if query_terms and overlap_count == 0:
            return None
        strength = _clamp01(edge.get("strength", 0.5))
        confidence = _clamp01(edge.get("confidence", 0.5))
        score = overlap_count + strength + confidence
        return {
            "text": text,
            "score": round(score, 3),
            "confidence": _weighted_confidence(
                [
                    (confidence, settings.memory_rank_semantic_weight),
                    (strength, settings.memory_rank_importance_weight),
                    (overlap_ratio, settings.memory_rank_overlap_weight),
                ]
            ),
            "relation": edge.get("relation", ""),
            "source": edge.get("source", ""),
            "target": edge.get("target", ""),
            "updated_at": edge.get("updated_at", ""),
            "inferred": False,
            "evidence": edge.get("evidence", ""),
        }

    def _graph_inference_ranks(self, active_edges: list[dict], query_terms: set[str]) -> list[dict]:
        bridge_relations = _configured_relation_set(settings.memory_inference_bridge_relations)
        if not bridge_relations:
            return []

        by_source = {}
        for edge in active_edges:
            by_source.setdefault(edge.get("source", ""), []).append(edge)

        ranks = []
        factor = _clamp01(settings.memory_inference_confidence_factor)
        for bridge in active_edges:
            if str(bridge.get("relation", "")).casefold() not in bridge_relations:
                continue
            bridge_text = self._edge_to_text(bridge)
            for detail in by_source.get(bridge.get("target", ""), []):
                if detail.get("id") == bridge.get("id"):
                    continue
                detail_text = self._edge_to_text(detail)
                text = f"{bridge_text} -> {detail_text}"
                overlap_count, overlap_ratio = _term_overlap(query_terms, text)
                if query_terms and overlap_count == 0:
                    continue
                strength = min(_clamp01(bridge.get("strength", 0.5)), _clamp01(detail.get("strength", 0.5)))
                confidence = min(_clamp01(bridge.get("confidence", 0.5)), _clamp01(detail.get("confidence", 0.5)))
                confidence = _clamp01(confidence * factor)
                ranks.append(
                    {
                        "text": text,
                        "score": round(overlap_count + strength + confidence, 3),
                        "confidence": _weighted_confidence(
                            [
                                (confidence, settings.memory_rank_semantic_weight),
                                (strength, settings.memory_rank_importance_weight),
                                (overlap_ratio, settings.memory_rank_overlap_weight),
                            ]
                        ),
                        "relation": detail.get("relation", ""),
                        "source": bridge.get("source", ""),
                        "target": detail.get("target", ""),
                        "updated_at": max(str(bridge.get("updated_at", "")), str(detail.get("updated_at", ""))),
                        "inferred": True,
                        "evidence": " | ".join(part for part in (bridge.get("evidence", ""), detail.get("evidence", "")) if part),
                    }
                )
        return ranks

    def graph_ranked(self, query: str = "", limit: int = 8) -> list[dict]:
        try:
            safe_limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            safe_limit = 8
        query_terms = _term_set(query, min_length=2)
        active_edges = [edge for edge in self._graph.get("edges", []) if edge.get("active", True)]
        ranked = []
        for edge in active_edges:
            edge_rank = self._graph_edge_rank(edge, query_terms)
            if edge_rank:
                ranked.append(edge_rank)
        ranked.extend(self._graph_inference_ranks(active_edges, query_terms))
        if not ranked:
            return []
        ranked.sort(key=lambda item: (item["score"], item["confidence"], item.get("updated_at", "")), reverse=True)
        seen = set()
        results = []
        for item in ranked:
            key = item["text"].casefold()
            if key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= safe_limit:
                break
        return results

    def graph_summary(self, query: str = "", limit: int = 8) -> str:
        ranked = self.graph_ranked(query, limit=limit)
        return " | ".join(item["text"] for item in ranked)

    def profile_context(self, limit: int = 8) -> str:
        try:
            safe_limit = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            safe_limit = 8

        inactive_evidence = {
            str(edge.get("evidence", "")).strip().casefold()
            for edge in self._graph.get("edges", [])
            if not edge.get("active", True)
        }

        text_items = []
        seen = set()
        memories = list(self.memories)
        memories.sort(
            key=lambda memory: (
                _normalize_importance(memory.get("importance", 0.5)),
                memory.get("_date", ""),
                memory.get("ts", ""),
            ),
            reverse=True,
        )
        for memory in memories:
            text = memory["text"].strip()
            key = text.casefold()
            if key in inactive_evidence or key in seen:
                continue
            seen.add(key)
            text_items.append(text)
            if len(text_items) >= safe_limit:
                break

        graph_items = [item["text"] for item in self.graph_ranked("", limit=safe_limit)]
        parts = []
        if text_items:
            parts.append(f"Text memory: {' | '.join(text_items)}")
        if graph_items:
            parts.append(f"Graph memory: {' | '.join(graph_items)}")
        return "\n".join(parts)

    def recall_context(self, query: str, k: int = 5, q_emb=None) -> str:
        text_ranked = self.recall_ranked(query, k=k, q_emb=q_emb)
        if len(text_ranked) > 1:
            best = text_ranked[0]
            second = text_ranked[1]
            if best["similarity"] >= second["similarity"] + settings.memory_winner_margin:
                text_ranked = [best]
        if text_ranked:
            inactive_evidence = {
                str(edge.get("evidence", "")).strip().casefold()
                for edge in self._graph.get("edges", [])
                if not edge.get("active", True)
            }
            kept = []
            for item in text_ranked:
                if item["text"].strip().casefold() in inactive_evidence:
                    continue
                kept.append(item)
            text_ranked = kept
        text_context = " | ".join(
            item["text"]
            for item in text_ranked
        )
        graph_context = self.graph_summary(query, limit=max(3, k))
        parts = []
        if text_context:
            parts.append(f"Text memory: {text_context}")
        if graph_context:
            parts.append(f"Graph memory: {graph_context}")
        return "\n".join(parts)

    def reflect(self, label: str = "session", limit: int = 20) -> dict:
        active_edges = [edge for edge in self._graph.get("edges", []) if edge.get("active", True)]
        active_edges.sort(
            key=lambda edge: (
                _safe_float(edge.get("strength"), 0.5),
                edge.get("updated_at", ""),
            ),
            reverse=True,
        )
        top_edges = active_edges[: max(1, min(int(limit), 50))]
        relation_counts = {}
        entity_counts = {}
        for edge in top_edges:
            relation = edge.get("relation", "")
            relation_counts[relation] = relation_counts.get(relation, 0) + 1
            for key in ("source", "target"):
                entity = edge.get(key, "")
                entity_counts[entity] = entity_counts.get(entity, 0) + 1
        top_relations = sorted(relation_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        top_entities = sorted(entity_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        nodes = self._graph.get("nodes", {})
        insight_parts = []
        if top_relations:
            insight_parts.append("active relations: " + ", ".join(name for name, _count in top_relations if name))
        if top_entities:
            names = [nodes.get(entity_id, {}).get("name", entity_id) for entity_id, _count in top_entities]
            insight_parts.append("central entities: " + ", ".join(name for name in names if name))
        summary = "; ".join(part for part in insight_parts if part) or "No strong memory graph signals yet."
        reflection = {
            "label": str(label or "session"),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "edge_count": len(active_edges),
            "summary": summary,
        }
        self._graph.setdefault("reflections", []).append(reflection)
        self._persist_graph()
        return reflection

    def graph_assessment(self) -> dict:
        edges = self._graph.get("edges", [])
        active_edges = [edge for edge in edges if edge.get("active", True)]
        return {
            "schema_version": self._graph.get("schema_version", _GRAPH_SCHEMA_VERSION),
            "node_count": len(self._graph.get("nodes", {})),
            "edge_count": len(edges),
            "active_edge_count": len(active_edges),
            "reflection_count": len(self._graph.get("reflections", [])),
            "relation_rule_count": len(self._graph_rules),
            "inference_bridge_relations": sorted(_configured_relation_set(settings.memory_inference_bridge_relations)),
            "graph_file": str(self._graph_path()),
        }

    def system_assessment(self) -> dict:
        texts = [item["text"].strip().lower() for item in self.memories]
        duplicate_count = len(texts) - len(set(texts))
        indexed_count = int(getattr(self._embeddings, "shape", (0,))[0]) if self._embeddings is not None else 0
        graph = self.graph_assessment()
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
            "ranking": {
                "semantic_weight": settings.memory_rank_semantic_weight,
                "importance_weight": settings.memory_rank_importance_weight,
                "overlap_weight": settings.memory_rank_overlap_weight,
            },
            "graph": graph,
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

    def _memory_confidence(self, similarity: float, importance: float, overlap: float) -> float:
        return _weighted_confidence(
            [
                (similarity, settings.memory_rank_semantic_weight),
                (importance, settings.memory_rank_importance_weight),
                (overlap, settings.memory_rank_overlap_weight),
            ]
        )

    def recall_ranked(self, query: str, k: int = 5, q_emb=None) -> list[dict]:
        if not self.memories:
            return []

        if q_emb is None:
            q_emb = embed(query)
        q_emb = np.asarray(q_emb, dtype=np.float32)
        if q_emb.ndim != 1:
            q_emb = q_emb.reshape(-1)

        if self._embeddings is None or getattr(self._embeddings, "shape", (0,))[0] != len(self.memories):
            self._rebuild_index()

        mem_embs = self._embeddings
        if mem_embs.size == 0:
            return []
        if len(mem_embs.shape) != 2:
            self._rebuild_index()
            mem_embs = self._embeddings
        if mem_embs.size == 0 or len(mem_embs.shape) != 2:
            return []
        if mem_embs.shape[1] != q_emb.shape[0]:
            self._rebuild_index()
            mem_embs = self._embeddings
            if mem_embs.size == 0 or len(mem_embs.shape) != 2 or mem_embs.shape[1] != q_emb.shape[0]:
                return []

        sims = np.dot(mem_embs, q_emb)
        query_terms = _term_set(query, min_length=2)
        ranked = []
        for idx, memory in enumerate(self.memories):
            similarity = float(sims[idx])
            if similarity < settings.memory_similarity_threshold:
                continue
            importance = _normalize_importance(memory.get("importance", 0.5))
            overlap_count, overlap_ratio = _term_overlap(query_terms, memory["text"])
            confidence = self._memory_confidence(similarity, importance, overlap_ratio)
            score = _weighted_confidence(
                [
                    (similarity, settings.memory_rank_semantic_weight),
                    (importance, settings.memory_rank_importance_weight),
                    (overlap_ratio, settings.memory_rank_overlap_weight),
                ]
            )
            ranked.append(
                {
                    "id": memory.get("id") or _memory_id(memory),
                    "text": memory["text"],
                    "similarity": round(similarity, 4),
                    "importance": importance,
                    "overlap": overlap_count,
                    "confidence": confidence,
                    "score": score,
                    "date": memory.get("_date", ""),
                    "time": memory.get("ts", ""),
                    "tier": memory.get("tier", "semantic"),
                }
            )

        ranked.sort(key=lambda item: (item["score"], item["similarity"], item["importance"], item["date"], item["time"]), reverse=True)

        if not ranked:
            return []

        unique = []
        seen = set()
        try:
            safe_k = max(1, int(k))
        except (TypeError, ValueError):
            safe_k = 5

        for item in ranked:
            key = item["text"].strip().casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) >= safe_k:
                break
        return unique

    def recall(self, query: str, k: int = 5, q_emb=None) -> str:
        ranked = self.recall_ranked(query, k=k, q_emb=q_emb)
        if not ranked:
            return ""

        if len(ranked) > 1:
            best = ranked[0]
            second = ranked[1]
            if best["similarity"] >= second["similarity"] + settings.memory_winner_margin:
                ranked = [best]

        return " | ".join(item["text"] for item in ranked)
