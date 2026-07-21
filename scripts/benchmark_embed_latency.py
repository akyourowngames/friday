"""Measure the embedding cost removed from the event loop by the latency fix.

``MemoryStore.search`` runs a local ONNX/SentenceTransformer embedding
synchronously on the caller's thread (the asyncio event loop during a streaming
turn).  Only the embedding is expensive (~hundreds of ms); the SQLite queries
are sub-millisecond and must stay on the loop because the connection is
``check_same_thread=True``.  The production fix offloads *just the embedding* to
a worker thread and passes the precomputed vector back into ``search``.  We
time the inline-embed baseline against the offloaded-embed path to quantify how
much event-loop block a ``to_thread`` change removes.

Read-only: builds a throwaway temp DB, does not touch agent behavior.

Run:
    python scripts/benchmark_embed_latency.py
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from pathlib import Path

import sys

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SOURCE_ROOT))

from ares.memory import MemoryStore
from ares.memory.embeddings import EmbeddingProvider


QUERIES = [
    "What theme do I prefer?",
    "What did I mention about the release yesterday?",
    "Remind me of my standing commitments",
    "Summarize my recent goals",
    "Tell me about the project context here",
]


def _build_store(directory: Path) -> MemoryStore:
    import os

    model_file = os.environ.get("ARES_BENCH_ONNX")
    provider = EmbeddingProvider(backend="onnx", file_name=model_file) if model_file else EmbeddingProvider(backend="onnx")
    store = MemoryStore(
        directory / "bench.db",
        embedding_provider=provider,
    )
    # Seed a little memory so vector + FTS paths actually execute.
    for i in range(20):
        store.store(f"User preference item number {i} about theme and releases", category="preference")
    return store


def _time_sync(store: MemoryStore, query: str) -> float:
    """Baseline: embedding runs inline on the caller thread (blocks the loop)."""
    started = time.perf_counter()
    store.search(query, limit=5, scope="all", semantic=True)
    return (time.perf_counter() - started) * 1000.0


async def _time_threaded(store: MemoryStore, query: str) -> float:
    """Production fix: only the embedding leaves the loop; search stays on it.

    Mirrors ``MemoryRecallService.prepare``: the ONNX forward pass is offloaded
    via ``asyncio.to_thread`` and the precomputed vector is handed back to
    ``search`` so all SQLite work stays on the event loop (the connection is
    ``check_same_thread=True``).
    """
    started = time.perf_counter()
    query_vector = await asyncio.to_thread(store._embed_query, query)
    store.search(query, limit=5, scope="all", semantic=True, query_vector=query_vector)
    return (time.perf_counter() - started) * 1000.0


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ares-embed-bench-") as tmp:
        directory = Path(tmp)
        store = _build_store(directory)
        try:
            # Warm up the model once so we measure steady-state, not cold load.
            store.search(QUERIES[0], limit=5, scope="all", semantic=True)
            print(f"vector_enabled={store.vector_enabled}")

            sync_samples: list[float] = []
            threaded_samples: list[float] = []
            for q in QUERIES:
                s = _time_sync(store, q)
                t = await _time_threaded(store, q)
                sync_samples.append(s)
                threaded_samples.append(t)
                diag = getattr(store, "last_search_diagnostics", {}) or {}
                print(
                    f"  q={q[:38]!r:40} sync={s:7.1f}ms  threaded={t:7.1f}ms  "
                    f"mode={diag.get('mode')} vec={diag.get('vector')}"
                )

            print()
            print(f"sync median    : {statistics.median(sync_samples):.1f} ms")
            print(f"threaded median: {statistics.median(threaded_samples):.1f} ms")
            print(
                f"event-loop block saved (sync - threaded): "
                f"{statistics.median(sync_samples) - statistics.median(threaded_samples):.1f} ms per search"
            )
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
