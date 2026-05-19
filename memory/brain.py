import json
from datetime import date, datetime
from pathlib import Path

import numpy as np

from config import settings
from agent.embedder import embed

MEMORY_DIR = Path(settings.memory_dir)

_VAGUE_PATTERNS = [
    "medical records",
    "doctor's offices",
    "urgent care",
    "area with available",
    "listened to",
    "playlist contains",
]

_CONTRADICTION_CATEGORIES = {
    "name": ["name is", "name :", "full name", "called "],
    "location": ["lives in", "live in", "lives at", "is from", "living in"],
    "age": ["year", "years old", "age is", " yrs"],
    "health": ["not feeling", "feeling sick", "feeling bad", "feels sick", "feels bad",
               "recovered", "feeling better", "feeling well", "is well", "is better",
               "is not well", "is sick", "is healthy"],
}


def _is_vague(text: str) -> bool:
    lower = text.lower()
    if len(text) < 15:
        return True
    for pat in _VAGUE_PATTERNS:
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


class Brain:
    def __init__(self):
        self.memories = []
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def _load_all(self):
        self.memories = []
        for f in sorted(MEMORY_DIR.glob("memory_*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            date_str = f.stem.replace("memory_", "")
            for item in data:
                item["_date"] = date_str
            self.memories.extend(data)

    def _today_path(self):
        return MEMORY_DIR / f"memory_{date.today().isoformat()}.json"

    def _save_today(self):
        today = date.today().isoformat()
        today_items = [m for m in self.memories if m.get("_date") == today]
        path = self._today_path()
        save = []
        for item in today_items:
            save.append({"text": item["text"], "importance": item["importance"], "ts": item["ts"]})
        path.write_text(json.dumps(save, indent=2, ensure_ascii=False), encoding="utf-8")

    def _save_all(self):
        grouped = {}
        for m in self.memories:
            d = m.get("_date", date.today().isoformat())
            grouped.setdefault(d, []).append(m)
        for d, items in grouped.items():
            path = MEMORY_DIR / f"memory_{d}.json"
            save = [{"text": i["text"], "importance": i["importance"], "ts": i["ts"]} for i in items]
            path.write_text(json.dumps(save, indent=2, ensure_ascii=False), encoding="utf-8")

    def _is_duplicate(self, text):
        lower = text.lower()
        words = set(lower.split())
        for m in self.memories:
            existing = m["text"].lower()
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
        for m in self.memories:
            if m["text"].lower() == lower:
                return True
        return False

    def _remove_contradictions(self, text):
        category = _contradiction_category(text)
        if not category:
            return
        keywords = _CONTRADICTION_CATEGORIES[category]
        to_remove = []
        for i, m in enumerate(self.memories):
            lower_existing = m["text"].lower()
            if any(kw in lower_existing for kw in keywords):
                to_remove.append(i)
        for i in reversed(to_remove):
            self.memories.pop(i)

    def commit(self, text: str, importance: float = 0.5):
        if _is_vague(text):
            return False
        text = _normalize_fact(text)
        if self._is_exact_duplicate(text):
            return False
        self._remove_contradictions(text)
        if self._is_duplicate(text):
            return False
        item = {
            "text": text,
            "importance": importance,
            "ts": datetime.now().strftime("%H:%M:%S"),
            "_date": date.today().isoformat(),
        }
        self.memories.append(item)
        self._save_all()
        return True

    def recall(self, query: str, k: int = 5, q_emb=None) -> str:
        if not self.memories:
            return ""

        if q_emb is None:
            q_emb = embed(query)

        texts = [m["text"] for m in self.memories]
        mem_embs = embed(texts)

        if mem_embs.ndim == 1:
            mem_embs = mem_embs.reshape(1, -1)

        sims = np.dot(mem_embs, q_emb)
        weighted = sims * np.array([m.get("importance", 0.5) for m in self.memories])

        top_idx = np.argsort(weighted)[-k:][::-1]
        top_idx = [i for i in top_idx if sims[i] >= settings.memory_similarity_threshold]

        if not top_idx:
            return ""

        unique_idx = []
        seen = set()
        for i in top_idx:
            key = self.memories[i]["text"].strip().lower()
            if key in seen:
                continue
            seen.add(key)
            unique_idx.append(i)
        top_idx = unique_idx

        if len(top_idx) > 1:
            best = top_idx[0]
            second = top_idx[1]
            if sims[best] >= sims[second] + settings.memory_winner_margin:
                top_idx = [best]

        parts = []
        for i in top_idx:
            text = self.memories[i]["text"]
            parts.append(text)
        return " | ".join(parts)
