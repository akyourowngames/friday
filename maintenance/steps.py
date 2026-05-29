from __future__ import annotations

from typing import Any

from .config import StepConfig
from .engine import MaintenanceEngine


def memory_daily_step(step: StepConfig, context: dict[str, Any]) -> dict:
    brain = context.get("brain")
    if brain is None:
        from memory.brain import Brain

        brain = Brain()
        context["brain"] = brain
    label = str(step.options.get("label") or "daily").strip() or "daily"
    return brain.daily_maintenance(label=label)


def cognition_scan_step(step: StepConfig, context: dict[str, Any]) -> dict:
    brain = context.get("brain")
    if brain is None:
        from memory.brain import Brain

        brain = Brain()
        context["brain"] = brain
    from cognition.orchestrator import run_cognition_pass

    embed_fn = context.get("embed_fn")
    if embed_fn is None and step.options.get("use_embeddings", True):
        from agent.embedder import embed as embed_fn
    return run_cognition_pass(brain, embed_fn=embed_fn, deep=bool(step.options.get("deep", True)))


def folder_scan_step(step: StepConfig, context: dict[str, Any]) -> dict:
    pipeline = context.get("folder_pipeline")
    if pipeline is None:
        from folder_watcher.configuration import load_config as load_folder_config
        from folder_watcher.index import FolderIndex
        from folder_watcher.ingest import IngestPipeline

        watcher_config = load_folder_config(".")
        index = FolderIndex(watcher_config.database_path)
        pipeline = IngestPipeline(watcher_config, index)
        context["folder_pipeline"] = pipeline
        context["_folder_index_owned"] = index
    result = pipeline.daily_maintenance()
    if step.options.get("include_summarize_pending"):
        result["summarize_pending"] = "skipped_unless_llm_available"
    return result


def telegram_summary_step(step: StepConfig, context: dict[str, Any]) -> dict:
    sender = context.get("telegram_sender")
    chat_ids = list(context.get("telegram_chat_ids") or [])
    summary_text = str(context.get("telegram_summary_text") or "").strip()
    if not summary_text:
        summary_text = _compose_default_summary(context, step)
    if not summary_text:
        return {"sent": 0, "reason": "no_summary_text"}
    if sender is None or not chat_ids:
        return {"sent": 0, "reason": "no_telegram_sender_or_chat_ids", "summary": summary_text}
    sent = 0
    failures: list[str] = []
    for chat_id in chat_ids:
        try:
            sender(chat_id, summary_text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{chat_id}:{type(exc).__name__}")
    return {"sent": sent, "failures": failures, "summary": summary_text}


def scheduler_due_step(step: StepConfig, context: dict[str, Any]) -> dict:
    scheduler = context.get("scheduler")
    if scheduler is None:
        from scheduler.engine import build_scheduler

        scheduler = build_scheduler(allowed_actions=context.get("scheduler_action_whitelist"))
        context["scheduler"] = scheduler
    horizon_minutes = int(step.options.get("horizon_minutes") or 1440)
    return scheduler.run_due(horizon_minutes=horizon_minutes)


def project_audit_step(step: StepConfig, context: dict[str, Any]) -> dict:
    manager = context.get("project_manager")
    if manager is None:
        from project_manager.manager import ProjectManager

        manager = ProjectManager()
        context["project_manager"] = manager
    return manager.audit()


def memory_consolidate_step(step: StepConfig, context: dict[str, Any]) -> dict:
    brain = context.get("brain")
    if brain is None:
        from memory.brain import Brain

        brain = Brain()
        context["brain"] = brain
    from memory.consolidation import consolidate

    return consolidate(brain)


def _compose_default_summary(context: dict[str, Any], step: StepConfig) -> str:
    parts: list[str] = ["KING daily maintenance ran."]
    if step.options.get("include_stats"):
        memory_after = context.get("memory_after")
        if isinstance(memory_after, dict):
            entries = memory_after.get("entry_count")
            if entries is not None:
                parts.append(f"Memory entries: {entries}.")
        folder_stats = context.get("folder_stats_after")
        if isinstance(folder_stats, dict) and folder_stats.get("active_files") is not None:
            parts.append(f"Folder watcher active files: {folder_stats['active_files']}.")
    return " ".join(parts).strip()


def register_default_steps(engine: MaintenanceEngine) -> None:
    engine.register("memory_daily", memory_daily_step)
    engine.register("cognition_scan", cognition_scan_step)
    engine.register("folder_scan", folder_scan_step)
    engine.register("telegram_summary", telegram_summary_step)
    engine.register("scheduler_due", scheduler_due_step)
    engine.register("project_audit", project_audit_step)
    engine.register("memory_consolidate", memory_consolidate_step)
