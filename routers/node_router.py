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
from services.operation_logger import operation_logger
from services.operation_log_context import build_operator_payload

router = APIRouter(prefix="/api", tags=["nodes"])


class NodeRequest(BaseModel):
    name: str
    address: str
    api_key: str
    node_id: str = "local"


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
            "api_key": ("***" if app_config.get("api_key") else ""),
            "data_dir": app_config.get("data_dir"),
            "init_ws_client_enabled": app_config.get("init_ws_client_enabled", False),
            "init_ws_client_url": app_config.get("init_ws_client_url", "ws://127.0.0.1:5100/onebot/v11/ws"),
            "init_ws_client_token": app_config.get("init_ws_client_token", ""),
            "init_bs_enabled": app_config.get("init_bs_enabled", False),
            "init_bs_client_base_port": app_config.get("init_bs_client_base_port", 6100),
            "init_bs_napcat_host": app_config.get("init_bs_napcat_host", "172.17.0.1"),
            "init_bs_targets": app_config.get("init_bs_targets", "[]"),
            "manager_host": app_config.get("manager_host", "127.0.0.1"),
            "manager_port": app_config.get("manager_port", 8000),
            "init_auto_join_groups_enabled": app_config.get("init_auto_join_groups_enabled", False),
            "init_auto_join_groups": app_config.get("init_auto_join_groups", "[]"),
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
                     "init_ws_client_enabled", "init_ws_client_url", "init_ws_client_token",
                     "init_bs_enabled", "init_bs_client_base_port", "init_bs_napcat_host", "init_bs_targets",
                     "manager_host", "manager_port",
                     "init_auto_join_groups_enabled", "init_auto_join_groups"}
    updates = {k: v for k, v in body.items() if k in allowed_keys}

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
        "instances": daemon_monitor.get_instance_status(),
        "chart": daemon_monitor.get_chart_data(),
    }


# ============ 节点 CRUD ============

@router.get("/nodes", dependencies=[Depends(speed_limit(2.0))])
async def api_get_nodes(quick: bool = False, session: dict = Depends(require_admin)):
    if quick:
        nodes = await cluster_manager.get_nodes_quick()
    else:
        nodes = await cluster_manager.get_nodes_with_status_async()
    return {"status": "ok", "nodes": nodes}


@router.post("/nodes", dependencies=[Depends(speed_limit(5.0))])
async def api_add_node(
    req: NodeRequest, request: Request,
    session: dict = Depends(require_admin),
):
    new_id = "node-" + uuid_mod.uuid4().hex[:8]
    cluster_manager.add_node(new_id, req.name, req.address, req.api_key)
    operation_logger.info(
        "node_add",
        build_operator_payload(
            request,
            session,
            {
                "node_name": req.name,
                "node_address": req.address,
                "node_id": new_id,
            },
        ),
    )
    return {"status": "ok", "node_id": new_id}


@router.put("/nodes/{node_id}", dependencies=[Depends(speed_limit(5.0))])
async def api_edit_node(
    node_id: str, req: NodeRequest, request: Request,
    session: dict = Depends(require_admin),
):
    cluster_manager.update_node(node_id, req.name, req.address, req.api_key or None)
    if node_id == "local" and req.api_key:
        app_config.set("api_key", req.api_key)
    operation_logger.info(
        "node_edit",
        build_operator_payload(
            request,
            session,
            {
                "node_id": node_id,
                "node_name": req.name,
                "node_address": req.address,
                "api_key_updated": bool(req.api_key),
            },
        ),
    )
    return {"status": "ok"}


@router.delete("/nodes/{node_id}", dependencies=[Depends(speed_limit(5.0))])
async def api_delete_node(
    node_id: str, request: Request,
    session: dict = Depends(require_admin),
):
    nodes = cluster_manager.get_nodes()
    node = next((n for n in nodes if n["id"] == node_id), None)
    cluster_manager.delete_node(node_id)
    operation_logger.warning(
        "node_delete",
        build_operator_payload(
            request,
            session,
            {
                "node_id": node_id,
                "node_name": node["name"] if node else "Unknown",
            },
        ),
    )
    return {"status": "ok"}


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

