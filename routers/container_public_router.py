"""
容器公开查询路由 - 公开容器列表 / QR 批量状态 / 分页查询
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from middleware.rate_limiter import public_speed_limit

from services.container_state import state_engine
from services.instance_subsystem import instance_subsystem

router = APIRouter(prefix="/api", tags=["containers"])


@router.get("/public/containers", dependencies=[Depends(public_speed_limit(0.5))])
async def api_public_containers():
    """公开容器列表 — 从状态引擎读内存快照，零阻塞。
    响应包含 bot_avatar 字段，ncqq 插件可直接使用，无需额外请求。
    """
    containers = state_engine.get_containers()
    result = []
    for container in containers:
        result.append({
            "id": container.get("id", ""),
            "name": container["name"],
            "status": container["status"],
            "display_status": container.get("display_status", container.get("status", "")),
            "node_id": container.get("node_id", "local"),
            "uin": container.get("uin", ""),
            "last_uin": container.get("last_uin", ""),
            "configured_uin": container.get("configured_uin", ""),
            "login_stage": container.get("login_stage", ""),
            "action_phase": container.get("action_phase", ""),
            "action": container.get("action", ""),
            "operation_id": container.get("operation_id", ""),
            "action_started_at": container.get("action_started_at", 0),
            "action_error": container.get("action_error", ""),
            "bot_online": container.get("bot_online", False),
            "bot_heartbeat_ts": container.get("bot_heartbeat_ts", 0),
            "bot_avatar": container.get("bot_avatar", ""),
        })
    return {"status": "ok", "containers": result}


@router.get("/public/qr/batch", dependencies=[Depends(public_speed_limit(0.5))])
async def api_batch_qr_status(_t: str | None = None):
    """批量获取所有容器的 QR 状态（公开版）— 不包含二维码图片数据，仅返回阶段信息。"""
    return JSONResponse(
        content={"status": "ok", "items": state_engine.get_qr_states_public()},
        headers={
            "Cache-Control": "no-store, no-cache, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/public/containers/page", dependencies=[Depends(public_speed_limit(0.5))])
async def api_paged_containers(
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    keyword: str | None = None,
):
    """分页查询容器列表 — 纯内存操作。"""
    return instance_subsystem.query(
        status=status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )

