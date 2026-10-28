"""Actual MES dispatch/assignment/release transactions, observed through MQTT."""
from .legacy_bridge import independent_path, load_legacy


def prepare_work_order(database_path, order, registry, emitter, ordinal=0):
    path = independent_path(database_path)
    _, _, mes, schemas, _, inputs = load_legacy()
    work = mes.dispatch(path, schemas.Dispatch(order_id=order["order_id"]))
    registry.bind_work_order(work, ordinal)
    events = [("dispatched", work)]
    equipment_by_kind = {"assembly": "E-ASM", "inspection": "E-QC", "transport": "E-MOVE"}
    for op in work["operations"]:
        result = mes.assign(path, op["operation_id"], schemas.Assignment(equipment_id=equipment_by_kind[op["kind"]]))
        events.append(("equipment_assigned", result))
    released = mes.release(path, work["work_order_id"])
    events.append(("released", released))
    for sequence, (event, detail) in enumerate(events, 1):
        emitter.emit("MES", "MQTT", "structured_record", "mes/work_orders/events", detail["created_at"],
                     {"event": event, "work_order": detail},
                     {"clock": "wall", "sequence": sequence, "order_id": order["order_id"],
                      "work_order_id": work["work_order_id"], "transport_mode": "memory_mqtt_mock"},
                     {"raw_id": work["work_order_id"], "suggested_type": "work_order", "domain_hint": "manufacturing", "level_hint": "factory"})
    return released, inputs.source_snapshot(path, [work["work_order_id"]])
