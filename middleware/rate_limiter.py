"""
速率限制中间件 — per-user per-endpoint 限速，内存 TTL，管理员豁免。
纯内存态，重启清零；单实例部署下风险可接受。
"""
import time
from typing import Dict, Tuple
from fastapi import Request, HTTPException, Depends, WebSocket

from services.log import logger


class RateLimiter:
    """内存速率限制器，重启清零。"""

    def __init__(self):
        # key: (user_uuid, path) -> expire_time
        self._records: Dict[Tuple[str, str], float] = {}
        self._last_cleanup = time.time()

    def check(self, user_uuid: str, path: str, seconds: float) -> bool:
        """检查是否被限速。返回 True 表示允许通过"""
        self._maybe_cleanup()
        key = (user_uuid, path)
        now = time.time()
        expire = self._records.get(key, 0)
        if now < expire:
            return False
        self._records[key] = now + seconds
        return True

    def remaining(self, user_uuid: str, path: str) -> float:
        """返回剩余冷却秒数"""
        key = (user_uuid, path)
        now = time.time()
        expire = self._records.get(key, 0)
        return max(0, expire - now)

    def _maybe_cleanup(self):
        """每 60 秒清理一次过期记录"""
        now = time.time()
        if now - self._last_cleanup < 60:
            return
        self._last_cleanup = now
        expired = [k for k, v in self._records.items() if now >= v]
        for k in expired:
            del self._records[k]


rate_limiter = RateLimiter()


def speed_limit(seconds: float = 3.0, admin_exempt: bool = True):
    """
    速率限制依赖工厂。
    用法: @app.post("/xxx", dependencies=[Depends(speed_limit(3.0))])
    """
    from middleware.auth import get_current_user
    from services.user_manager import ROLE

    async def _limiter(request: Request, session: dict = Depends(get_current_user)):
        if admin_exempt and session.get("permission", 0) >= ROLE.ADMIN:
            return

        user_uuid = session.get("uuid", "anonymous")
        path = request.url.path

        if not rate_limiter.check(user_uuid, path, seconds):
            remaining = rate_limiter.remaining(user_uuid, path)
            raise HTTPException(
                status_code=429,
                detail=f"操作过于频繁，请 {remaining:.0f} 秒后重试",
            )

    return _limiter


def public_speed_limit(seconds: float = 1.0):
    """公开接口速率限制依赖工厂，按 client ip + path 限速。"""

    async def _limiter(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        key = f"ip:{client_ip}"
        path = request.url.path
        if not rate_limiter.check(key, path, seconds):
            remaining = rate_limiter.remaining(key, path)
            raise HTTPException(
                status_code=429,
                detail=f"操作过于频繁，请 {remaining:.0f} 秒后重试",
            )

    return _limiter


def websocket_public_speed_limit(seconds: float = 1.0):
    """公开 WebSocket 握手前限速，按 client ip + path。"""

    async def _check(ws: WebSocket) -> bool:
        client_ip = ws.client.host if ws.client else "unknown"
        key = f"ip:{client_ip}"
        path = ws.url.path
        if rate_limiter.check(key, path, seconds):
            return True
        return False

    return _check

