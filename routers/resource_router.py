"""
资源路由 - 壁纸/背景图自动发现 & 头像代理缓存
扫描 resource/images/{category}/ 下的图片，按宽高比分为横图/竖图返回。
头像缓存至 resource/avatars/{uin}.jpg，前端统一使用本地端点。
"""
import os
import urllib.request
from fastapi import APIRouter, Query, Path, Response, Depends
from fastapi.responses import FileResponse
from middleware.rate_limiter import public_speed_limit
from services.log import logger

router = APIRouter(prefix="/api/resource", tags=["resource"])

_RESOURCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resource")
_AVATAR_DIR = os.path.join(_RESOURCE_DIR, "avatars")
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# 确保头像缓存目录存在
os.makedirs(_AVATAR_DIR, exist_ok=True)


def _classify_images(category: str) -> dict:
    """扫描目录，用 PIL 读取尺寸，分为 landscape / portrait。"""
    folder = os.path.join(_RESOURCE_DIR, "images", category)
    result: dict = {"landscape": [], "portrait": []}
    if not os.path.isdir(folder):
        return result

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow 未安装，壁纸尺寸检测不可用")
        # 回退：全部归入 landscape
        for f in os.listdir(folder):
            if os.path.splitext(f)[1].lower() in _IMG_EXTS:
                result["landscape"].append(f"/resource/images/{category}/{f}")
        return result

    for f in os.listdir(folder):
        ext = os.path.splitext(f)[1].lower()
        if ext not in _IMG_EXTS:
            continue
        path = os.path.join(folder, f)
        try:
            with Image.open(path) as img:
                w, h = img.size
            url = f"/resource/images/{category}/{f}"
            if w >= h:
                result["landscape"].append(url)
            else:
                result["portrait"].append(url)
        except Exception:
            logger.debug("无法读取图片 %s", path)
    return result


@router.get("/wallpapers")
async def get_wallpapers(category: str = Query(default="user-dashboard")):
    """返回指定分类下按方向分组的壁纸列表。"""
    data = _classify_images(category)
    return {"status": "ok", **data}


@router.get("/avatar/{uin}", dependencies=[Depends(public_speed_limit(2.0))])
async def get_avatar(uin: str = Path(..., pattern=r"^\d{5,12}$")):
    """代理并缓存 QQ 头像到 resource/avatars/{uin}.jpg。

    首次请求从 q1.qlogo.cn 下载并持久化；后续直接返回本地文件。
    登录新账号时，因 uin 不同会写入新文件，旧文件自动冷存。
    """
    cache_path = os.path.join(_AVATAR_DIR, f"{uin}.jpg")
    if os.path.exists(cache_path):
        return FileResponse(cache_path, media_type="image/jpeg")

    # 从腾讯 CDN 拉取头像并缓存
    try:
        url = f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
        if len(data) > 500:  # 过滤掉无效的默认头像（过小）
            with open(cache_path, "wb") as f:
                f.write(data)
            return Response(content=data, media_type="image/jpeg")
    except Exception as exc:
        logger.debug("头像下载失败 uin=%s: %s", uin, exc)

    # 回退：302 重定向到腾讯 CDN
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=640")

