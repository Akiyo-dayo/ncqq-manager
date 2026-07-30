"""
容器运行态路由 - 操作 / 统计 / 日志 / QR / 登录刷新 / 内部登录事件
"""

import asyncio
import base64
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
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
    require_admin,
)
from middleware.rate_limiter import public_speed_limit, speed_limit
from services.cluster_manager import cluster_manager
from services.config import app_config, get_data_dir
from services.action_jobs import action_job_manager
from services.container_state import state_engine
from services.instance_subsystem import instance_subsystem
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
# 后台任务强引用池 — 事件循环只持弱引用，不存住会被 GC 静默取消。
_BACKGROUND_TASKS: set = set()
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



_QR_MAX_AGE = 120
_QR_CONTAINER_PATH = "/app/napcat/cache/qrcode.png"
_QR_URL_RE = re.compile(r"(?:二维码解码URL|qrcode|QR(?:Code)? URL)[:：\s]*(https?://\S+)", re.IGNORECASE)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _iso_from_ts(ts: float | int | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _qr_time_fields(generated_at: float | int | None, *, fetched_at: float | int | None = None) -> dict:
    fetched = float(fetched_at or time.time())
    generated = float(generated_at or fetched)
    age = max(0, int(fetched - generated))
    expires_at = int(generated + _QR_MAX_AGE)
    return {
        "generated_at": int(generated),
        "generated_at_iso": _iso_from_ts(generated),
        "fetched_at": int(fetched),
        "fetched_at_iso": _iso_from_ts(fetched),
        "age_seconds": age,
        "expires_at": expires_at,
        "expires_at_iso": _iso_from_ts(expires_at),
        "expires_in": max(0, int(_QR_MAX_AGE - age)),
        "max_age_seconds": _QR_MAX_AGE,
    }


def _qr_waiting_response(status: str = "waiting", *, source: str = "none", fetched_at: float | None = None, **extra) -> dict:
    fetched = fetched_at or time.time()
    payload = {
        "status": status,
        "source": source,
        "type": source,
        "generated_at": 0,
        "generated_at_iso": "",
        "fetched_at": int(fetched),
        "fetched_at_iso": _iso_from_ts(fetched),
        "age_seconds": None,
        "expires_at": 0,
        "expires_at_iso": "",
        "expires_in": None,
        "max_age_seconds": _QR_MAX_AGE,
    }
    payload.update(extra)
    return payload


def _build_png_qr_response(raw: bytes, generated_at: float, *, source: str, fetched_at: float | None = None) -> dict | None:
    if not raw or not raw.startswith(_PNG_MAGIC):
        logger.debug("忽略无效 PNG 二维码 source=%s size=%s", source, len(raw or b""))
        return None
    image_base64 = base64.b64encode(raw).decode("utf-8")
    return {
        "status": "ok",
        "url": f"data:image/png;base64,{image_base64}",
        "image_base64": image_base64,
        "content_type": "image/png",
        "type": "image",
        "source": source,
        **_qr_time_fields(generated_at, fetched_at=fetched_at),
    }



def _expire_if_stale_qr(result: dict) -> dict:
    if result.get("status") != "ok":
        return result
    age = result.get("age_seconds")
    if not isinstance(age, int) or age <= _QR_MAX_AGE:
        return result
    generated_at = float(result.get("generated_at") or 0)
    fetched_at = float(result.get("fetched_at") or time.time())
    return {
        "status": "expired",
        "source": result.get("source") or "stale_qr",
        "type": result.get("type") or "stale_qr",
        **_qr_time_fields(generated_at, fetched_at=fetched_at),
    }

def _parse_docker_log_timestamp(line: str) -> float | None:
    # Docker timestamps look like 2026-06-08T01:23:45.123456789Z prefixing the log line.
    token = (line or "").split(" ", 1)[0].strip()
    if not token or "T" not in token:
        return None
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    # Python accepts at most 6 fractional digits.
    if "." in token:
        head, tail = token.split(".", 1)
        tz = ""
        for sep in ("+", "-"):
            if sep in tail:
                frac, rest = tail.split(sep, 1)
                tz = sep + rest
                break
        else:
            frac = tail
        token = head + "." + frac[:6] + tz
    try:
        return datetime.fromisoformat(token).timestamp()
    except Exception:
        return None


def _latest_qr_url_from_logs(logs: str) -> tuple[str, float | None]:
    latest_url = ""
    latest_ts: float | None = None
    for line in (logs or "").splitlines():
        matches = _QR_URL_RE.findall(line)
        if not matches:
            continue
        # Use the last URL on this line and keep scanning, so final result is the latest log URL.
        latest_url = matches[-1].rstrip("\"'<>),;")
        latest_ts = _parse_docker_log_timestamp(line) or latest_ts
    return latest_url, latest_ts


async def _get_local_qr_status(name: str) -> dict:
    fetched_at = time.time()

    try:
        inst = instance_subsystem.get(name)
        if inst and inst.logged_in:
            return {
                "status": "logged_in",
                "uin": inst.uin or "",
                "last_uin": inst.last_uin,
                "source": "login_state",
                "type": "login_state",
                "fetched_at": int(fetched_at),
                "fetched_at_iso": _iso_from_ts(fetched_at),
                "age_seconds": 0,
            }
    except Exception:
        pass

    try:
        status = await asyncio.to_thread(docker_manager.get_container_status, name)
        if status and status != "running":
            return _qr_waiting_response("waiting", source="container_not_running", fetched_at=fetched_at)
    except Exception as exc:
        logger.debug("获取容器状态失败 [%s]: %s", name, exc)

    png_result: dict | None = None
    try:
        container_file = await asyncio.to_thread(
            docker_manager.get_container_file_binary_with_mtime,
            name,
            _QR_CONTAINER_PATH,
        )
        if container_file:
            raw, mtime = container_file
            png_result = _build_png_qr_response(raw, mtime or fetched_at, source="container_file", fetched_at=fetched_at)
    except Exception as exc:
        logger.debug("读取容器内二维码失败 [%s]: %s", name, exc)

    # Host bind mount fallback. Kept after container_file because remote fresh reads should not rely on state cache.
    if not png_result:
        try:
            qr_path = os.path.join(get_data_dir(), name, "cache", "qrcode.png")
            if os.path.exists(qr_path):
                qr_mtime = os.path.getmtime(qr_path)
                with open(qr_path, "rb") as file_handle:
                    raw = file_handle.read()
                png_result = _build_png_qr_response(raw, qr_mtime, source="host_file", fetched_at=fetched_at)
        except Exception as exc:
            logger.debug("读取宿主二维码文件失败 [%s]: %s", name, exc)

    log_result: dict | None = None
    try:
        logs = await asyncio.to_thread(docker_manager.get_logs_with_timestamps, name, 300)
        qr_url, log_ts = _latest_qr_url_from_logs(logs)
        if qr_url:
            generated_at = log_ts or fetched_at
            log_result = {
                "status": "ok",
                "url": qr_url,
                "type": "url",
                "source": "log_latest",
                **_qr_time_fields(generated_at, fetched_at=fetched_at),
            }
    except Exception as exc:
        logger.debug("从日志获取二维码失败 [%s]: %s", name, exc)

    chosen: dict | None = None
    if png_result and log_result:
        # Prefer current qrcode.png when it is at least as new as the log URL. This avoids stale first-log URLs.
        chosen = png_result if (png_result.get("generated_at") or 0) >= (log_result.get("generated_at") or 0) else log_result
    elif png_result:
        chosen = png_result
    elif log_result:
        chosen = log_result

    if chosen:
        return _expire_if_stale_qr(chosen)
    return _qr_waiting_response("waiting", source="none", fetched_at=fetched_at)


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


def _cleanup_instance_services(name: str, node_id: str = "local") -> None:
    """统一清理与指定容器关联的内存态服务资源（实例状态 / 登录缓存 / WS 注册表 / 心跳）。

    在容器删除、重建或全量数据清理时调用，避免过期数据残留 —— 不清的话，
    重建后的容器会带着上一个账号的 uin / last_uin / bot_online 继续显示。
    所有操作均为同步、幂等，失败不抛异常。
    """
    # 1. 清理 instance_subsystem 中的实例状态（连同该账号的心跳记录）
    try:
        from services.instance_subsystem import instance_subsystem
        from services.bot_heartbeat import bot_heartbeat

        inst = instance_subsystem.get(name, node_id)
        for stale_uin in {getattr(inst, "uin", ""), getattr(inst, "last_uin", "")} - {""}:
            bot_heartbeat.forget(stale_uin)
        instance_subsystem.remove(name, node_id)
        logger.info("已清理实例状态: %s@%s", name, node_id)
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

    # 清理 WS 注入标记和内部状态（scope=all 时）
    if scope == "all":
        from services.docker_lifecycle import LifecycleMixin

        marker_dir = root / LifecycleMixin._INJECT_MARKER_DIR
        if marker_dir.exists():
            shutil.rmtree(marker_dir, ignore_errors=True)
            logger.info("已清理 WS 注入标记: %s", name)

        _cleanup_instance_services(name)

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
        stopped = await async_docker_manager.action_container(name, "stop", node_id="local")
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
        restarted = await async_docker_manager.action_container(name, "start", node_id="local")

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

    old_removed = await async_docker_manager.action_container(name, "delete", node_id="local")
    if not old_removed:
        return _build_error(
            500, "RECREATE_DELETE_FAILED", "failed to delete old container", request_id
        )

    # 无论是否清数据，重建都必须丢掉旧容器的内存态：uin / last_uin / bot_online /
    # WS 注册表都是按容器名缓存的，不清的话新容器会顶着上一个账号显示。
    _cleanup_instance_services(name)

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
    # 普通用户只允许 start/stop/restart，其余操作需管理员权限
    _USER_ALLOWED_ACTIONS = {"start", "stop", "restart"}
    if session.get("permission", 0) < 10 and action.value not in _USER_ALLOWED_ACTIONS:
        raise HTTPException(status_code=403, detail="Permission denied: only start/stop/restart allowed")
    action_value = action.value

    operator_payload = {
        "operator_ip": request.client.host if request.client else "unknown",
        "operator_name": session["userName"],
        "operator_uuid": session.get("uuid"),
        "container_name": name,
        "action": action_value,
        "node_id": node_id,
        "delete_data": delete_data,
    }

    if action_value in {"start", "stop", "restart"}:
        existing_op = action_job_manager.has_active_job(name, node_id, action_value)
        if existing_op:
            # 连点同一个动作不再新建 job：旧 job 的监控还在跑，重复创建会让两个监控
            # 互相观察对方造成的状态迁移，判定结果错乱。换成别的动作则照常新建。
            return JSONResponse(
                status_code=202,
                content={
                    "status": "accepted", "operation_id": existing_op, "phase": "running",
                    "action": action_value, "name": name, "node_id": node_id,
                    "message": f"该实例已有进行中的{action_value}操作，正在跟踪原操作",
                },
            )

        job = await action_job_manager.create(name=name, action=action_value, node_id=node_id)
        # Lifecycle actions can make an old QR invalid before the state engine sees
        # the new file. Clear local QR cache immediately so restart behaves like
        # stop+start in the UI instead of showing the previous code.
        # 只清二维码，不强制翻转登录态：把 logged_in 打成 False 会顺带把 login_ts
        # 刷成 now，反而推迟下一次登录复核，用户会看到几秒的"待登录"假状态。
        inst = instance_subsystem.get(name, node_id)
        if inst and action_value in {"start", "restart"}:
            inst.clear_qr()

        is_local = node_id == "local"

        async def _do_action() -> bool:
            return (
                await async_docker_manager.action_container(name, action_value, node_id="local")
                if is_local
                else await cluster_manager.action_container_async(node_id, name, action_value)
            )

        # create_task 的返回值必须持有强引用，否则事件循环只有弱引用，
        # 任务可能在 await 点被 GC 掉，job 就永远停在 running。
        task = asyncio.create_task(action_job_manager.run(
            job.operation_id,
            _do_action,
            cluster_manager.inspect_container_state_async,
        ))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        state_engine.notify_change()
        operation_logger.info("container_action", {**operator_payload, "operation_id": job.operation_id, "accepted": True})
        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "operation_id": job.operation_id,
                "phase": "accepted",
                "action": action_value,
                "name": name,
                "node_id": node_id,
                "started_at": job.started_at,
                "action_started_at": job.started_at,
            },
        )

    # 非生命周期操作保持同步语义，尤其 delete/delete_data 的安全清理逻辑不异步化。
    # delete_data 必须转发给远程节点，否则 UI 上勾了"同时删除数据"对远程实例静默无效。
    success = (
        await async_docker_manager.action_container(name, action_value, node_id="local")
        if node_id == "local"
        else await cluster_manager.action_container_async(node_id, name, action_value, delete_data=delete_data)
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

    operation_logger.info("container_action", operator_payload)
    return {"status": "ok"}


@router.get("/containers/{name}/state", dependencies=[Depends(speed_limit(0.5))])
async def api_container_state(
    name: str,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    """单容器实时状态精查（含 State.StartedAt）。

    主控面板用它判定远程节点上的重启是否真的发生过 —— 容器列表里没有
    StartedAt，只靠 running 判断会把重启前的旧状态当成重启成功。
    """
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    state = await cluster_manager.inspect_container_state_async(node_id, name)
    return {"status": "ok", "state": state}


@router.get("/operations/{operation_id}")
async def api_get_operation(operation_id: str, session: dict = Depends(get_current_user)):
    job = action_job_manager.get(operation_id)
    if not job:
        raise HTTPException(status_code=404, detail="Operation not found")
    if not check_instance_permission(session, job.get("node_id", "local"), job.get("name", "")):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    return {"status": "ok", "operation": job, **job}


@router.get("/containers/{name}/operation")
async def api_get_container_operation(
    name: str,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    job = action_job_manager.get_latest(name, node_id)
    return {"status": "ok", "operation": job}


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

        # 以状态引擎为准覆盖登录态（兼容非标准挂载布局）。
        # uin 只表示当前确认登录账号；last_uin 保留离线上次账号。
        inst = instance_subsystem.get(name)
        if inst:
            stats["uin"] = inst.uin if inst.logged_in else ""
            stats["last_uin"] = inst.last_uin
            stats["login_stage"] = inst.login_stage
            stats["login_method"] = inst.login_method
            stats["bot_online"] = inst.bot_online
            stats["bot_heartbeat_ts"] = inst.bot_heartbeat_ts
            try:
                decorated = action_job_manager.decorate_container({"name": name, "node_id": node_id, "status": stats.get("status", "")})
                for key in ("action_phase", "action", "operation_id", "action_started_at", "action_updated_at", "action_error", "display_status"):
                    if key in decorated:
                        stats[key] = decorated[key]
            except Exception:
                stats.setdefault("display_status", stats.get("status", ""))
    elif isinstance(stats, dict):
        try:
            decorated = action_job_manager.decorate_container({"name": name, "node_id": node_id, "status": stats.get("status", "")})
            for key in ("action_phase", "action", "operation_id", "action_started_at", "action_updated_at", "action_error", "display_status"):
                if key in decorated:
                    stats[key] = decorated[key]
        except Exception:
            pass
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
async def get_qr_code(name: str, node_id: str = "local", session: dict = Depends(get_current_user)):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if node_id != "local":
        # Remote QR must be fetched live from the target node. Do not use the Japan
        # panel's stale in-memory container state, and force no-cache over aiohttp.
        result = await cluster_manager.get_qr_status_async(node_id, name, bust_cache=True)
        if not result:
            result = _qr_waiting_response("waiting", source="remote_unavailable")
        return JSONResponse(
            content=result,
            headers={
                "Cache-Control": "no-store, no-cache, max-age=0",
                "Pragma": "no-cache",
            },
        )

    result = await _get_local_qr_status(name)
    try:
        # Keep local memory metadata roughly in sync for public batch / WS hints, but
        # never serve QR images from this cache in the authenticated endpoint.
        inst = instance_subsystem.get(name)
        if inst and result.get("status") == "ok" and result.get("url", "").startswith("data:image/png;base64,"):
            inst.update_qr(
                result.get("url"),
                expired=False,
                generated_at=float(result.get("generated_at") or 0),
                fetched_at=float(result.get("fetched_at") or 0),
                expires_at=float(result.get("expires_at") or 0),
                source=str(result.get("source") or "container_file"),
                type=str(result.get("type") or "image"),
            )
    except Exception:
        pass
    return JSONResponse(
        content=result,
        headers={
            "Cache-Control": "no-store, no-cache, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.post("/containers/{name}/refresh-login", dependencies=[Depends(public_speed_limit(0.5))])
async def refresh_login_status(name: str, node_id: str = "local", session: dict = Depends(get_current_user)):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if node_id != "local":
        return {"status": "ok", "logged_in": False, "method": "remote_unsupported"}

    # 使用异步检测链路（WS/HTTP/文件系统），兼容无 3000 端口映射的容器
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
    # 登录事件到达后立即触发状态引擎刷新，缩短前端可见延迟
    state_engine.notify_change()
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
