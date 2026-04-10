"""
容器运行态路由 - 操作 / 统计 / 日志 / QR / 登录刷新 / 内部登录事件
"""

import base64
import json
import os
import re
import shutil
import time
from enum import Enum
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from middleware.auth import (
    check_instance_permission,
    get_api_key_user,
    get_current_user,
)
from middleware.rate_limiter import public_speed_limit, speed_limit
from services.cluster_manager import cluster_manager
from services.config import app_config, get_data_dir
from services.container_state import state_engine
from services.docker_async import async_docker_manager
from services.docker_manager import docker_manager, read_login_cache
from services.log import logger
from services.operation_log_context import build_operator_payload
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["containers"])


class ContainerAction(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    PAUSE = "pause"
    UNPAUSE = "unpause"
    KILL = "kill"
    DELETE = "delete"


_ALLOWED_DATA_SCOPES = {"all", "config", "cache", "logs"}
_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class RecreateRequest(BaseModel):
    node_id: str = "local"
    clean_data: bool = False
    keep_config: bool = False
    docker_image: str | None = None
    webui_port: int = 0
    http_port: int = 0
    ws_port: int = 0
    memory_limit: int | None = None
    restart_policy: str | None = None
    network_mode: str | None = None
    env_vars: list[str] | None = None


def _get_request_id(request: Request) -> str:
    request_id = (request.headers.get("x-request-id") or "").strip()
    return request_id or uuid4().hex


def _build_error(
    status_code: int, code: str, message: str, request_id: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "code": code,
            "message": message,
            "request_id": request_id,
        },
    )


def _validate_container_name(name: str) -> bool:
    return bool(_CONTAINER_NAME_RE.match(name or ""))


def _validate_scope(scope: str) -> str:
    scope_value = (scope or "all").strip().lower()
    if scope_value not in _ALLOWED_DATA_SCOPES:
        raise HTTPException(status_code=400, detail="INVALID_SCOPE")
    return scope_value


def _instance_root_path(name: str) -> Path:
    base = Path(get_data_dir()).resolve()
    root = (base / name).resolve()
    if base not in root.parents and root != base:
        raise HTTPException(status_code=400, detail="INVALID_PATH")
    return root


def _cleanup_instance_services(name: str) -> None:
    """统一清理与指定容器关联的内存态服务资源（实例状态 / 登录缓存 / WS 注册表）。

    在容器删除或全量数据清理时调用，避免过期数据残留。
    所有操作均为同步、幂等，失败不抛异常。
    """
    # 1. 清理 instance_subsystem 中的实例状态
    try:
        from services.instance_subsystem import instance_subsystem

        instance_subsystem.remove(name)
        logger.info("已清理实例状态: %s", name)
    except Exception as e:
        logger.debug("清理实例状态失败 [%s]: %s", name, e)

    # 2. 清理登录缓存
    try:
        from services.docker_login import clear_login_cache

        if clear_login_cache(name):
            logger.info("已清理登录缓存: %s", name)
    except Exception as e:
        logger.debug("清理登录缓存失败 [%s]: %s", name, e)

    # 3. 清理 NapCat WS 服务注册表 + API 代理
    try:
        from services.napcat_ws_service import napcat_ws_service

        napcat_ws_service.cleanup(name)
    except Exception as e:
        logger.debug("清理 WS 服务注册表失败 [%s]: %s", name, e)


def _schedule_bs_cleanup(name: str) -> None:
    """调度异步清理 BotShepherd 连接配置（fire-and-forget）。

    在有运行中的事件循环时创建异步任务；无事件循环时静默跳过。
    """
    try:
        import asyncio
        from services.botshepherd import botshepherd_manager

        async def _cleanup_bs():
            try:
                await botshepherd_manager.delete_connection(name)
                logger.info("已清理 BS 连接配置: %s", name)
            except Exception as e:
                logger.debug("清理 BS 连接配置失败 [%s]: %s", name, e)

        try:
            asyncio.get_running_loop()
            asyncio.create_task(_cleanup_bs())
        except RuntimeError:
            pass
    except Exception as e:
        logger.debug("调度 BS 清理任务失败 [%s]: %s", name, e)


def _clear_instance_data(name: str, scope: str, keep_config: bool = False) -> list[str]:
    root = _instance_root_path(name)
    if not root.exists():
        return []
    targets: list[str]
    if scope == "all":
        targets = ["qq_data", "config", "plugins", "cache", "logs"]
    elif scope == "config":
        targets = ["config"]
    elif scope == "cache":
        targets = ["cache"]
    else:
        targets = ["logs"]
    if keep_config and "config" in targets:
        targets = [item for item in targets if item != "config"]
    cleared: list[str] = []
    for item in targets:
        target_path = (root / item).resolve()
        if root not in target_path.parents:
            continue
        if target_path.exists():
            shutil.rmtree(target_path, ignore_errors=True)
            cleared.append(item)
        if item in {"qq_data", "config", "plugins", "cache"}:
            os.makedirs(target_path, exist_ok=True)

    # 清理 BS 注入标记和内部状态（scope=all 时）
    if scope == "all":
        # 1. 删除 BS 注入标记目录
        bs_marker_dir = root / ".bs_injected"
        if bs_marker_dir.exists():
            shutil.rmtree(bs_marker_dir, ignore_errors=True)
            logger.info("已清理 BS 注入标记: %s", name)

        # 2-4. 统一清理内存态服务资源
        _cleanup_instance_services(name)

        # 5. 异步清理 BotShepherd 连接配置
        _schedule_bs_cleanup(name)

    return cleared


def _parse_env_list(env_vars: list[str] | None) -> dict[str, str]:
    if env_vars is None:
        return {}
    env: dict[str, str] = {}
    for item in env_vars:
        if "=" not in item:
            raise HTTPException(status_code=400, detail=f"INVALID_ENV:{item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise HTTPException(status_code=400, detail=f"INVALID_ENV:{item}")
        env[key] = value
    return env


def _snapshot_container(name: str) -> dict:
    if not docker_manager.client:
        return {}
    container = docker_manager.client.containers.get(name)
    attrs = container.attrs or {}
    config = attrs.get("Config") or {}
    host_cfg = attrs.get("HostConfig") or {}
    port_bindings = host_cfg.get("PortBindings") or {}

    def _host_port(container_port: str) -> int:
        value = port_bindings.get(container_port) or []
        if not value or not isinstance(value, list):
            return 0
        first = value[0] if isinstance(value[0], dict) else {}
        p = str(first.get("HostPort", "0"))
        return int(p) if p.isdigit() else 0

    memory_bytes = int(host_cfg.get("Memory") or 0)
    memory_mb = memory_bytes // (1024 * 1024) if memory_bytes > 0 else 0
    env_dict: dict[str, str] = {}
    for line in config.get("Env") or []:
        if isinstance(line, str) and "=" in line:
            k, v = line.split("=", 1)
            env_dict[k] = v

    return {
        "image": str(config.get("Image") or ""),
        "webui_port": _host_port("6099/tcp"),
        "http_port": _host_port("3000/tcp"),
        "ws_port": _host_port("3001/tcp"),
        "memory_limit": memory_mb,
        "restart_policy": str((host_cfg.get("RestartPolicy") or {}).get("Name") or ""),
        "network_mode": str(host_cfg.get("NetworkMode") or ""),
        "env": env_dict,
    }


def _is_running(name: str) -> bool:
    containers = state_engine.get_containers()
    for item in containers:
        if item.get("name") == name:
            return item.get("status") == "running"
    return False


@router.delete("/containers/{name}/data", dependencies=[Depends(speed_limit(2.0))])
async def api_clear_container_data(
    name: str,
    request: Request,
    scope: str = "all",
    node_id: str = "local",
    session: dict = Depends(get_api_key_user),
):
    request_id = _get_request_id(request)
    if not _validate_container_name(name):
        return _build_error(400, "INVALID_NAME", "invalid container name", request_id)

    try:
        scope_value = _validate_scope(scope)
    except HTTPException:
        return _build_error(
            400, "INVALID_SCOPE", "scope must be all|config|cache|logs", request_id
        )

    if not check_instance_permission(session, node_id, name):
        return _build_error(
            403, "NO_PERMISSION", "no permission for this instance", request_id
        )

    if node_id != "local":
        code, body, _ = await cluster_manager.proxy_to_node_async(
            node_id,
            "DELETE",
            f"/api/containers/{name}/data?scope={scope_value}&node_id=local",
            timeout=10.0,
        )
        if code >= 400:
            message = (
                body.decode("utf-8", errors="ignore") if body else "remote clear failed"
            )
            return _build_error(code, "REMOTE_CLEAN_FAILED", message, request_id)
        remote_data = (
            json.loads(body)
            if body
            else {"status": "ok", "name": name, "cleared": [], "restarted": False}
        )
        if isinstance(remote_data, dict):
            remote_data["request_id"] = request_id
        return remote_data

    was_running = _is_running(name)
    if was_running:
        stopped = await async_docker_manager.action_container(name, "stop")
        if not stopped:
            return _build_error(
                500, "STOP_FAILED", "failed to stop container before clear", request_id
            )

    try:
        cleared = _clear_instance_data(name, scope_value)
    except HTTPException as exc:
        return _build_error(
            exc.status_code, str(exc.detail), "invalid data path", request_id
        )
    except Exception as exc:
        return _build_error(
            500, "CLEAN_FAILED", f"clear data failed: {exc}", request_id
        )

    restarted = False
    if was_running:
        restarted = await async_docker_manager.action_container(name, "start")

    state_engine.notify_change()
    operation_logger.info(
        "container_data_clear",
        build_operator_payload(
            request,
            session,
            {
                "request_id": request_id,
                "container_name": name,
                "node_id": node_id,
                "scope": scope_value,
                "cleared": cleared,
                "restarted": restarted,
            },
        ),
    )
    return {
        "status": "ok",
        "name": name,
        "cleared": cleared,
        "restarted": restarted,
        "request_id": request_id,
    }


@router.post("/containers/{name}/recreate", dependencies=[Depends(speed_limit(1.0))])
async def api_recreate_container(
    name: str,
    req: RecreateRequest,
    request: Request,
    session: dict = Depends(get_api_key_user),
):
    request_id = _get_request_id(request)
    if not _validate_container_name(name):
        return _build_error(400, "INVALID_NAME", "invalid container name", request_id)

    if not check_instance_permission(session, req.node_id, name):
        return _build_error(
            403, "NO_PERMISSION", "no permission for this instance", request_id
        )

    if req.node_id != "local":
        code, body, _ = await cluster_manager.proxy_to_node_async(
            req.node_id,
            "POST",
            f"/api/containers/{name}/recreate",
            timeout=20.0,
            json={
                "node_id": "local",
                "clean_data": req.clean_data,
                "keep_config": req.keep_config,
                "docker_image": req.docker_image,
                "webui_port": req.webui_port,
                "http_port": req.http_port,
                "ws_port": req.ws_port,
                "memory_limit": req.memory_limit,
                "restart_policy": req.restart_policy,
                "network_mode": req.network_mode,
                "env_vars": req.env_vars,
            },
        )
        if code >= 400:
            message = (
                body.decode("utf-8", errors="ignore")
                if body
                else "remote recreate failed"
            )
            return _build_error(code, "REMOTE_RECREATE_FAILED", message, request_id)
        remote_data = json.loads(body) if body else {"status": "ok", "name": name}
        if isinstance(remote_data, dict):
            remote_data["request_id"] = request_id
        return remote_data

    try:
        snapshot = await run_in_threadpool(_snapshot_container, name)
    except Exception:
        return _build_error(404, "NOT_FOUND", "container not found", request_id)

    old_removed = await async_docker_manager.action_container(name, "delete")
    if not old_removed:
        return _build_error(
            500, "RECREATE_DELETE_FAILED", "failed to delete old container", request_id
        )

    cleared: list[str] = []
    if req.clean_data:
        try:
            cleared = _clear_instance_data(name, "all", req.keep_config)
        except HTTPException as exc:
            return _build_error(
                exc.status_code, str(exc.detail), "invalid data path", request_id
            )
        except Exception as exc:
            return _build_error(
                500, "CLEAN_FAILED", f"clear data failed: {exc}", request_id
            )

    data_dir = os.path.join(get_data_dir(), name)
    volumes = {
        os.path.join(data_dir, "qq_data"): {"bind": "/app/.config/QQ", "mode": "rw"},
        os.path.join(data_dir, "config"): {"bind": "/app/napcat/config", "mode": "rw"},
        os.path.join(data_dir, "plugins"): {
            "bind": "/app/napcat/plugins",
            "mode": "rw",
        },
        os.path.join(data_dir, "cache"): {"bind": "/app/napcat/cache", "mode": "rw"},
    }
    for host_dir in volumes:
        os.makedirs(host_dir, exist_ok=True)

    used_ports = await async_docker_manager.get_used_ports()
    webui_port = req.webui_port or int(snapshot.get("webui_port") or 0)
    if webui_port <= 0:
        webui_port = async_docker_manager.find_available_port(
            app_config.get("webui_base_port", 6000), used_ports
        )
    used_ports.add(webui_port)

    http_port = req.http_port or int(snapshot.get("http_port") or 0)
    if http_port <= 0:
        http_port = async_docker_manager.find_available_port(
            app_config.get("http_base_port", 3000), used_ports
        )
    used_ports.add(http_port)

    ws_port = req.ws_port or int(snapshot.get("ws_port") or 0)
    if ws_port <= 0:
        ws_port = async_docker_manager.find_available_port(
            app_config.get("ws_base_port", 3001), used_ports
        )

    image = req.docker_image or str(
        snapshot.get("image")
        or app_config.get("docker_image", "mlikiowa/napcat-docker:latest")
    )

    if req.env_vars is None:
        env_dict = dict(snapshot.get("env") or {})
    else:
        try:
            env_dict = _parse_env_list(req.env_vars)
        except HTTPException:
            return _build_error(
                400, "INVALID_ENV", "env_vars must be KEY=VALUE list", request_id
            )
    if "ACCOUNT" not in env_dict:
        env_dict["ACCOUNT"] = ""

    restart_policy_name = (
        req.restart_policy
        if req.restart_policy is not None
        else str(snapshot.get("restart_policy") or "always")
    )
    if not restart_policy_name or restart_policy_name == "no":
        restart_policy = {"Name": "always"}
    else:
        restart_policy = {"Name": restart_policy_name}

    memory_limit = (
        req.memory_limit
        if req.memory_limit is not None
        else int(snapshot.get("memory_limit") or 0)
    )

    network_mode = (
        req.network_mode
        if req.network_mode is not None
        else str(snapshot.get("network_mode") or "bridge")
    )
    network_mode_arg = (
        network_mode if network_mode and network_mode != "bridge" else None
    )

    cid = await async_docker_manager.create_container(
        name=name,
        image=image,
        volumes=volumes,
        ports={"6099/tcp": webui_port, "3000/tcp": http_port, "3001/tcp": ws_port},
        environment=env_dict,
        restart_policy=restart_policy,
        mem_limit=f"{memory_limit}m" if memory_limit > 0 else None,
        network_mode=network_mode_arg,
    )
    if not cid:
        return _build_error(
            500, "RECREATE_CREATE_FAILED", "failed to create new container", request_id
        )

    state_engine.notify_change()
    operation_logger.info(
        "container_recreate",
        build_operator_payload(
            request,
            session,
            {
                "request_id": request_id,
                "container_name": name,
                "node_id": req.node_id,
                "clean_data": req.clean_data,
                "keep_config": req.keep_config,
                "cleared": cleared,
                "ports": {"webui": webui_port, "http": http_port, "ws": ws_port},
                "image": image,
            },
        ),
    )
    return {
        "status": "ok",
        "name": name,
        "old_removed": True,
        "new_created": True,
        "started": True,
        "cleared": cleared,
        "ports": {"webui": webui_port, "http": http_port, "ws": ws_port},
        "request_id": request_id,
    }


@router.post("/containers/{name}/action", dependencies=[Depends(speed_limit(2.0))])
async def api_container_action(
    name: str,
    action: ContainerAction,
    request: Request,
    node_id: str = "local",
    delete_data: bool = False,
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    action_value = action.value
    success = (
        await async_docker_manager.action_container(name, action_value)
        if node_id == "local"
        else await cluster_manager.action_container_async(node_id, name, action_value)
    )
    if not success:
        raise HTTPException(status_code=500, detail="Action failed")
    state_engine.notify_change()
    if action_value == "delete" and node_id == "local":
        # 统一清理内存态服务资源（登录缓存 / WS 注册表 / 实例状态）
        _cleanup_instance_services(name)

        # 删除数据目录（仅勾选 delete_data 时）
        if delete_data:
            data_dir = os.path.join(get_data_dir(), name)
            if os.path.exists(data_dir):
                shutil.rmtree(data_dir, ignore_errors=True)
                logger.info("已删除本地数据目录: %s", data_dir)

        # 删除 BS 连接配置（BS 已启用时），避免僵尸连接堆积
        from services.config import app_config as _cfg

        if _cfg.get("init_bs_enabled", False):
            try:
                from services.botshepherd import botshepherd_manager

                r = await botshepherd_manager.delete_connection(name)
                if isinstance(r, dict) and r.get("success", True) is not False:
                    logger.info("已删除 BS 连接配置: %s", name)
                else:
                    logger.debug("BS 连接配置删除结果: %s → %s", name, r)
            except Exception as _e:
                logger.debug("删除 BS 连接配置失败（可忽略）: %s → %s", name, _e)
    operation_logger.info(
        "container_action",
        {
            "operator_ip": request.client.host if request.client else "unknown",
            "operator_name": session["userName"],
            "operator_uuid": session.get("uuid"),
            "container_name": name,
            "action": action_value,
            "node_id": node_id,
            "delete_data": delete_data,
        },
    )
    return {"status": "ok"}


@router.get("/containers/{name}/stats")
async def get_container_stats(
    name: str, node_id: str = "local", session: dict = Depends(get_current_user)
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    stats = await cluster_manager.get_stats_async(node_id, name)
    if node_id == "local" and isinstance(stats, dict):
        from services.docker_events import docker_event_watcher
        from services.instance_subsystem import instance_subsystem

        last = docker_event_watcher.get_last_event(name)
        stats["last_event"] = last

        # 以状态引擎为准覆盖登录态（兼容非标准挂载布局）
        inst = instance_subsystem.get(name)
        if inst and inst.uin:
            stats["uin"] = inst.uin
    return stats


@router.get("/containers/{name}/logs")
async def get_container_logs(
    name: str,
    lines: int = 100,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    logs = (
        await async_docker_manager.get_logs(name, lines)
        if node_id == "local"
        else await cluster_manager.get_logs_async(node_id, name, lines)
    )
    return {"status": "ok", "logs": logs}


@router.get("/containers/{name}/logs/download")
async def download_container_logs(
    name: str,
    lines: int = 2000,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    logs = (
        await async_docker_manager.get_logs(name, lines)
        if node_id == "local"
        else await cluster_manager.get_logs_async(node_id, name, lines)
    )
    filename = f"{name}_logs_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    return PlainTextResponse(
        content=logs or "",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/containers/{name}/qrcode", dependencies=[Depends(public_speed_limit(0.5))]
)
async def get_qr_code(name: str, node_id: str = "local"):
    if node_id != "local":
        result = await cluster_manager.get_qr_status_async(node_id, name)
        return result or {"status": "waiting"}

    from services.instance_subsystem import instance_subsystem
    from services.docker_async import async_login_checker

    inst = instance_subsystem.get(name)
    if inst and inst.logged_in:
        return {"status": "logged_in", "uin": inst.uin or ""}

    cached = read_login_cache(name)
    if cached.get("logged_in"):
        return {"status": "logged_in", "uin": cached.get("uin", "")}

    try:
        http_port = inst.http_port if inst else 0
        webui_port = inst.webui_port if inst else 0
        login = await async_login_checker.check_login_status(name, http_port, webui_port)
        if login.get("logged_in"):
            if inst:
                inst.update_login(
                    logged_in=True,
                    uin=login.get("uin", ""),
                    stage="logged_in",
                    method=login.get("method", ""),
                    reason=login.get("reason", ""),
                )
            return {"status": "logged_in", "uin": login.get("uin", "")}
    except Exception as exc:
        logger.debug(f"异步登录检测失败: {exc}")

    _QR_MAX_AGE = 120
    try:
        qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
        if os.path.exists(qr_path):
            qr_mtime = os.path.getmtime(qr_path)
            age = time.time() - qr_mtime
            if age < _QR_MAX_AGE:
                with open(qr_path, "rb") as file_handle:
                    data = base64.b64encode(file_handle.read()).decode("utf-8")
                expires_in = max(0, int(_QR_MAX_AGE - age))
                return {
                    "status": "ok",
                    "url": f"data:image/png;base64,{data}",
                    "type": "file",
                    "generated_at": int(qr_mtime),
                    "expires_in": expires_in,
                    "expires_at": int(qr_mtime + _QR_MAX_AGE),
                }
    except Exception as exc:
        logger.debug(f"读取本地二维码文件失败: {exc}")

    try:
        if docker_manager.client:
            container = docker_manager.client.containers.get(name)
            if container.status != "running":
                return {"status": "waiting"}
            logs = container.logs(tail=80).decode("utf-8", errors="ignore")
            qr_url_match = re.search(r"二维码解码URL:\s*(https://[^\s]+)", logs)
            if qr_url_match:
                return {"status": "ok", "url": qr_url_match.group(1), "type": "log"}
    except Exception as exc:
        logger.debug(f"从日志获取二维码失败: {exc}")
    return {"status": "waiting"}


@router.post("/containers/{name}/refresh-login", dependencies=[Depends(public_speed_limit(0.5))])
async def refresh_login_status(name: str, node_id: str = "local"):
    if node_id != "local":
        return {"status": "ok", "logged_in": False, "method": "remote_unsupported"}

    # 使用异步检测链路（WS/BS/HTTP/文件系统），兼容无 3000 端口映射的容器
    from services.instance_subsystem import instance_subsystem
    from services.docker_async import async_login_checker

    inst = instance_subsystem.get(name)
    http_port = inst.http_port if inst else 0
    webui_port = inst.webui_port if inst else 0

    login = await async_login_checker.check_login_status(name, http_port, webui_port)

    if inst:
        inst.update_login(
            logged_in=login.get("logged_in", False),
            uin=login.get("uin", ""),
            stage=login.get("stage", "waiting"),
            method=login.get("method", ""),
            reason=login.get("reason", ""),
        )
    state_engine.notify_change()
    return {
        "status": "ok",
        "logged_in": login.get("logged_in", False),
        "uin": login.get("uin", ""),
        "nickname": login.get("nickname", ""),
        "method": login.get("method", ""),
    }


@router.post("/internal/login-event")
async def receive_login_event(request: Request):
    internal_key = request.headers.get("x-internal-key", "")
    expected_key = app_config.get("internal_api_key", "")
    if not expected_key or internal_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid internal key")
    body = await request.json()
    container_name = body.get("name", "")
    if not container_name:
        raise HTTPException(status_code=400, detail="Missing container name")
    docker_manager.update_login_cache(container_name, body)
    return {"status": "ok"}


@router.get("/containers/{name}/events")
async def stream_container_events(
    name: str,
    request: Request,
    timeout: int = 60,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    """SSE 事件流 — 推送指定容器的 Docker 生命周期事件。

    每条事件格式（text/event-stream）：
      data: {"name":"...","action":"start","status":"start","time":1700000000,"exit_code":null}

    参数：
      timeout  最长订阅秒数（默认 60，最大 300），超时后服务端主动关闭流。
      node_id  仅 local 节点支持；远程节点返回 501。
    """
    import asyncio
    import json
    from fastapi.responses import StreamingResponse
    from services.docker_events import docker_event_watcher

    if not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Invalid container name")
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if node_id != "local":
        raise HTTPException(
            status_code=501, detail="Event stream only supported on local node"
        )

    _timeout = min(max(timeout, 5), 300)
    loop = asyncio.get_event_loop()
    q = docker_event_watcher.subscribe(name, loop)

    async def _generate():
        try:
            deadline = loop.time() + _timeout
            while True:
                if await request.is_disconnected():
                    break
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    payload = await asyncio.wait_for(
                        q.get(), timeout=min(remaining, 15)
                    )
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # 心跳注释行，保持连接活跃
                    yield ": keep-alive\n\n"
        finally:
            docker_event_watcher.unsubscribe(name, q)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
