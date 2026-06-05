from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from llama_index.core import (
    Settings as LlamaSettings,
    SimpleDirectoryReader,
    StorageContext,
    VectorStoreIndex,
    load_index_from_storage,
)
from llama_index.core.llms.mock import MockLLM
from openai import OpenAI

from .config import AssistantSettings
from .minilm import MiniLMEmbedding
from .session_store import DbHit, SessionStore


MEMORY_BUCKETS = {
    "project": "project.txt",
    "user": "user.txt",
    "preferences": "preferences.txt",
    "preference": "preferences.txt",
    "personal": "personal.txt",
}


@dataclass(frozen=True)
class RagHit:
    source: str
    text: str
    score: float | None


class KnowledgeRAG:
    def __init__(self, settings: AssistantSettings) -> None:
        self.settings = settings
        self._index: VectorStoreIndex | None = None
        self._configure_llama_index()
        self.ensure_sources()

    def _configure_llama_index(self) -> None:
        LlamaSettings.embed_model = MiniLMEmbedding(embed_batch_size=32)
        LlamaSettings.llm = MockLLM()

    def ensure_sources(self) -> None:
        self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
        self.settings.knowledge_dir.mkdir(parents=True, exist_ok=True)
        templates = {
            "project.txt": "# Project Memory\n",
            "user.txt": "# User Memory\n",
            "preferences.txt": "# Preference Memory\n",
            "personal.txt": "# Personal Memory\n",
        }
        for filename, text in templates.items():
            path = self.settings.memory_dir / filename
            if not path.exists():
                path.write_text(text, encoding="utf-8")
        readme = self.settings.knowledge_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Knowledge RAG\n\nDrop project notes, docs, and reference markdown here. Friday indexes them locally.\n",
                encoding="utf-8",
            )

    def source_files(self) -> list[Path]:
        files = [self.settings.memory_dir / name for name in dict.fromkeys(MEMORY_BUCKETS.values())]
        for root in (self.settings.knowledge_dir,):
            for pattern in ("*.txt", "*.md"):
                files.extend(sorted(root.rglob(pattern)))
        return [path for path in files if path.exists() and path.is_file()]

    def memory_context(self) -> str:
        blocks: list[str] = []
        for filename in dict.fromkeys(MEMORY_BUCKETS.values()):
            path = self.settings.memory_dir / filename
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            if text:
                blocks.append(f"[{filename}]\n{text}")
        if not blocks:
            return ""
        return (
            "Saved permanent memory facts. Treat these as true for the current user "
            "when they are relevant:\n"
            + "\n\n---\n\n".join(blocks)
        )

    def _marker_path(self) -> Path:
        return self.settings.rag_index_dir / ".source_mtime"

    def _source_mtime(self) -> float:
        return max((path.stat().st_mtime for path in self.source_files()), default=0.0)

    def needs_rebuild(self) -> bool:
        marker = self._marker_path()
        if not marker.exists():
            return True
        try:
            indexed = float(marker.read_text(encoding="utf-8").strip())
        except ValueError:
            return True
        return self._source_mtime() > indexed

    def rebuild(self) -> None:
        files = self.source_files()
        self.settings.rag_index_dir.mkdir(parents=True, exist_ok=True)
        documents = SimpleDirectoryReader(input_files=[str(path) for path in files]).load_data()
        self._index = VectorStoreIndex.from_documents(documents, show_progress=False)
        self._index.storage_context.persist(persist_dir=str(self.settings.rag_index_dir))
        self._marker_path().write_text(str(self._source_mtime()), encoding="utf-8")

    def load(self) -> VectorStoreIndex:
        if self._index is not None and not self.needs_rebuild():
            return self._index
        if self.needs_rebuild():
            self.rebuild()
            if self._index is None:
                raise RuntimeError("RAG rebuild did not produce an index.")
            return self._index
        storage = StorageContext.from_defaults(persist_dir=str(self.settings.rag_index_dir))
        self._index = load_index_from_storage(storage)
        return self._index

    def search(self, query: str, top_k: int | None = None) -> list[RagHit]:
        index = self.load()
        retriever = index.as_retriever(similarity_top_k=top_k or self.settings.rag_top_k)
        hits = []
        for node in retriever.retrieve(query):
            text = node.get_content(metadata_mode="none").strip()
            if not text:
                continue
            source = Path(str(node.metadata.get("file_name", "knowledge"))).name
            hits.append(RagHit(source=source, text=text, score=node.score))
        return hits

    def append_fact(self, bucket: str, fact: str) -> Path:
        filename = MEMORY_BUCKETS.get(bucket.strip().lower())
        if not filename:
            raise ValueError("Unknown memory bucket. Use project, user, preferences, or personal.")
        path = self.settings.memory_dir / filename
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        line = f"- {fact.strip()}"
        if line.lower() not in existing.lower():
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{line}\n")
        self._index = None
        return path


class AgenticRAG:
    def __init__(self, settings: AssistantSettings, rag: KnowledgeRAG, store: SessionStore) -> None:
        self.settings = settings
        self.rag = rag
        self.store = store
        self.client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=30.0, max_retries=0)

    def plan_queries(self, user_text: str, recent_context: str) -> list[str]:
        if not self.settings.agentic_rag_enabled:
            return [user_text]
        prompt = (
            "Create compact local RAG search queries for this assistant. "
            "Use the current user request and recent chat. Return only JSON like "
            '{"queries":["..."]}. Keep 1 to '
            f"{self.settings.agentic_query_count} queries. No web search.\n\n"
            f"Recent chat:\n{recent_context[-2000:] or '(none)'}\n\nUser request:\n{user_text}"
        )
        try:
            response = self.client.chat.completions.create(
                model=self.settings.model,
                messages=[
                    {"role": "system", "content": "You write JSON search plans for local memory retrieval."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=160,
            )
            raw = response.choices[0].message.content or ""
            data = json.loads(raw.strip().strip("`"))
            queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
        except Exception:
            queries = []
        queries.insert(0, user_text)
        deduped = []
        for query in queries:
            if query.lower() not in {item.lower() for item in deduped}:
                deduped.append(query)
        return deduped[: max(1, self.settings.agentic_query_count)]

    def retrieve(self, user_text: str) -> str:
        blocks = []
        memory_context = self.rag.memory_context()
        if memory_context:
            blocks.append(memory_context)
        if self.rag.needs_rebuild():
            return "\n\n".join(blocks)

        recent = self.store.recent_text(self.settings.last_messages)
        queries = self.plan_queries(user_text, recent)
        rag_hits: list[RagHit] = []
        db_hits: list[DbHit] = []
        seen = set()
        for query in queries:
            for hit in self.rag.search(query):
                key = (hit.source, hit.text[:160])
                if key not in seen:
                    seen.add(key)
                    rag_hits.append(hit)
            db_hits.extend(self.store.search_messages(query, limit=2))

        if rag_hits:
            chunks = []
            for hit in rag_hits[: self.settings.rag_top_k]:
                score = f"{hit.score:.3f}" if hit.score is not None else "n/a"
                chunks.append(f"[{hit.source} score={score}]\n{hit.text}")
            blocks.append("Knowledge and memory RAG:\n" + "\n\n---\n\n".join(chunks))
        if db_hits:
            chunks = [f"[{hit.session_id} {hit.role} at {hit.at}]\n{hit.content}" for hit in db_hits[:4]]
            blocks.append("SQLite chat recall:\n" + "\n\n---\n\n".join(chunks))
        return "\n\n".join(blocks)
