import json
from datetime import date, datetime
from pathlib import Path

import numpy as np

from config import settings
from agent.embedder import embed

MEMORY_DIR = Path(__file__).resolve().parent.parent / "storage" / "memories"


class Brain:
    def __init__(self):
        self.memories = []
        self._embeddings = None
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

    def _is_duplicate(self, text):
        lower = text.lower()
        for m in self.memories:
            if m["text"].lower() == lower:
                return True
            if len(text) > 20 and lower in m["text"].lower():
                return True
            if len(m["text"]) > 20 and m["text"].lower() in lower:
                return True
        return False

    def commit(self, text: str, importance: float = 0.5):
        if self._is_duplicate(text):
            return
        item = {
            "text": text,
            "importance": importance,
            "ts": datetime.now().strftime("%H:%M:%S"),
            "_date": date.today().isoformat(),
        }
        self.memories.append(item)
        if self._embeddings is not None:
            emb = embed(text).reshape(1, -1)
            self._embeddings = np.vstack([self._embeddings, emb])
        self._save_today()

    def recall(self, query: str, k: int = 5) -> str:
        if not self.memories:
            return ""

        if self._embeddings is None:
            texts = [m["text"] for m in self.memories]
            self._embeddings = embed(texts)

        q_emb = embed(query)
        sims = np.dot(self._embeddings, q_emb)
        weighted = sims * np.array([m.get("importance", 0.5) for m in self.memories])

        top_idx = np.argsort(weighted)[-k:][::-1]
        top_idx = [i for i in top_idx if sims[i] > 0.15]

        if not top_idx:
            return ""

        parts = []
        for i in top_idx:
            parts.append(self.memories[i]["text"])
        return " | ".join(parts)
