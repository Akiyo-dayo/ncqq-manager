"""
WebSocket 路由 - 实时事件推送 + 日志流

端点：
  /ws/events         — 管理员专用（需认证），推送全量容器状态
  /ws/public         — 公开端点（无需认证），推送容器列表 + QR 状态，支持按需分页订阅
  /ws/logs/{name}    — 管理员专用，推送容器日志流
  /ws/onebot/v11/ws  — OneBot v11 反向 WS 接收端点（BS 默认目标），用于 Bot 掉线检测
  /ws/napcat/{name}  — 带容器名的主路径端点，支持 NapCatApiProxy 主动 API 调用
"""

import asyncio

import orjson

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from starlette.concurrency import run_in_threadpool

from services.ws_manager import ws_manager
from services.cluster_manager import cluster_manager
from services.instance_subsystem import instance_subsystem
from services.log import logger
from services.container_state import state_engine
from middleware.auth import validate_token_value, check_instance_permission
from middleware.rate_limiter import websocket_public_speed_limit

# napcat-sdk 不可用；ws_router 使用手写 OneBot 事件分发（无外部依赖）

router = APIRouter(tags=["websocket"])

_MAX_PUBLIC_WS = 50  # 公开 WS 最大并发连接数


def _mask_uin(uin: str) -> str:
    """QQ号脱敏：长号保留前3后3位中间用*，短号保留首尾中间用*。"""
    if not uin:
        return ""
    n = len(uin)
    if n <= 2:
        return "*" * n
    if n <= 6:
        return uin[0] + "*" * (n - 2) + uin[-1]
    return uin[:3] + "*" * (n - 6) + uin[-3:]


def _mask_containers_uin(containers: list) -> list:
    """批量脱敏容器列表中的 uin 字段。"""
    for c in containers:
        if isinstance(c, dict) and "uin" in c:
            c["uin"] = _mask_uin(c["uin"])
    return containers


def _mask_qr_states_uin(qr_states: dict) -> dict:
    """批量脱敏 QR 状态中的 uin 字段。"""
    for v in qr_states.values():
        if isinstance(v, dict) and "uin" in v:
            v["uin"] = _mask_uin(v["uin"])
    return qr_states


def _build_snapshot(containers: list) -> dict:
    """构建容器快照字典（用于增量 diff 比较）。key=name, value=精简状态。"""
    snap = {}
    for c in containers:
        snap[c["name"]] = {
            "status": c.get("status", ""),
            "uin": c.get("uin", ""),
            "node_id": c.get("node_id", "local"),
            "bot_online": c.get("bot_online", False),
        }
    return snap


def _build_public_version(sub_page: int, sub_page_size: int, payload: dict) -> tuple:
    # 版本号基于实际推送内容，避免 tick 慢时前端长时间看不到状态变化
    return (sub_page, sub_page_size, hash(orjson.dumps(payload)))


def _resolve_ws_token(ws: WebSocket) -> str:
    """从 cookie 中提取认证 token。
    httpOnly cookie 无法被前端 JS 读取，但浏览器在 WS 握手时会自动携带。
    """
    return ws.cookies.get("auth_token", "")


@router.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """容器状态实时推送 — 从状态引擎读内存快照，零 Docker API 调用。"""
    effective_token = _resolve_ws_token(ws)
    session = validate_token_value(effective_token) if effective_token else None
    if not session:
        await ws.close(code=4001, reason="Unauthorized")
        return

    # 普通用户只推送其分配的实例
    allowed_set = None
    if session.get("permission", 0) < 10:
        from services.user_manager import user_manager
        user = user_manager.get_user_by_uuid(session["uuid"])
        if user:
            allowed_set = {(inst.get("node_id"), inst.get("container_name")) for inst in user.get("instances", [])}
        else:
            allowed_set = set()

    await ws_manager.connect(ws)
    prev_snapshot: dict = {}
    try:
        while True:
            # 从状态引擎读内存快照（零阻塞，<1ms）
            containers = state_engine.get_containers()
            if allowed_set is not None:
                containers = [c for c in containers if (c.get("node_id", "local"), c["name"]) in allowed_set]
            curr_snapshot = _build_snapshot(containers)

            try:
                if curr_snapshot != prev_snapshot:
                    await asyncio.wait_for(
                        ws.send_json({"type": "containers", "data": containers}),
                        timeout=5,
                    )
                    prev_snapshot = curr_snapshot
                else:
                    await asyncio.wait_for(
                        ws.send_json({"type": "heartbeat"}), timeout=5
                    )
            except (asyncio.TimeoutError, Exception):
                break

            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS events 连接异常: %s", e)
    finally:
        await ws_manager.disconnect(ws)


@router.websocket("/ws/logs/{name}")
async def ws_container_logs(
    ws: WebSocket,
    name: str,
    node_id: str = Query(default="local"),
):
    """容器日志实时流推送"""
    effective_token = _resolve_ws_token(ws)
    session = validate_token_value(effective_token) if effective_token else None
    if not session:
        await ws.close(code=4001, reason="Unauthorized")
        return

    # 检查实例权限
    if not check_instance_permission(session, node_id, name):
        await ws.close(code=4003, reason="No permission for this instance")
        return

    await ws.accept()
    try:
        while True:
            try:
                logs = await asyncio.wait_for(
                    run_in_threadpool(cluster_manager.get_logs, node_id, name, 200),
                    timeout=8,
                )
            except (asyncio.TimeoutError, Exception):
                logs = ""
            try:
                await asyncio.wait_for(
                    ws.send_json({"type": "logs", "data": logs or ""}), timeout=5
                )
            except (asyncio.TimeoutError, Exception):
                break
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS logs 连接异常 [%s]: %s", name, e)


# ============ 公开 WS — 无需认证，推送容器列表 + QR 状态 ============


@router.websocket("/ws/public")
async def ws_public(ws: WebSocket):
    """公开 WS 端点 — 用户面板专用，推送容器列表 + QR 状态。

    协议：
      服务端 → 客户端：
        {"type": "full",      "data": {"containers": [...], "qr": {...}}}
        {"type": "heartbeat"}
      客户端 → 服务端（可选，按需订阅分页）：
        {"type": "subscribe", "page": 1, "pageSize": 20}
    """
    ws_public_limiter = websocket_public_speed_limit(1.0)
    if not await ws_public_limiter(ws):
        await ws.close(code=4429, reason="Rate limited")
        return

    if not await ws_manager.connect_if_available(ws, _MAX_PUBLIC_WS):
        await ws.close(code=4429, reason="Too many connections")
        return

    # 默认推送全量（向后兼容），客户端可发 subscribe 切换分页
    sub_page = 0  # 0 = 全量模式
    sub_page_size = 20
    prev_version: tuple | None = None

    async def _recv_loop():
        """接收客户端的订阅消息（翻页/搜索时发送）。"""
        nonlocal sub_page, sub_page_size
        try:
            async for raw in ws.iter_text():
                try:
                    msg = orjson.loads(raw)
                    if msg.get("type") == "subscribe":
                        sub_page = int(msg.get("page", 1))
                        sub_page_size = min(int(msg.get("pageSize", 20)), 50)
                except (orjson.JSONDecodeError, ValueError, TypeError):
                    pass
        except WebSocketDisconnect:
            pass

    recv_task = asyncio.create_task(_recv_loop())
    try:
        while True:
            # 构建推送数据
            if sub_page > 0:
                # 分页模式 — 只推送当前页（MCSM instance/select 模式）
                page_result = instance_subsystem.query(
                    page=sub_page, page_size=sub_page_size
                )
                containers = page_result["data"]
                qr_states = {}
                for item in containers:
                    inst = instance_subsystem.get(item["name"])
                    if inst:
                        qr_states[item["name"]] = inst.to_qr_dict()
                payload = {"containers": page_result, "qr": qr_states}
            else:
                # 全量模式 — 兼容简单客户端
                containers = state_engine.get_containers()
                qr_states = state_engine.get_qr_states()
                payload = {"containers": containers, "qr": qr_states}

            curr_version = _build_public_version(sub_page, sub_page_size, payload)

            try:
                if curr_version != prev_version:
                    await asyncio.wait_for(
                        ws.send_json({"type": "full", "data": payload}),
                        timeout=5,
                    )
                    prev_version = curr_version
                else:
                    await asyncio.wait_for(
                        ws.send_json({"type": "heartbeat"}),
                        timeout=5,
                    )
            except (asyncio.TimeoutError, Exception):
                break

            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS public 连接异常: %s", e)
    finally:
        recv_task.cancel()
        await ws_manager.disconnect(ws)


# ============ OneBot v11 反向 WS — BS 默认目标端点 ============
# 同时支持：
#   /ws/onebot/v11/ws           — 旧兼容路径（无容器归属，依赖 header self_id）
#   /ws/napcat/{name}           — 新路径（携带容器名，接入 napcat_ws_service）


def _handle_ob11_event(
    event: dict,
    name: str,
    header_sid: str | None,
    seen_sids: set,
    ws_ref: "WebSocket | None" = None,
) -> None:
    """
    解析单条 OneBot 事件并分发至 bot_heartbeat / napcat_ws_service。
    使用手写字段检查，无外部依赖。
    name 为空字符串时退化为旧兼容模式（纯 bot_heartbeat）。
    """
    from services.bot_heartbeat import bot_heartbeat
    from services.napcat_ws_service import napcat_ws_service

    raw_sid = event.get("self_id") or header_sid
    if not raw_sid:
        return
    sid = str(raw_sid)
    # "0" 是 BS probe_target_endpoint 探测时携带的哑值，忽略避免污染心跳表
    if sid == "0":
        return
    seen_sids.add(sid)

    # ★ 核心修复：事件中的 self_id 是真实 uin，补全到 WS 注册表
    # 解决"扫码登录后 uin 未写入注册表 → 实例卡在待登录"问题
    if name:
        napcat_ws_service.ensure_uin(name, sid, ws=ws_ref)

    post_type = event.get("post_type", "")
    meta_type = event.get("meta_event_type", "")
    notice_type = event.get("notice_type", "")

    if post_type == "meta_event":
        if meta_type == "heartbeat":
            interval_ms = event.get("interval", 30000)
            status_data = event.get("status", {})
            online = bool(status_data.get("online", True))
            bot_heartbeat.on_heartbeat(sid, interval_ms, status_data)
            if name:
                napcat_ws_service.on_heartbeat(name, online)
            logger.debug(
                "Bot 心跳: name=%s self_id=%s online=%s", name or "?", sid, online
            )

        elif meta_type == "lifecycle":
            sub_type = event.get("sub_type", "")
            if sub_type == "connect":
                bot_heartbeat.on_connect(sid)
                logger.info(
                    "Bot lifecycle.connect: name=%s self_id=%s", name or "?", sid
                )
            elif sub_type == "disconnect":
                bot_heartbeat.on_disconnect(sid)
                logger.info(
                    "Bot lifecycle.disconnect: name=%s self_id=%s", name or "?", sid
                )

    elif post_type == "notice" and notice_type == "bot_offline":
        # NapCat BotOfflineEvent：tag/message 字段记录掉线原因
        tag = event.get("tag", "")
        msg = event.get("message", "")
        bot_heartbeat.on_disconnect(sid)
        logger.info(
            "Bot offline notice: name=%s self_id=%s tag=%s msg=%s",
            name or "?",
            sid,
            tag,
            msg,
        )


async def _ob11_recv_loop(ws: WebSocket, name: str, header_sid: str | None) -> None:
    """通用接收循环，被两个端点复用。
    - 带 echo 的消息：API 响应，路由给 NapCatApiProxy（仅 named 端点有 proxy）
    - 不带 echo 的消息：OneBot 事件，分发给 _handle_ob11_event
    """
    from services.napcat_ws_service import napcat_ws_service

    seen_sids: set = set()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=120)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
                continue
            try:
                data = orjson.loads(raw)
            except Exception:
                continue

            # 带 echo → API 响应，路由给代理；无 echo → OneBot 事件
            if isinstance(data, dict) and data.get("echo"):
                proxy = napcat_ws_service.get_proxy(name) if name else None
                if proxy:
                    proxy.on_response(str(data["echo"]), data)
            else:
                _handle_ob11_event(data, name, header_sid, seen_sids, ws_ref=ws)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("OneBot WS 接收异常 [%s]: %s", name or "compat", e)
    finally:
        from services.bot_heartbeat import bot_heartbeat

        for sid in seen_sids:
            bot_heartbeat.on_ws_lost(sid)
        if name:
            napcat_ws_service.on_disconnect(name, ws=ws)
        logger.info("OneBot WS 断开: name=%s seen_sids=%s", name or "compat", seen_sids)


@router.websocket("/ws/napcat/{name}")
async def ws_napcat_named(ws: WebSocket, name: str):
    """
    带容器名的 NapCat 反向 WS 接入点（主路径）。

    连接建立时：
      1. 从 X-Self-Id 头 / 首条心跳事件中获取 uin
      2. 注册到 napcat_ws_service（更新 instance_subsystem 由 container_state 负责）
      3. 注册 NapCatApiProxy（支持通过此连接主动调用 OneBot API）
      4. 持续接收心跳 / lifecycle / BotOfflineEvent / API 响应
    """
    from services.napcat_ws_service import napcat_ws_service

    await ws.accept()
    header_sid = ws.headers.get("x-self-id") or ws.headers.get("X-Self-Id") or ""
    logger.info(
        "NapCat WS [%s] 新连接 header_self_id=%s client=%s", name, header_sid, ws.client
    )

    # 连接建立时先用 header_sid 预注册（事件到来前的宽限期）
    # "0" 是 BS probe_target_endpoint 探测握手的哑值，跳过避免污染注册表 uin
    is_probe = header_sid == "0"
    if header_sid and not is_probe:
        napcat_ws_service.on_connect(name, header_sid, ws=ws)

    # 注册 API 代理（复用此 WS 连接主动调用 NapCat API）
    # 探测连接不注册 proxy，避免覆盖正常连接的 proxy
    if not is_probe:
        napcat_ws_service.register_proxy(name, ws)
    try:
        await _ob11_recv_loop(ws, name, header_sid or None)
    finally:
        if not is_probe:
            napcat_ws_service.unregister_proxy(name, ws=ws)


@router.websocket("/ws/onebot/v11/ws")
async def ws_onebot_receiver(ws: WebSocket):
    """
    OneBot v11 反向 WS 兼容端点（旧路径，保持向后兼容）。

    作为 BotShepherd target_endpoint 的默认值，接收 BS 转发的 OneBot 事件。
    无容器名归属，依赖 X-Self-Id 头部做 uin 关联。
    新部署建议改用 /ws/napcat/{name} 端点。
    """
    await ws.accept()
    header_sid = ws.headers.get("x-self-id") or ws.headers.get("X-Self-Id")
    logger.info(
        "OneBot WS 兼容端点：新连接 header_self_id=%s client=%s", header_sid, ws.client
    )
    await _ob11_recv_loop(ws, "", header_sid)
