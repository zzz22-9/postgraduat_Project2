"""只读输入工单；快照是可重复实验的边界，不带入生产历史和实时设备状态。"""
from copy import deepcopy

from erp.database import connect
from mes import service as mes
from .schemas import FieldScenario


def demo_snapshot():
    equipment = [
        {"equipment_id": "E-ASM", "name": "仿真组装工位", "kind": "assembly"},
        {"equipment_id": "E-QC", "name": "仿真质检工位", "kind": "inspection"},
        {"equipment_id": "E-MOVE", "name": "仿真搬运资源", "kind": "transport"},
    ]
    orders = []
    for number in (1, 2):
        operations = []
        for sequence, (name, kind, minutes, eq) in enumerate([
            ("组装", "assembly", 3, "E-ASM"), ("过程质检", "inspection", 2, "E-QC"),
            ("搬运", "transport", 1, "E-MOVE"),
        ], 1):
            operations.append(dict(operation_id=f"DEMO-{number}-OP-{sequence}", sequence=sequence,
                                   name=name, kind=kind, minutes=minutes, equipment_id=eq))
        orders.append(dict(work_order_id=f"DEMO-WO-{number}", order_id=f"DEMO-ORD-{number}",
                           product_id="P001", product_name=f"教学装配件 · 订单 {number}",
                           quantity=1, priority=2, planned_date=None, due_date=None,
                           created_at="2026-10-20T00:00:00+00:00", operations=operations,
                           failure=None))
    return {"source": "demo", "orders": orders, "equipment": equipment}


def source_snapshot(path, ids):
    with connect(path) as conn:
        conn.execute("BEGIN")  # 所有工单和设备来自同一个读取快照。
        orders, devices = [], {}
        for wo_id in ids:
            wo = mes.detail(conn, wo_id)
            mes.require(wo["status"] != "cancelled", "已撤回工单不能用于仿真")
            item = {key: wo[key] for key in (
                "work_order_id", "order_id", "product_id", "product_name", "quantity",
                "priority", "planned_date", "due_date", "created_at")}
            item["operations"] = []
            item["failure"] = None
            for op in wo["operations"]:
                mes.require(op["equipment_id"], f"{wo['product_name']} / {op['name']} 尚未分配设备")
                eq = mes.equipment(conn, op["equipment_id"])
                mes.require(eq["kind"] == op["kind"], "工序与设备类型不匹配")
                devices[eq["equipment_id"]] = {k: eq[k] for k in ("equipment_id", "name", "kind")}
                item["operations"].append({k: op[k] for k in
                    ("operation_id", "sequence", "name", "kind", "minutes", "equipment_id")})
            orders.append(item)
    return {"source": "work_orders", "orders": orders, "equipment": list(devices.values())}


def configure(snapshot, overrides):
    snapshot = deepcopy(snapshot)
    orders = {w["work_order_id"]: w for w in snapshot["orders"]}
    mes.require(1 <= len(orders) <= 50, "每次实验需要 1—50 张工单", 422)
    mes.require(set(overrides) <= set(orders), "参数覆盖包含本次实验之外的工单", 422)
    for wo_id, override in overrides.items():
        wo = orders[wo_id]
        if override.quantity is not None:
            wo["quantity"] = override.quantity
        ops = {op["operation_id"]: op for op in wo["operations"]}
        mes.require(set(override.minutes) <= set(ops), "时长覆盖包含不属于该工单的工序", 422)
        for op_id, minutes in override.minutes.items():
            ops[op_id]["minutes"] = minutes
        # 显式 null 清除复制实验的失败配置；未提供则沿用原配置。
        if "failure" in override.model_fields_set:
            wo["failure"] = override.failure.model_dump() if override.failure else None
        if "field_protocols" in override.model_fields_set:
            wo["field_protocols"] = (override.field_protocols or FieldScenario()).model_dump()
    for wo in orders.values():
        scenario = FieldScenario.model_validate(wo.get("field_protocols") or {})
        wo["field_protocols"] = scenario.model_dump()
        # 视觉异常需要能够进入已有MES返工状态机；在创建批次前验证路线。
        if scenario.mode != "normal" and "E-QC" in scenario.anomaly_equipment_ids:
            for vision in (op for op in wo["operations"] if op["equipment_id"] == "E-QC"):
                mes.require(vision["kind"] == "inspection" and any(
                    op["kind"] != "inspection" and op["sequence"] < vision["sequence"]
                    for op in wo["operations"]), "视觉异常需要前置加工工序用于返工", 422)
        mes.require(1 <= len(wo["operations"]) <= 12, "工单需要 1—12 道工序", 422)
        failure = wo["failure"]
        if failure:
            ops = {op["operation_id"]: op for op in wo["operations"]}
            source = ops.get(failure["inspection_operation_id"])
            target = ops.get(failure["target_operation_id"])
            mes.require(source and source["kind"] == "inspection", "失败点必须是本工单质检工序", 422)
            mes.require(target and target["kind"] != "inspection" and
                        target["sequence"] < source["sequence"], "返修目标必须是质检前的加工或搬运工序", 422)
            mes.require(failure["quantity"] <= wo["quantity"], "不合格数量不能超过本批数量", 422)
    snapshot["orders"].sort(key=lambda w: (
        w["priority"], w["planned_date"] is None, w["planned_date"] or "", w["created_at"], w["work_order_id"]))
    snapshot["equipment"].sort(key=lambda e: e["equipment_id"])
    return snapshot
