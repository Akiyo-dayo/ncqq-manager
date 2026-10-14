"""
WebSocket 连接管理器 - 实时事件推送
支持多客户端连接，容器状态变更广播
"""
import json
import asyncio
from typing import Dict, Set
from fastapi import WebSocket
from services.log import logger
from services.metrics import metrics


class WSManager:
    """管理所有 WebSocket 连接，支持分组广播"""

    def __init__(self):
        self._connections: Set[WebSocket] = set()
        # 管理端与公开端各自计数：过去共用一个集合，管理员每开一个标签页
        # 就吃掉一个匿名用户的名额，标签页一多匿名用户全部被拒。
        self._admin: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket, admin: bool = False):
        await ws.accept()
        async with self._lock:
            self._connections.add(ws)
            if admin:
                self._admin.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._connections.discard(ws)
            self._admin.discard(ws)

    async def connect_if_available(self, ws: WebSocket, limit: int) -> bool:
        """接受一条公开连接，名额只按公开连接数计算。"""
        await ws.accept()
        async with self._lock:
            public_count = len(self._connections) - len(self._admin)
            if public_count >= limit:
                return False
            self._connections.add(ws)
            return True

    async def broadcast(self, event_type: str, data: dict):
        """广播事件给所有连接的客户端"""
        message = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        async with self._lock:
            connections = list(self._connections)

        dead = []
        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)

        metrics.ws_broadcasts.inc()
        if dead:
            metrics.ws_broadcast_fails.inc(len(dead))
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


ws_manager = WSManager()

