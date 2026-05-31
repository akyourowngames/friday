"""KING Nightly Automation — full rebuild and reindex pipeline.

This is the single source of truth for nightly maintenance. It rebuilds
everything from scratch: graph, embeddings, sessions, consolidation, vault.

Usage:
    python nightly.py              Run everything now (one-click)
    python nightly.py --step X     Run only step X (for debugging)
    python nightly.py --install    Install Windows Task Scheduler job for midnight
    python nightly.py --uninstall  Remove the scheduled task

Pipeline (in order):
    1. Rebuild graph from all memories
    2. Rebuild embedding index + vector store
    3. Session digest (ingest transcripts into brain)
    4. Memory consolidation (dedup, insights, decay)
    5. User file ingestion (vault files -> brain)
    6. Obsidian vault sync (brain -> vault pages)
    7. Cognition pass (cadence, episodes, proactive)
    8. Project manager audit
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

TASK_NAME = "KING_Nightly"
LOG_PATH = PROJECT_ROOT / "storage" / "nightly.log"


def _log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


# ─── Pipeline steps ──────────────────────────────────────────────────────────


def step_rebuild_graph(brain) -> dict:
    """Rebuild the entire graph from scratch using all stored memories."""
    _log("  Rebuilding graph from memories...")
    try:
        dirty_dates = brain._rebuild_graph_from_memories()
        node_count = len(brain._graph.get("nodes", {}))
        edge_count = len([e for e in brain._graph.get("edges", []) if e.get("active", True)])
        _log(f"    Graph: {node_count} nodes, {edge_count} active edges, {len(dirty_dates)} dirty dates")
        return {"nodes": node_count, "edges": edge_count, "dirty_dates": len(dirty_dates)}
    except Exception as e:
        _log(f"    Graph rebuild failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_rebuild_embeddings(brain) -> dict:
    """Rebuild the embedding index and vector store from all memories."""
    _log("  Rebuilding embedding index...")
    try:
        brain._rebuild_index()
        emb_count = 0
        if brain._embeddings is not None:
            emb_count = brain._embeddings.shape[0] if hasattr(brain._embeddings, "shape") else 0
        vec_count = brain._vector_store.size() if hasattr(brain, "_vector_store") else 0
        _log(f"    Embeddings: {emb_count} vectors, vector store: {vec_count} entries")
        return {"embeddings": emb_count, "vector_store": vec_count}
    except Exception as e:
        _log(f"    Embedding rebuild failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_session_digest(brain) -> dict:
    """Digest all undigested session transcripts into brain facts."""
    _log("  Processing session transcripts...")
    try:
        from memory.session_store import SessionStore
        from memory.session_digest import process_undigested

        store = SessionStore()
        results = process_undigested(store, brain=brain)
        digested = sum(1 for r in results if r.get("status") == "digested")
        total_facts = sum(r.get("facts_stored", 0) for r in results)
        _log(f"    Sessions: {digested} digested, {total_facts} facts stored")
        return {"sessions": len(results), "digested": digested, "facts": total_facts}
    except Exception as e:
        _log(f"    Session digest failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_consolidation(brain) -> dict:
    """Run memory consolidation: dedup, insights, decay."""
    _log("  Running memory consolidation...")
    try:
        from memory.consolidation import consolidate

        result = consolidate(brain)
        dedup = result.get("dedup", {})
        insights = result.get("insights", {})
        decay = result.get("decay", {})
        _log(f"    Dedup: {dedup.get('merged', 0)} merged, "
             f"Insights: {insights.get('insights', 0)} created, "
             f"Decay: {decay.get('decayed', 0)} faded")
        return result
    except Exception as e:
        _log(f"    Consolidation failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_ingest_user_files(brain) -> dict:
    """Ingest user-created files from the Obsidian vault into brain."""
    _log("  Ingesting user vault files...")
    try:
        from memory.worker import ingest_user_files

        result = ingest_user_files(brain)
        count = result.get("facts_ingested", 0)
        files = result.get("user_files_found", 0)
        _log(f"    Ingested: {count} facts from {files} files")
        return result
    except Exception as e:
        _log(f"    User file ingestion failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_sync_vault(brain) -> dict:
    """Rebuild the Obsidian memory vault from brain state."""
    _log("  Syncing Obsidian vault...")
    try:
        from memory.worker import sync_vault

        result = sync_vault(brain.memories, brain._graph)
        written = result.get("files_written", 0)
        people = result.get("people", 0)
        facts = result.get("facts", 0)
        _log(f"    Vault: {written} files, {people} people pages, {facts} fact pages")
        return result
    except Exception as e:
        _log(f"    Vault sync failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_cognition(brain) -> dict:
    """Run cognition pass: cadence, episodes, proactive candidates."""
    _log("  Running cognition pass...")
    try:
        from agent.embedder import embed
        from cognition.orchestrator import run_cognition_pass

        result = run_cognition_pass(brain, embed_fn=embed, persist=True, deep=True)
        cadence = result.get("cadence_nodes", 0)
        episodes = result.get("episodes", 0)
        deviations = result.get("actionable_deviations", 0)
        signals = result.get("memory_signals", 0)
        _log(f"    Cognition: {cadence} cadence, {episodes} episodes, {deviations} deviations, {signals} signals")
        return result
    except Exception as e:
        _log(f"    Cognition failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_project_audit() -> dict:
    """Run project manager audit to check health and generate alerts."""
    _log("  Running project audit...")
    try:
        from project_manager.manager import ProjectManager

        manager = ProjectManager()
        result = manager.audit()
        audited = result.get("audited", 0)
        alerts = result.get("alerts", 0)
        _log(f"    Projects: {audited} audited, {alerts} alerts")
        return result
    except Exception as e:
        _log(f"    Project audit failed: {e}")
        return {"status": "failed", "error": str(e)}


def step_folder_watcher_reindex() -> dict:
    """Trigger folder watcher reindex if the service is reachable."""
    _log("  Checking folder watcher...")
    try:
        from config import settings
        import urllib.request

        base_url = settings.folder_watcher_base_url
        if not base_url:
            _log("    Folder watcher: no URL configured, skipping")
            return {"status": "skipped", "reason": "no_base_url"}

        try:
            req = urllib.request.Request(f"{base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                json.loads(resp.read().decode())
        except Exception:
            _log("    Folder watcher: service not reachable, skipping")
            return {"status": "skipped", "reason": "unreachable"}

        try:
            req = urllib.request.Request(
                f"{base_url}/scan",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())
            indexed = result.get("indexed", "?")
            _log(f"    Folder watcher: {indexed} files indexed")
            return result
        except Exception as e:
            _log(f"    Folder watcher reindex failed: {e}")
            return {"status": "failed", "error": str(e)}
    except Exception as e:
        _log(f"    Folder watcher failed: {e}")
        return {"status": "failed", "error": str(e)}


# ─── Pipeline runner ─────────────────────────────────────────────────────────

ALL_STEPS = [
    "rebuild_graph",
    "rebuild_embeddings",
    "session_digest",
    "consolidation",
    "ingest_user_files",
    "sync_vault",
    "cognition",
    "project_audit",
    "folder_watcher",
]

STEP_FUNCTIONS = {
    "rebuild_graph": lambda brain: step_rebuild_graph(brain),
    "rebuild_embeddings": lambda brain: step_rebuild_embeddings(brain),
    "session_digest": lambda brain: step_session_digest(brain),
    "consolidation": lambda brain: step_consolidation(brain),
    "ingest_user_files": lambda brain: step_ingest_user_files(brain),
    "sync_vault": lambda brain: step_sync_vault(brain),
    "cognition": lambda brain: step_cognition(brain),
    "project_audit": lambda brain: step_project_audit(),
    "folder_watcher": lambda brain: step_folder_watcher_reindex(),
}


def run_pipeline(steps=None):
    """Run the specified steps (or all) with a shared Brain instance."""
    steps = steps or ALL_STEPS
    _log("=" * 60)
    _log(f"KING Nightly — running {len(steps)} steps")
    _log("=" * 60)
    start = time.perf_counter()

    # Load Brain once — shared across all steps.
    _log("Loading brain...")
    brain_start = time.perf_counter()
    from memory.brain import Brain
    brain = Brain()
    _log(f"Brain loaded in {time.perf_counter() - brain_start:.1f}s "
         f"({len(brain.memories)} memories, "
         f"{len(brain._graph.get('nodes', {}))} nodes)")

    results = {}
    for step_name in steps:
        fn = STEP_FUNCTIONS.get(step_name)
        if not fn:
            _log(f"Unknown step: {step_name}")
            continue
        step_start = time.perf_counter()
        results[step_name] = fn(brain)
        elapsed = time.perf_counter() - step_start
        _log(f"  [{step_name}] done in {elapsed:.1f}s")

    elapsed = time.perf_counter() - start
    _log(f"Nightly complete in {elapsed:.1f}s")
    _log("")
    return results


# ─── CLI ─────────────────────────────────────────────────────────────────────


def install_task():
    """Install a Windows Task Scheduler task to run at midnight daily."""
    python_exe = sys.executable
    script_path = str(PROJECT_ROOT / "nightly.py")

    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{python_exe}" "{script_path}"',
        "/SC", "DAILY",
        "/ST", "00:00",
        "/F",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Scheduled task '{TASK_NAME}' installed.")
            print(f"  Runs daily at midnight.")
            print(f"  Script: {script_path}")
            print(f"  Python: {python_exe}")
            print(f"\n  Manual: python nightly.py")
            print(f"  Remove: python nightly.py --uninstall")
        else:
            print(f"Failed: {result.stderr}")
            print("Try running as Administrator.")
    except Exception as e:
        print(f"Error: {e}")


def uninstall_task():
    """Remove the Windows Task Scheduler task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Task '{TASK_NAME}' removed.")
        else:
            print(f"Failed: {result.stderr}")
    except Exception as e:
        print(f"Error: {e}")


def main():
    os.chdir(PROJECT_ROOT)

    parser = argparse.ArgumentParser(description="KING Nightly Automation")
    parser.add_argument("--install", action="store_true", help="Install scheduled task")
    parser.add_argument("--uninstall", action="store_true", help="Remove scheduled task")
    parser.add_argument("--step", type=str, help="Run only this step (e.g. --step rebuild_graph)")
    parser.add_argument("--list-steps", action="store_true", help="List all available steps")
    args = parser.parse_args()

    if args.list_steps:
        for s in ALL_STEPS:
            print(f"  {s}")
        return
    if args.install:
        install_task()
        return
    if args.uninstall:
        uninstall_task()
        return

    steps = [args.step] if args.step else None
    if args.step and args.step not in STEP_FUNCTIONS:
        print(f"Unknown step: {args.step}")
        print(f"Available: {', '.join(ALL_STEPS)}")
        return

    try:
        run_pipeline(steps)
    except Exception as e:
        _log(f"FATAL: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
