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
    node_id: str = "local"
    uin: str
    nickname: str
    # WS 链路是否连着。注意：链路连着不代表 QQ 已登录 —— NapCat 进程活着、
    # WS 也连着，但账号已退登等扫码时，这个值仍是 True。
    ws_connected: bool
    # QQ 是否真的在线：以 OneBot 心跳新鲜度为准，这才是外部插件该看的字段。
    bot_online: bool
    # 兼容旧客户端的别名，语义与 bot_online 一致（过去它等于 ws_connected，会误导）
    connected: bool
    last_seen: float  # Unix timestamp，0 = 从未连接
    stale: bool = False  # 所属节点已失联，状态不可信


class CallRequest(BaseModel):
    action: str
    params: Dict[str, Any] = {}
    timeout: float = 10.0


class SendRequest(BaseModel):
    msg_type: str  # "private" | "group"
    target_id: str
    message: str


def _bot_is_online(inst) -> bool:
    """Bot 是否真的在线 —— 公开接口与内部判定必须用同一个口径。

    条件是「确认登录」且「心跳新鲜」。只看 WS 连着会把「NapCat 活着但 QQ 已退登、
    正在等扫码」也报成在线，外部插件据此去调 API 只会拿到失败。
    """
    if not inst.logged_in or not inst.uin:
        return False
    try:
        from services.bot_heartbeat import bot_heartbeat
        return bot_heartbeat.is_online(inst.uin)
    except Exception:
        return bool(inst.bot_online)


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

    # 以 (node_id, name) 为键：过去只按 name 去重，跨节点同名实例会互相覆盖。
    merged: Dict[tuple, BotStatusItem] = {}

    # ── 1. 实例快照为主：登录真值与在线判定都在状态引擎里 ───────────────────
    for inst in instance_subsystem.get_all():
        entry = napcat_ws_service.get_entry_snapshot(inst.name) or {}
        stale = inst.is_stale
        merged[(inst.node_id, inst.name)] = BotStatusItem(
            name=inst.name,
            node_id=inst.node_id,
            uin="" if stale else (inst.uin if inst.logged_in else ""),
            nickname=entry.get("nickname") or "",
            ws_connected=bool(entry.get("connected")) and not stale,
            bot_online=_bot_is_online(inst) and not stale,
            connected=_bot_is_online(inst) and not stale,
            last_seen=entry.get("last_seen") or inst.login_ts,
            stale=stale,
        )

    # ── 2. WS 表兜底：曾直连但已无对应容器（如容器已删）的记录 ──────────────
    for name in napcat_ws_service.all_names():
        if any(key[1] == name for key in merged):
            continue
        entry = napcat_ws_service.get_entry_snapshot(name)
        if entry is None:
            continue
        merged[("local", name)] = BotStatusItem(
            name=name,
            node_id="local",
            uin=entry["uin"] or "",
            nickname=entry["nickname"] or "",
            ws_connected=entry["connected"],
            # 没有对应实例就无法做心跳校准，只能保守判定为不在线。
            bot_online=False,
            connected=False,
            last_seen=entry["last_seen"],
        )

    result = list(merged.values())
    logger.debug("GET /api/bots → %d bots (ws=%d inst=%d)",
                 len(result), len(napcat_ws_service.all_names()), instance_subsystem.count)
    return result


@router.get("/{name}/status", response_model=BotStatusItem)
async def get_bot_status(name: str, node_id: str = "local", _user=Depends(get_current_user)):
    """查询指定 Bot 的连接状态。"""
    from services.napcat_ws_service import napcat_ws_service
    from services.instance_subsystem import instance_subsystem

    inst = instance_subsystem.get(name, node_id)
    entry = napcat_ws_service.get_entry_snapshot(name)
    if inst is None and entry is None:
        raise HTTPException(status_code=404, detail=f"Bot [{name}] 未知或从未连接")
    if inst is None:
        return BotStatusItem(
            name=name, node_id=node_id, uin=entry["uin"] or "",
            nickname=entry["nickname"] or "", ws_connected=entry["connected"],
            bot_online=False, connected=False, last_seen=entry["last_seen"],
        )
    stale = inst.is_stale
    online = _bot_is_online(inst) and not stale
    return BotStatusItem(
        name=name,
        node_id=inst.node_id,
        uin="" if stale else (inst.uin if inst.logged_in else ""),
        nickname=(entry or {}).get("nickname") or "",
        ws_connected=bool((entry or {}).get("connected")) and not stale,
        bot_online=online,
        connected=online,
        last_seen=(entry or {}).get("last_seen") or inst.login_ts,
        stale=stale,
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

