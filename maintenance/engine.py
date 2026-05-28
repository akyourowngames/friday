from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import MaintenanceConfig, StepConfig, load_config
from .state import MaintenanceState

StepHandler = Callable[[StepConfig, dict], dict]


@dataclass
class MaintenanceResult:
    started_at: str
    finished_at: str
    triggered_by: str
    status: str
    steps: list[dict] = field(default_factory=list)
    skipped_reason: str | None = None
    config_path: str = ""

    def to_dict(self) -> dict:
        payload = {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "triggered_by": self.triggered_by,
            "status": self.status,
            "steps": list(self.steps),
            "config_path": self.config_path,
        }
        if self.skipped_reason is not None:
            payload["skipped_reason"] = self.skipped_reason
        return payload


class MaintenanceEngine:
    def __init__(
        self,
        config: MaintenanceConfig,
        state: MaintenanceState | None = None,
        handlers: dict[str, StepHandler] | None = None,
    ):
        self.config = config
        self.state = state or MaintenanceState()
        self._handlers: dict[str, StepHandler] = dict(handlers or {})

    def register(self, step_name: str, handler: StepHandler) -> None:
        self._handlers[str(step_name)] = handler

    def run(
        self,
        triggered_by: str = "cli",
        dry_run: bool = False,
        force: bool = False,
        context: dict | None = None,
    ) -> MaintenanceResult:
        started = datetime.now()
        ctx = dict(context or {})
        if not self.config.enabled:
            return self._skipped(started, triggered_by, "disabled_in_markdown")
        if not force and self.state.already_ran_today(started.date()):
            return self._skipped(started, triggered_by, "already_ran_today")
        if not force and self.state.too_soon(self.config.min_run_interval_minutes, now=started):
            return self._skipped(started, triggered_by, "min_run_interval_not_met")

        steps_report: list[dict] = []
        any_failed = False
        for step in self.config.steps:
            entry = self._run_step(step, ctx, dry_run)
            steps_report.append(entry)
            if entry["status"] == "failed":
                any_failed = True

        finished = datetime.now()
        status = "dry_run" if dry_run else ("partial" if any_failed else "ok")
        result = MaintenanceResult(
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            triggered_by=str(triggered_by or "cli"),
            status=status,
            steps=steps_report,
            config_path=str(self.config.config_path),
        )
        if not dry_run:
            self._persist(started, finished, result)
        return result

    def status(self) -> dict:
        state = self.state.load()
        recent = self.state.recent_runs(limit=5)
        return {
            "config": self.config.public_dict(),
            "last_run_date": state.get("last_run_date"),
            "last_run_at": state.get("last_run_at"),
            "last_status": state.get("last_status"),
            "recent_runs": recent,
            "registered_handlers": sorted(self._handlers.keys()),
        }

    def _run_step(self, step: StepConfig, context: dict, dry_run: bool) -> dict:
        if not step.enabled:
            return self._step_entry(step, "skipped", reason="disabled")
        handler = self._handlers.get(step.name)
        if handler is None:
            return self._step_entry(step, "skipped", reason="no_handler_registered")
        if dry_run:
            return self._step_entry(step, "dry_run", reason="dry_run", evidence={"options": dict(step.options)})
        try:
            evidence = handler(step, context) or {}
        except Exception as exc:  # noqa: BLE001
            return self._step_entry(step, "failed", reason=f"{type(exc).__name__}: {exc}")
        if not isinstance(evidence, dict):
            evidence = {"value": evidence}
        return self._step_entry(step, "ok", evidence=evidence)

    def _step_entry(self, step: StepConfig, status: str, reason: str | None = None, evidence: dict | None = None) -> dict:
        entry = {
            "name": step.name,
            "status": status,
            "options": dict(step.options),
        }
        if reason:
            entry["reason"] = reason
        if evidence is not None:
            entry["evidence"] = evidence
        return entry

    def _skipped(self, started: datetime, triggered_by: str, reason: str) -> MaintenanceResult:
        return MaintenanceResult(
            started_at=started.isoformat(timespec="seconds"),
            finished_at=started.isoformat(timespec="seconds"),
            triggered_by=str(triggered_by or "cli"),
            status="skipped",
            steps=[],
            skipped_reason=reason,
            config_path=str(self.config.config_path),
        )

    def _persist(self, started: datetime, finished: datetime, result: MaintenanceResult) -> None:
        state_payload = {
            "last_run_date": started.date().isoformat(),
            "last_run_at": started.isoformat(timespec="seconds"),
            "last_finished_at": finished.isoformat(timespec="seconds"),
            "last_status": result.status,
            "config_path": str(self.config.config_path),
            "step_count": len(result.steps),
        }
        self.state.save(state_payload)
        self.state.append_log(result.to_dict(), self.config.log_max_runs)


def build_engine(repo_root: str | Path = ".", config_path: str | Path | None = None) -> MaintenanceEngine:
    config = load_config(repo_root, config_path)
    return MaintenanceEngine(config)
