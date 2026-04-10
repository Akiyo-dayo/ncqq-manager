"""
Docker 镜像管理路由 - 列表/拉取/删除（异步版，使用 async_docker_manager）
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from middleware.auth import require_admin
from middleware.rate_limiter import speed_limit
from services.docker_async import async_docker_manager
from services.operation_logger import operation_logger

router = APIRouter(prefix="/api", tags=["images"])


@router.get("/images", dependencies=[Depends(speed_limit(2.0))])
async def list_images(session: dict = Depends(require_admin)):
    """列出本地 Docker 镜像"""
    images = await async_docker_manager.list_images()
    return {"status": "ok", "images": images}


class PullImageRequest(BaseModel):
    image: str


@router.post("/images/pull", dependencies=[Depends(speed_limit(5.0))])
async def pull_image(
    req: PullImageRequest, request: Request,
    session: dict = Depends(require_admin),
):
    """拉取 Docker 镜像"""
    if not req.image:
        raise HTTPException(status_code=400, detail="Image name is required")
    success = await async_docker_manager.pull_image(req.image)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to pull image")
    operation_logger.info("image_pull", {
        "operator_name": session["userName"],
        "operator_uuid": session.get("uuid"),
        "operator_ip": request.client.host if request.client else "unknown",
        "image": req.image,
    })
    return {"status": "ok"}


@router.delete("/images/{image_id}", dependencies=[Depends(speed_limit(3.0))])
async def delete_image(
    image_id: str, request: Request,
    force: bool = False,
    session: dict = Depends(require_admin),
):
    """删除 Docker 镜像"""
    success = await async_docker_manager.delete_image(image_id, force)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete image")
    operation_logger.info("image_delete", {
        "operator_name": session["userName"],
        "operator_uuid": session.get("uuid"),
        "operator_ip": request.client.host if request.client else "unknown",
        "image_id": image_id,
    })
    return {"status": "ok"}

