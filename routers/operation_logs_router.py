"""
操作日志路由 - 提供操作审计日志查询接口
"""
import json
import time

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from middleware.auth import require_admin
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["operation_logs"])


@router.get("/operation_logs")
def get_operation_logs(
    limit: int = 50,
    page: int = 1,
    operator: str = "",
    type: str = "",
    level: str = "",
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    session: dict = Depends(require_admin)
) -> dict:
    """
    获取操作日志

    Args:
        limit: 返回的日志条数 (1-200)
        session: 管理员会话

    Returns:
        操作日志列表，按时间倒序
    """
    result = operation_logger.get(
        limit=limit,
        page=page,
        operator=operator,
        operation_type=type,
        level=level,
        start_time=start_time,
        end_time=end_time,
    )
    return {"status": "ok", **result}


@router.get("/operation_logs/download")
def download_operation_logs(
    limit: int = 200,
    page: int = 1,
    operator: str = "",
    type: str = "",
    level: str = "",
    start_time: Optional[int] = None,
    end_time: Optional[int] = None,
    session: dict = Depends(require_admin),
):
    """导出操作日志为 JSON 文件"""
    result = operation_logger.get(
        limit=limit if 1 <= limit <= 1000 else 200,
        page=page,
        operator=operator,
        operation_type=type,
        level=level,
        start_time=start_time,
        end_time=end_time,
    )
    ts = time.strftime("%Y%m%d_%H%M%S")
    filename = f"operation_logs_{ts}.json"
    content = json.dumps(result, ensure_ascii=False, indent=2)
    return PlainTextResponse(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

