"""KING Nightly Automation — runs maintenance, memory worker, and folder watcher reindex.

Usage:
    python nightly.py              Run everything now (one-click)
    python nightly.py --install    Install Windows Task Scheduler job for midnight
    python nightly.py --uninstall  Remove the scheduled task

What it does:
    1. Daily maintenance (memory backup, cleanup, cognition pass)
    2. Memory worker (ingest user vault files, rebuild Obsidian vault)
    3. Folder watcher reindex (if configured and reachable)
"""

import argparse
import json
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


def run_maintenance(brain=None) -> dict:
    """Run the daily maintenance routine."""
    _log("Starting daily maintenance...")
    try:
        from maintenance.engine import build_engine
        from maintenance.steps import register_default_steps

        engine = build_engine(PROJECT_ROOT, None)
        register_default_steps(engine)
        ctx = {}
        if brain is not None:
            ctx["brain"] = brain
        result = engine.run(triggered_by="nightly", force=True, context=ctx)
        _log(f"  Maintenance done: {result.status}")
        return result.to_dict()
    except Exception as e:
        _log(f"  Maintenance failed: {e}")
        return {"status": "failed", "error": str(e)}


def run_memory_worker(brain=None) -> dict:
    """Ingest user files + rebuild the Obsidian memory vault."""
    _log("Starting memory worker...")
    try:
        from memory.worker import ingest_user_files, sync_vault

        if brain is None:
            from memory.brain import Brain
            brain = Brain()

        # 1. Ingest any user-created files in the vault
        ingest_result = ingest_user_files(brain)
        _log(f"  Ingestion: {ingest_result.get('facts_ingested', 0)} facts from {ingest_result.get('user_files_found', 0)} files")

        # 2. Rebuild the full vault from current memories
        sync_result = sync_vault(brain.memories, brain._graph)
        _log(f"  Vault sync: {sync_result.get('files_written', 0)} files, {sync_result.get('people', 0)} people")

        return {"ingest": ingest_result, "sync": sync_result}
    except Exception as e:
        _log(f"  Memory worker failed: {e}")
        return {"status": "failed", "error": str(e)}


def run_cognition(brain=None) -> dict:
    """Run a cognition pass (cadence, episodes, proactive candidates)."""
    _log("Starting cognition pass...")
    try:
        from agent.embedder import embed
        from cognition.orchestrator import run_cognition_pass

        if brain is None:
            from memory.brain import Brain
            brain = Brain()

        result = run_cognition_pass(brain, embed_fn=embed, persist=True, deep=True)
        _log(f"  Cognition: {result.get('cadence_nodes', 0)} cadence nodes, {result.get('episodes', 0)} episodes, {result.get('actionable_deviations', 0)} deviations, {result.get('memory_signals', 0)} memory signals")
        return result
    except Exception as e:
        _log(f"  Cognition failed: {e}")
        return {"status": "failed", "error": str(e)}


def run_session_digest(brain=None) -> dict:
    """Digest any undigested session transcripts."""
    _log("Starting session digest...")
    try:
        from memory.session_store import SessionStore
        from memory.session_digest import process_undigested

        store = SessionStore()
        if brain is None:
            from memory.brain import Brain
            brain = Brain()
        results = process_undigested(store, brain=brain)
        digested = sum(1 for r in results if r.get("status") == "digested")
        total_facts = sum(r.get("facts_stored", 0) for r in results)
        _log(f"  Session digest: {digested} sessions, {total_facts} facts stored")
        return {"sessions": len(results), "digested": digested, "facts": total_facts}
    except Exception as e:
        _log(f"  Session digest failed: {e}")
        return {"status": "failed", "error": str(e)}


def run_folder_watcher_reindex() -> dict:
    """Trigger a folder watcher full reindex if the service is reachable."""
    _log("Starting folder watcher reindex...")
    try:
        from config import settings
        import urllib.request

        base_url = settings.folder_watcher_base_url
        if not base_url:
            _log("  Folder watcher: no base URL configured, skipping")
            return {"status": "skipped", "reason": "no_base_url"}

        # Check if service is up
        try:
            req = urllib.request.Request(f"{base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                health = json.loads(resp.read().decode())
        except Exception:
            _log("  Folder watcher: service not reachable, skipping")
            return {"status": "skipped", "reason": "unreachable"}

        # Trigger reindex
        try:
            req = urllib.request.Request(
                f"{base_url}/scan",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=b"{}",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode())
            _log(f"  Folder watcher reindex: {result.get('indexed', '?')} files")
            return result
        except Exception as e:
            _log(f"  Folder watcher reindex failed: {e}")
            return {"status": "failed", "error": str(e)}
    except Exception as e:
        _log(f"  Folder watcher failed: {e}")
        return {"status": "failed", "error": str(e)}


def run_all():
    """Run the full nightly pipeline."""
    _log("=" * 50)
    _log("KING Nightly Automation starting")
    _log("=" * 50)
    start = time.perf_counter()

    # Load Brain once — shared across all steps to avoid triple embedding load.
    from memory.brain import Brain
    brain = Brain()

    results = {
        "maintenance": run_maintenance(brain),
        "memory_worker": run_memory_worker(brain),
        "session_digest": run_session_digest(brain),
        "cognition": run_cognition(brain),
        "folder_watcher": run_folder_watcher_reindex(),
    }

    elapsed = time.perf_counter() - start
    _log(f"Nightly complete in {elapsed:.1f}s")
    _log("")
    return results


def install_task():
    """Install a Windows Task Scheduler task to run at midnight daily."""
    python_exe = sys.executable
    script_path = str(PROJECT_ROOT / "nightly.py")

    # Create the scheduled task
    cmd = [
        "schtasks", "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{python_exe}" "{script_path}"',
        "/SC", "DAILY",
        "/ST", "00:00",
        "/F",  # Force overwrite if exists
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Scheduled task '{TASK_NAME}' installed successfully.")
            print(f"  Runs daily at midnight.")
            print(f"  Script: {script_path}")
            print(f"  Python: {python_exe}")
            print(f"\n  To run manually: python nightly.py")
            print(f"  To remove: python nightly.py --uninstall")
        else:
            print(f"Failed to create task: {result.stderr}")
            print("Try running as Administrator.")
    except Exception as e:
        print(f"Error: {e}")


def uninstall_task():
    """Remove the Windows Task Scheduler task."""
    cmd = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"Scheduled task '{TASK_NAME}' removed.")
        else:
            print(f"Failed to remove task: {result.stderr}")
    except Exception as e:
        print(f"Error: {e}")


def main():
    # Ensure working directory is the project root, not System32 or wherever
    # Task Scheduler / cron launches the process.
    import os
    os.chdir(PROJECT_ROOT)

    parser = argparse.ArgumentParser(description="KING Nightly Automation")
    parser.add_argument("--install", action="store_true", help="Install Windows scheduled task for midnight")
    parser.add_argument("--uninstall", action="store_true", help="Remove the scheduled task")
    args = parser.parse_args()

    if args.install:
        install_task()
    elif args.uninstall:
        uninstall_task()
    else:
        try:
            run_all()
        except Exception as e:
            _log(f"FATAL: {type(e).__name__}: {e}")
            raise


if __name__ == "__main__":
    main()
