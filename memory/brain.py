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
_INDEX_SCHEMA_VERSION = 3
_INDEX_SCHEMA_COMPAT = (2, 3)
_GRAPH_SCHEMA_VERSION = 2

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


def _load_auto_relation_settings(path: Path | None = None) -> dict:
    rule_path = _resolve_project_path(path or Path(settings.memory_auto_relations_file))
    values = {
        "relation": "associated_with",
        "mode": "multi",
        "tier": "semantic",
        "min_entity_name_length": 2,
    }
    if not rule_path.exists():
        return values
    section = ""
    for raw_line in rule_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if section != "settings" or not line.startswith("- "):
            continue
        cleaned = line[2:].strip()
        if ":" not in cleaned:
            continue
        key, _, raw_value = cleaned.partition(":")
        key = key.strip().lower()
        raw_value = raw_value.strip()
        if not key or not raw_value:
            continue
        if key == "min_entity_name_length":
            try:
                values[key] = int(raw_value)
            except ValueError:
                pass
            continue
        values[key] = raw_value
    return values


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
        self._auto_relation = _load_auto_relation_settings()
        self._query_cache = {}
        self._query_cache_order = []
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
            "memory_links": {},
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
        graph.setdefault("memory_links", {})
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
        normalized["storage"] = str(item.get("storage") or "unified")
        graph_edges = item.get("graph_edges")
        if isinstance(graph_edges, list):
            normalized["graph_edges"] = [str(edge_id) for edge_id in graph_edges if str(edge_id).strip()]
        else:
            normalized["graph_edges"] = []
        graph_nodes = item.get("graph_nodes")
        if isinstance(graph_nodes, list):
            normalized["graph_nodes"] = [str(node_id) for node_id in graph_nodes if str(node_id).strip()]
        else:
            normalized["graph_nodes"] = []
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

        migrate_dates = set()
        for item in self.memories:
            if item.get("graph_edges"):
                self._sync_memory_graph_refs(item)
                continue
            self._ingest_graph_memory(item)
            self._auto_relate_entities(item)
            self._sync_memory_graph_refs(item)
            migrate_dates.add(item.get("_date", date.today().isoformat()))
        if migrate_dates:
            self._persist_changes(migrate_dates)
        elif self._graph.get("memory_links"):
            self._persist_graph()

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
                    meta.get("schema_version") in _INDEX_SCHEMA_COMPAT
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

    def _embed_query(self, query: str):
        key = str(query or "").strip().casefold()
        if not key:
            return embed(query)
        if not hasattr(self, "_query_cache"):
            self._query_cache = {}
        if not hasattr(self, "_query_cache_order"):
            self._query_cache_order = []
        limit = max(0, int(settings.memory_query_cache_size))
        if limit and key in self._query_cache:
            return self._query_cache[key]
        vector = embed(query)
        if limit:
            self._query_cache[key] = vector
            self._query_cache_order.append(key)
            while len(self._query_cache_order) > limit:
                oldest = self._query_cache_order.pop(0)
                self._query_cache.pop(oldest, None)
        return vector

    def _rebuild_index(self):
        if not self.memories:
            self._embeddings = np.empty((0, 0), dtype=np.float32)
        else:
            texts = [item["text"] for item in self.memories]
            batch_size = max(1, int(settings.memory_rebuild_batch_size))
            if len(texts) <= batch_size:
                embeddings = embed(texts)
                self._embeddings = _ensure_2d(np.asarray(embeddings, dtype=np.float32))
            else:
                chunks = []
                for start in range(0, len(texts), batch_size):
                    batch = texts[start : start + batch_size]
                    batch_emb = embed(batch)
                    chunks.append(_ensure_2d(np.asarray(batch_emb, dtype=np.float32)))
                self._embeddings = np.vstack(chunks) if chunks else np.empty((0, 0), dtype=np.float32)
            if self._embeddings.shape[0] != len(self.memories):
                self._embeddings = np.empty((0, 0), dtype=np.float32)
        if not hasattr(self, "_query_cache"):
            self._query_cache = {}
        if not hasattr(self, "_query_cache_order"):
            self._query_cache_order = []
        self._query_cache.clear()
        self._query_cache_order.clear()
        self._index_state = "warm"
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
                    "storage": item.get("storage", "unified"),
                    "graph_edges": list(item.get("graph_edges") or []),
                    "graph_nodes": list(item.get("graph_nodes") or []),
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

    def _ingest_graph_fallback_memory(self, item: dict) -> bool:
        text = _clean_graph_value(item.get("text", ""))
        if not text:
            return False
        importance = item.get("importance", 0.5)
        memory_id = item.get("id") or _memory_id(item)
        now = datetime.now().isoformat(timespec="seconds")
        source_name = _clean_graph_value(settings.memory_graph_fallback_source)
        relation = _clean_graph_value(settings.memory_graph_fallback_relation).casefold()
        tier = _clean_graph_value(settings.memory_graph_fallback_tier) or "semantic"
        if not source_name or not relation:
            return False

        source_id = self._ensure_graph_node(source_name, _node_type(source_name), importance)
        target_id = self._ensure_graph_node(text, "memory", importance)
        edge_id = self._edge_id(source_id, relation, target_id)
        for edge in self._graph.setdefault("edges", []):
            if edge.get("id") != edge_id:
                continue
            changed = False
            if not edge.get("active", True):
                edge["active"] = True
                edge["valid_to"] = None
                changed = True
            if edge.get("memory_id") != memory_id:
                edge["memory_id"] = memory_id
                changed = True
            target_strength = max(_safe_float(edge.get("strength"), 0.5), _normalize_importance(importance))
            if _safe_float(edge.get("strength"), 0.5) != target_strength:
                edge["strength"] = target_strength
                changed = True
            if changed:
                edge["updated_at"] = now
                self._persist_graph()
            return changed

        self._graph.setdefault("edges", []).append(
            {
                "id": edge_id,
                "source": source_id,
                "target": target_id,
                "relation": relation,
                "strength": _normalize_importance(importance),
                "confidence": 0.6,
                "memory_id": memory_id,
                "created_at": now,
                "updated_at": now,
                "valid_from": now,
                "valid_to": None,
                "active": True,
                "tier": tier,
                "mode": "multi",
                "evidence": text,
                "supersedes": [],
            }
        )
        self._persist_graph()
        return True

    def _entities_in_text(self, text: str) -> list[str]:
        text_tokens = _term_set(text, min_length=int(self._auto_relation.get("min_entity_name_length", 2)))
        if not text_tokens:
            return []
        text_lower = str(text or "").casefold()
        matches = []
        seen = set()
        for node in self._graph.get("nodes", {}).values():
            name = _clean_graph_value(node.get("name", ""))
            if not name or name.casefold() == "user":
                continue
            name_tokens = _term_set(name, min_length=int(self._auto_relation.get("min_entity_name_length", 2)))
            if name_tokens and name_tokens <= text_tokens:
                key = name.casefold()
                if key not in seen:
                    seen.add(key)
                    matches.append(name)
                continue
            name_lower = name.casefold()
            if len(name_lower) >= int(self._auto_relation.get("min_entity_name_length", 2)) and name_lower in text_lower:
                key = name.casefold()
                if key not in seen:
                    seen.add(key)
                    matches.append(name)
        matches.sort(key=len, reverse=True)
        return matches

    def _auto_relate_entities(self, item: dict) -> bool:
        if not settings.memory_auto_relations_enabled:
            return False
        text = item.get("text", "")
        memory_id = item.get("id") or _memory_id(item)
        entities = self._entities_in_text(text)
        if len(entities) < 2:
            return False
        relation = _clean_graph_value(self._auto_relation.get("relation", "associated_with")).casefold()
        mode = _clean_graph_value(self._auto_relation.get("mode", "multi"))
        tier = _clean_graph_value(self._auto_relation.get("tier", "semantic")) or "semantic"
        importance = item.get("importance", 0.5)
        now = datetime.now().isoformat(timespec="seconds")
        changed = False
        for left in range(len(entities)):
            for right in range(left + 1, len(entities)):
                source_name = entities[left]
                target_name = entities[right]
                source_id = self._ensure_graph_node(source_name, _node_type(source_name), importance)
                target_id = self._ensure_graph_node(target_name, _node_type(target_name), importance)
                edge_id = self._edge_id(source_id, relation, target_id)
                existing = None
                for edge in self._graph.setdefault("edges", []):
                    if edge.get("id") == edge_id:
                        existing = edge
                        break
                if existing:
                    if not existing.get("active", True):
                        existing["active"] = True
                        existing["valid_to"] = None
                        changed = True
                    if existing.get("memory_id") != memory_id:
                        existing["memory_id"] = memory_id
                        changed = True
                    continue
                self._graph.setdefault("edges", []).append(
                    {
                        "id": edge_id,
                        "source": source_id,
                        "target": target_id,
                        "relation": relation,
                        "strength": _normalize_importance(importance),
                        "confidence": 0.65,
                        "memory_id": memory_id,
                        "created_at": now,
                        "updated_at": now,
                        "valid_from": now,
                        "valid_to": None,
                        "active": True,
                        "tier": tier,
                        "mode": mode,
                        "evidence": text,
                        "supersedes": [],
                        "auto": True,
                    }
                )
                changed = True
        if changed:
            self._persist_graph()
        return changed

    def _sync_memory_graph_refs(self, item: dict, persist_graph: bool = False):
        memory_id = item.get("id") or _memory_id(item)
        edge_ids = []
        node_ids = set()
        for edge in self._graph.get("edges", []):
            if edge.get("memory_id") != memory_id or not edge.get("active", True):
                continue
            edge_id = edge.get("id")
            if edge_id:
                edge_ids.append(edge_id)
            node_ids.add(edge.get("source", ""))
            node_ids.add(edge.get("target", ""))
        node_ids.discard("")
        item["graph_edges"] = edge_ids
        item["graph_nodes"] = sorted(node_ids)
        item["storage"] = "unified"
        item["tier"] = item.get("tier") or ("graph" if edge_ids else "semantic")
        self._graph.setdefault("memory_links", {})[memory_id] = edge_ids
        if persist_graph:
            self._persist_graph()

    def _memory_id_for_graph_hit(self, graph_item: dict) -> str:
        target_text = str(graph_item.get("text", "")).strip().casefold()
        target_evidence = str(graph_item.get("evidence", "")).strip().casefold()
        graph = getattr(self, "_graph", {"edges": []})
        for edge in graph.get("edges", []):
            if not edge.get("active", True):
                continue
            edge_text = self._edge_to_text(edge).casefold()
            edge_evidence = str(edge.get("evidence", "")).strip().casefold()
            if target_text and (edge_text == target_text or edge_evidence == target_text):
                return str(edge.get("memory_id", ""))
            if target_evidence and edge_evidence == target_evidence:
                return str(edge.get("memory_id", ""))
        return ""

    def _memory_by_id(self, memory_id: str) -> dict | None:
        for memory in self.memories:
            if (memory.get("id") or _memory_id(memory)) == memory_id:
                return memory
        return None

    def _expand_graph_neighbors(self, seed_memory_ids: set[str], limit: int) -> list[dict]:
        hops = max(0, int(settings.memory_unified_expansion_hops))
        if hops <= 0 or not seed_memory_ids:
            return []
        seed_nodes = set()
        for memory_id in seed_memory_ids:
            memory = self._memory_by_id(memory_id)
            if memory:
                seed_nodes.update(memory.get("graph_nodes") or [])
        if not seed_nodes:
            return []
        expanded = []
        seen_memory = set(seed_memory_ids)
        graph = getattr(self, "_graph", {"edges": []})
        for edge in graph.get("edges", []):
            if not edge.get("active", True):
                continue
            if edge.get("source") not in seed_nodes and edge.get("target") not in seed_nodes:
                continue
            memory_id = str(edge.get("memory_id", ""))
            if not memory_id or memory_id in seen_memory:
                continue
            memory = self._memory_by_id(memory_id)
            if not memory:
                continue
            seen_memory.add(memory_id)
            expanded.append(
                {
                    "id": memory_id,
                    "text": memory["text"],
                    "score": round(_clamp01(_safe_float(edge.get("strength"), 0.5)) * settings.memory_unified_graph_weight, 4),
                    "confidence": _clamp01(_safe_float(edge.get("confidence"), 0.5)),
                    "sources": ["graph_expand"],
                    "graph_path": self._edge_to_text(edge),
                    "tier": memory.get("tier", "semantic"),
                }
            )
            if len(expanded) >= limit:
                break
        return expanded

    def recall_unified(self, query: str, k: int = 5, q_emb=None) -> list[dict]:
        try:
            safe_k = max(1, min(int(k), 50))
        except (TypeError, ValueError):
            safe_k = 5
        graph_weight = _clamp01(settings.memory_unified_graph_weight)
        merged: dict[str, dict] = {}

        if not str(query or "").strip() and self.memories:
            profile_sorted = sorted(
                self.memories,
                key=lambda memory: (
                    _normalize_importance(memory.get("importance", 0.5)),
                    memory.get("_date", ""),
                    memory.get("ts", ""),
                ),
                reverse=True,
            )
            for memory in profile_sorted[:safe_k]:
                memory_id = memory.get("id") or _memory_id(memory)
                merged[memory_id] = {
                    "id": memory_id,
                    "text": memory["text"],
                    "similarity": 1.0,
                    "importance": _normalize_importance(memory.get("importance", 0.5)),
                    "overlap": 0,
                    "confidence": 0.75,
                    "score": _normalize_importance(memory.get("importance", 0.5)),
                    "unified_score": _normalize_importance(memory.get("importance", 0.5)),
                    "date": memory.get("_date", ""),
                    "time": memory.get("ts", ""),
                    "tier": memory.get("tier", "semantic"),
                    "sources": ["profile"],
                    "graph_path": "",
                }

        for item in self._recall_ranked_semantic(query, k=safe_k * 2, q_emb=q_emb):
            memory_id = item["id"]
            merged[memory_id] = {
                **item,
                "sources": ["text"],
                "graph_path": "",
                "unified_score": item.get("score", 0.0),
            }

        for graph_item in self.graph_ranked(query, limit=safe_k * 2):
            memory_id = self._memory_id_for_graph_hit(graph_item)
            boost = round(float(graph_item.get("score", 0.0)) * graph_weight, 4)
            if memory_id and memory_id in merged:
                merged[memory_id]["unified_score"] = round(merged[memory_id]["unified_score"] + boost, 4)
                merged[memory_id]["sources"] = sorted(set(merged[memory_id]["sources"]) | {"graph"})
                merged[memory_id]["graph_path"] = graph_item.get("text", "")
                continue
            if not memory_id:
                continue
            memory = self._memory_by_id(memory_id)
            if not memory:
                continue
            merged[memory_id] = {
                "id": memory_id,
                "text": memory["text"],
                "similarity": 0.0,
                "importance": _normalize_importance(memory.get("importance", 0.5)),
                "overlap": 0,
                "confidence": graph_item.get("confidence", 0.5),
                "score": boost,
                "unified_score": boost,
                "date": memory.get("_date", ""),
                "time": memory.get("ts", ""),
                "tier": memory.get("tier", "semantic"),
                "sources": ["graph"],
                "graph_path": graph_item.get("text", ""),
            }

        seed_ids = set(merged)
        for item in self._expand_graph_neighbors(seed_ids, limit=safe_k):
            memory_id = item["id"]
            if memory_id in merged:
                merged[memory_id]["unified_score"] = round(merged[memory_id]["unified_score"] + item["score"], 4)
                merged[memory_id]["sources"] = sorted(set(merged[memory_id]["sources"]) | set(item["sources"]))
                if item.get("graph_path"):
                    merged[memory_id]["graph_path"] = item["graph_path"]
                continue
            merged[memory_id] = {
                "id": memory_id,
                "text": item["text"],
                "similarity": 0.0,
                "importance": _normalize_importance(0.5),
                "overlap": 0,
                "confidence": item.get("confidence", 0.5),
                "score": item["score"],
                "unified_score": item["score"],
                "date": "",
                "time": "",
                "tier": item.get("tier", "semantic"),
                "sources": list(item.get("sources") or ["graph_expand"]),
                "graph_path": item.get("graph_path", ""),
            }

        graph = getattr(self, "_graph", {"edges": []})
        inactive_evidence = {
            str(edge.get("evidence", "")).strip().casefold()
            for edge in graph.get("edges", [])
            if not edge.get("active", True)
        }
        ranked = [
            item
            for item in merged.values()
            if item["text"].strip().casefold() not in inactive_evidence
        ]
        ranked.sort(
            key=lambda item: (
                item.get("unified_score", item.get("score", 0.0)),
                item.get("similarity", 0.0),
                item.get("importance", 0.0),
            ),
            reverse=True,
        )
        if len(ranked) > 1:
            best = ranked[0]
            second = ranked[1]
            if float(best.get("similarity", 0.0)) >= float(second.get("similarity", 0.0)) + settings.memory_winner_margin:
                if best.get("unified_score", 0.0) >= second.get("unified_score", 0.0):
                    ranked = [best]
        results = []
        seen_text = set()
        for item in ranked:
            key = item["text"].strip().casefold()
            if key in seen_text:
                continue
            seen_text.add(key)
            results.append(item)
            if len(results) >= safe_k:
                break
        return results

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
        graph_changed = self._ingest_graph_memory(item)
        if not graph_changed:
            graph_changed = self._ingest_graph_fallback_memory(item)
        self._auto_relate_entities(item)
        self._sync_memory_graph_refs(item, persist_graph=True)
        if graph_changed:
            item["tier"] = "graph"
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
        graph = getattr(self, "_graph", {"edges": []})
        active_edges = [edge for edge in graph.get("edges", []) if edge.get("active", True)]
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
        ranked = self.recall_unified("", k=safe_limit)
        return self._unified_context_string(ranked)

    def _unified_context_string(self, ranked: list[dict]) -> str:
        if not ranked:
            return ""
        parts = []
        for item in ranked:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            path = str(item.get("graph_path", "")).strip()
            if path and path.casefold() != text.casefold():
                parts.append(f"{text} (via {path})")
            else:
                parts.append(text)
        return " | ".join(parts)

    def recall_context(self, query: str, k: int = 5, q_emb=None) -> str:
        return self._unified_context_string(self.recall_unified(query, k=k, q_emb=q_emb))

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

    def verify_integrity(self) -> dict:
        checks = []
        entry_count = len(self.memories)
        indexed_count = int(getattr(self._embeddings, "shape", (0,))[0]) if self._embeddings is not None else 0
        coverage = (indexed_count / entry_count) if entry_count else 1.0
        checks.append(
            {
                "name": "index_coverage",
                "ok": coverage >= settings.memory_tier_min_coverage,
                "detail": f"coverage={coverage:.3f}",
            }
        )
        signature = _memory_signature(self.memories)
        meta_ok = False
        if self._index_meta_path().exists():
            try:
                meta = json.loads(self._index_meta_path().read_text(encoding="utf-8"))
                meta_ok = meta.get("signature") == signature and int(meta.get("entry_count", -1)) == entry_count
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                meta_ok = False
        checks.append({"name": "index_signature", "ok": meta_ok or entry_count == 0, "detail": "metadata matches corpus"})
        dim_ok = True
        if entry_count and self._embeddings is not None and len(getattr(self._embeddings, "shape", ())) == 2:
            dim_ok = self._embeddings.shape[0] == entry_count and self._embeddings.shape[1] > 0
        checks.append({"name": "embedding_shape", "ok": dim_ok, "detail": "matrix rows match memories"})
        node_ids = set(self._graph.get("nodes", {}))
        orphan_edges = 0
        for edge in self._graph.get("edges", []):
            if edge.get("source") not in node_ids or edge.get("target") not in node_ids:
                orphan_edges += 1
        checks.append(
            {
                "name": "graph_nodes",
                "ok": orphan_edges == 0,
                "detail": f"orphan_edges={orphan_edges}",
            }
        )
        texts = [item["text"].strip().casefold() for item in self.memories]
        duplicate_count = len(texts) - len(set(texts))
        checks.append(
            {
                "name": "duplicate_text",
                "ok": duplicate_count == 0,
                "detail": f"duplicates={duplicate_count}",
                "severity": "warning" if duplicate_count else "ok",
            }
        )
        failed = [item for item in checks if not item.get("ok") and item.get("severity") != "warning"]
        return {
            "ok": len(failed) == 0,
            "checks": checks,
            "failed_count": len(failed),
        }

    def tier_report(self) -> dict:
        integrity = self.verify_integrity()
        assessment = self.system_assessment(include_integrity=False)
        coverage = assessment.get("index_coverage_ratio", 0.0)
        min_coverage = _safe_float(settings.memory_tier_min_coverage, 1.0)
        tier = "developing"
        if integrity.get("ok") and coverage >= min_coverage and assessment.get("index_state") in ("warm", "empty"):
            tier = "gd"
        elif integrity.get("failed_count", 0) > 0 or coverage < min_coverage:
            tier = "degraded"
        return {
            "tier": tier,
            "integrity_ok": integrity.get("ok"),
            "index_coverage_ratio": coverage,
            "index_state": assessment.get("index_state"),
            "entry_count": assessment.get("entry_count"),
            "failed_checks": integrity.get("failed_count", 0),
        }

    def maintain(self, rebuild: bool = False, backup: bool = True) -> dict:
        before = self.tier_report()
        backup_path = ""
        if backup and self._memory_files():
            backup_path = self.create_backup("maintain")
        integrity = self.verify_integrity()
        if rebuild or not integrity.get("ok"):
            if self._memory_files() and not backup_path and backup:
                backup_path = self.create_backup("maintain-rebuild")
            self._rebuild_index()
        after = self.tier_report()
        return {
            "status": "ok" if after.get("tier") == "gd" else "partial",
            "backup_path": backup_path,
            "rebuilt": rebuild or not integrity.get("ok"),
            "before": before,
            "after": after,
            "integrity": integrity,
        }

    def system_assessment(self, include_integrity: bool = True) -> dict:
        texts = [item["text"].strip().lower() for item in self.memories]
        duplicate_count = len(texts) - len(set(texts))
        indexed_count = int(getattr(self._embeddings, "shape", (0,))[0]) if self._embeddings is not None else 0
        graph = self.graph_assessment()
        payload = {
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
            "query_cache_size": len(self._query_cache),
            "query_cache_limit": max(0, int(settings.memory_query_cache_size)),
            "rebuild_batch_size": max(1, int(settings.memory_rebuild_batch_size)),
            "ranking": {
                "semantic_weight": settings.memory_rank_semantic_weight,
                "importance_weight": settings.memory_rank_importance_weight,
                "overlap_weight": settings.memory_rank_overlap_weight,
            },
            "graph": graph,
        }
        if include_integrity:
            payload["integrity"] = self.verify_integrity()
            payload["tier"] = self.tier_report().get("tier")
        return payload

    def benchmark_recall(self, query: str, runs: int = 25, k: int = 5) -> dict:
        runs = max(1, int(runs))
        q_emb = self._embed_query(query)
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
        return [
            {
                "id": item["id"],
                "text": item["text"],
                "similarity": item.get("similarity", 0.0),
                "importance": item.get("importance", 0.5),
                "overlap": item.get("overlap", 0),
                "confidence": item.get("confidence", 0.0),
                "score": item.get("unified_score", item.get("score", 0.0)),
                "date": item.get("date", ""),
                "time": item.get("time", ""),
                "tier": item.get("tier", "semantic"),
                "sources": item.get("sources", ["unified"]),
                "graph_path": item.get("graph_path", ""),
            }
            for item in self.recall_unified(query, k=k, q_emb=q_emb)
        ]

    def _recall_ranked_semantic(self, query: str, k: int = 5, q_emb=None) -> list[dict]:
        if not self.memories:
            return []

        if q_emb is None:
            q_emb = self._embed_query(query)
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
        return self.recall_context(query, k=k, q_emb=q_emb)
