"""页面操作 → 路由 → service.py 的业务函数；本文件不直接执行 SQL。"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import service
from .schemas import Assignment, Dispatch, EquipmentCreate, EquipmentState, Inspection, Note, Plan, Report, ReworkAction


def create_router(database_path):
    router = APIRouter(prefix="/mes", tags=["MES 生产执行"])

    def call(function, *args):
        try:
            return function(database_path, *args)
        except service.BusinessError as error:
            raise HTTPException(status_code=error.code, detail=str(error)) from error

    @router.get("/dashboard", summary="生产看板")
    def dashboard():
        return call(service.dashboard)

    @router.get("/work-orders", summary="按优先级与计划日期查看工单")
    def work_orders():
        return call(service.list_work_orders)

    @router.post("/work-orders", status_code=201, summary="ERP 订单下发为 MES 工单")
    def dispatch(data: Dispatch):
        return call(service.dispatch, data)

    @router.get("/work-orders/{wo_id}", summary="工单及全部执行记录")
    def work_order(wo_id: str):
        return call(service.get_work_order, wo_id)

    @router.patch("/work-orders/{wo_id}/plan", summary="修改尚未开工的计划")
    def plan(wo_id: str, data: Plan):
        return call(service.update_plan, wo_id, data)

    @router.post("/work-orders/{wo_id}/release", summary="下达已分配设备的工单")
    def release(wo_id: str):
        return call(service.release, wo_id)

    @router.post("/work-orders/{wo_id}/cancel", summary="撤回未开工工单，来源订单恢复待下发")
    def cancel(wo_id: str, data: Note):
        return call(service.cancel, wo_id, data)

    @router.post("/work-orders/{wo_id}/inspections", summary="最终整批质检与返工")
    def inspect(wo_id: str, data: Inspection):
        return call(service.inspect, wo_id, data)

    @router.get("/work-orders/{wo_id}/export", summary="导出关联订单、工序、设备及事件的 JSON")
    def export(wo_id: str):
        result = call(service.export_trace, wo_id)
        filename = result["work_order"]["work_order_id"]
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})

    @router.put("/operations/{op_id}/equipment", summary="分配同类型设备")
    def assign(op_id: str, data: Assignment):
        return call(service.assign, op_id, data)

    @router.post("/operations/{op_id}/start", summary="依序开工并占用设备")
    def start(op_id: str, data: Note):
        return call(service.execute_operation, op_id, "start", data)

    @router.post("/operations/{op_id}/pause", summary="暂停；暂停期间保留设备占用")
    def pause(op_id: str, data: Note):
        return call(service.execute_operation, op_id, "pause", data)

    @router.post("/operations/{op_id}/resume", summary="恢复已暂停工序")
    def resume(op_id: str, data: Note):
        return call(service.execute_operation, op_id, "resume", data)

    @router.post("/operations/{op_id}/reports", summary="报工；合格数量达到计划时自动完成工序")
    def report(op_id: str, data: Report):
        return call(service.report, op_id, data)

    @router.get("/equipment", summary="设备及占用情况")
    def equipment():
        return call(service.list_equipment)

    @router.post("/reworks/{rework_id}/{action}", summary="返修开工、完成、复检开工及复检结果")
    def rework(rework_id: str, action: str, data: ReworkAction):
        return call(service.execute_rework, rework_id, action, data)

    @router.post("/equipment", status_code=201, summary="登记设备")
    def create_equipment(data: EquipmentCreate):
        return call(service.create_equipment, data)

    @router.patch("/equipment/{eq_id}", summary="空闲设备维护或恢复")
    def equipment_state(eq_id: str, data: EquipmentState):
        return call(service.set_equipment_state, eq_id, data)

    return router
