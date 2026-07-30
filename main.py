"""
NapCat QQ Manager - 精简入口点
所有路由已拆分到 routers/ 目录，服务层在 services/，中间件在 middleware/
"""
import os
import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

from services.log import logger
from services.config import FRONTEND_DIST, APP_VERSION
from services.operation_logger import operation_logger
from middleware.auth import cleanup_expired_tokens
import services.database as database

from routers.auth_router import router as auth_router
from routers.user_router import router as user_router
from routers.container_public_router import router as container_public_router
from routers.container_config_router import router as container_config_router
from routers.container_crud_router import router as container_crud_router
from routers.container_runtime_router import router as container_runtime_router
from routers.node_router import router as node_router
from routers.operation_logs_router import router as operation_logs_router
from routers.image_router import router as image_router
from routers.ws_router import router as ws_router
from routers.alert_router import router as alert_router
from routers.backup_router import router as backup_router
from routers.scheduler_router import router as scheduler_router
from routers.resource_router import router as resource_router
from routers.bot_radar_router import router as bot_radar_router
from routers.bot_api_router import router as bot_api_router
from routers.registration_router import router as registration_router


# ============ 生命周期管理 ============

import asyncio
from services.daemon_monitor import daemon_monitor

async def background_monitor():
    _gc_counter = 0
    _session_gc_counter = 0
    while True:
        daemon_monitor.record_tick()
        _gc_counter += 1
        _session_gc_counter += 1
        # 每 120 次 tick（约 1 小时）执行一次 bot_heartbeat GC
        if _gc_counter >= 120:
            try:
                from services.bot_heartbeat import bot_heartbeat
                bot_heartbeat.gc()
            except Exception as e:
                logger.debug("bot_heartbeat GC 异常: %s", e)
            _gc_counter = 0
        # action job 的 GC 过去只在新建 job 时触发，之后没人再操作就永远不回收 ——
        # 一个卡住的 job 会无限期把容器卡片钉在「重启中」上。
        try:
            from services.action_jobs import action_job_manager
            action_job_manager.gc()
        except Exception as e:
            logger.debug("action job GC 异常: %s", e)
        # 每 360 次 tick（约 3 小时）清理过期 session，防止 sessions 表膨胀
        if _session_gc_counter >= 360:
            try:
                cleanup_expired_tokens()
            except Exception:
                pass
            _session_gc_counter = 0
        await asyncio.sleep(30)

async def background_flush_logs():
    """定时将操作日志缓冲区写入磁盘，防止异常退出丢失"""
    while True:
        await asyncio.sleep(60)
        operation_logger.flush()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭生命周期"""
    # SQLite 初始化
    database.init_db()

    # 加载运行时配置（从 SQLite settings 表）
    from services.config import app_config
    app_config.load_runtime_once()

    # 仅在已完成初始化设置时才确保默认管理员存在
    # 首次部署时由 /api/setup/init 端点创建管理员
    if app_config.get("initialized", False):
        from services.user_manager import user_manager
        user_manager.ensure_default_admin()

    # 启动时同步节点 key
    from services.cluster_manager import cluster_manager
    cluster_manager.init()

    # 启动集群管理器 aiohttp 连接池（远程节点异步通信）
    await cluster_manager.start_session()

    # 启动时清理过期 token
    cleanup_expired_tokens()

    logger.info("NapCat QQ Manager 启动中...")
    logger.info("前端路径: %s", FRONTEND_DIST)

    # COOKIE_SECURE 安全检查
    import os as _os
    _cookie_secure = _os.environ.get("COOKIE_SECURE", "false").lower() in ("1", "true", "yes")
    if not _cookie_secure:
        logger.warning("COOKIE_SECURE=false — HTTPS 部署下请设置 COOKIE_SECURE=true 以防止 cookie 明文传输")

    # 将 uvicorn 日志也接入内存缓冲区（Web 控制台可查看）
    from services.log import attach_memory_handler_to
    for uvi_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        attach_memory_handler_to(uvi_name)

    # 启动 Daemon 监控任务 (CPU/MEM 10分钟平均使用率)
    monitor_task = asyncio.create_task(background_monitor())
    # 启动操作日志定时刷盘任务
    flush_task = asyncio.create_task(background_flush_logs())

    # 启动异步登录检测器（aiohttp 连接池）
    from services.docker_async import async_login_checker, async_docker_manager
    await async_login_checker.start()

    # 启动异步Docker管理器（aiodocker — 替代 docker-py 热路径）
    await async_docker_manager.start()

    # 启动容器状态引擎（后台异步刷新，API/WS 零阻塞读内存）
    from services.container_state import state_engine
    from services.action_jobs import action_job_manager
    action_job_manager.set_notify_callback(state_engine.notify_change)
    await state_engine.start()

    # 启动 Docker 事件监听（事件驱动替代定时轮询）
    from services.docker_events import docker_event_watcher
    docker_event_watcher.start(notify_fn=state_engine.notify_change)

    # 启动定时任务调度器
    from services.scheduler import scheduler
    await scheduler.start()

    # 注入主事件循环引用到 docker_manager（供线程池回调中 fire-and-forget 注入使用）
    from services.docker_manager import set_main_event_loop
    set_main_event_loop(asyncio.get_running_loop())

    yield

    # 关闭时清理
    monitor_task.cancel()
    flush_task.cancel()
    try:
        await asyncio.gather(monitor_task, flush_task, return_exceptions=True)
    except Exception:
        pass
    docker_event_watcher.stop()
    try:
        action_job_manager.set_notify_callback(None)
    except Exception:
        pass
    await state_engine.stop()
    await async_docker_manager.stop()
    await async_login_checker.stop()
    await cluster_manager.stop_session()
    await scheduler.stop()
    operation_logger.flush()
    cleanup_expired_tokens()
    database.close_db()
    logger.info("NapCat QQ Manager 已关闭")


# ============ 创建 FastAPI 应用 ============

app = FastAPI(
    title="NapCatQQ Manager API",
    description="NapCat QQ Bot Docker 容器管理面板",
    version=APP_VERSION,
    lifespan=lifespan,
)

# CORS 中间件
# allow_origins=["*"] 与 allow_credentials=True 不可同时使用（浏览器规范）
# 开发环境默认允许 localhost；生产环境应通过环境变量 CORS_ORIGINS 指定
_cors_origins_env = os.environ.get("CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else ["http://localhost:5173", "http://localhost:8000", "http://127.0.0.1:5173", "http://127.0.0.1:8000"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF 防护中间件 — 对 Cookie 认证的写操作要求 X-Requested-With 头
# API Key 认证（x-request-api-key）和安全方法（GET/HEAD/OPTIONS）豁免
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method not in _CSRF_SAFE_METHODS:
            has_cookie = "auth_token" in request.cookies
            has_api_key = request.headers.get("x-request-api-key")
            # 仅对 Cookie 认证的写操作校验 CSRF 头
            if has_cookie and not has_api_key:
                xhr = request.headers.get("x-requested-with", "")
                if xhr.lower() != "xmlhttprequest":
                    return JSONResponse(
                        {"status": "error", "message": "CSRF validation failed"},
                        status_code=403,
                    )
        return await call_next(request)

app.add_middleware(CSRFMiddleware)


class NoCacheAPIMiddleware(BaseHTTPMiddleware):
    """API 响应禁止浏览器缓存 — 防止前端读到旧数据。"""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

app.add_middleware(NoCacheAPIMiddleware)

# Gzip 压缩中间件 — 对 >500B 的响应自动压缩（API JSON + 静态资源，传输量 -60%）
app.add_middleware(GZipMiddleware, minimum_size=500)

# ============ 注册路由 ============

app.include_router(auth_router)
app.include_router(user_router)
app.include_router(container_public_router)
app.include_router(container_config_router)
app.include_router(container_crud_router)
app.include_router(container_runtime_router)
app.include_router(node_router)
app.include_router(operation_logs_router)
app.include_router(image_router)
app.include_router(ws_router)
app.include_router(alert_router)
app.include_router(backup_router)
app.include_router(scheduler_router)
app.include_router(resource_router)
app.include_router(bot_radar_router)
app.include_router(bot_api_router)
app.include_router(registration_router)


# ============ 全局异常处理器 ============

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """兜底异常处理器，避免向客户端暴露内部堆栈信息"""
    logger.error("未处理异常 [%s %s]: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "内部服务器错误，请稍后重试"},
    )


# ============ 健康检查 ============

import time as _time
_start_time = _time.time()


@app.get("/api/health")
async def health_check():
    """健康检查端点，供负载均衡器/Docker HEALTHCHECK 使用。"""
    from services.docker_manager import docker_manager
    from services.container_state import state_engine
    from services.docker_async import async_docker_manager
    from services.ws_manager import ws_manager
    from services.scheduler import scheduler
    from services.metrics import metrics

    scheduler_tasks = scheduler.list_tasks()
    scheduler_failed_count = sum(1 for t in scheduler_tasks if t.get("last_result") == "error")
    scheduler_timeout_count = sum(1 for t in scheduler_tasks if t.get("last_result") == "timeout")
    last_task = max(scheduler_tasks, key=lambda x: x.get("last_run", 0), default=None)

    state_health = state_engine.health_info

    degraded_reasons = []
    if not async_docker_manager.connected:
        degraded_reasons.append("async_docker_disconnected")
    if not state_health.get("running", False):
        degraded_reasons.append("state_engine_not_running")
    if scheduler_failed_count > 0:
        degraded_reasons.append("scheduler_task_failed")
    if scheduler_timeout_count > 0:
        degraded_reasons.append("scheduler_task_timeout")

    return {
        "status": "degraded" if degraded_reasons else "ok",
        "degraded_reasons": degraded_reasons,
        "docker": docker_manager.client is not None,
        "uptime": round(_time.time() - _start_time, 1),
        "state_engine": state_health,
        "async_docker": async_docker_manager.connected,
        "ws_public": ws_manager.connection_count,
        "operation_logger_buffer": len(getattr(operation_logger, "_buffer", [])),
        "operation_logger_flush_fails": operation_logger.flush_fail_count,
        "operation_logger_last_flush_ms": round(operation_logger.last_flush_duration * 1000, 1),
        "metrics": metrics.snapshot(),
        "scheduler": {
            "total": len(scheduler_tasks),
            "failed": scheduler_failed_count,
            "timeout": scheduler_timeout_count,
            "last_task": {
                "id": last_task.get("id"),
                "name": last_task.get("name"),
                "last_run": last_task.get("last_run"),
                "last_result": last_task.get("last_result"),
                "last_error": last_task.get("last_error"),
            } if last_task else None,
        },
    }


# ============ 静态文件挂载 ============
# 注意: /data 目录不再静态挂载，所有文件访问通过已鉴权的 /api/containers/{name}/files 端点

if os.path.exists(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="frontend_assets")

# 本地资源目录（登录页图片、背景图等）
RESOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource")
if os.path.isdir(RESOURCE_DIR):
    app.mount("/resource", StaticFiles(directory=RESOURCE_DIR), name="resource_assets")


# ============ 使用手册 ============

DOCS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")

@app.get("/manual")
async def serve_manual():
    """提供本地使用手册页面"""
    manual_path = os.path.join(DOCS_DIR, "manual.html")
    if os.path.exists(manual_path):
        return FileResponse(manual_path, media_type="text/html")
    return HTMLResponse("<html><body><h1>Manual not found.</h1></body></html>", status_code=404)


# ============ SPA 前端路由 (Catch-all) ============

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    """所有未匹配的路由返回前端 SPA"""
    # 未匹配的 /api/* 必须返回 JSON 404，不能落到 SPA。否则调用方拿到的是一整页
    # HTML，JSON 解析失败后报出的错误跟真正的原因（路径不存在）毫无关系。
    if full_path.startswith("api/"):
        return JSONResponse(
            {"status": "error", "detail": f"接口不存在: /{full_path}"},
            status_code=404,
        )
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return HTMLResponse(
        "<html><body><h1>Frontend not built yet. Run npm run build in frontend folder.</h1></body></html>"
    )


# ============ 应用入口 ============

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
