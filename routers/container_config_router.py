"""
容器配置与文件路由 - 配置读写 / 文件列表 / 文件删除

这些端点操作的都是宿主机上的容器数据目录，因此远程节点的请求必须转发给对方，
由对方以 node_id=local 在自己的磁盘上执行。过去所有端点都只认本机路径：
远程容器读不到配置、保存会在主控机上凭空建目录、删除更是会误删本机同名路径。
"""

import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from middleware.auth import check_instance_permission, get_current_user
from services.cluster_manager import cluster_manager
from services.config import get_data_dir
from services.log import logger
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["containers"])


class ConfigRequest(BaseModel):
    content: str


def _is_remote(node_id: str) -> bool:
    return bool(node_id) and node_id != "local"


async def _forward(node_id: str, method: str, path: str, **kwargs) -> JSONResponse:
    """把请求转发到目标节点，由它以本地身份执行。"""
    separator = "&" if "?" in path else "?"
    result = await cluster_manager.call(
        node_id, method, f"{path}{separator}node_id=local", kind="read", **kwargs,
    )
    if result.ok:
        return JSONResponse(content=result.json({}), status_code=result.status)
    raise HTTPException(
        status_code=result.status or 502,
        detail=result.detail or f"节点 {node_id} 通信失败",
    )


def _safe_path(base: str, *parts: str) -> str:
    """安全路径构建 - 防止路径遍历。"""
    joined = os.path.join(base, *parts)
    real = os.path.realpath(joined)
    real_base = os.path.realpath(base)
    if not real.startswith(real_base):
        raise HTTPException(status_code=400, detail="Invalid path: directory traversal detected")
    return real


@router.get("/containers/{name}/config/{filename:path}")
async def read_container_config(
    name: str,
    filename: str,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if _is_remote(node_id):
        return await _forward(node_id, "GET", f"/api/containers/{name}/config/{filename}")
    file_path = _safe_path(get_data_dir(), name, filename)
    if not os.path.exists(file_path):
        return {"status": "not_found", "content": ""}
    with open(file_path, "r", encoding="utf-8") as file_handle:
        return {"status": "ok", "content": file_handle.read()}


@router.post("/containers/{name}/config/{filename:path}")
async def save_container_config(
    name: str,
    filename: str,
    req: ConfigRequest,
    request: Request,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if _is_remote(node_id):
        return await _forward(
            node_id, "POST", f"/api/containers/{name}/config/{filename}",
            json={"content": req.content},
        )
    file_path = _safe_path(get_data_dir(), name, filename)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as file_handle:
        file_handle.write(req.content)
    operation_logger.info("config_save", {
        "operator_ip": request.client.host if request.client else "unknown",
        "operator_name": session["userName"],
        "container_name": name,
        "filename": filename,
    })
    return {"status": "ok"}


@router.get("/containers/{name}/files")
async def list_container_files(
    name: str,
    path: str = "",
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if _is_remote(node_id):
        return await _forward(node_id, "GET", f"/api/containers/{name}/files?path={path}")
    target_dir = _safe_path(get_data_dir(), name, path)
    if not os.path.exists(target_dir):
        return {"status": "ok", "files": [], "folders": [], "current_path": path}

    files = []
    folders = []
    if os.path.isdir(target_dir):
        for entry_name in os.listdir(target_dir):
            entry_path = os.path.join(target_dir, entry_name)
            if os.path.isfile(entry_path):
                stat = os.stat(entry_path)
                files.append({"name": entry_name, "size": stat.st_size, "mtime": stat.st_mtime})
            elif os.path.isdir(entry_path):
                folders.append({"name": entry_name})
    return {"status": "ok", "files": files, "folders": folders, "current_path": path}


@router.delete("/containers/{name}/files")
async def delete_container_file(
    name: str,
    path: str,
    request: Request,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    """删除容器数据目录下的文件或文件夹（不可删根目录）。

    path: 相对于 data/{name}/ 的路径（必填，不可为空以防止误删根目录）。
    文件直接删除；文件夹递归删除（shutil.rmtree）。
    """
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    if not path or path.strip("/") == "":
        raise HTTPException(status_code=400, detail="path 不能为空，禁止删除根目录")
    if _is_remote(node_id):
        return await _forward(node_id, "DELETE", f"/api/containers/{name}/files?path={path}")

    target = _safe_path(get_data_dir(), name, path)
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="文件或目录不存在")

    is_dir = os.path.isdir(target)
    try:
        if is_dir:
            shutil.rmtree(target)
        else:
            os.remove(target)
    except OSError as exc:
        logger.error("文件删除失败 container=%s path=%s: %s", name, path, exc)
        raise HTTPException(status_code=500, detail=f"删除失败: {exc}") from exc

    operation_logger.warning("file_delete", {
        "operator_ip": request.client.host if request.client else "unknown",
        "operator_name": session["userName"],
        "container_name": name,
        "path": path,
        "is_dir": is_dir,
    })
    logger.info("文件已删除 container=%s path=%s is_dir=%s", name, path, is_dir)
    return {"status": "ok"}
