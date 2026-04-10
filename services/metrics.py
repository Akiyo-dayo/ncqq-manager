"""
轻量级运行指标收集 — 纯内存计数器 + 速率计算

仅用于 /api/health 暴露关键观测指标，不引入外部依赖。
重启清零，不持久化。
"""
import time
import threading
from typing import Dict, Any


class _Counter:
    """线程安全的单调递增计数器 + 滑动窗口速率。"""

    __slots__ = ("_value", "_lock", "_window_start", "_window_count", "_rate")

    def __init__(self):
        self._value: int = 0
        self._lock = threading.Lock()
        self._window_start: float = time.monotonic()
        self._window_count: int = 0
        self._rate: float = 0.0

    def inc(self, n: int = 1):
        with self._lock:
            self._value += n
            self._window_count += n
            elapsed = time.monotonic() - self._window_start
            if elapsed >= 60.0:
                self._rate = self._window_count / elapsed
                self._window_start = time.monotonic()
                self._window_count = 0

    @property
    def value(self) -> int:
        return self._value

    @property
    def rate_per_min(self) -> float:
        """近似每分钟速率。"""
        with self._lock:
            elapsed = time.monotonic() - self._window_start
            if elapsed < 1.0:
                return self._rate * 60.0
            return (self._window_count / elapsed) * 60.0


class Metrics:
    """全局指标注册表。"""

    def __init__(self):
        self.op_log_writes = _Counter()
        self.op_log_flush_fails = _Counter()
        self.ws_broadcasts = _Counter()
        self.ws_broadcast_fails = _Counter()
        self.http_requests = _Counter()

    def snapshot(self) -> Dict[str, Any]:
        """导出当前指标快照（供 health endpoint 使用）。"""
        return {
            "op_log_writes": self.op_log_writes.value,
            "op_log_writes_rate": round(self.op_log_writes.rate_per_min, 2),
            "op_log_flush_fails": self.op_log_flush_fails.value,
            "ws_broadcasts": self.ws_broadcasts.value,
            "ws_broadcasts_rate": round(self.ws_broadcasts.rate_per_min, 2),
            "ws_broadcast_fails": self.ws_broadcast_fails.value,
            "http_requests": self.http_requests.value,
            "http_requests_rate": round(self.http_requests.rate_per_min, 2),
        }


metrics = Metrics()

