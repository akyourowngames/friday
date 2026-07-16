"""Tool handlers for managing cron jobs without detached work."""
from __future__ import annotations

import asyncio
import json
import re
import traceback
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable

from ares.cron.policy import normalize_cron_policy, validate_dependency_graph
from ares.cron.schedule_utils import parse_natural_schedule, simulate_next_runs, validate_cron
from ares.cron.store import CronConflictError, CronStore, utc_now
from ares.tools.results import structured_result, wants_structured


class CronToolHandlers:
    def __init__(self, store: CronStore, runner_factory: Callable[..., Any] | None = None):
        self.store = store
        self._runner_factory = runner_factory
        self._run_tasks: dict[str, asyncio.Task] = {}

    def _runner(self):
        if self._runner_factory is not None:
            return self._runner_factory(store=self.store)
        from ares.cron.runner import CronRunner

        return CronRunner(store=self.store)

    def create_cron_job(self, args: dict) -> str:
        cron = validate_cron(parse_natural_schedule(str(args["cron"])))
        policy = normalize_cron_policy(args.get("policy") or {})
        simulation = simulate_next_runs(
            cron, str(args.get("timezone") or "UTC"), count=int(args.get("preview_count", 5)),
        )
        if bool(args.get("preview", False)):
            data = {"preview": True, "name": args["name"], "cron": cron, "policy": policy, "schedule_simulation": simulation}
            return structured_result("Cron job preview generated.", status="preview", data=data) if wants_structured(args) else json.dumps(data, indent=2, ensure_ascii=False)
        job = self.store.create_job(
            args["name"],
            args["prompt"],
            cron,
            args.get("timezone", "UTC"),
            args.get("enabled", True),
            args.get("max_iterations"),
            policy,
            expected_store_revision=args.get("expected_store_revision", args.get("expected_revision")),
        )
        if wants_structured(args):
            return structured_result(
                f"Created cron job {job['id']}.", data={"job": job, "schedule_simulation": simulation},
                metrics={"next_run_count": len(simulation["next_runs"]), "policy_fields": len(policy)},
            )
        return f"Created cron job: {job}"

    def list_cron_jobs(self, args: dict) -> str:
        jobs = self.store.list_jobs(bool(args.get("include_disabled", True)))
        if wants_structured(args):
            return structured_result(
                f"Found {len(jobs)} cron job(s).", data={"jobs": jobs},
                metrics={"job_count": len(jobs), "enabled_count": sum(bool(job.get("enabled")) for job in jobs)},
            )
        if not jobs:
            return "No cron jobs."
        return "\n".join(
            f"- {job['id']}: {job['name']} [{job['state']}] next={job.get('next_run_at')} "
            f"enabled={job.get('enabled')} revision={job.get('revision')}"
            for job in jobs
        )

    def get_cron_job(self, args: dict) -> str:
        job = self.store.get_job(args["job_id"])
        if not job:
            return f"Cron job {args['job_id']} not found"
        job["schedule_simulation"] = simulate_next_runs(
            job["cron"],
            job.get("timezone", "UTC"),
            last_run_at=job.get("last_run_at"),
        )
        if wants_structured(args):
            return structured_result(
                f"Loaded cron job {job['id']}.", data={"job": job},
                metrics={"run_count": job.get("run_count", 0), "consecutive_failures": job.get("consecutive_failures", 0)},
            )
        return json.dumps(job, indent=2, ensure_ascii=False)

    def update_cron_job(self, args: dict) -> str:
        payload = dict(args)
        job_id = payload.pop("job_id")
        expected_revision = payload.pop("expected_revision", None)
        expected_store_revision = payload.pop("expected_store_revision", None)
        preview = bool(payload.pop("preview", False))
        payload.pop("response_format", None)
        preview_count = int(payload.pop("preview_count", 5))
        if "policy" in payload:
            payload["policy"] = normalize_cron_policy(payload["policy"])
        if preview:
            current = self.store.get_job(job_id)
            if current is None:
                raise ValueError(f"Cron job '{job_id}' not found")
            candidate = {**current, **{key: value for key, value in payload.items() if value is not None}}
            candidate["cron"] = validate_cron(parse_natural_schedule(str(candidate["cron"])))
            candidate["policy"] = normalize_cron_policy(candidate.get("policy") or {})
            jobs = {item["id"]: item for item in self.store.list_jobs(True)}
            jobs[job_id] = candidate
            validate_dependency_graph(jobs)
            simulation = simulate_next_runs(candidate["cron"], candidate["timezone"], count=preview_count, last_run_at=candidate.get("last_run_at"))
            data = {"preview": True, "job": candidate, "schedule_simulation": simulation}
            return structured_result("Cron update preview generated.", status="preview", data=data) if wants_structured(args) else json.dumps(data, indent=2, ensure_ascii=False)
        job = self.store.update_job(
            job_id,
            expected_revision=expected_revision,
            expected_store_revision=expected_store_revision,
            **payload,
        )
        if wants_structured(args):
            return structured_result(f"Updated cron job {job_id}.", data={"job": job})
        return f"Updated cron job: {job}"

    def delete_cron_job(self, args: dict) -> str:
        self.store.delete_job(args["job_id"], expected_revision=args.get("expected_revision"))
        if wants_structured(args):
            return structured_result(
                f"Deleted cron job {args['job_id']}.", data={"job_id": args["job_id"], "logs_retained": True},
            )
        return f"Deleted cron job {args['job_id']} (logs retained)."

    def _setup_failure(self, job_id: str, lease_id: str, error: BaseException) -> None:
        path = self.store.log_dir(job_id) / f"{utc_now().replace(':', '-')}-setup-failed.md"
        path.write_text(f"# Cron Run Setup Failure\n\n{traceback.format_exc()}\n", encoding="utf-8")
        with suppress(Exception):
            self.store.complete_job(job_id, lease_id, status="failed", log_path=path)

    def _observe_task(self, job_id: str, task: asyncio.Task) -> None:
        def done(completed: asyncio.Task) -> None:
            if self._run_tasks.get(job_id) is completed:
                self._run_tasks.pop(job_id, None)
            with suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(done)

    def run_cron_job_now(self, args: dict) -> str:
        """Claim synchronously, then run synchronously or as a tracked task."""
        job_id = args["job_id"]
        # Make invalid IDs observable before creating any coroutine/task.
        if self.store.get_job(job_id) is None:
            raise ValueError(f"Cron job '{job_id}' not found")
        try:
            claimed = self.store.claim_job(job_id)
        except CronConflictError as exc:
            message = str(exc) or f"Cron job {job_id} is blocked."
            return structured_result(message, status="conflict", data={"job_id": job_id}) if wants_structured(args) else message
        lease_id = str(claimed["lease_id"])
        runner = self._runner()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                path = asyncio.run(runner.run_job(job_id, lease_id=lease_id))
            except Exception as exc:
                # run_job normally records its own terminal state.  This path
                # protects the narrow window before its lifecycle starts.
                self._setup_failure(job_id, lease_id, exc)
                raise
            if wants_structured(args):
                return structured_result(
                    f"Ran cron job {job_id}.", data={"job_id": job_id, "background": False},
                    artifacts=[{"path": str(path), "name": path.name, "kind": "cron_log"}],
                )
            return f"Ran cron job {job_id}; log: {path}"

        try:
            task = loop.create_task(runner.run_job(job_id, lease_id=lease_id), name=f"ares-cron-manual-{job_id}")
        except Exception as exc:
            self._setup_failure(job_id, lease_id, exc)
            raise
        self._run_tasks[job_id] = task
        self._observe_task(job_id, task)
        if wants_structured(args):
            return structured_result(
                f"Started cron job {job_id}.", data={"job_id": job_id, "background": True, "tracked": True},
            )
        return f"Started cron job {job_id} in the background (tracked task)."

    def get_cron_logs(self, args: dict) -> str:
        advanced = any(key in args for key in ("status", "date_from", "date_to", "include", "cursor")) or wants_structured(args)
        logs = self.store.recent_logs(args["job_id"], 50 if advanced else int(args.get("limit", 5)))
        if not logs:
            return structured_result("No cron logs found.", status="not_found", data={"logs": []}) if wants_structured(args) else "No logs found."
        if advanced:
            records = [self._parse_log(path) for path in logs]
            status = str(args.get("status") or "").casefold()
            if status:
                records = [record for record in records if record["status"].casefold() == status]
            for key, operator in (("date_from", lambda left, right: left >= right), ("date_to", lambda left, right: left <= right)):
                if not args.get(key):
                    continue
                boundary = datetime.fromisoformat(str(args[key]).replace("Z", "+00:00"))
                if boundary.tzinfo is None:
                    boundary = boundary.replace(tzinfo=timezone.utc)
                records = [record for record in records if record["started_at"] and operator(datetime.fromisoformat(record["started_at"].replace("Z", "+00:00")), boundary)]
            offset = max(0, int(args.get("cursor") or 0))
            limit = max(1, min(int(args.get("limit", 5)), 50))
            page = records[offset:offset + limit]
            include = set(args.get("include") or [])
            if "content" not in include:
                for record in page:
                    record.pop("content", None)
            next_cursor = str(offset + limit) if offset + limit < len(records) else None
            if wants_structured(args):
                return structured_result(
                    f"Loaded {len(page)} cron log(s).",
                    data={"logs": page, "next_cursor": next_cursor, "total_matching": len(records)},
                    metrics={
                        "log_count": len(page),
                        "failed_count": sum(record["status"] == "failed" for record in page),
                        "duration_seconds": round(sum(float(record.get("duration_seconds") or 0) for record in page), 3),
                        "retry_count": sum(max(0, int(record.get("retry_attempt") or 1) - 1) for record in page),
                    },
                )
            return json.dumps({"logs": page, "next_cursor": next_cursor, "total_matching": len(records)}, indent=2, ensure_ascii=False)
        return "\n\n".join(f"# {path.name}\n{path.read_text(encoding='utf-8')[:4000]}" for path in logs)

    @staticmethod
    def _parse_log(path) -> dict[str, Any]:
        content = path.read_text(encoding="utf-8", errors="replace")[:100_000]

        def field(name: str) -> str:
            match = re.search(rf"^\*\*{re.escape(name)}:\*\*\s*(.+)$", content, re.MULTILINE)
            return match.group(1).strip() if match else ""

        def bullet(name: str) -> str:
            match = re.search(rf"^- {re.escape(name)}:\s*(.+)$", content, re.MULTILINE | re.IGNORECASE)
            return match.group(1).strip() if match else ""

        error_match = re.search(r"^## Error\s*\n(.+?)(?:\n## |\Z)", content, re.MULTILINE | re.DOTALL)
        duration_text = field("Duration").removesuffix("s")
        started = field("Run")
        return {
            "path": str(path.resolve()), "name": path.name, "job_id": field("Job"),
            "started_at": started, "status": field("Status") or "unknown",
            "duration_seconds": float(duration_text) if duration_text.replace(".", "", 1).isdigit() else None,
            "retry_attempt": int(bullet("Retry attempt") or 1),
            "failure_summary": (error_match.group(1).strip().splitlines()[0][:500] if error_match else ""),
            "content": content,
        }
