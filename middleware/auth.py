"""
认证中间件 - Token 管理 + 认证依赖 + 权限检查
SQLite 持久化，WAL 事务保证原子性，服务重启不丢失会话
"""
import uuid
import time
from fastapi import Request, HTTPException, Depends
from typing import Optional

from services.config import app_config
from services.log import logger
import services.database as db


_TOKEN_TTL = 86400 * 7  # 7天过期
_SLIDE_INTERVAL = 600   # 滑动过期：每 10 分钟刷新一次 last_active
_PERMISSION_SYNC_INTERVAL = 300  # 权限/用户名同步：每 5 分钟回查 users 表


def create_token(user: dict) -> str:
    """为用户创建会话 Token"""
    token = uuid.uuid4().hex
    now = time.time()
    db.execute(
        "INSERT INTO sessions (token,uuid,userName,permission,loginTime,created_at,last_active) VALUES (?,?,?,?,?,?,?)",
        (token, user["uuid"], user["userName"], user["permission"],
         time.strftime("%Y-%m-%d %H:%M:%S"), now, now),
    )
    db.commit()
    return token


def remove_token(token: str):
    """移除 Token (登出)"""
    db.execute("DELETE FROM sessions WHERE token=?", (token,))
    db.commit()


def remove_user_tokens(user_uuid: str):
    """移除指定用户的所有 Token"""
    db.execute("DELETE FROM sessions WHERE uuid=?", (user_uuid,))
    db.commit()


def cleanup_expired_tokens():
    """清理过期 Token"""
    cutoff = time.time() - _TOKEN_TTL
    cur = db.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
    if cur.rowcount > 0:
        db.commit()
        logger.debug("清理了 %d 个过期 Token", cur.rowcount)


def _validate_token(token: str) -> Optional[dict]:
    """验证 Token 并返回 session 信息，含滑动过期与权限同步。"""
    row = db.fetchone("SELECT * FROM sessions WHERE token=?", (token,))
    if not row:
        return None
    session = db.row_to_dict(row)
    now = time.time()
    # 检查过期
    if now - session["created_at"] > _TOKEN_TTL:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        db.commit()
        return None

    needs_slide_refresh = now - session.get("last_active", 0) > _SLIDE_INTERVAL
    needs_permission_sync = now - session.get("last_active", 0) > _PERMISSION_SYNC_INTERVAL

    if needs_permission_sync and session.get("uuid"):
        user_row = db.fetchone("SELECT userName, permission FROM users WHERE uuid=?", (session["uuid"],))
        if not user_row:
            db.execute("DELETE FROM sessions WHERE token=?", (token,))
            db.commit()
            return None
        user = db.row_to_dict(user_row)
        if (
            user.get("permission") != session.get("permission")
            or user.get("userName") != session.get("userName")
        ):
            db.execute(
                "UPDATE sessions SET userName=?, permission=?, last_active=? WHERE token=?",
                (user.get("userName", ""), user.get("permission", 0), now, token),
            )
            db.commit()
            session["userName"] = user.get("userName", "")
            session["permission"] = user.get("permission", 0)
            session["last_active"] = now
            return session

    # 滑动过期：定期刷新 last_active
    if needs_slide_refresh:
        db.execute("UPDATE sessions SET last_active=? WHERE token=?", (now, token))
        db.commit()
        session["last_active"] = now
    return session


def get_current_user(request: Request) -> dict:
    """
    核心认证依赖 - 支持多种认证方式:
    1. Cookie Token
    2. API Key (Header)
    """
    from services.user_manager import user_manager, ROLE

    # 1. Cookie Token 认证
    token = request.cookies.get("auth_token")
    if token:
        session = _validate_token(token)
        if session:
            if session.get("permission", 0) == ROLE.BAN:
                raise HTTPException(status_code=403, detail="Account banned")
            return session

    # 2. API Key 认证（仅请求头）
    api_key = request.headers.get("x-request-api-key")

    if api_key:
        configured_key = app_config.get("api_key")
        if configured_key and api_key == configured_key:
            return {"uuid": "cluster", "userName": "api_user", "permission": ROLE.ADMIN}
        user = user_manager.get_user_by_api_key(api_key)
        if user:
            return {
                "uuid": user["uuid"],
                "userName": user["userName"],
                "permission": user["permission"],
            }

    raise HTTPException(status_code=401, detail="Unauthorized")


def get_api_key_user(request: Request) -> dict:
    """仅允许 x-request-api-key 认证（插件对接专用）。"""
    from services.user_manager import user_manager, ROLE

    api_key = request.headers.get("x-request-api-key", "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="MISSING_API_KEY")

    if api_key == app_config.get("api_key"):
        return {"uuid": "cluster", "userName": "api_user", "permission": ROLE.ADMIN}

    user = user_manager.get_user_by_api_key(api_key)
    if not user:
        raise HTTPException(status_code=401, detail="INVALID_API_KEY")
    if user.get("permission", 0) == ROLE.BAN:
        raise HTTPException(status_code=403, detail="ACCOUNT_BANNED")

    return {
        "uuid": user["uuid"],
        "userName": user["userName"],
        "permission": user["permission"],
    }


def validate_token_value(token: str) -> Optional[dict]:
    """直接校验 token 字符串（供 WebSocket 鉴权使用）"""
    return _validate_token(token)



def get_optional_user(request: Request) -> Optional[dict]:
    """可选认证 — 未登录时返回 None 而非抛 401"""
    try:
        return get_current_user(request)
    except HTTPException:
        return None


def require_admin(session: dict = Depends(get_current_user)) -> dict:
    """要求管理员权限"""
    from services.user_manager import ROLE
    if session.get("permission", 0) < ROLE.ADMIN:
        raise HTTPException(status_code=403, detail="Admin permission required")
    return session


def require_user(session: dict = Depends(get_current_user)) -> dict:
    """要求至少普通用户权限"""
    from services.user_manager import ROLE
    if session.get("permission", 0) < ROLE.USER:
        raise HTTPException(status_code=403, detail="User permission required")
    return session


def check_instance_permission(session: dict, node_id: str, container_name: str) -> bool:
    """检查用户是否有指定实例的操作权限"""
    from services.user_manager import user_manager, ROLE
    if session.get("permission", 0) >= ROLE.ADMIN:
        return True
    user = user_manager.get_user_by_uuid(session["uuid"])
    if not user:
        return False
    return user_manager.has_instance(user, node_id, container_name)

