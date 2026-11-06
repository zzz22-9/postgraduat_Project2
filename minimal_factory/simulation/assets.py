"""只读提供四张已知法兰图片；不接受任意本地路径。"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

IMAGE_ROOT = Path(__file__).resolve().parents[1] / "synthetic_images" / "rendered_v1"
IMAGE_NAMES = frozenset(f"{label}_{view}.png" for label in ("normal", "defect") for view in ("front", "side"))


def create_router():
    router = APIRouter()

    @router.get("/static/rendered_v1/{filename}", include_in_schema=False)
    def vision_image(filename: str):
        if filename not in IMAGE_NAMES:
            raise HTTPException(status_code=404, detail="图片不存在")
        path = (IMAGE_ROOT / filename).resolve()
        if not path.is_relative_to(IMAGE_ROOT.resolve()) or not path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")
        return FileResponse(path, media_type="image/png", headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    return router
