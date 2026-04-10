"""
操作审计日志系统 - SQLite 持久化
缓冲区批量写入 + 事务保证
"""
import json
import uuid
import time
from typing import List, Dict, Any, Optional
from collections import deque

from services.log import logger
from services.metrics import metrics
import services.database as db


class OperationLogger:
    """操作日志记录器 - 缓冲写入 + DB 读取"""

    def __init__(self, buffer_size: int = 10):
        self._buffer: deque = deque(maxlen=buffer_size)
        self._flush_fail_count: int = 0
        self._last_flush_duration: float = 0.0

    def log(self, operation_type: str, payload: Dict[str, Any],
            level: str = "info") -> str:
        """记录一条操作日志"""
        operation_id = uuid.uuid4().hex[:16]
        entry = {
            "id": operation_id,
            "type": operation_type,
            "level": level,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": int(time.time()),
            "operator_name": payload.get("operator_name"),
            "operator_uuid": payload.get("operator_uuid"),
            "operator_ip": payload.get("operator_ip"),
            "payload": payload,
        }
        self._buffer.append(entry)
        metrics.op_log_writes.inc()
        if len(self._buffer) >= self._buffer.maxlen:
            self.flush()
        return operation_id

    def info(self, operation_type: str, payload: Dict[str, Any]) -> str:
        return self.log(operation_type, payload, "info")

    def warning(self, operation_type: str, payload: Dict[str, Any]) -> str:
        return self.log(operation_type, payload, "warning")

    def error(self, operation_type: str, payload: Dict[str, Any]) -> str:
        return self.log(operation_type, payload, "error")

    @staticmethod
    def _normalize_limit(limit: int) -> int:
        return limit if 1 <= limit <= 200 else 50

    @staticmethod
    def _normalize_page(page: int) -> int:
        return page if page >= 1 else 1

    @staticmethod
    def _normalize_time_bound(value: Optional[int]) -> Optional[int]:
        return value if isinstance(value, int) and value >= 0 else None

    _FLUSH_MAX_RETRIES = 2
    _FLUSH_RETRY_DELAY = 0.3  # 秒

    def flush(self):
        """将缓冲区写入 SQLite，带重试与失败计数。"""
        if not self._buffer:
            return
        start = time.monotonic()
        snapshot = list(self._buffer)
        for attempt in range(1, self._FLUSH_MAX_RETRIES + 1):
            try:
                for entry in snapshot:
                    db.execute(
                        "INSERT OR IGNORE INTO operation_logs "
                        "(id,type,level,time,timestamp,operator_name,operator_uuid,operator_ip,payload) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (entry["id"], entry["type"], entry["level"],
                         entry["time"], entry["timestamp"],
                         entry.get("operator_name"), entry.get("operator_uuid"), entry.get("operator_ip"),
                         json.dumps(entry["payload"], ensure_ascii=False)),
                    )
                db.commit()
                # 写入成功，清除已写条目
                for _ in range(len(snapshot)):
                    if self._buffer:
                        self._buffer.popleft()
                self._last_flush_duration = time.monotonic() - start
                return
            except Exception as e:
                if attempt < self._FLUSH_MAX_RETRIES:
                    logger.warning("操作日志写入失败(第%d次重试): %s", attempt, e)
                    time.sleep(self._FLUSH_RETRY_DELAY)
                else:
                    self._flush_fail_count += 1
                    metrics.op_log_flush_fails.inc()
                    self._last_flush_duration = time.monotonic() - start
                    logger.error("操作日志写入最终失败(已重试%d次): %s", self._FLUSH_MAX_RETRIES, e)

    @property
    def flush_fail_count(self) -> int:
        return self._flush_fail_count

    @property
    def last_flush_duration(self) -> float:
        return self._last_flush_duration

    def get(
        self,
        limit: int = 50,
        page: int = 1,
        operator: str = "",
        operation_type: str = "",
        level: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> Dict[str, Any]:
        """获取操作日志（缓冲区 + DB），支持筛选与分页。"""
        limit = self._normalize_limit(limit)
        page = self._normalize_page(page)
        start_time = self._normalize_time_bound(start_time)
        end_time = self._normalize_time_bound(end_time)

        if start_time is not None and end_time is not None and start_time > end_time:
            start_time, end_time = end_time, start_time

        combined = self._get_combined_items(
            operator=operator,
            operation_type=operation_type,
            level=level,
            start_time=start_time,
            end_time=end_time,
        )
        total = len(combined)
        start_index = (page - 1) * limit
        end_index = start_index + limit
        logs = combined[start_index:end_index]
        return {
            "logs": logs,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit if total else 0,
            },
            "filters": {
                "operator": operator,
                "type": operation_type,
                "level": level,
                "start_time": start_time,
                "end_time": end_time,
            },
        }

    def _get_combined_items(
        self,
        operator: str = "",
        operation_type: str = "",
        level: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict]:
        buffer_items = list(self._buffer)
        db_items = self._query_db(
            operator=operator,
            operation_type=operation_type,
            level=level,
            start_time=start_time,
            end_time=end_time,
        )
        buf_flat = self._flatten(buffer_items)
        filtered_buffer = self._filter_items(
            buf_flat,
            operator=operator,
            operation_type=operation_type,
            level=level,
            start_time=start_time,
            end_time=end_time,
        )
        db_ids = {item.get("id") for item in db_items}
        combined = db_items + [item for item in filtered_buffer if item.get("id") not in db_ids]
        combined.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return combined

    def _query_db(
        self,
        operator: str = "",
        operation_type: str = "",
        level: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict]:
        where_clauses = []
        params: list[Any] = []
        if operator:
            where_clauses.append("(operator_name LIKE ? OR operator_uuid LIKE ?)")
            like = f"%{operator}%"
            params.extend([like, like])
        if operation_type:
            where_clauses.append("type = ?")
            params.append(operation_type)
        if level:
            where_clauses.append("level = ?")
            params.append(level)
        if start_time is not None:
            where_clauses.append("timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            where_clauses.append("timestamp <= ?")
            params.append(end_time)

        sql = "SELECT * FROM operation_logs"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)
        sql += " ORDER BY timestamp DESC"

        rows = db.fetchall(sql, tuple(params))
        items = []
        for row in rows:
            entry = dict(row)
            entry["payload"] = json.loads(entry.get("payload", "{}"))
            items.append(entry)
        return self._flatten(items)

    @staticmethod
    def _filter_items(
        items: List[Dict],
        operator: str = "",
        operation_type: str = "",
        level: str = "",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict]:
        operator_lower = operator.lower().strip()
        result = []
        for item in items:
            if operator_lower:
                name = str(item.get("operator_name") or "").lower()
                operator_uuid = str(item.get("operator_uuid") or "").lower()
                if operator_lower not in name and operator_lower not in operator_uuid:
                    continue
            if operation_type and item.get("type") != operation_type:
                continue
            if level and item.get("level") != level:
                continue
            timestamp = item.get("timestamp", 0)
            if start_time is not None and timestamp < start_time:
                continue
            if end_time is not None and timestamp > end_time:
                continue
            result.append(item)
        return result

    @staticmethod
    def _flatten(items: List[Dict]) -> List[Dict]:
        """将嵌套 payload 展平到顶层（兼容前端读取）"""
        result = []
        for entry in items:
            flat = {k: v for k, v in entry.items() if k != "payload"}
            if isinstance(entry.get("payload"), dict):
                flat.update(entry["payload"])
            if "operator" not in flat:
                if "operator_name" in flat:
                    flat["operator"] = flat["operator_name"]
                elif "admin_user" in flat:
                    flat["operator"] = flat["admin_user"]
                elif "target_user_name" in flat:
                    flat["operator"] = flat["target_user_name"]
            result.append(flat)
        return result


# 全局单例
operation_logger = OperationLogger()

