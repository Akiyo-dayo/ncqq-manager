"""
Bot 雷达路由 — 端点探测 / 端点库 / 一键注入到实例。

外部自动化可直接调用 /api/bot-radar/inject-by-alias，用别名把某个 Bot 框架端点
注入到指定实例，不必关心该框架的 URL 与 token。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import require_admin
from middleware.rate_limiter import speed_limit
from services.bot_heartbeat import bot_heartbeat
from services.bot_radar import bot_radar
from services.operation_logger import operation_logger
from services.operation_log_context import build_operator_payload

router = APIRouter(prefix="/api/bot-radar", tags=["bot-radar"])


class ProbeTargetRequest(BaseModel):
    url: str
    token: str = ""


class SaveEndpointsRequest(BaseModel):
    endpoints: list


class InjectByAliasRequest(BaseModel):
    alias: str
    container_name: str
    uin: str = "default"


@router.post("/probe", dependencies=[Depends(speed_limit(1.0))])
async def probe_target(body: ProbeTargetRequest, _user=Depends(require_admin)):
    """探测 Bot 框架 WS 端点（AstrBot/NoneBot/Koishi 等）是否可连。

    携带 OneBot v11 标准头部发起 WS 握手，通过 WSServerHandshakeError 状态码区分
    「端口可达但握手被拒（在线，通常是 token 不对）」与「端口不通（离线）」。
    """
    if not body.url:
        raise HTTPException(status_code=400, detail="请填写端点 URL")
    return await bot_radar.probe_target_endpoint(body.url, body.token)


@router.get("/endpoints")
async def get_endpoints(_user=Depends(require_admin)):
    """读取端点库。"""
    return {"status": "ok", "endpoints": bot_radar.get_endpoints()}


@router.post("/endpoints")
async def save_endpoints(body: SaveEndpointsRequest, _user=Depends(require_admin)):
    """全量覆盖写入端点库。body: {endpoints: [{alias, url, token}]}"""
    for item in body.endpoints:
        if not isinstance(item, dict) or not item.get("alias") or not item.get("url"):
            raise HTTPException(status_code=400, detail="每条端点都必须有 alias 和 url")
    aliases = [e["alias"] for e in body.endpoints]
    if len(aliases) != len(set(aliases)):
        raise HTTPException(status_code=400, detail="别名不能重复")
    bot_radar.save_endpoints(body.endpoints)
    return {"status": "ok", "count": len(body.endpoints)}


@router.post("/inject-by-alias", dependencies=[Depends(speed_limit(1.0))])
async def inject_by_alias(
    body: InjectByAliasRequest, request: Request, session: dict = Depends(require_admin),
):
    """按端点别名把该端点注入到指定实例的 onebot11 配置。

    示例：
      POST /api/bot-radar/inject-by-alias
      {"alias": "gscore", "container_name": "miya"}
    """
    result = bot_radar.inject_to_container(
        alias=body.alias, container_name=body.container_name, uin=body.uin,
    )
    if result.get("success"):
        operation_logger.info(
            "bot_radar_inject",
            build_operator_payload(request, session, {
                "alias": body.alias,
                "container_name": body.container_name,
                "uin": body.uin,
            }),
        )
    return result


@router.get("/bots/heartbeat")
async def get_bots_heartbeat(_user=Depends(require_admin)):
    """查询所有已接入管理器 OneBot WS 端点的 Bot 在线状态。

    数据来源：/ws/onebot/v11/ws 与 /ws/napcat/{name} 收到的 meta_event.heartbeat。
    online=true 表示最近一个心跳周期内有心跳且 NapCat 上报 online=true。
    """
    return {"status": "ok", "bots": bot_heartbeat.get_all()}


@router.get("/bots/heartbeat/{self_id}")
async def get_bot_heartbeat(self_id: str, _user=Depends(require_admin)):
    """查询指定 Bot（self_id）的在线状态。"""
    result = bot_heartbeat.get_one(self_id)
    if result is None:
        return {"status": "ok", "online": False, "detail": "no heartbeat received"}
    return {"status": "ok", **result}
