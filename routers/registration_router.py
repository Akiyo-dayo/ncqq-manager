"""
注册申请路由 — 公开注册 + 管理员审核

公开端点：
  POST /api/register           — 提交注册申请
  GET  /api/register/status    — 查看注册功能是否开放

管理员端点：
  GET    /api/registration-requests              — 分页列表
  GET    /api/registration-requests/count         — 待审核数量
  POST   /api/registration-requests/{id}/approve  — 通过
  POST   /api/registration-requests/{id}/reject   — 拒绝
  DELETE /api/registration-requests/{id}          — 删除记录
"""
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

from middleware.auth import require_admin
from middleware.rate_limiter import public_speed_limit
from services.user_manager import user_manager, ROLE
from services.operation_logger import operation_logger
from services.operation_log_context import build_operator_payload
from services.log import logger
import services.database as db

router = APIRouter(prefix="/api", tags=["registration"])


# ============ 请求体 ============

class RegisterRequest(BaseModel):
    username: str
    password: str


class ReviewRequest(BaseModel):
    reason: Optional[str] = ""


# ============ 公开端点 ============

@router.get("/register/status")
async def api_register_status():
    """检查系统是否允许注册"""
    from services.config import app_config
    initialized = app_config.get("initialized", False)
    return {"status": "ok", "register_enabled": initialized}


@router.post("/register", dependencies=[Depends(public_speed_limit(0.2))])
async def api_register(req: RegisterRequest, request: Request):
    """提交注册申请 — 需要管理员审核后才能使用"""
    from services.config import app_config
    if not app_config.get("initialized", False):
        return JSONResponse(
            {"status": "error", "message": "System not initialized"},
            status_code=400,
        )

    username = req.username.strip()
    password = req.password

    if not username or not password:
        return JSONResponse(
            {"status": "error", "message": "Username and password are required"},
            status_code=400,
        )

    if len(username) < 2 or len(username) > 32:
        return JSONResponse(
            {"status": "error", "message": "Username must be 2-32 characters"},
            status_code=400,
        )

    if len(password) < 6:
        return JSONResponse(
            {"status": "error", "message": "Password must be at least 6 characters"},
            status_code=400,
        )

    # 检查用户名是否已被使用（正式用户）
    if user_manager.get_user_by_username(username):
        return JSONResponse(
            {"status": "error", "message": "Username already taken"},
            status_code=409,
        )

    # 检查是否已有待审核的同名申请
    existing = db.fetchone(
        "SELECT id, status FROM registration_requests WHERE userName=?",
        (username,),
    )
    if existing:
        row = dict(existing)
        if row["status"] == "pending":
            return JSONResponse(
                {"status": "error", "message": "Registration request already pending"},
                status_code=409,
            )
        if row["status"] == "rejected":
            # 被拒绝的可以重新申请 — 更新密码和状态
            hashed = user_manager._hash_password(password)
            db.execute(
                "UPDATE registration_requests SET passWord=?, status='pending', requested_at=?, reviewed_at=0, reviewed_by='', review_reason='' WHERE id=?",
                (hashed, time.time(), row["id"]),
            )
            ip = request.client.host if request.client else "unknown"
            operation_logger.info("user_register_retry", {
                "operator_ip": ip,
                "operator_name": username,
            })
            return {"status": "ok", "message": "Registration request resubmitted"}

    # 创建新申请
    req_id = uuid.uuid4().hex[:24]
    hashed = user_manager._hash_password(password)
    now = time.time()
    db.execute(
        "INSERT INTO registration_requests (id, userName, passWord, status, requested_at) VALUES (?,?,?,?,?)",
        (req_id, username, hashed, "pending", now),
    )

    ip = request.client.host if request.client else "unknown"
    operation_logger.info("user_register_request", {
        "operator_ip": ip,
        "operator_name": username,
    })
    return {"status": "ok", "message": "Registration request submitted"}


# ============ 管理员端点 ============

@router.get("/registration-requests")
async def api_list_requests(
    page: int = 1,
    page_size: int = 20,
    status_filter: Optional[str] = None,
    session: dict = Depends(require_admin),
):
    """分页列出注册申请"""
    offset = (page - 1) * page_size
    conditions = []
    params = []
    if status_filter:
        conditions.append("status=?")
        params.append(status_filter)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    total_row = db.fetchone(
        f"SELECT COUNT(*) as cnt FROM registration_requests {where}",
        tuple(params),
    )
    total = total_row["cnt"] if total_row else 0

    rows = db.fetchall(
        f"SELECT id, userName, status, requested_at, reviewed_at, reviewed_by, review_reason "
        f"FROM registration_requests {where} ORDER BY requested_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (page_size, offset),
    )
    data = []
    for r in rows:
        d = dict(r)
        data.append(d)

    return {"status": "ok", "total": total, "page": page, "pageSize": page_size, "data": data}


@router.get("/registration-requests/count")
async def api_pending_count(session: dict = Depends(require_admin)):
    """获取待审核申请数量"""
    row = db.fetchone(
        "SELECT COUNT(*) as cnt FROM registration_requests WHERE status='pending'"
    )
    return {"status": "ok", "pending": row["cnt"] if row else 0}


@router.post("/registration-requests/{req_id}/approve")
async def api_approve_request(
    req_id: str,
    request: Request,
    session: dict = Depends(require_admin),
):
    """通过注册申请 — 创建 USER 权限账号，不分配实例"""
    row = db.fetchone(
        "SELECT * FROM registration_requests WHERE id=?", (req_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    req_data = dict(row)
    if req_data["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request already reviewed")

    # 再次检查用户名
    if user_manager.get_user_by_username(req_data["userName"]):
        db.execute(
            "UPDATE registration_requests SET status='rejected', reviewed_at=?, reviewed_by=?, review_reason=? WHERE id=?",
            (time.time(), session["uuid"], "Username already taken", req_id),
        )
        raise HTTPException(status_code=409, detail="Username already taken")

    # 创建用户 — 直接使用已哈希的密码
    user_uuid = uuid.uuid4().hex[:24]
    api_key = uuid.uuid4().hex
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO users (uuid,userName,passWord,permission,registerTime,loginTime,apiKey,instances) VALUES (?,?,?,?,?,?,?,?)",
        (user_uuid, req_data["userName"], req_data["passWord"], ROLE.USER, now_str, "", api_key, "[]"),
    )

    # 更新申请状态
    db.execute(
        "UPDATE registration_requests SET status='approved', reviewed_at=?, reviewed_by=? WHERE id=?",
        (time.time(), session["uuid"], req_id),
    )

    operation_logger.info(
        "registration_approved",
        build_operator_payload(request, session, {
            "target_user_name": req_data["userName"],
            "target_user_uuid": user_uuid,
        }),
    )
    return {"status": "ok", "uuid": user_uuid, "userName": req_data["userName"]}


@router.post("/registration-requests/{req_id}/reject")
async def api_reject_request(
    req_id: str,
    body: ReviewRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    """拒绝注册申请"""
    row = db.fetchone(
        "SELECT * FROM registration_requests WHERE id=?", (req_id,)
    )
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    req_data = dict(row)
    if req_data["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request already reviewed")

    db.execute(
        "UPDATE registration_requests SET status='rejected', reviewed_at=?, reviewed_by=?, review_reason=? WHERE id=?",
        (time.time(), session["uuid"], body.reason or "", req_id),
    )

    operation_logger.info(
        "registration_rejected",
        build_operator_payload(request, session, {
            "target_user_name": req_data["userName"],
            "reason": body.reason or "",
        }),
    )
    return {"status": "ok"}


@router.delete("/registration-requests/{req_id}")
async def api_delete_request(
    req_id: str,
    request: Request,
    session: dict = Depends(require_admin),
):
    """删除注册申请记录"""
    cur = db.execute("DELETE FROM registration_requests WHERE id=?", (req_id,))
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Request not found")

    operation_logger.info(
        "registration_deleted",
        build_operator_payload(request, session, {"request_id": req_id}),
    )
    return {"status": "ok"}
