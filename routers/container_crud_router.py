"""
容器 CRUD 路由 - 列表 / 创建 / WS 客户端注入
"""

import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import check_instance_permission, get_current_user, require_admin
from middleware.rate_limiter import speed_limit
from services.cluster_manager import cluster_manager
from services.config import app_config, get_data_dir
from services.container_state import state_engine
from services.docker_async import async_docker_manager
from services.log import logger
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["containers"])

_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MAX_ENV_VARS = 20
_ENV_BLOCKED_KEYS = {"ACCOUNT", "HOME", "PATH", "USER"}
_ENV_BLOCKED_PREFIXES = ("DOCKER_", "LD_")


class CreateRequest(BaseModel):
    name: str
    node_id: str = "local"
    docker_image: str = ""
    webui_port: int = 0
    http_port: int = 0
    ws_port: int = 0
    memory_limit: int = 0
    restart_policy: str = "always"
    network_mode: str = "bridge"
    env_vars: list[str] = []


def _parse_env_vars(env_vars: list[str]) -> dict[str, str]:
    if not env_vars:
        return {"ACCOUNT": ""}
    if len(env_vars) > _MAX_ENV_VARS:
        raise HTTPException(status_code=400, detail=f"env_vars exceeds max size {_MAX_ENV_VARS}")
    env: dict[str, str] = {"ACCOUNT": ""}
    for item in env_vars:
        if "=" not in item:
            raise HTTPException(status_code=400, detail="env_vars item must be KEY=VALUE format")
        key, value = item.split("=", 1)
        key = key.strip()
        if not _ENV_KEY_RE.match(key):
            raise HTTPException(status_code=400, detail=f"Invalid env key: {key}")
        if key in _ENV_BLOCKED_KEYS or any(key.startswith(prefix) for prefix in _ENV_BLOCKED_PREFIXES):
            raise HTTPException(status_code=400, detail=f"Blocked env key: {key}")
        env[key] = value
    return env


def _generate_onebot11_config_with_ws_client(config_dir: str, ws_client_url: str, ws_client_token: str = "", uin: str = "default") -> None:
    config_file = os.path.join(config_dir, f"onebot11_{uin}.json")
    ws_client_config = {
        "name": "botshepherd",
        "enable": True,
        "url": ws_client_url,
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": ws_client_token,
        "debug": False,
        "heartInterval": 30000,
        "reconnectInterval": 30000,
    }
    full_config = {"network": {}}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as file_handle:
                full_config = json.load(file_handle)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取现有配置失败，将重新生成: {exc}")
    network = full_config.get("network", {})
    ws_clients = network.get("websocketClients", []) if isinstance(network.get("websocketClients", []), list) else []
    if not any(item.get("url") == ws_client_url for item in ws_clients if isinstance(item, dict)):
        ws_clients.append(ws_client_config)
        network["websocketClients"] = ws_clients
        full_config["network"] = network
        with open(config_file, "w", encoding="utf-8") as file_handle:
            json.dump(full_config, file_handle, indent=2, ensure_ascii=False)
        logger.info(f"已为账号 {uin} 注入/更新 WS 客户端配置: {config_file}")


@router.get("/containers")
async def api_list_containers(session: dict = Depends(get_current_user)):
    from services.user_manager import user_manager, ROLE
    containers = state_engine.get_containers()
    # 管理员返回全量，普通用户仅返回分配的实例
    if session.get("permission", 0) >= ROLE.ADMIN:
        return {"status": "ok", "containers": containers}
    user = user_manager.get_user_by_uuid(session["uuid"])
    if not user:
        return {"status": "ok", "containers": []}
    allowed = {(inst.get("node_id"), inst.get("container_name")) for inst in user.get("instances", [])}
    filtered = [c for c in containers if (c.get("node_id", "local"), c["name"]) in allowed]
    return {"status": "ok", "containers": filtered}


@router.post("/containers", dependencies=[Depends(speed_limit(5.0))])
async def api_create_container(req: CreateRequest, request: Request, session: dict = Depends(require_admin)):
    if not _CONTAINER_NAME_RE.match(req.name):
        raise HTTPException(status_code=400, detail="容器名称只能包含字母、数字、连字符、下划线和点号，长度 1-64 字符，且必须以字母或数字开头")
    if req.node_id != "local":
        node = next((item for item in cluster_manager.get_nodes() if item["id"] == req.node_id), None)
        if not node:
            raise HTTPException(status_code=400, detail="Invalid node_id")
        _, body, _ = await cluster_manager.proxy_to_node_async(req.node_id, "POST", "/api/containers", timeout=5.0, json={"name": req.name, "node_id": "local", "docker_image": req.docker_image, "webui_port": req.webui_port, "http_port": req.http_port, "ws_port": req.ws_port, "memory_limit": req.memory_limit, "restart_policy": req.restart_policy, "network_mode": req.network_mode, "env_vars": req.env_vars})
        return json.loads(body) if body else {"status": "error", "message": "Remote node unreachable"}
    data_dir = os.path.join(get_data_dir(), req.name)
    volumes = {
        os.path.join(data_dir, "qq_data"): {"bind": "/app/.config/QQ", "mode": "rw"},
        os.path.join(data_dir, "config"): {"bind": "/app/napcat/config", "mode": "rw"},
        os.path.join(data_dir, "plugins"): {"bind": "/app/napcat/plugins", "mode": "rw"},
        os.path.join(data_dir, "cache"): {"bind": "/app/napcat/cache", "mode": "rw"},
    }
    for host_dir in volumes:
        os.makedirs(host_dir, exist_ok=True)
    used_ports = await async_docker_manager.get_used_ports()
    webui_port = req.webui_port if req.webui_port > 0 else async_docker_manager.find_available_port(app_config.get("webui_base_port", 6000), used_ports)
    used_ports.add(webui_port)
    http_port = req.http_port if req.http_port > 0 else async_docker_manager.find_available_port(app_config.get("http_base_port", 3000), used_ports)
    used_ports.add(http_port)
    ws_port = req.ws_port if req.ws_port > 0 else async_docker_manager.find_available_port(app_config.get("ws_base_port", 3001), used_ports)
    cid = await async_docker_manager.create_container(name=req.name, image=req.docker_image or app_config.get("docker_image", "mlikiowa/napcat-docker:latest"), volumes=volumes, ports={"6099/tcp": webui_port, "3000/tcp": http_port, "3001/tcp": ws_port}, environment=_parse_env_vars(req.env_vars or []), restart_policy={"Name": req.restart_policy} if req.restart_policy and req.restart_policy != "no" else {"Name": "always"}, mem_limit=f"{req.memory_limit}m" if req.memory_limit > 0 else None, network_mode=req.network_mode if req.network_mode != "bridge" else None)
    if not cid:
        raise HTTPException(status_code=500, detail="Failed to create container")
    state_engine.notify_change()
    operation_logger.info("container_create", {"operator_ip": request.client.host if request.client else "unknown", "operator_name": session["userName"], "operator_uuid": session.get("uuid"), "container_name": req.name, "node_id": req.node_id, "ports": {"webui": webui_port, "http": http_port, "ws": ws_port}})
    return {"status": "ok", "container_id": cid, "ports": {"webui": webui_port, "http": http_port, "ws": ws_port}}


@router.post("/containers/{name}/inject-ws-client")
async def api_inject_ws_client(name: str, uin: str = "default", node_id: str = "local", session: dict = Depends(get_current_user)):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if not app_config.get("init_ws_client_enabled", False):
        raise HTTPException(status_code=400, detail="WS Client Injection is disabled in cluster settings")
    ws_client_url = str(app_config.get("init_ws_client_url", ""))
    if not ws_client_url:
        raise HTTPException(status_code=400, detail="Injection URL is not configured")
    _generate_onebot11_config_with_ws_client(os.path.join(get_data_dir(), name, "config"), ws_client_url, str(app_config.get("init_ws_client_token", "")), uin)
    return {"status": "ok", "message": f"Injected into onebot11_{uin}.json"}


_ALLOWED_NET_KEYS = frozenset({
    "httpServers",
    "httpClients",
    "httpSseServers",
    "websocketServers",
    "websocketClients",
})


class InjectNetworkConfigRequest(BaseModel):
    uin: str = "default"
    node_id: str = "local"
    network: dict


@router.post("/containers/{name}/inject-network-config")
async def api_inject_network_config(
    name: str,
    req: InjectNetworkConfigRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    """覆盖写入 onebot11_{uin}.json 中的指定网络端点键。
    只允许白名单内的键；其余键保持原值不变。
    """
    bad_keys = set(req.network.keys()) - _ALLOWED_NET_KEYS
    if bad_keys:
        raise HTTPException(status_code=400, detail=f"不允许的网络配置键: {bad_keys}")

    config_dir = os.path.join(get_data_dir(), name, "config")
    config_file = os.path.join(config_dir, f"onebot11_{req.uin}.json")

    full_config: dict = {"network": {}}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as fh:
                full_config = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取配置失败，将覆盖重建: {exc}")

    network = full_config.get("network", {})
    for key, value in req.network.items():
        network[key] = value
    full_config["network"] = network

    os.makedirs(config_dir, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as fh:
        json.dump(full_config, fh, indent=2, ensure_ascii=False)

    operator_ip = request.client.host if request.client else "unknown"
    operation_logger.info(
        "inject_network_config",
        {
            "operator_ip": operator_ip,
            "operator_name": session["userName"],
            "operator_uuid": session.get("uuid"),
            "container_name": name,
            "uin": req.uin,
            "keys_updated": list(req.network.keys()),
        },
    )
    logger.info(f"[{name}] 注入网络配置 uin={req.uin} keys={list(req.network.keys())}")
    return {"status": "ok", "message": f"已写入 onebot11_{req.uin}.json", "keys_updated": list(req.network.keys())}

