"""
定时任务管理路由 - CRUD
"""
import uuid as uuid_mod

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, Dict

from middleware.auth import require_admin
from services.scheduler import scheduler

router = APIRouter(prefix="/api", tags=["scheduler"])


class TaskRequest(BaseModel):
    name: str
    type: str = "backup_db"
    interval_seconds: int = 3600
    config: Dict = {}


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None
    config: Optional[Dict] = None


@router.get("/scheduler/tasks")
def list_tasks(session: dict = Depends(require_admin)):
    tasks = scheduler.list_tasks()
    return {"status": "ok", "tasks": tasks}


@router.post("/scheduler/tasks")
def create_task(req: TaskRequest, session: dict = Depends(require_admin)):
    task_id = "task-" + uuid_mod.uuid4().hex[:8]
    success = scheduler.create_task(
        task_id, req.name, req.type, req.interval_seconds, req.config,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create task")
    return {"status": "ok", "task_id": task_id}


@router.put("/scheduler/tasks/{task_id}")
def update_task(task_id: str, req: TaskUpdate, session: dict = Depends(require_admin)):
    scheduler.update_task(task_id, req.name, req.enabled, req.interval_seconds, req.config)
    return {"status": "ok"}


@router.delete("/scheduler/tasks/{task_id}")
def delete_task(task_id: str, session: dict = Depends(require_admin)):
    scheduler.delete_task(task_id)
    return {"status": "ok"}

