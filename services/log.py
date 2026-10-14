"""
统一日志模块 - 替代全项目的 print()
内含环形内存缓冲区，供 Web 控制台读取节点程序日志。
"""
import logging
import sys
from collections import deque
from typing import List


# ============ 内存环形缓冲 Handler ============

class MemoryLogHandler(logging.Handler):
    """将日志写入内存 deque，供 API 层实时读取。"""

    def __init__(self, max_lines: int = 2000, level: int = logging.DEBUG):
        super().__init__(level)
        self._buffer: deque = deque(maxlen=max_lines)

    def emit(self, record: logging.LogRecord):
        try:
            self._buffer.append(self.format(record))
        except Exception:
            pass

    def get_logs(self, lines: int = 500) -> List[str]:
        """返回最近 N 行日志（从旧到新）"""
        buf = list(self._buffer)
        return buf[-lines:] if lines < len(buf) else buf


# 全局唯一内存 Handler 实例
_memory_handler = MemoryLogHandler(max_lines=2000, level=logging.DEBUG)
_log_fmt = logging.Formatter(
    "[%(asctime)s] [%(name)s/%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
_memory_handler.setFormatter(_log_fmt)


def setup_logger(name: str = "ncqq", level: int = logging.INFO) -> logging.Logger:
    _logger = logging.getLogger(name)
    if _logger.handlers:
        return _logger
    _logger.setLevel(level)

    # 控制台输出
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(_log_fmt)
    _logger.addHandler(handler)

    # 内存缓冲（Web 控制台读取）
    _logger.addHandler(_memory_handler)
    return _logger


def attach_memory_handler_to(logger_name: str):
    """为第三方 logger（如 uvicorn）挂载内存 Handler"""
    target = logging.getLogger(logger_name)
    if _memory_handler not in target.handlers:
        target.addHandler(_memory_handler)


def get_node_logs(lines: int = 500) -> str:
    """供 API 层调用：读取节点程序日志"""
    return "\n".join(_memory_handler.get_logs(lines))


logger = setup_logger()

