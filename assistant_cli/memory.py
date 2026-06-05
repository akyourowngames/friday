from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI

from .config import Settings


MEMORY_FILES = ("project.txt", "user.txt", "preferences.txt", "personal.txt")
MEMORY_BUCKETS = {
    "project": "project.txt",
    "user": "user.txt",
    "preferences": "preferences.txt",
    "preference": "preferences.txt",
    "personal": "personal.txt",
}


@dataclass(frozen=True)
class MemoryHit:
    source: str
    score: float | None
    text: str


class MemoryManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.memory_dir = Path(settings.memory_dir)
        self.index_dir = Path(settings.memory_index_dir)
        self._index: object | None = None
        self._llama_configured = False
        self.ensure_files()
        self.client = OpenAI(base_url=settings.base_url, api_key=settings.api_key, timeout=10.0, max_retries=0)

    def _configure_llama_index(self) -> None:
        if self._llama_configured:
            return

        from llama_index.core import Settings as LlamaSettings
        from llama_index.core.llms.mock import MockLLM

        from .embeddings import NvidiaEmbedding

        LlamaSettings.embed_model = NvidiaEmbedding(
            model_name=self.settings.embed_model,
            api_key=self.settings.api_key,
            api_base=self.settings.base_url,
            embed_batch_size=16,
            timeout=60,
        )
        LlamaSettings.llm = MockLLM()
        self._llama_configured = True

    def ensure_files(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        templates = {
            "project.txt": "# Project Memory\n",
            "user.txt": "# User Memory\n",
            "preferences.txt": "# Preference Memory\n",
            "personal.txt": "# Personal Memory\n",
        }
        for name in MEMORY_FILES:
            path = self.memory_dir / name
            if not path.exists():
                path.write_text(templates[name], encoding="utf-8")

    def files(self) -> list[Path]:
        return [self.memory_dir / name for name in MEMORY_FILES]

    def append(self, bucket: str, fact: str) -> Path:
        filename = MEMORY_BUCKETS.get(bucket.strip().lower())
        if not filename:
            names = ", ".join(sorted(key for key in MEMORY_BUCKETS if key != "preference"))
            raise ValueError(f"Unknown memory bucket. Use one of: {names}.")

        clean_fact = " ".join(str(fact or "").strip().split())
        if not clean_fact:
            raise ValueError("Memory fact is empty.")
        if not clean_fact.endswith((".", "!", "?")):
            clean_fact += "."

        path = self.memory_dir / filename
        line = f"- {clean_fact}"
        existing = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        existing_lines = {row.strip().lower() for row in existing.splitlines()}
        if line.lower() in existing_lines:
            return path

        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{line}\n")
        self._index = None
        return path

    def prompt_context(self) -> str:
        blocks: list[str] = []
        for path in self.files():
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                blocks.append(f"Source: {path.name}\n{text}")

        if not blocks:
            return ""
        return (
            "Saved permanent memory facts are below. Treat these facts as true for the current user. "
            "A saved name is identity context, and saved likes/preferences answer preference questions. "
            "If a requested fact is present below, use it directly. If it is absent, say it is not saved.\n\n"
            + "\n\n---\n\n".join(blocks)
        )

    def capture_user_facts(self, user_text: str, assistant_text: str = "") -> list[str]:
        if not self.settings.auto_llm_memory:
            return []

        facts = self._llm_facts(user_text, assistant_text)
        saved: list[str] = []
        seen: set[tuple[str, str]] = set()
        for bucket, fact in facts:
            normalized_bucket = "preferences" if bucket == "preference" else bucket
            clean_fact = " ".join(str(fact or "").strip().split())
            if normalized_bucket not in MEMORY_BUCKETS or not clean_fact:
                continue

            key = (normalized_bucket, clean_fact.lower())
            if key in seen:
                continue
            seen.add(key)

            before = self._memory_text().lower()
            self.append(normalized_bucket, clean_fact)
            if clean_fact.lower() not in before:
                saved.append(clean_fact)
        return saved

    def _llm_facts(self, user_text: str, assistant_text: str = "") -> list[tuple[str, str]]:
        text = str(user_text or "").strip()
        if not text or len(text) > 1800:
            return []

        prompt = {
            "user_message": text,
            "allowed_buckets": ["personal", "preferences", "project", "user"],
            "output_contract": {
                "facts": [
                    {
                        "bucket": "one allowed bucket",
                        "fact": "one durable fact written as a complete sentence",
                    }
                ]
            },
            "rules": [
                "Return JSON only.",
                "Return an empty facts list when there is no durable memory to save.",
                "Save stable identity, preference, project, and long-term user facts.",
                "Do not save transient chat filler or one-off questions.",
            ],
        }

        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You extract durable memory facts for a local assistant. Return valid JSON only.",
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=220,
            )
            raw = response.choices[0].message.content or ""
            data = self._parse_json_object(raw)
        except Exception:
            return []

        facts: list[tuple[str, str]] = []
        for item in data.get("facts", []):
            if not isinstance(item, dict):
                continue
            bucket = str(item.get("bucket") or "").strip().lower()
            fact = str(item.get("fact") or "").strip()
            if bucket and fact:
                facts.append((bucket, fact))
        return facts

    def _parse_json_object(self, raw: str) -> dict:
        text = str(raw or "").strip()
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            try:
                value = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
            return value if isinstance(value, dict) else {}

    def _marker_path(self) -> Path:
        return self.index_dir / ".source_mtime"

    def _latest_source_mtime(self) -> float:
        return max((path.stat().st_mtime for path in self.files() if path.exists()), default=0)

    def needs_rebuild(self) -> bool:
        marker = self._marker_path()
        if not marker.exists():
            return True
        try:
            indexed_mtime = float(marker.read_text(encoding="utf-8").strip())
        except ValueError:
            return True
        return self._latest_source_mtime() > indexed_mtime

    def rebuild(self) -> None:
        from llama_index.core import SimpleDirectoryReader, VectorStoreIndex

        self._configure_llama_index()
        self.index_dir.mkdir(parents=True, exist_ok=True)
        documents = SimpleDirectoryReader(input_files=[str(path) for path in self.files()]).load_data()
        index = VectorStoreIndex.from_documents(documents, show_progress=False)
        index.storage_context.persist(persist_dir=str(self.index_dir))
        self._marker_path().write_text(str(self._latest_source_mtime()), encoding="utf-8")
        self._index = index

    def load(self) -> object:
        from llama_index.core import StorageContext, load_index_from_storage

        self._configure_llama_index()
        if self._index is not None and not self.needs_rebuild():
            return self._index
        if self.needs_rebuild():
            self.rebuild()
            if self._index is None:
                raise RuntimeError("Memory index rebuild did not create an index.")
            return self._index
        storage_context = StorageContext.from_defaults(persist_dir=str(self.index_dir))
        self._index = load_index_from_storage(storage_context)
        return self._index

    def search(self, query: str) -> list[MemoryHit]:
        index = self.load()
        retriever = index.as_retriever(similarity_top_k=self.settings.memory_top_k)
        nodes = retriever.retrieve(query)
        hits: list[MemoryHit] = []
        for node in nodes:
            source = Path(str(node.metadata.get("file_name", "memory"))).name
            text = node.get_content(metadata_mode="none").strip()
            if text:
                hits.append(MemoryHit(source=source, score=node.score, text=text))
        return hits

    def _memory_text(self) -> str:
        return "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in self.files()
            if path.exists()
        )
