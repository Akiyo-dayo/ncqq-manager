"""
操作日志上下文构造器
统一生成 operator_name/operator_uuid/operator_ip 字段
"""
from typing import Any

from fastapi import Request


def build_operator_payload(
    request: Request | None,
    session: dict,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "operator_name": session.get("userName"),
        "operator_uuid": session.get("uuid"),
        "operator_ip": request.client.host if request and request.client else "unknown",
    }
    if extra:
        payload.update(extra)
    return payload

