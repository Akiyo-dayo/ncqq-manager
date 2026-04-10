"""
备份与恢复路由 - config 和 data 文件夹导出/导入
"""
import os
import time
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Depends, Request, UploadFile, File
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from middleware.auth import require_admin
from middleware.rate_limiter import speed_limit
from services.config import CONFIG_DIR, DATA_DIR
from services.operation_logger import operation_logger
from services.log import logger

router = APIRouter(prefix="/api", tags=["backup"])

_MAX_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB
_ALLOWED_BACKUP_ROOTS = {"config", "data"}
_CHUNK_SIZE = 1024 * 1024


def _get_dir_size(path: str) -> int:
    """计算目录大小（字节）"""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
    except Exception:
        pass
    return total


def _cleanup_file(path: str) -> None:
    if path and os.path.exists(path):
        os.remove(path)


def _validate_zip_members(zipf: zipfile.ZipFile) -> None:
    total_uncompressed = 0
    for info in zipf.infolist():
        entry = PurePosixPath(info.filename)
        if entry.is_absolute() or ".." in entry.parts:
            raise HTTPException(status_code=400, detail=f"Invalid zip entry: {info.filename}")
        if not entry.parts:
            continue
        if entry.parts[0] not in _ALLOWED_BACKUP_ROOTS:
            raise HTTPException(status_code=400, detail=f"Unexpected zip root: {info.filename}")
        total_uncompressed += max(info.file_size, 0)
        if total_uncompressed > _MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=400, detail="Zip content too large after extraction")

def _iter_backup_data_files(base_dir: str):
    """遍历允许进入备份的 data 文件：仅保留各实例 config 目录，排除 qq_data。"""
    base_path = Path(base_dir)
    if not base_path.exists():
        return
    for child in base_path.iterdir():
        if not child.is_dir():
            continue
        if child.name == "qq_data":
            continue
        config_dir = child / "config"
        if not config_dir.is_dir():
            continue
        for file_path in config_dir.rglob("*"):
            if file_path.is_file() and "qq_data" not in file_path.parts:
                yield file_path, Path("data") / child.name / "config" / file_path.relative_to(config_dir)


def _copy_filtered_data_tree(src_dir: str, dst_dir: str) -> None:
    """复制 data 目录中允许保留的内容：仅 data/<instance>/config/**。"""
    src_path = Path(src_dir)
    dst_path = Path(dst_dir)
    if not src_path.exists():
        return
    for file_path, rel_path in _iter_backup_data_files(src_dir):
        target_path = dst_path / rel_path.relative_to("data")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target_path)


def _remove_runtime_data_dirs(data_dir: str) -> None:
    """恢复前删除 data 下非 config 的运行时目录，避免旧资源残留。"""
    base_path = Path(data_dir)
    if not base_path.exists():
        return
    for child in base_path.iterdir():
        if not child.is_dir():
            continue
        if child.name == "qq_data":
            shutil.rmtree(child, ignore_errors=True)
            continue
        for inner in child.iterdir():
            if inner.name != "config":
                if inner.is_dir():
                    shutil.rmtree(inner, ignore_errors=True)
                else:
                    inner.unlink(missing_ok=True)


def _get_filtered_data_size(path: str) -> int:
    total = 0
    for file_path, _ in _iter_backup_data_files(path):
        try:
            total += file_path.stat().st_size
        except OSError:
            pass
    return total



@router.get("/backup/download", dependencies=[Depends(speed_limit(3.0, admin_exempt=False))])
async def download_backup(request: Request, session: dict = Depends(require_admin)):
    """下载 config 和 data 文件夹备份"""
    if not os.path.exists(CONFIG_DIR):
        raise HTTPException(status_code=404, detail="Config directory not found")
    if not os.path.exists(DATA_DIR):
        raise HTTPException(status_code=404, detail="Data directory not found")

    ts = time.strftime("%Y%m%d_%H%M%S")
    tmp_path = os.path.join(tempfile.gettempdir(), f"napcat_backup_{ts}.zip")

    try:
        # 创建 zip 备份：仅包含管理器 config/ 与 data/<instance>/config/
        with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(CONFIG_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.join("config", os.path.relpath(file_path, CONFIG_DIR))
                    zipf.write(file_path, arcname)

            for file_path, arcname in _iter_backup_data_files(DATA_DIR):
                zipf.write(str(file_path), str(arcname))

        operation_logger.info("backup_download", {
            "operator_name": session["userName"],
            "operator_uuid": session.get("uuid"),
            "operator_ip": request.client.host if request.client else "unknown",
        })

        return FileResponse(
            path=tmp_path,
            filename=f"napcat_backup_{ts}.zip",
            media_type="application/zip",
            background=BackgroundTask(_cleanup_file, tmp_path),
        )
    except Exception as e:
        logger.error(f"创建备份失败: {e}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise HTTPException(status_code=500, detail=f"Backup creation failed: {str(e)}")


@router.post("/backup/upload", dependencies=[Depends(speed_limit(5.0, admin_exempt=False))])
async def upload_backup(
    request: Request,
    file: UploadFile = File(...),
    session: dict = Depends(require_admin),
):
    """上传恢复 config 和 data 文件夹备份（覆盖当前文件夹，需要重启生效）"""
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Invalid file type. Only .zip files accepted.")

    # 先通过 Content-Length 快速拒绝超大文件
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {_MAX_UPLOAD_SIZE // (1024*1024)} MB.")

    tmp_upload = ""
    backup_base = ""

    try:
        size = 0
        ts = time.strftime("%Y%m%d_%H%M%S")
        tmp_upload = os.path.join(tempfile.gettempdir(), f"upload_{ts}.zip")
        with open(tmp_upload, "wb") as tmp_file:
            while True:
                chunk = await file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {_MAX_UPLOAD_SIZE // (1024*1024)} MB.")
                tmp_file.write(chunk)

        if size < 100:
            raise HTTPException(status_code=400, detail="File too small to be a valid backup")

        # 验证 zip 文件
        if not zipfile.is_zipfile(tmp_upload):
            raise HTTPException(status_code=400, detail="Invalid zip file")

        # 备份当前 config 与 data/<instance>/config/
        backup_base = os.path.join(tempfile.gettempdir(), f"pre_restore_{ts}")
        os.makedirs(backup_base, exist_ok=True)

        if os.path.exists(CONFIG_DIR):
            backup_config = os.path.join(backup_base, "config")
            shutil.copytree(CONFIG_DIR, backup_config, dirs_exist_ok=True)
            logger.info("已备份当前 config 到: %s", backup_config)

        if os.path.exists(DATA_DIR):
            backup_data = os.path.join(backup_base, "data")
            _copy_filtered_data_tree(DATA_DIR, backup_data)
            logger.info("已备份当前筛选 data 到: %s", backup_data)

        if os.path.exists(DATA_DIR):
            _remove_runtime_data_dirs(DATA_DIR)

        # 解压并恢复
        with zipfile.ZipFile(tmp_upload, 'r') as zipf:
            _validate_zip_members(zipf)
            base_dir = os.path.dirname(CONFIG_DIR)
            zipf.extractall(base_dir)

        operation_logger.info("backup_restore", {
            "operator_name": session["userName"],
            "operator_uuid": session.get("uuid"),
            "operator_ip": request.client.host if request.client else "unknown",
            "filename": file.filename,
        })

        return {"status": "ok", "message": "Backup restored successfully. Restart required."}

    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Corrupted zip file")
    except Exception as e:
        logger.error(f"恢复备份失败: {e}")
        raise HTTPException(status_code=500, detail=f"Restore failed: {str(e)}")
    finally:
        # 清理临时文件
        if os.path.exists(tmp_upload):
            os.remove(tmp_upload)


@router.get("/backup/info", dependencies=[Depends(speed_limit(3.0))])
async def backup_info(session: dict = Depends(require_admin)):
    """获取当前备份范围信息"""
    config_size = _get_dir_size(CONFIG_DIR) if os.path.exists(CONFIG_DIR) else 0
    data_size = _get_filtered_data_size(DATA_DIR) if os.path.exists(DATA_DIR) else 0
    total_size = config_size + data_size

    info = {
        "exists": os.path.exists(CONFIG_DIR) and os.path.exists(DATA_DIR),
        "size": round(total_size / 1024, 1),
        "config_size": round(config_size / 1024, 1),
        "data_size": round(data_size / 1024, 1),
        "modified": "",
        "path": "config + data/<instance>/config",
    }

    # 获取最近修改时间
    try:
        config_mtime = os.path.getmtime(CONFIG_DIR) if os.path.exists(CONFIG_DIR) else 0
        data_mtime = os.path.getmtime(DATA_DIR) if os.path.exists(DATA_DIR) else 0
        latest_mtime = max(config_mtime, data_mtime)
        if latest_mtime > 0:
            info["modified"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(latest_mtime))
    except Exception:
        pass

    return {"status": "ok", "info": info}

