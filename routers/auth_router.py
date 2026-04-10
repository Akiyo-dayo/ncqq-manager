"""
认证路由 - 登录/登出/状态检查/首次初始化
"""
import os
import socket
from typing import Optional
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from middleware.auth import (
    get_current_user, create_token, remove_token, get_optional_user,
)
from services.user_manager import user_manager
from services.config import app_config
from services.operation_logger import operation_logger
from services.log import logger

router = APIRouter(prefix="/api", tags=["auth"])

# 生产环境建议设置 COOKIE_SECURE=true（需要 HTTPS）
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
# Cookie SameSite 策略：默认 lax，可通过环境变量覆盖（none / lax / strict）
_COOKIE_SAMESITE_RAW = os.environ.get("COOKIE_SAMESITE", "lax").lower().strip()
_COOKIE_SAMESITE: str = _COOKIE_SAMESITE_RAW if _COOKIE_SAMESITE_RAW in ("none", "lax", "strict") else "lax"


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def api_login(req: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not user_manager.check_ban_ip(ip):
        return JSONResponse(
            {"status": "error", "message": "IP banned due to too many failures"},
            status_code=403,
        )

    user = user_manager.check_login(req.username, req.password)
    if user:
        user_manager.clear_login_failure(ip)
        token = create_token(user)
        operation_logger.info("user_login", {
            "operator_ip": ip,
            "operator_name": user["userName"],
            "operator_uuid": user["uuid"],
        })
        response = JSONResponse({
            "status": "ok",
            "message": "Authenticated",
            "user": {
                "uuid": user["uuid"],
                "userName": user["userName"],
                "permission": user["permission"],
            },
        })
        response.set_cookie(
            key="auth_token", value=token,
            max_age=86400 * 7, httponly=True, samesite=_COOKIE_SAMESITE,
            secure=_COOKIE_SECURE,
        )
        return response

    user_manager.record_login_failure(ip)
    operation_logger.warning("user_login_failed", {
        "operator_ip": ip,
        "operator_name": req.username,
        "target_user_name": req.username,
    })
    return JSONResponse(
        {"status": "error", "message": "Invalid credentials"},
        status_code=401,
    )


@router.post("/logout")
async def api_logout(request: Request):
    token = request.cookies.get("auth_token")
    session = get_optional_user(request)
    if token:
        remove_token(token)
    operation_logger.info("user_logout", {
        "operator_ip": request.client.host if request.client else "unknown",
        "operator_name": session.get("userName") if session else None,
        "operator_uuid": session.get("uuid") if session else None,
    })
    response = JSONResponse({"status": "ok", "message": "Logged out"})
    response.delete_cookie("auth_token")
    return response


@router.get("/auth/status")
async def api_auth_status(session: dict = Depends(get_current_user)):
    return {
        "status": "ok",
        "user": {
            "uuid": session["uuid"],
            "userName": session["userName"],
            "permission": session["permission"],
        },
    }


# ============ 首次初始化设置 ============

def _get_local_ip() -> str:
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class SetupRequest(BaseModel):
    admin_username: str
    admin_password: str
    host: str = "0.0.0.0"
    port: int = 8000
    data_dir: Optional[str] = None


@router.get("/setup/status")
async def api_setup_status():
    """检查系统是否已完成首次初始化"""
    initialized = app_config.get("initialized", False)

    # 兼容旧版本升级：如果 DB 中已有用户但 config 中未标记 initialized，
    # 则自动标记为已初始化（旧版本用默认 admin/admin 创建过用户）
    if not initialized:
        import services.database as db
        row = db.fetchone("SELECT 1 FROM users LIMIT 1")
        if row:
            app_config.set("initialized", True)
            initialized = True

    return {
        "status": "ok",
        "initialized": initialized,
        "local_ip": _get_local_ip(),
        "default_data_dir": app_config.get("data_dir", ""),
        "default_port": app_config.get("port", 8000),
    }


@router.post("/setup/init")
async def api_setup_init(req: SetupRequest, request: Request):
    """首次初始化系统 — 设置管理员账号和运行参数"""
    if app_config.get("initialized", False):
        return JSONResponse(
            {"status": "error", "message": "System already initialized"},
            status_code=400,
        )

    if not req.admin_username or not req.admin_password:
        return JSONResponse(
            {"status": "error", "message": "Username and password are required"},
            status_code=400,
        )

    if len(req.admin_password) < 6:
        return JSONResponse(
            {"status": "error", "message": "Password must be at least 6 characters"},
            status_code=400,
        )

    # 创建管理员账号
    from services.user_manager import user_manager, ROLE
    user = user_manager.create_user(
        username=req.admin_username,
        password=req.admin_password,
        permission=ROLE.ADMIN,
    )
    if not user:
        return JSONResponse(
            {"status": "error", "message": "Failed to create admin user"},
            status_code=500,
        )

    # 更新配置
    updates = {
        "initialized": True,
        "host": req.host,
        "port": req.port,
    }
    if req.data_dir:
        updates["data_dir"] = req.data_dir
    app_config.update(updates)

    ip = request.client.host if request.client else "unknown"
    operation_logger.info("system_initialized", {
        "operator_ip": ip,
        "operator_name": req.admin_username,
        "admin_user": req.admin_username,
        "host": req.host,
        "port": req.port,
    })
    logger.info("系统初始化完成: admin=%s, host=%s, port=%d", req.admin_username, req.host, req.port)

    # 自动登录
    token = create_token(user)
    response = JSONResponse({
        "status": "ok",
        "message": "System initialized successfully",
        "user": {
            "uuid": user["uuid"],
            "userName": user["userName"],
            "permission": user["permission"],
        },
    })
    response.set_cookie(
        key="auth_token", value=token,
        max_age=86400 * 7, httponly=True, samesite=_COOKIE_SAMESITE,
        secure=_COOKIE_SECURE,
    )
    return response

