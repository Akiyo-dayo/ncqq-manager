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
_FINAL_PHASES = {"succeeded", "failed", "timeout", "stuck", "superseded", "unknown"}
_START_STUCK_AFTER = 120.0
_STOP_STUCK_AFTER = 60.0
_RESTART_STUCK_AFTER = 120.0
_JOB_RETENTION_SECONDS = 15 * 60.0
# A job that never reaches a final phase must still stop masking the real container
# status. Past this age decorate_container falls back to the Docker-reported status.
_MAX_OPTIMISTIC_DISPLAY_SECONDS = 180.0
# Hard ceiling for non-final jobs, so a monitor that died silently cannot leak.
_ABANDONED_JOB_AFTER = 600.0


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
            # Terminate whatever job was tracking this container so its monitor stops
            # and it can be garbage collected instead of lingering as "running".
            previous_id = self._latest_by_container.get(self.key(name, node_id))
            previous = self._jobs.get(previous_id) if previous_id else None
            if previous and previous.phase not in _FINAL_PHASES:
                previous.phase = "superseded"
                previous.completed_at = now
                previous.updated_at = now
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

    def has_active_job(self, name: str, node_id: str, action: str) -> Optional[str]:
        """Return the in-flight operation id for the *same* action on this container.

        Only identical actions are deduplicated. A different action (stop while a
        restart is in flight) must create its own job and supersede the old one —
        otherwise the caller is handed an unrelated operation id and ends up
        tracking a "succeeded" restart while its stop never ran.
        """
        op_id = self._latest_by_container.get(self.key(name, node_id))
        job = self._jobs.get(op_id) if op_id else None
        if job and job.action == action and job.phase not in _FINAL_PHASES:
            return job.operation_id
        return None

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
        baseline_started_at: str = "",
        action_done: Optional[asyncio.Event] = None,
    ) -> None:
        """Monitor Docker state until the latest job reaches its target or becomes stuck.

        ``inspect_fn`` is called as ``inspect_fn(node_id, name)`` — the same argument
        order every other cluster helper uses — and returns
        ``{"found": bool, "running": bool, "status": str, "started_at": str, ...}``.

        ``baseline_started_at`` is the container's ``State.StartedAt`` sampled before
        the action ran; a restart is proven once that value changes. ``action_done``
        is set by :meth:`run` when the Docker call returns, so the monitor can never
        declare success from the state the container was already in beforehand.
        """
        while True:
            applied = action_done.is_set() if action_done else True
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

            # 生命周期动作按容器串行执行，排队等待期间不该计入"卡住"判定 ——
            # 否则一个排在别人后面的 job 可能还没轮到执行就被判成 stuck。
            elapsed = (time.time() - started_at) if applied else 0.0
            try:
                info = await inspect_fn(node_id, name)
            except Exception as exc:
                info = {"found": False, "running": None, "status": "unknown", "error": str(exc)}
            running = info.get("running")
            docker_status = str(info.get("status") or "unknown")
            observed_started_at = str(info.get("started_at") or "")
            if info.get("error"):
                await self.update(operation_id, docker_status=docker_status, error=str(info.get("error") or ""), running=running)
            else:
                await self.update(operation_id, docker_status=docker_status, running=running, error="")

            if action == "start":
                if applied and running is True and docker_status != "restarting":
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
                    job = self._jobs.get(operation_id)
                    seen_transition = bool(job and job.seen_not_running)
                restarted = seen_transition or bool(
                    baseline_started_at and observed_started_at
                    and observed_started_at != baseline_started_at
                )
                if applied and running is True and docker_status != "restarting" and restarted:
                    await self.succeed(operation_id)
                    return
                if elapsed >= _RESTART_STUCK_AFTER:
                    # The container is healthy; only our evidence of the transition is
                    # missing. Report success rather than scaring the user with "stuck".
                    if running is True and docker_status != "restarting":
                        await self.succeed(operation_id)
                    else:
                        await self.update(operation_id, phase="stuck", error=f"restart still not running after {int(elapsed)}s", completed=True)
                    return
            elif action == "stop":
                # A missing container counts as stopped, but only when the inspect
                # itself succeeded — an unreachable node also reports found=False.
                gone = info.get("found") is False and not info.get("error") and docker_status != "unknown"
                # Everything here is gated on `applied`: lifecycle actions are
                # serialized per container, so a stop queued behind an in-flight
                # restart would otherwise see the restart's own down-phase and
                # report success while the container ends up running again.
                if applied and (running is False or gone):
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
        action_done = asyncio.Event()
        baseline_started_at = ""
        try:
            async with self._lock:
                job = self._jobs.get(operation_id)
                name = job.name if job else ""
                node_id = job.node_id if job else "local"
            if name:
                try:
                    baseline = await inspect_fn(node_id, name)
                    baseline_started_at = str(baseline.get("started_at") or "")
                except Exception as exc:
                    logger.debug("action job %s baseline inspect failed: %s", operation_id, exc)

            # 监控与动作并发跑：动作本身可能要十几秒（stop 宽限期），串行的话
            # 这段时间 UI 只有一个空白的「重启中」，而且监控启动时容器已经重新
            # running，永远观察不到 stop → start 的迁移。
            monitor_task = asyncio.create_task(self.monitor(
                operation_id,
                inspect_fn,
                interval=1.0,
                baseline_started_at=baseline_started_at,
                action_done=action_done,
            ))
            try:
                ok = await action_fn()
            except BaseException:
                monitor_task.cancel()
                raise
            finally:
                action_done.set()
            if not ok:
                monitor_task.cancel()
                await asyncio.gather(monitor_task, return_exceptions=True)
                await self.fail(operation_id, "Docker action failed")
                return
            await monitor_task
        except asyncio.TimeoutError as exc:
            await self.timeout(operation_id, str(exc) or "timeout")
        except Exception as exc:
            # A crash in our own monitoring says nothing about the container. Mark the
            # job unknown so the UI falls back to the real Docker status instead of
            # claiming the action failed.
            logger.exception("container action job %s monitoring crashed", operation_id)
            await self.update(operation_id, phase="unknown", error=str(exc), completed=True)
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
        started_at = latest.get("started_at") or 0
        age = time.time() - started_at if started_at else 0.0

        # An in-flight job that outlived its monitor must stop hiding reality. Past the
        # cutoff we drop the optimistic badge entirely and show the Docker status —
        # this is the safety net for "stuck at restarting while the container is fine".
        if phase in {"queued", "running"} and age > _MAX_OPTIMISTIC_DISPLAY_SECONDS:
            item["action_phase"] = ""
            item["action"] = ""
            item["operation_id"] = ""
            item["action_started_at"] = 0
            item["action_error"] = ""
            item.setdefault("display_status", item.get("status", ""))
            return item

        item["action_phase"] = phase
        item["action"] = action
        item["operation_id"] = latest.get("operation_id", "")
        item["action_started_at"] = started_at
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

    def gc(self) -> None:
        """Periodic cleanup entry point — safe to call from a scheduler tick."""
        self._gc_locked(time.time())

    def _gc_locked(self, now: float) -> None:
        expired = []
        for op_id, job in self._jobs.items():
            if job.phase in _FINAL_PHASES:
                if (job.completed_at or job.updated_at) + _JOB_RETENTION_SECONDS < now:
                    expired.append(op_id)
            elif job.started_at + _ABANDONED_JOB_AFTER < now:
                # Non-final job whose monitor is gone (task cancelled, process churn).
                expired.append(op_id)
        for op_id in expired:
            job = self._jobs.pop(op_id, None)
            if not job:
                continue
            key = self.key(job.name, job.node_id)
            if self._latest_by_container.get(key) == op_id:
                self._latest_by_container.pop(key, None)


action_job_manager = ActionJobManager()
