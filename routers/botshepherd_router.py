"""
BotShepherd 集成路由 - 初始化/启停/状态/连接管理/账号管理
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from middleware.auth import require_admin
from services.botshepherd import botshepherd_manager
from services.bs_activation_service import bs_activation_service

router = APIRouter(prefix="/api/botshepherd", tags=["botshepherd"])


@router.get("/status")
async def get_status(_user=Depends(require_admin)):
    return botshepherd_manager.status()


@router.get("/logs")
async def get_bs_logs(
    lines: int = Query(100, ge=1, le=500), _user=Depends(require_admin)
):
    """获取 BotShepherd 进程控制台输出（最近 N 行）"""
    return {"status": "ok", "logs": botshepherd_manager.read_logs(lines)}


@router.post("/setup")
async def run_setup(_user=Depends(require_admin)):
    return await botshepherd_manager.setup()


@router.post("/start")
async def start_service(_user=Depends(require_admin)):
    result = botshepherd_manager.start()
    # BS 启动成功后自动拉起连接健康监控
    if result.get("status") == "ok" and not bs_activation_service._running:
        await bs_activation_service.start()
    return result


@router.post("/stop")
async def stop_service(_user=Depends(require_admin)):
    # 停止 BS 前先停止健康监控（避免监控空转报错）
    if bs_activation_service._running:
        await bs_activation_service.stop()
    return botshepherd_manager.stop()


# ---- 连接管理 ----


@router.get("/connections")
async def get_connections(_user=Depends(require_admin)):
    return await botshepherd_manager.get_connections()


@router.put("/connections/{connection_id}")
async def update_connection(
    connection_id: str, body: dict, _user=Depends(require_admin)
):
    return await botshepherd_manager.update_connection(connection_id, body)


@router.post("/connections/{connection_id}/copy")
async def copy_connection(connection_id: str, body: dict, _user=Depends(require_admin)):
    return await botshepherd_manager.copy_connection(connection_id, body)


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, _user=Depends(require_admin)):
    return await botshepherd_manager.delete_connection(connection_id)


# ---- 账号管理 ----


@router.get("/accounts")
async def get_accounts(_user=Depends(require_admin)):
    return await botshepherd_manager.get_accounts()


@router.put("/accounts/{account_id}")
async def update_account(account_id: str, body: dict, _user=Depends(require_admin)):
    return await botshepherd_manager.update_account(account_id, body)


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, _user=Depends(require_admin)):
    return await botshepherd_manager.delete_account(account_id)


@router.get("/accounts/{account_id}/online-status")
async def get_account_online(account_id: str, _user=Depends(require_admin)):
    return await botshepherd_manager.get_account_online(account_id)


# ---- Bot 掉线检测（管理器内置 OneBot WS 端点采集） ----


@router.get("/bots/heartbeat")
async def get_bots_heartbeat(_user=Depends(require_admin)):
    """查询所有已接入管理器 OneBot WS 端点的 Bot 在线状态。

    数据来源：/ws/onebot/v11/ws 端点接收的 meta_event.heartbeat 事件。
    online=true 表示最近一个心跳周期内有心跳且 NapCat 上报 online=true。
    """
    from services.bot_heartbeat import bot_heartbeat

    return {"status": "ok", "bots": bot_heartbeat.get_all()}


@router.get("/bots/heartbeat/{self_id}")
async def get_bot_heartbeat(self_id: str, _user=Depends(require_admin)):
    """查询指定 Bot（self_id）的在线状态。"""
    from services.bot_heartbeat import bot_heartbeat

    result = bot_heartbeat.get_one(self_id)
    if result is None:
        return {"status": "ok", "online": False, "detail": "no heartbeat received"}
    return {"status": "ok", **result}


# ---- Bot 框架端点探测 ----


class ProbeTargetRequest(BaseModel):
    url: str
    token: str = ""


class ActivationRequest(BaseModel):
    url: str = ""
    token: str = ""


@router.get("/activation")
async def get_activation_status(_user=Depends(require_admin)):
    return {"status": "ok", "activation": bs_activation_service.status()}


@router.post("/activation/start")
async def start_activation(
    body: ActivationRequest = ActivationRequest(), _user=Depends(require_admin)
):
    return await bs_activation_service.start(body.url, body.token)


@router.post("/activation/stop")
async def stop_activation(_user=Depends(require_admin)):
    return await bs_activation_service.stop()


@router.post("/probe-target")
async def probe_target(body: ProbeTargetRequest, _user=Depends(require_admin)):
    """探测对端 Bot 框架 WS 端点（AstrBot/NoneBot 等）是否可连。

    携带 OneBot v11 标准头部发起 WS 握手，通过 WSServerHandshakeError 状态码区分
    「端口可达但握手被拒（在线）」与「端口不通（离线）」两种场景。
    """
    return await botshepherd_manager.probe_target_endpoint(body.url, body.token)


# ---- Bot 雷达端点库 ----


@router.get("/radar/endpoints")
async def get_radar_endpoints(_user=Depends(require_admin)):
    """读取 Bot 雷达端点库（config/bot_radar_endpoints.json）。"""
    return {"status": "ok", "endpoints": botshepherd_manager.get_radar_endpoints()}


@router.post("/radar/endpoints")
async def save_radar_endpoints(body: dict, _user=Depends(require_admin)):
    """全量覆盖写入 Bot 雷达端点库。body: {endpoints: [{alias, url, token}]}"""
    endpoints = body.get("endpoints", [])
    if not isinstance(endpoints, list):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="endpoints 必须为数组")
    botshepherd_manager.save_radar_endpoints(endpoints)
    return {"status": "ok", "count": len(endpoints)}


class InjectByAliasRequest(BaseModel):
    alias: str
    target: str  # "bs" | "nc"
    conn_id: str = ""  # target=bs 时必填
    container_name: str = ""  # target=nc 时必填
    uin: str = "default"  # target=nc 时使用


@router.post("/radar/inject-by-alias")
async def inject_by_alias(body: InjectByAliasRequest, _user=Depends(require_admin)):
    """按端点别名执行注入（供外部插件/自动化调用）。

    - target='bs'：将别名对应端点 url 追加到指定 BS 连接的 target_endpoints（热重载立即生效）
    - target='nc'：将别名对应端点 url 追加到指定容器的 websocketClients（需重载 NapCat 配置生效）

    示例（管理插件触发：实例 miya 注入 gscore 到 BS 连接）：
      POST /api/botshepherd/radar/inject-by-alias
      {"alias": "gscore", "target": "bs", "conn_id": "conn_miya"}
    """
    return await botshepherd_manager.inject_by_alias(
        alias=body.alias,
        target=body.target,
        conn_id=body.conn_id,
        container_name=body.container_name,
        uin=body.uin,
    )


class RemoveEndpointRequest(BaseModel):
    conn_id: str
    endpoint_url: str


@router.post("/connections/{connection_id}/remove-endpoint")
async def remove_endpoint_from_connection(
    connection_id: str, body: RemoveEndpointRequest, _user=Depends(require_admin)
):
    """从指定 BS 连接的 target_endpoints 中移除已知端点。

    安全限制：只允许删除以下两类端点，拒绝删除用户手动配置的未知第三方端点：
      1. 管理器自身端点（/ws/napcat/*）
      2. Bot 雷达端点库中的已知端点（config/bot_radar_endpoints.json）

    用途：清理管理器注册的端点或 Bot 雷达注入的端点。

    示例：
      POST /api/botshepherd/connections/conn_miya/remove-endpoint
      {"conn_id": "conn_miya", "endpoint_url": "ws://192.168.1.211:8000/ws/napcat/698076448"}
    """
    # 优先使用路径参数，兼容 body 参数
    conn_id = connection_id or body.conn_id
    return await botshepherd_manager.remove_endpoint_from_bs(conn_id, body.endpoint_url)
