"""Root-owned resource coordination and safe builder workspace isolation."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from ares.multi_agent.policy import (
    ToolCallResource,
    ToolResource,
    call_resource,
    paths_overlap,
    required_capabilities,
)
from ares.multi_agent import AgentCapability


@dataclass(frozen=True, slots=True)
class BuilderWorkspace:
    root: str
    isolated: bool
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"root": self.root, "isolated": self.isolated, "reason": self.reason}


_DEFAULT_RESOURCE_CONCURRENCY: dict[str, int] = {
    "browser": 4,
    "shell": 4,
    "repl": 1,
    "project_check": 4,
    "database": 1,
    "communication": 8,
    "external": 12,
    "delegation": 8,
}


class ResourceCoordinator:
    """Coordinate mutable resources across every root and child Agent.

    Files use a path-aware readers/writer lease.  Stateful global surfaces use
    a bounded-concurrency semaphore each so independent specialists can run
    in parallel up to a safe limit instead of being serialized by a single
    ``asyncio.Lock``.
    """

    def __init__(
        self,
        *,
        provider_limit: int = 0,
        resource_limits: dict[str, int] | None = None,
    ) -> None:
        limits = {**_DEFAULT_RESOURCE_CONCURRENCY}
        if resource_limits:
            limits.update(resource_limits)
        self._locks = {
            name: asyncio.Semaphore(max(1, int(limit)))
            for name, limit in limits.items()
        }
        self._fs_condition = asyncio.Condition()
        self._fs_leases: dict[int, tuple[bool, tuple[str, ...]]] = {}
        self._fs_waiters: list[tuple[int, bool, tuple[str, ...]]] = []
        self._next_lease = 0
        self._next_waiter = 0
        self._provider_limit = max(0, int(provider_limit))
        self._provider = (
            asyncio.Semaphore(self._provider_limit) if self._provider_limit else None
        )
        self._quarantine_condition = asyncio.Condition()
        self._quarantined: dict[int, dict[str, str]] = {}
        self._next_quarantine = 0

    def state(self) -> dict[str, Any]:
        reads = sum(not write for write, _paths in self._fs_leases.values())
        writes = sum(write for write, _paths in self._fs_leases.values())
        return {
            "filesystem_reads": reads,
            "filesystem_writes": writes,
            "filesystem_waiters": len(self._fs_waiters),
            "provider_limit": self._provider_limit,
            "locked": {name: lock.locked() for name, lock in self._locks.items()},
            "quarantined_operations": list(self._quarantined.values()),
        }

    async def _wait_for_quarantine(self, resource: str = "") -> None:
        # A detached tool may still hold a process, socket, or external side
        # effect. Quarantine only same-resource work so one unresponsive call
        # does not freeze unrelated parallel workers. When no resource is
        # named, fall back to the global behavior for backwards compatibility.
        async with self._quarantine_condition:
            if resource:
                predicate = lambda: not any(
                    q.get("resource") == resource for q in self._quarantined.values()
                )
            else:
                predicate = lambda: not self._quarantined
            await self._quarantine_condition.wait_for(predicate)

    async def quarantine_call(
        self,
        tool_name: str,
        operation: asyncio.Task[Any],
        *,
        owner_run_id: str = "",
        reason: str = "unresponsive",
        resource: str = "",
    ) -> dict[str, str]:
        """Detach an unresponsive operation while retaining a logical lease."""
        async with self._quarantine_condition:
            self._next_quarantine += 1
            token = self._next_quarantine
            payload = {
                "tool": str(tool_name), "owner_run_id": str(owner_run_id),
                "reason": str(reason), "state": "quarantined",
                "resource": str(resource),
            }
            self._quarantined[token] = payload

        async def release_when_done() -> None:
            try:
                await operation
            except BaseException:
                pass
            finally:
                async with self._quarantine_condition:
                    self._quarantined.pop(token, None)
                    self._quarantine_condition.notify_all()

        asyncio.create_task(release_when_done(), name=f"ares-quarantine:{tool_name}:{token}")
        return dict(payload)

    @staticmethod
    def _filesystem_conflict(
        write: bool,
        paths: tuple[str, ...],
        existing_write: bool,
        existing_paths: tuple[str, ...],
    ) -> bool:
        if not write and not existing_write:
            return False
        # An unknown read/write scope is conservative.  Unknown reads can
        # overlap other reads, but never a write.
        if not paths or not existing_paths:
            return True
        return paths_overlap(paths, existing_paths)

    @asynccontextmanager
    async def _filesystem_lease(
        self, *, write: bool, paths: tuple[str, ...]
    ) -> AsyncIterator[None]:
        async with self._fs_condition:
            self._next_waiter += 1
            waiter_id = self._next_waiter
            waiter = (waiter_id, write, paths)
            self._fs_waiters.append(waiter)

            def ready() -> bool:
                if any(
                    self._filesystem_conflict(write, paths, active_write, active_paths)
                    for active_write, active_paths in self._fs_leases.values()
                ):
                    return False
                # Do not let a later conflicting read jump ahead of a queued
                # writer. Independent paths and read/read pairs may still
                # overlap, retaining useful concurrency without starvation.
                for queued_id, queued_write, queued_paths in self._fs_waiters:
                    if queued_id == waiter_id:
                        break
                    if self._filesystem_conflict(
                        write, paths, queued_write, queued_paths
                    ):
                        return False
                return True

            try:
                await self._fs_condition.wait_for(ready)
            except BaseException:
                if waiter in self._fs_waiters:
                    self._fs_waiters.remove(waiter)
                    self._fs_condition.notify_all()
                raise
            self._fs_waiters.remove(waiter)
            self._next_lease += 1
            lease_id = self._next_lease
            self._fs_leases[lease_id] = (write, paths)
        try:
            yield
        finally:
            async with self._fs_condition:
                self._fs_leases.pop(lease_id, None)
                self._fs_condition.notify_all()

    @asynccontextmanager
    async def acquire(self, resource: ToolCallResource) -> AsyncIterator[None]:
        await self._wait_for_quarantine(resource.resource.value)
        async with AsyncExitStack() as stack:
            if resource.resource is ToolResource.FILESYSTEM_READ:
                await stack.enter_async_context(
                    self._filesystem_lease(write=False, paths=resource.paths)
                )
            elif resource.resource is ToolResource.FILESYSTEM_WRITE:
                await stack.enter_async_context(
                    self._filesystem_lease(write=True, paths=resource.paths)
                )
            elif resource.resource in {
                ToolResource.BROWSER_READ,
                ToolResource.BROWSER_INTERACTION,
            }:
                await stack.enter_async_context(self._locks["browser"])
            elif resource.resource is ToolResource.SHELL_SHARED:
                await stack.enter_async_context(self._locks["shell"])
                await stack.enter_async_context(self._locks["database"])
                await stack.enter_async_context(self._locks["communication"])
                await stack.enter_async_context(self._locks["external"])
                # Shell text can address paths outside cwd through aliases,
                # subprocesses, and generated scripts.  Treat it as an
                # unknown-path write unless a future structured command plan
                # proves a narrower scope.
                await stack.enter_async_context(
                    self._filesystem_lease(write=True, paths=())
                )
            elif resource.resource is ToolResource.REPL_SHARED:
                await stack.enter_async_context(self._locks["repl"])
                await stack.enter_async_context(self._locks["database"])
                await stack.enter_async_context(self._locks["communication"])
                await stack.enter_async_context(self._locks["external"])
                await stack.enter_async_context(
                    self._filesystem_lease(write=True, paths=())
                )
            elif resource.resource is ToolResource.PROJECT_CHECK:
                await stack.enter_async_context(self._locks["project_check"])
                await stack.enter_async_context(
                    self._filesystem_lease(write=True, paths=())
                )
            elif resource.resource in {
                ToolResource.DATABASE_READ,
                ToolResource.DATABASE_WRITE,
            }:
                await stack.enter_async_context(self._locks["database"])
            elif resource.resource is ToolResource.COMMUNICATION:
                await stack.enter_async_context(self._locks["communication"])
            elif resource.resource is ToolResource.EXTERNAL_MUTATION:
                await stack.enter_async_context(self._locks["external"])
            elif resource.resource is ToolResource.DELEGATION:
                await stack.enter_async_context(self._locks["delegation"])
            if self._provider is not None and resource.name.startswith("mcp__"):
                await stack.enter_async_context(self._provider)
            yield

    @asynccontextmanager
    async def acquire_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> AsyncIterator[None]:
        call = {"function": {"name": tool_name}}
        resource = call_resource(0, call, arguments)
        capabilities = required_capabilities(tool_name)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(self.acquire(resource))
            if (
                AgentCapability.FILESYSTEM_WRITE in capabilities
                and resource.resource is not ToolResource.FILESYSTEM_WRITE
            ):
                await stack.enter_async_context(self._filesystem_lease(write=True, paths=()))
            yield


class BuilderWorktreeManager:
    """Create detached Git worktrees, or safely serialize live-tree builders."""

    _SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or "~/.ares/agent-worktrees").expanduser().resolve()
        self._live_tree_lock = asyncio.Lock()

    @staticmethod
    def _git(
        repository: Path, *arguments: str, timeout: int = 20
    ) -> subprocess.CompletedProcess[str]:
        command = ["git", "-C", str(repository), *arguments]
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, -1, "", str(exc))

    def prepare(
        self,
        repository: str | Path,
        *,
        root_run_id: str,
        child_run_id: str,
    ) -> BuilderWorkspace:
        repo = Path(repository).expanduser().resolve()
        if shutil.which("git") is None:
            return BuilderWorkspace(str(repo), False, "git is unavailable; mutation builders are serialized")
        top = self._git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return BuilderWorkspace(str(repo), False, "repository is not a Git worktree; mutation builders are serialized")
        repo = Path(top.stdout.strip()).resolve()
        status = self._git(repo, "status", "--porcelain")
        if status.returncode != 0 or status.stdout.strip():
            return BuilderWorkspace(
                str(repo), False,
                "working tree has uncommitted changes; builders are serialized to preserve current state",
            )
        safe_root = self._SAFE_ID.sub("_", root_run_id)[:80]
        safe_child = self._SAFE_ID.sub("_", child_run_id)[:80]
        target = (self.root / safe_root / safe_child).resolve()
        try:
            target.relative_to(self.root)
        except ValueError:
            return BuilderWorkspace(str(repo), False, "unsafe worktree target; builders are serialized")
        if target.exists():
            existing = self._git(target, "rev-parse", "--show-toplevel")
            if existing.returncode == 0 and Path(existing.stdout.strip()).resolve() == target:
                return BuilderWorkspace(str(target), True, "reusing isolated detached worktree")
            return BuilderWorkspace(
                str(repo), False,
                "worktree target already exists but is not a valid isolated worktree; builders are serialized",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        added = self._git(repo, "worktree", "add", "--detach", str(target), "HEAD", timeout=60)
        if added.returncode != 0:
            return BuilderWorkspace(
                str(repo), False,
                f"worktree creation failed; builders are serialized: {added.stderr.strip()[:300]}",
            )
        return BuilderWorkspace(str(target), True, "isolated detached Git worktree")

    def capture_patch(
        self,
        workspace: BuilderWorkspace,
        *,
        root_run_id: str,
        child_run_id: str,
    ) -> tuple[str | None, str]:
        """Persist the complete isolated-worktree diff as a reviewable artifact.

        ``git add -N`` makes newly-created files visible to ``git diff`` without
        staging their contents.  The patch lives beside, rather than inside, the
        worktree so it never becomes part of a subsequent builder diff.
        """
        if not workspace.isolated:
            return None, "builder used the serialized live working tree"
        root = Path(workspace.root).expanduser().resolve()
        try:
            relative = root.relative_to(self.root)
        except ValueError:
            return None, "isolated builder workspace is outside the managed worktree root"
        status = self._git(root, "status", "--porcelain")
        if status.returncode != 0:
            return None, f"could not inspect builder worktree: {status.stderr.strip()[:300]}"
        untracked = [
            line[3:]
            for line in status.stdout.splitlines()
            if line.startswith("?? ") and line[3:].strip()
        ]
        if untracked:
            added = self._git(root, "add", "-N", "--", *untracked)
            if added.returncode != 0:
                return None, f"could not include new builder files in patch: {added.stderr.strip()[:300]}"
        diff = self._git(root, "diff", "--binary", "--no-ext-diff", "HEAD")
        if diff.returncode != 0:
            return None, f"could not capture builder patch: {diff.stderr.strip()[:300]}"
        if not diff.stdout.strip():
            return None, "builder produced no tracked file changes"
        safe_child = self._SAFE_ID.sub("_", child_run_id)[:80]
        patch_dir = (self.root / relative.parts[0] / "patches").resolve()
        try:
            patch_dir.relative_to(self.root)
        except ValueError:
            return None, "unsafe builder patch target"
        patch_dir.mkdir(parents=True, exist_ok=True)
        patch_path = patch_dir / f"{safe_child}.patch"
        patch_path.write_text(diff.stdout, encoding="utf-8")
        return str(patch_path), "captured isolated builder patch"

    def apply_patch(
        self,
        repository: str | Path,
        patch_path: str | Path,
    ) -> tuple[bool, str]:
        """Apply one approved patch only to a still-clean target repository."""
        repo = Path(repository).expanduser().resolve()
        patch = Path(patch_path).expanduser().resolve()
        try:
            patch.relative_to(self.root)
        except ValueError:
            return False, "refusing to apply a patch outside the managed worktree root"
        if not patch.is_file():
            return False, "reviewed builder patch no longer exists"
        top = self._git(repo, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return False, "target repository is not a Git worktree"
        repo = Path(top.stdout.strip()).resolve()
        status = self._git(repo, "status", "--porcelain")
        if status.returncode != 0 or status.stdout.strip():
            return False, "target working tree changed; approved patch was retained for manual application"
        checked = self._git(repo, "apply", "--check", "--whitespace=nowarn", str(patch))
        if checked.returncode != 0:
            return False, f"approved patch did not apply cleanly: {checked.stderr.strip()[:300]}"
        applied = self._git(repo, "apply", "--whitespace=nowarn", str(patch))
        if applied.returncode != 0:
            return False, f"approved patch application failed: {applied.stderr.strip()[:300]}"
        return True, "approved builder patch applied sequentially to the target working tree"

    @asynccontextmanager
    async def mutation_slot(self, workspace: BuilderWorkspace | None) -> AsyncIterator[None]:
        if workspace is not None and workspace.isolated:
            yield
            return
        async with self._live_tree_lock:
            yield
