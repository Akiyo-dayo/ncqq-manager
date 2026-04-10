"""
容器配置与文件路由 - 配置读写 / 文件列表 / 文件删除
"""

import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from middleware.auth import check_instance_permission, get_current_user
from services.config import get_data_dir
from services.log import logger
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["containers"])


class ConfigRequest(BaseModel):
    content: str


def _safe_path(base: str, *parts: str) -> str:
    """安全路径构建 - 防止路径遍历。"""
    joined = os.path.join(base, *parts)
    real = os.path.realpath(joined)
    real_base = os.path.realpath(base)
    if not real.startswith(real_base):
        raise HTTPException(status_code=400, detail="Invalid path: directory traversal detected")
    return real


@router.get("/containers/{name}/config/{filename:path}")
def read_container_config(
    name: str,
    filename: str,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    file_path = _safe_path(get_data_dir(), name, filename)
    if not os.path.exists(file_path):
        return {"status": "not_found", "content": ""}
    with open(file_path, "r", encoding="utf-8") as file_handle:
        return {"status": "ok", "content": file_handle.read()}


@router.post("/containers/{name}/config/{filename:path}")
def save_container_config(
    name: str,
    filename: str,
    req: ConfigRequest,
    request: Request,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
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
def list_container_files(
    name: str,
    path: str = "",
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
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
def delete_container_file(
    name: str,
    path: str,
    request: Request,
    node_id: str = "local",
    session: dict = Depends(get_current_user),
):
    if not check_instance_permission(session, node_id, name):
        raise HTTPException(status_code=403, detail="No permission for this instance")
    """删除容器数据目录下的文件或文件夹（不可删根目录）。

    path: 相对于 data/{name}/ 的路径（必填，不可为空以防止误删根目录）。
    文件直接删除；文件夹递归删除（shutil.rmtree）。
    """
    if not path or path.strip("/") == "":
        raise HTTPException(status_code=400, detail="path 不能为空，禁止删除根目录")

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