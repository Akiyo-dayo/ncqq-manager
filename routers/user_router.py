"""
用户管理路由 - 对标 MCSM manage_user_router + general_user_router
"""
import uuid as uuid_mod

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional

from middleware.auth import require_admin, get_current_user, remove_user_tokens
from services.user_manager import user_manager
from services.operation_logger import operation_logger
from services.operation_log_context import build_operator_payload

router = APIRouter(prefix="/api", tags=["users"])


class UserCreateRequest(BaseModel):
    username: str
    password: str
    permission: int = 1


class UserEditRequest(BaseModel):
    userName: Optional[str] = None
    passWord: Optional[str] = None
    permission: Optional[int] = None


class UserInstancesRequest(BaseModel):
    instances: list


@router.get("/users")
async def api_list_users(
    page: int = 1, page_size: int = 20, search: str = "",
    session: dict = Depends(require_admin),
):
    return {"status": "ok", **user_manager.list_users(page, page_size, search)}


@router.get("/users/count")
async def api_user_count(session: dict = Depends(require_admin)):
    return {"status": "ok", **user_manager.count()}


@router.post("/users")
async def api_create_user(
    req: UserCreateRequest, request: Request,
    session: dict = Depends(require_admin),
):
    user = user_manager.create_user(req.username, req.password, req.permission)
    if not user:
        raise HTTPException(status_code=400, detail="Username already exists")
    operation_logger.info(
        "user_create",
        build_operator_payload(
            request,
            session,
            {"target_user_name": req.username},
        ),
    )
    return {"status": "ok", "uuid": user["uuid"], "userName": user["userName"]}


@router.put("/users/{user_uuid}")
async def api_edit_user(
    user_uuid: str, req: UserEditRequest, request: Request,
    session: dict = Depends(require_admin),
):
    success = user_manager.edit_user(
        user_uuid,
        userName=req.userName,
        passWord=req.passWord,
        permission=req.permission,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Edit failed")
    operation_logger.info(
        "user_edit",
        build_operator_payload(
            request,
            session,
            {"target_user_uuid": user_uuid},
        ),
    )
    return {"status": "ok"}


@router.delete("/users/{user_uuid}")
async def api_delete_user(
    user_uuid: str, request: Request,
    session: dict = Depends(require_admin),
):
    if session["uuid"] == user_uuid:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target_user = user_manager.get_user_by_uuid(user_uuid)
    if not user_manager.delete_user(user_uuid):
        raise HTTPException(status_code=404, detail="User not found")
    # 清除该用户的所有活跃 token
    remove_user_tokens(user_uuid)
    operation_logger.warning(
        "user_delete",
        build_operator_payload(
            request,
            session,
            {
                "target_user_name": target_user["userName"] if target_user else "Unknown",
            },
        ),
    )
    return {"status": "ok"}


@router.put("/users/{user_uuid}/instances")
async def api_assign_instances(
    user_uuid: str, req: UserInstancesRequest, request: Request,
    session: dict = Depends(require_admin),
):
    if not user_manager.assign_instances(user_uuid, req.instances):
        raise HTTPException(status_code=404, detail="User not found")
    operation_logger.info(
        "user_assign_instances",
        build_operator_payload(
            request,
            session,
            {
                "target_user_uuid": user_uuid,
                "instance_count": len(req.instances),
                "instances": req.instances,
            },
        ),
    )
    return {"status": "ok"}


@router.put("/users/{user_uuid}/apikey")
async def api_regenerate_apikey(
    user_uuid: str, request: Request,
    session: dict = Depends(require_admin),
):
    new_key = uuid_mod.uuid4().hex
    if not user_manager.edit_user(user_uuid, apiKey=new_key):
        raise HTTPException(status_code=404, detail="User not found")
    operation_logger.info(
        "user_regenerate_apikey",
        build_operator_payload(
            request,
            session,
            {
                "target_user_uuid": user_uuid,
                "api_key_regenerated": True,
            },
        ),
    )
    return {"status": "ok", "apiKey": new_key}

