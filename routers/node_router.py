"""
节点管理路由 - 节点 CRUD + 状态 + 代理
"""
import uuid as uuid_mod

from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel

from middleware.auth import get_current_user, require_admin
from middleware.rate_limiter import speed_limit
from services.cluster_manager import cluster_manager
from services.config import app_config, APP_VERSION
from services.log import logger
from services.operation_logger import operation_logger
from services.operation_log_context import build_operator_payload

router = APIRouter(prefix="/api", tags=["nodes"])


class NodeRequest(BaseModel):
    name: str
    address: str
    api_key: str = ""
    insecure_tls: bool = False


class NodeProbeRequest(BaseModel):
    address: str
    api_key: str = ""
    insecure_tls: bool = False


# 旧版前端可能仍会把掩码值回传，编辑节点时必须识别并忽略，
# 否则会把该节点的密钥覆写成字面量 "***"。
_MASKED_SECRET = "***"


# ============ 集群配置 ============

@router.get("/cluster/config", dependencies=[Depends(speed_limit(2.0))])
async def get_cluster_config(session: dict = Depends(require_admin)):
    import sys
    from services.daemon_monitor import daemon_monitor
    return {
        "status": "ok",
        "config": {
            "docker_image": app_config.get("docker_image"),
            "webui_base_port": app_config.get("webui_base_port"),
            "http_base_port": app_config.get("http_base_port"),
            "ws_base_port": app_config.get("ws_base_port"),
            # 集群密钥不在这里下发。曾经这里返回掩码 "***"，前端把整个 config
            # 原样 POST 回来就把真密钥覆写成了字面量 "***"，整个集群立刻 401。
            # 读取/重置密钥请走 /api/cluster/key。
            "has_api_key": bool(app_config.get("api_key")),
            "data_dir": app_config.get("data_dir"),
            "init_ws_client_enabled": app_config.get("init_ws_client_enabled", False),
            "init_ws_client_url": app_config.get("init_ws_client_url", "ws://127.0.0.1:5100/onebot/v11/ws"),
            "init_ws_client_token": app_config.get("init_ws_client_token", ""),
            "init_auto_join_groups_enabled": app_config.get("init_auto_join_groups_enabled", False),
            "init_auto_join_groups": app_config.get("init_auto_join_groups", "[]"),
            "container_keywords": app_config.get("container_keywords", '["napcat"]'),
        },
        "system": {
            "cpu_percent": daemon_monitor.current_cpu,
            "mem_percent": daemon_monitor.current_mem,
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "app_version": APP_VERSION,
        },
    }


@router.post("/cluster/config", dependencies=[Depends(speed_limit(5.0))])
async def save_cluster_config(
    request: Request,
    session: dict = Depends(require_admin),
):
    body = await request.json()
    allowed_keys = {"webui_base_port", "http_base_port", "ws_base_port", "docker_image", "api_key", "data_dir",
                     "container_keywords",
                     "init_ws_client_enabled", "init_ws_client_url", "init_ws_client_token",
                     "init_auto_join_groups_enabled", "init_auto_join_groups"}
    updates = {k: v for k, v in body.items() if k in allowed_keys}

    # 集群密钥只能通过专用端点修改。GET 返回的是掩码，前端整体回传时必须丢弃，
    # 否则每保存一次设置就把密钥写成 "***"，所有远程节点立刻断连。
    if updates.pop("api_key", None) is not None:
        logger.debug("集群配置保存请求携带了 api_key，已忽略（请使用 /api/cluster/key）")

    # 端口范围校验
    for port_key in ("webui_base_port", "http_base_port", "ws_base_port"):
        if port_key in updates:
            port_val = updates[port_key]
            if not isinstance(port_val, int) or not (1024 <= port_val <= 65535):
                raise HTTPException(
                    status_code=400,
                    detail=f"{port_key} must be an integer between 1024 and 65535",
                )

    # data_dir 合法性校验
    if "data_dir" in updates:
        data_dir = updates["data_dir"]
        if not isinstance(data_dir, str) or not data_dir.strip():
            raise HTTPException(status_code=400, detail="data_dir must be a non-empty string")
        import os as _os
        # 尝试创建目录以验证路径合法性
        try:
            _os.makedirs(data_dir, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Invalid data_dir path: {e}")

    # docker_image 基本校验
    if "docker_image" in updates:
        img = updates["docker_image"]
        if not isinstance(img, str) or not img.strip():
            raise HTTPException(status_code=400, detail="docker_image must be a non-empty string")

    app_config.update(updates)
    operation_logger.info(
        "cluster_config_save",
        build_operator_payload(
            request,
            session,
            {
                "updated_keys": sorted(updates.keys()),
                "updated_count": len(updates),
            },
        ),
    )
    return {"status": "ok"}


@router.get("/cluster/status", dependencies=[Depends(speed_limit(2.0))])
async def cluster_status(session: dict = Depends(require_admin)):
    """供远程节点健康检查用 (需 x-request-api-key 认证)"""
    import sys
    from services.daemon_monitor import daemon_monitor

    return {
        "status": "online",
        "system": {
            "cpu_percent": daemon_monitor.current_cpu,
            "mem_percent": daemon_monitor.current_mem,
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "app_version": APP_VERSION,
        },
        # 只报告本机实例，否则父面板看到的是本机 + 本机下级节点的总和。
        "instances": daemon_monitor.get_instance_status(node_id="local"),
        "chart": daemon_monitor.get_chart_data(),
    }


# ============ 本机集群密钥 ============

@router.get("/cluster/key", dependencies=[Depends(speed_limit(2.0))])
async def api_get_cluster_key(session: dict = Depends(require_admin)):
    """返回本机集群密钥明文 —— 其它面板把本机加为节点时需要填这个。

    过去这个值在 UI 上无处可见，运维只能去 SQLite 里手动 SELECT。
    """
    return {"status": "ok", "api_key": app_config.get("api_key") or ""}


@router.post("/cluster/key/reset", dependencies=[Depends(speed_limit(5.0))])
async def api_reset_cluster_key(request: Request, session: dict = Depends(require_admin)):
    new_key = uuid_mod.uuid4().hex
    app_config.set("api_key", new_key)
    cluster_manager.init()
    operation_logger.warning(
        "cluster_key_reset",
        build_operator_payload(request, session, {}),
    )
    return {
        "status": "ok",
        "api_key": new_key,
        "warning": "已重置本机集群密钥。所有把本机加为节点的其它面板必须更新为新密钥，否则会显示离线。",
    }


# ============ 节点 CRUD ============

@router.get("/nodes", dependencies=[Depends(speed_limit(2.0))])
async def api_get_nodes(quick: bool = False, refresh: bool = False,
                        session: dict = Depends(require_admin)):
    if quick:
        nodes = await cluster_manager.get_nodes_quick()
    else:
        nodes = await cluster_manager.get_nodes_with_status_async(force=refresh)
    return {"status": "ok", "nodes": nodes}


@router.post("/nodes/probe", dependencies=[Depends(speed_limit(2.0))])
async def api_probe_node(req: NodeProbeRequest, session: dict = Depends(require_admin)):
    """握手探测 —— 添加节点前先验证地址与密钥，返回可读的失败原因。"""
    return {"status": "ok", "probe": await cluster_manager.probe_node(
        req.address, req.api_key, req.insecure_tls,
    )}


@router.post("/nodes", dependencies=[Depends(speed_limit(5.0))])
async def api_add_node(
    req: NodeRequest, request: Request,
    force: bool = False,
    session: dict = Depends(require_admin),
):
    valid, reason = cluster_manager.validate_address(req.address)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)
    if not req.api_key:
        raise HTTPException(status_code=400, detail="请填写对方节点的集群密钥（在对方面板的集群设置里查看）")

    existing = cluster_manager.find_node_by_address(req.address)
    if existing and existing.get("enabled", 1):
        raise HTTPException(
            status_code=409,
            detail=f"该地址已经添加为节点「{existing['name']}」，请勿重复添加",
        )

    probe = await cluster_manager.probe_node(req.address, req.api_key, req.insecure_tls)
    if not probe.get("ok") and not force:
        # 探测失败时不静默入库 —— 过去无论成败都返回 ok，用户看到绿色提示却得到一个死节点。
        return JSONResponse(
            status_code=422,
            content={"status": "error", "probe": probe,
                     "message": probe.get("detail", "节点连接测试失败")},
        )

    if existing:
        # 复活软删除的节点，沿用原 ID，用户对该节点实例的授权因此不会失效。
        node_id = existing["id"]
        cluster_manager.revive_node(node_id, req.name, req.address, req.api_key, req.insecure_tls)
    else:
        node_id = "node-" + uuid_mod.uuid4().hex[:8]
        cluster_manager.add_node(node_id, req.name, req.address, req.api_key, req.insecure_tls)

    operation_logger.info(
        "node_add",
        build_operator_payload(
            request,
            session,
            {
                "node_name": req.name,
                "node_address": req.address,
                "node_id": node_id,
                "revived": bool(existing),
                "probe_ok": probe.get("ok", False),
            },
        ),
    )
    return {"status": "ok", "node_id": node_id, "probe": probe, "revived": bool(existing)}


@router.put("/nodes/{node_id}", dependencies=[Depends(speed_limit(5.0))])
async def api_edit_node(
    node_id: str, req: NodeRequest, request: Request,
    session: dict = Depends(require_admin),
):
    if node_id == "local":
        # 本地节点只允许改备注名；地址与密钥由本机自身配置决定。
        node = cluster_manager._get_node("local")
        address = node.get("address", "127.0.0.1:8000") if node else "127.0.0.1:8000"
        if not cluster_manager.update_node(node_id, req.name, address):
            raise HTTPException(status_code=404, detail="本地节点记录缺失，请重启服务以完成数据库迁移")
        return {"status": "ok"}

    valid, reason = cluster_manager.validate_address(req.address)
    if not valid:
        raise HTTPException(status_code=400, detail=reason)
    api_key = req.api_key if req.api_key and req.api_key != _MASKED_SECRET else None
    if not cluster_manager.update_node(node_id, req.name, req.address, api_key, req.insecure_tls):
        raise HTTPException(status_code=404, detail="节点不存在")
    operation_logger.info(
        "node_edit",
        build_operator_payload(
            request,
            session,
            {
                "node_id": node_id,
                "node_name": req.name,
                "node_address": req.address,
                "api_key_updated": api_key is not None,
            },
        ),
    )
    return {"status": "ok"}


@router.delete("/nodes/{node_id}", dependencies=[Depends(speed_limit(5.0))])
async def api_delete_node(
    node_id: str, request: Request,
    session: dict = Depends(require_admin),
):
    if node_id == "local":
        raise HTTPException(status_code=400, detail="本地节点不可删除")
    node = cluster_manager._get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    cluster_manager.delete_node(node_id)

    # 级联清理：不清的话该节点的容器会以绿色 running 永久滞留在面板上，
    # 因为状态引擎只清理"成功响应过的节点"，已删除的节点再也不会被查询。
    from services.instance_subsystem import instance_subsystem
    removed = instance_subsystem.remove_by_node(node_id)

    operation_logger.warning(
        "node_delete",
        build_operator_payload(
            request,
            session,
            {
                "node_id": node_id,
                "node_name": node.get("name", "Unknown"),
                "instances_cleared": removed,
            },
        ),
    )
    return {
        "status": "ok",
        "instances_cleared": removed,
        "message": "节点已移除。重新添加同一地址时会自动复用原节点 ID，用户授权不会失效。",
    }


# ============ 节点程序日志 ============

@router.get("/node/logs", dependencies=[Depends(speed_limit(2.0))])
async def get_node_logs(
    lines: int = 500,
    node_id: str = "local",
    session: dict = Depends(require_admin),
):
    """获取节点程序运行日志（非容器日志）。

    - 本地节点：直接读取内存环形缓冲区
    - 远程节点：代理请求远程节点的 /api/node/logs
    """
    if lines < 1 or lines > 5000:
        lines = 500

    if node_id == "local" or not node_id:
        from services.log import get_node_logs as _get_logs
        return {"status": "ok", "logs": _get_logs(lines)}

    # 远程节点：异步代理获取
    code, body, _ = await cluster_manager.proxy_to_node_async(
        node_id, "GET", f"/api/node/logs?lines={lines}",
    )
    if code == 200 and body:
        import json
        data = json.loads(body)
        return {"status": "ok", "logs": data.get("logs", "")}
    return {"status": "error", "logs": ""}


# ============ 节点代理 ============

# 允许代理的路径前缀白名单
_PROXY_PATH_WHITELIST = (
    "containers",
    "cluster/status",
    "node/logs",
    "qr",
)


@router.api_route(
    "/nodes/{node_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    dependencies=[Depends(speed_limit(1.0, admin_exempt=False))],
)
async def proxy_node_request(
    node_id: str, path: str, request: Request,
    session: dict = Depends(require_admin),
):
    # 路径白名单校验 - 防止泛化代理滥用
    if not any(path == prefix or path.startswith(prefix + "/") for prefix in _PROXY_PATH_WHITELIST):
        raise HTTPException(status_code=403, detail=f"Proxy path not allowed: {path}")

    nodes = cluster_manager.get_nodes()
    node = next((n for n in nodes if n["id"] == node_id), None)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    # 构建查询参数字符串
    qs = str(request.query_params)
    full_path = f"/api/{path}" + (f"?{qs}" if qs else "")
    body = await request.body()

    code, resp_body, ct = await cluster_manager.proxy_to_node_async(
        node_id, request.method, full_path,
        timeout=10.0, data=body if body else None,
    )
    if resp_body is not None:
        return Response(content=resp_body, status_code=code, media_type=ct)
    return JSONResponse(
        content={"status": "error", "message": "Node unreachable"},
        status_code=502,
    )

