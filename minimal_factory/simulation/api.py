from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from mes.service import BusinessError
from .schemas import RunRequest


def create_router(service):
    router = APIRouter(prefix="/simulation", tags=["SimPy 生产仿真"])

    def call(function, *args):
        try:
            return function(*args)
        except BusinessError as error:
            raise HTTPException(status_code=error.code, detail=str(error)) from error

    @router.post("/preview", summary="只读预览实验输入，不创建或执行批次")
    def preview(data: RunRequest):
        return call(service.preview, data)

    @router.post("/runs", status_code=202, summary="从输入快照启动独立仿真")
    def start(data: RunRequest):
        return call(service.start, data)

    @router.get("/runs", summary="实验运行历史")
    def history():
        return call(service.list_runs)

    @router.get("/runs/{run_id}/telemetry", summary="CNC/Robot连续合成时序数据")
    def telemetry(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
        return call(service.telemetry, run_id, offset, limit)

    @router.get("/runs/{run_id}", summary="输入、来源、事件和结果")
    def result(run_id: str):
        return call(service.get, run_id)

    @router.get("/runs/{run_id}/export", summary="导出完整 JSON 或事件/工单汇总 CSV")
    def export(run_id: str, format: Literal["json", "events_csv", "summary_csv"] = "json"):
        body, media_type = call(service.export, run_id, format)
        extension = "json" if format == "json" else "csv"
        return Response(body, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="{run_id}-{format}.{extension}"'})

    return router
