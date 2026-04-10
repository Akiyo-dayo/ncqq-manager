"""
定时任务调度器 - 轻量级基于 asyncio 的调度
支持: 重启容器、清理日志、备份数据库
"""
import os
import json
import time
import shutil
import asyncio
from typing import Dict, List, Optional

from services.log import logger
from services.database import DB_PATH
import services.database as db


class Scheduler:
    """基于 SQLite 存储 + asyncio 循环的轻量调度器"""

    _EXECUTE_TIMEOUT = 60
    _BACKUP_RETENTION_COUNT = 7

    def __init__(self):
        self._init_table()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._running_tasks: set[str] = set()

    def _init_table(self):
        db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                cron_expr TEXT DEFAULT '',
                interval_seconds INTEGER DEFAULT 3600,
                config TEXT DEFAULT '{}',
                last_run REAL DEFAULT 0,
                last_result TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                run_count INTEGER DEFAULT 0,
                created_at REAL DEFAULT 0
            )
        """)
        db.commit()

    def list_tasks(self) -> List[Dict]:
        rows = db.fetchall("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
        return [self._parse(r) for r in rows]

    def create_task(self, task_id: str, name: str, task_type: str,
                    interval_seconds: int = 3600, config: Dict = None) -> bool:
        try:
            db.execute(
                "INSERT INTO scheduled_tasks (id,name,type,interval_seconds,config,created_at) VALUES (?,?,?,?,?,?)",
                (task_id, name, task_type, interval_seconds,
                 json.dumps(config or {}), time.time()),
            )
            db.commit()
            return True
        except Exception as e:
            logger.error("创建定时任务失败: %s", e)
            return False

    def update_task(self, task_id: str, name: str = None,
                    enabled: bool = None, interval_seconds: int = None,
                    config: Dict = None) -> bool:
        updates, params = [], []
        if name is not None:
            updates.append("name=?"); params.append(name)
        if enabled is not None:
            updates.append("enabled=?"); params.append(1 if enabled else 0)
        if interval_seconds is not None:
            updates.append("interval_seconds=?"); params.append(interval_seconds)
        if config is not None:
            updates.append("config=?"); params.append(json.dumps(config))
        if not updates:
            return False
        params.append(task_id)
        db.execute(f"UPDATE scheduled_tasks SET {','.join(updates)} WHERE id=?", params)
        db.commit()
        return True

    def _record_result(self, task_id: str, *, last_run: float, result: str, error: str):
        db.execute(
            "UPDATE scheduled_tasks "
            "SET last_run=?, last_result=?, last_error=?, run_count=run_count+1 "
            "WHERE id=?",
            (last_run, result, error, task_id),
        )
        db.commit()

    def delete_task(self, task_id: str) -> bool:
        db.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))
        db.commit()
        return True

    async def start(self):
        """启动调度循环"""
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        """停止调度循环"""
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self):
        """主循环：每 60 秒检查一次待执行任务"""
        while self._running:
            try:
                await self._check_and_run()
            except Exception as e:
                logger.debug("调度检查异常: %s", e)
            await asyncio.sleep(60)

    async def _check_and_run(self):
        tasks = self.list_tasks()
        now = time.time()
        for task in tasks:
            if not task["enabled"]:
                continue
            task_id = task["id"]
            if task_id in self._running_tasks:
                continue
            interval = task.get("interval_seconds", 3600)
            if now - task.get("last_run", 0) < interval:
                continue
            self._running_tasks.add(task_id)
            try:
                result, error = await self._execute(task)
                self._record_result(task_id, last_run=now, result=result, error=error)
            finally:
                self._running_tasks.discard(task_id)

    async def _execute(self, task: Dict) -> tuple[str, str]:
        """执行具体任务"""
        task_type = task["type"]
        timeout = int(task.get("config", {}).get("timeout", self._EXECUTE_TIMEOUT))
        try:
            if task_type == "backup_db":
                await asyncio.wait_for(asyncio.to_thread(self._do_backup, task.get("config", {})), timeout=timeout)
            elif task_type == "restart_container":
                await asyncio.wait_for(self._do_restart(task.get("config", {})), timeout=timeout)
            elif task_type == "cleanup_logs":
                await asyncio.wait_for(asyncio.to_thread(self._do_cleanup, task.get("config", {})), timeout=timeout)
            else:
                return ("error", f"unsupported task type: {task_type}")
            logger.info("定时任务执行完成: %s (%s)", task["name"], task_type)
            return ("success", "")
        except asyncio.TimeoutError:
            logger.error("定时任务执行超时 %s: %ss", task["name"], timeout)
            return ("timeout", f"timeout>{timeout}s")
        except Exception as e:
            logger.error("定时任务执行失败 %s: %s", task["name"], e)
            return ("error", str(e))

    def _do_backup(self, config: Dict):
        """自动备份数据库"""
        if not os.path.exists(DB_PATH):
            return
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = DB_PATH + f".auto_{ts}"
        shutil.copy2(DB_PATH, dst)
        self._prune_auto_backups(config.get("keep_count", self._BACKUP_RETENTION_COUNT))

    def _prune_auto_backups(self, keep_count: int):
        keep_count = max(1, int(keep_count))
        backup_dir = os.path.dirname(DB_PATH)
        prefix = os.path.basename(DB_PATH) + ".auto_"
        backups = []
        for name in os.listdir(backup_dir):
            if not name.startswith(prefix):
                continue
            fpath = os.path.join(backup_dir, name)
            if os.path.isfile(fpath):
                backups.append((os.path.getmtime(fpath), fpath))
        backups.sort(reverse=True)
        for _, fpath in backups[keep_count:]:
            os.remove(fpath)

    async def _do_restart(self, config: Dict):
        """重启指定容器"""
        from starlette.concurrency import run_in_threadpool
        from services.docker_manager import docker_manager
        container_name = config.get("container_name", "")
        if container_name:
            await run_in_threadpool(docker_manager.action_container, container_name, "restart")

    def _do_cleanup(self, config: Dict):
        """清理旧备份文件"""
        keep_days = config.get("keep_days", 7)
        cutoff = time.time() - keep_days * 86400
        backup_dir = os.path.dirname(DB_PATH)
        for f in os.listdir(backup_dir):
            if f.startswith("app.db.auto_") or f.startswith("app.db.pre_restore_"):
                fpath = os.path.join(backup_dir, f)
                if os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)

    def _parse(self, row) -> Dict:
        d = dict(row)
        d["config"] = json.loads(d.get("config", "{}"))
        d["enabled"] = bool(d.get("enabled", 0))
        d["run_count"] = int(d.get("run_count", 0) or 0)
        d["last_result"] = d.get("last_result", "")
        d["last_error"] = d.get("last_error", "")
        return d


scheduler = Scheduler()

