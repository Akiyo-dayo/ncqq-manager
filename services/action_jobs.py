"""
Lightweight in-memory action job tracking for container lifecycle operations.

The manager intentionally keeps only volatile state. It exists to make
start/stop/restart HTTP calls acknowledge immediately while exposing the
current phase to API responses, WebSocket snapshots and the UI.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional
from uuid import uuid4

from services.log import logger

NotifyCallback = Callable[[], None]

_LIFECYCLE_ACTIONS = {"start", "stop", "restart"}
_FINAL_PHASES = {"succeeded", "failed", "timeout", "stuck"}
_START_STUCK_AFTER = 120.0
_STOP_STUCK_AFTER = 60.0
_RESTART_MIN_DISPLAY_SECONDS = 15.0
_JOB_RETENTION_SECONDS = 15 * 60.0


@dataclass
class ActionJob:
    operation_id: str
    action: str
    name: str
    node_id: str = "local"
    phase: str = "queued"
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    error: str = ""
    docker_status: str = ""
    running: Optional[bool] = None
    seen_not_running: bool = False

    def to_dict(self) -> Dict:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "name": self.name,
            "node_id": self.node_id,
            "phase": self.phase,
            "status": self.phase,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "docker_status": self.docker_status,
            "running": self.running,
        }


class ActionJobManager:
    def __init__(self) -> None:
        self._jobs: Dict[str, ActionJob] = {}
        self._latest_by_container: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._notify: Optional[NotifyCallback] = None

    @staticmethod
    def key(name: str, node_id: str = "local") -> str:
        return f"{node_id or 'local'}:{name}"

    def set_notify_callback(self, callback: Optional[NotifyCallback]) -> None:
        self._notify = callback

    def _emit_change(self) -> None:
        if not self._notify:
            return
        try:
            self._notify()
        except Exception as exc:
            logger.debug("action_jobs notify callback failed: %s", exc)

    async def create(self, name: str, action: str, node_id: str = "local") -> ActionJob:
        if action not in _LIFECYCLE_ACTIONS:
            raise ValueError(f"unsupported async action: {action}")
        async with self._lock:
            now = time.time()
            self._gc_locked(now)
            job = ActionJob(
                operation_id=uuid4().hex,
                action=action,
                name=name,
                node_id=node_id or "local",
                phase="queued",
                started_at=now,
                updated_at=now,
            )
            self._jobs[job.operation_id] = job
            self._latest_by_container[self.key(name, node_id)] = job.operation_id
        self._emit_change()
        return job

    async def mark_running(self, operation_id: str) -> None:
        await self.update(operation_id, phase="running")

    async def succeed(self, operation_id: str) -> None:
        await self.update(operation_id, phase="succeeded", completed=True)

    async def fail(self, operation_id: str, error: str = "") -> None:
        await self.update(operation_id, phase="failed", error=error, completed=True)

    async def timeout(self, operation_id: str, error: str = "timeout") -> None:
        await self.update(operation_id, phase="timeout", error=error, completed=True)

    async def update(
        self,
        operation_id: str,
        *,
        phase: Optional[str] = None,
        error: Optional[str] = None,
        docker_status: Optional[str] = None,
        running: Optional[bool] = None,
        completed: bool = False,
    ) -> Optional[ActionJob]:
        changed = False
        async with self._lock:
            job = self._jobs.get(operation_id)
            if not job:
                return None
            now = time.time()
            if phase is not None and job.phase != phase:
                job.phase = phase
                changed = True
            if error is not None and job.error != error:
                job.error = error
                changed = True
            if docker_status is not None and job.docker_status != docker_status:
                job.docker_status = docker_status
                changed = True
            if running is not None and job.running != running:
                job.running = running
                changed = True
            if completed and job.completed_at is None:
                job.completed_at = now
                changed = True
            if changed:
                job.updated_at = now
        if changed:
            self._emit_change()
        return job

    async def monitor(
        self,
        operation_id: str,
        inspect_fn: Callable[[str, str], Awaitable[Dict]],
        *,
        interval: float = 2.0,
    ) -> None:
        """Monitor Docker state until the latest job reaches its target or becomes stuck.

        inspect_fn returns {"found": bool, "running": bool, "status": str, ...} for
        the given (name, node_id). For remote nodes this should call the remote
        container list through the existing cluster path.
        """
        while True:
            async with self._lock:
                job = self._jobs.get(operation_id)
                if not job:
                    return
                if job.phase in _FINAL_PHASES:
                    return
                action = job.action
                started_at = job.started_at
                name = job.name
                node_id = job.node_id
                latest = self._latest_by_container.get(self.key(name, node_id)) == operation_id
            if not latest:
                # Superseded by a newer operation on the same container.
                return

            elapsed = time.time() - started_at
            try:
                info = await inspect_fn(name, node_id)
            except Exception as exc:
                info = {"found": False, "running": None, "status": "unknown", "error": str(exc)}
            running = info.get("running")
            docker_status = str(info.get("status") or "unknown")
            if info.get("error"):
                await self.update(operation_id, docker_status=docker_status, error=str(info.get("error") or ""), running=running)
            else:
                await self.update(operation_id, docker_status=docker_status, running=running)

            if action == "start":
                if running is True and docker_status not in {"restarting"}:
                    await self.succeed(operation_id)
                    return
                if elapsed >= _START_STUCK_AFTER:
                    await self.update(operation_id, phase="stuck", error=f"start still not running after {int(elapsed)}s", completed=True)
                    return
            elif action == "restart":
                if running is False or docker_status in {"exited", "dead", "created", "restarting"}:
                    async with self._lock:
                        job = self._jobs.get(operation_id)
                        if job:
                            job.seen_not_running = True
                            job.updated_at = time.time()
                async with self._lock:
                    seen_transition = bool(self._jobs.get(operation_id) and self._jobs[operation_id].seen_not_running)
                if running is True and docker_status not in {"restarting"} and (seen_transition or elapsed >= _RESTART_MIN_DISPLAY_SECONDS):
                    await self.succeed(operation_id)
                    return
                if elapsed >= _START_STUCK_AFTER:
                    await self.update(operation_id, phase="stuck", error=f"restart still not running after {int(elapsed)}s", completed=True)
                    return
            elif action == "stop":
                if running is False or (not info.get("found") and running is not True):
                    await self.succeed(operation_id)
                    return
                if elapsed >= _STOP_STUCK_AFTER:
                    await self.update(operation_id, phase="stuck", error=f"stop still running after {int(elapsed)}s", completed=True)
                    return

            await asyncio.sleep(interval)

    async def run(
        self,
        operation_id: str,
        action_fn: Callable[[], Awaitable[bool]],
        inspect_fn: Callable[[str, str], Awaitable[Dict]],
    ) -> None:
        """Run the Docker action in the background and continue state monitoring."""
        await self.mark_running(operation_id)
        try:
            ok = await action_fn()
            if not ok:
                await self.fail(operation_id, "Docker action failed")
                return
            # Successful Docker API return means the command was accepted/executed.
            # Now verify the target state. Starting the monitor only after Docker
            # accepts avoids restart jobs being marked succeeded just because the
            # container was still running before the restart actually began.
            await asyncio.sleep(0.1)
            async with self._lock:
                job = self._jobs.get(operation_id)
                final = bool(job and job.phase in _FINAL_PHASES)
            if not final:
                await self.monitor(operation_id, inspect_fn, interval=1.0)
        except asyncio.TimeoutError as exc:
            await self.timeout(operation_id, str(exc) or "timeout")
        except Exception as exc:
            logger.exception("container action job %s failed", operation_id)
            await self.fail(operation_id, str(exc))
        finally:
            self._emit_change()

    def get(self, operation_id: str) -> Optional[Dict]:
        job = self._jobs.get(operation_id)
        return job.to_dict() if job else None

    def get_latest(self, name: str, node_id: str = "local") -> Optional[Dict]:
        op_id = self._latest_by_container.get(self.key(name, node_id))
        if not op_id:
            return None
        job = self._jobs.get(op_id)
        return job.to_dict() if job else None

    def decorate_container(self, item: Dict) -> Dict:
        latest = self.get_latest(item.get("name", ""), item.get("node_id", "local"))
        if not latest:
            item.setdefault("action_phase", "")
            item.setdefault("action", "")
            item.setdefault("operation_id", "")
            item.setdefault("action_started_at", 0)
            item.setdefault("action_error", "")
            return item
        phase = latest.get("phase", "")
        action = latest.get("action", "")
        item["action_phase"] = phase
        item["action"] = action
        item["operation_id"] = latest.get("operation_id", "")
        item["action_started_at"] = latest.get("started_at") or 0
        item["action_updated_at"] = latest.get("updated_at") or 0
        item["action_error"] = latest.get("error") or ""
        if phase in {"queued", "running"}:
            if action == "start":
                item["display_status"] = "starting"
            elif action == "stop":
                item["display_status"] = "stopping"
            elif action == "restart":
                item["display_status"] = "restarting"
        elif phase == "stuck":
            item["display_status"] = f"{action}_stuck" if action else "stuck"
        elif phase in {"failed", "timeout"}:
            item["display_status"] = "failed"
        else:
            item.setdefault("display_status", item.get("status", ""))
        return item

    def _gc_locked(self, now: float) -> None:
        expired = [
            op_id for op_id, job in self._jobs.items()
            if job.phase in _FINAL_PHASES and (job.completed_at or job.updated_at) + _JOB_RETENTION_SECONDS < now
        ]
        for op_id in expired:
            job = self._jobs.pop(op_id, None)
            if not job:
                continue
            key = self.key(job.name, job.node_id)
            if self._latest_by_container.get(key) == op_id:
                self._latest_by_container.pop(key, None)


action_job_manager = ActionJobManager()
