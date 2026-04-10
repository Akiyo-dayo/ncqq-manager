"""
Bot API 对外代理路由

供 AstrBot 插件或外部系统查询/操作已连接 Bot 状态与 OneBot API。
所有端点需要 API Key 或 Cookie 认证（与其余管理 API 保持一致）。

端点列表：
  GET  /api/bots                    → 列出所有已知 Bot（name/uin/connected/nickname）
  GET  /api/bots/{name}/status      → 查询单个 Bot 连接状态
  POST /api/bots/{name}/call        → 代理调用 OneBot API（透传 action/params）
  POST /api/bots/{name}/send        → 便捷发消息（私聊/群聊）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from middleware.auth import get_current_user
from services.log import logger

router = APIRouter(prefix="/api/bots", tags=["bot-api"])

# ─── 请求/响应模型 ────────────────────────────────────────────────────────────


class BotStatusItem(BaseModel):
    name: str
    uin: str
    nickname: str
    connected: bool
    last_seen: float  # Unix timestamp，0 = 从未连接


class CallRequest(BaseModel):
    action: str
    params: Dict[str, Any] = {}
    timeout: float = 10.0


class SendRequest(BaseModel):
    msg_type: str  # "private" | "group"
    target_id: str
    message: str


# ─── 端点实现 ─────────────────────────────────────────────────────────────────


@router.get("", response_model=List[BotStatusItem])
async def list_bots(_user=Depends(get_current_user)):
    """列出所有已知 Bot。

    数据来源（优先级高→低）：
      1. napcat_ws_service 连接注册表 — 曾通过 WS 直连管理器的 Bot（含 nickname/uin）
      2. instance_subsystem           — Docker 容器列表（WS 表无记录时兜底，确保哨兵配置
                                       不受"Bot 是否曾直连管理器"限制）
    """
    from services.napcat_ws_service import napcat_ws_service
    from services.instance_subsystem import instance_subsystem

    merged: Dict[str, BotStatusItem] = {}

    # ── 1. WS 直连历史（数据最准，含 nickname/uin/在线状态）──────────────
    for name in napcat_ws_service.all_names():
        entry = napcat_ws_service.get_entry_snapshot(name)
        if entry is None:
            continue
        merged[name] = BotStatusItem(
            name=name,
            uin=entry["uin"] or "",
            nickname=entry["nickname"] or "",
            connected=entry["connected"],
            last_seen=entry["last_seen"],
        )

    # ── 2. Docker 容器兜底（WS 表未收录时补入，connected=false）────────────
    for inst in instance_subsystem.get_all():
        if inst.name in merged:
            continue  # WS 表优先，不覆盖
        merged[inst.name] = BotStatusItem(
            name=inst.name,
            uin=inst.uin or "",
            nickname="",
            connected=False,
            last_seen=inst.login_ts,
        )

    result = list(merged.values())
    logger.debug("GET /api/bots → %d bots (ws=%d inst=%d)",
                 len(result), len(napcat_ws_service.all_names()), instance_subsystem.count)
    return result


@router.get("/{name}/status", response_model=BotStatusItem)
async def get_bot_status(name: str, _user=Depends(get_current_user)):
    """查询指定 Bot 的连接状态。"""
    from services.napcat_ws_service import napcat_ws_service
    entry = napcat_ws_service.get_entry_snapshot(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Bot [{name}] 未知或从未连接")
    return BotStatusItem(
        name=name,
        uin=entry["uin"] or "",
        nickname=entry["nickname"] or "",
        connected=entry["connected"],
        last_seen=entry["last_seen"],
    )


@router.post("/{name}/call")
async def call_bot_api(
    name: str,
    body: CallRequest,
    _user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    通过反向 WS 连接代理调用 OneBot API。
    Bot 必须当前在线（已连接到 /ws/napcat/{name}）。
    返回 OneBot 响应的 data 字段。
    """
    from services.napcat_ws_service import napcat_ws_service
    if not napcat_ws_service.is_connected(name):
        raise HTTPException(status_code=503, detail=f"Bot [{name}] 当前未连接")
    try:
        data = await napcat_ws_service.call_action(
            name, body.action, body.params, timeout=body.timeout
        )
        logger.info("Bot API 代理: name=%s action=%s", name, body.action)
        return {"status": "ok", "data": data}
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/{name}/send")
async def send_bot_message(
    name: str,
    body: SendRequest,
    _user=Depends(get_current_user),
) -> Dict[str, Any]:
    """
    通过指定 Bot 发送消息（私聊/群聊）。
    msg_type: "private" | "group"
    target_id: QQ 号（私聊）或群号（群聊）
    """
    from services.napcat_ws_service import napcat_ws_service
    if not napcat_ws_service.is_connected(name):
        raise HTTPException(status_code=503, detail=f"Bot [{name}] 当前未连接")
    msg_id = await napcat_ws_service.send_message(
        name, body.msg_type, body.target_id, body.message
    )
    if msg_id is None:
        raise HTTPException(status_code=502, detail="消息发送失败（无 message_id 返回）")
    logger.info("Bot 发消息代理: name=%s type=%s target=%s msg_id=%s",
                name, body.msg_type, body.target_id, msg_id)
    return {"status": "ok", "message_id": msg_id}

