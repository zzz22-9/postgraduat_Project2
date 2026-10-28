"""SimPy 控制时间和队列，MES 控制状态及数量；只操作每次运行的独立数据库。"""
import json
from pathlib import Path

import simpy

from erp import database
from mes import service as mes
from mes.schemas import Inspection, Note, Report, ReworkAction


class Interrupted(Exception):
    pass


def seed_database(path, snapshot):
    database.initialize(path)
    mes.initialize(path)
    with mes.transaction(path) as conn:
        conn.execute("DELETE FROM equipment")  # 新建副本中的三台默认教学资源，替换为输入设备。
        for eq in snapshot["equipment"]:
            conn.execute("INSERT INTO equipment VALUES (?,?,?,'available',?)",
                         (eq["equipment_id"], eq["name"], eq["kind"], mes.now()))
        for wo in snapshot["orders"]:
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,'dispatched',?)", (
                wo["order_id"], wo["product_id"], wo["product_name"], wo["quantity"], wo["due_date"], wo["created_at"]))
            conn.execute("""INSERT INTO work_orders(work_order_id,order_id,status,priority,planned_date,created_at)
                            VALUES (?,?,'draft',?,?,?)""",
                         (wo["work_order_id"], wo["order_id"], wo["priority"], wo["planned_date"], wo["created_at"]))
            for op in wo["operations"]:
                conn.execute("""INSERT INTO operations(operation_id,work_order_id,sequence,name,kind,minutes,
                                equipment_id,status) VALUES (?,?,?,?,?,?,?,'pending')""",
                             (op["operation_id"], wo["work_order_id"], op["sequence"], op["name"],
                              op["kind"], op["minutes"], op["equipment_id"]))
    for wo in snapshot["orders"]:
        mes.release(path, wo["work_order_id"])


def simulate(folder: Path, run_id, snapshot, stop, progress, observer=None):
    """Optional observer only adds source observations; None preserves legacy scheduling.

    observe(env, context, completed, stop) must end with the existing completion
    event and must not schedule timeouts beyond context['end_min'].
    """
    path = folder / "mes.db"
    seed_database(path, snapshot)
    env = simpy.Environment()
    resources = {e["equipment_id"]: simpy.Resource(env, capacity=1) for e in snapshot["equipment"]}
    intervals, order_results = [], []
    waiting = {w["work_order_id"]: 0 for w in snapshot["orders"]}
    sequence = 0
    request_number = 0
    note = Note(operator="SimPy", note="仿真副本自动执行")

    def request_id():
        nonlocal request_number
        request_number += 1
        return f"{run_id}-{request_number}"

    with (folder / "events.jsonl").open("w", encoding="utf-8") as event_file:
        def emit(event_type, wo, op=None, quantity=None, phase="normal", rework_id=None, **extra):
            nonlocal sequence
            if stop.is_set():
                raise Interrupted("服务关闭，实验中断；可复制参数重新运行")
            sequence += 1
            event = dict(run_id=run_id, sequence=sequence, sim_time_min=env.now, event_type=event_type,
                         order_id=wo["order_id"], work_order_id=wo["work_order_id"],
                         operation_id=op["operation_id"] if op else None,
                         equipment_id=op["equipment_id"] if op else None,
                         quantity=wo["quantity"] if quantity is None else quantity,
                         phase=phase, rework_id=rework_id, recorded_at=mes.now(), **extra)
            event_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            event_file.flush()
            if observer is not None:
                observer.on_event(dict(event))

        def processing(wo, op, quantity, phase, start, finish, rework_id=None):
            asked = env.now
            emit("resource_requested", wo, op, quantity, phase, rework_id)
            with resources[op["equipment_id"]].request() as request:
                yield request
                waiting[wo["work_order_id"]] += env.now - asked
                start()
                began = env.now
                emit("resource_acquired", wo, op, quantity, phase, rework_id, wait_min=began-asked)
                emit("processing_started", wo, op, quantity, phase, rework_id)
                completed = env.timeout(op["minutes"] * quantity)
                if observer is not None:
                    observation_context = dict(run_id=run_id, order_id=wo["order_id"],
                        work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                        equipment_id=op["equipment_id"], quantity=quantity, phase=phase,
                        rework_id=rework_id, start_min=began, end_min=began + op["minutes"] * quantity)
                    env.process(observer.observe(env, observation_context, completed, stop))
                yield completed
                emit("processing_finished", wo, op, quantity, phase, rework_id)
                finish()
                intervals.append(dict(work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                                      equipment_id=op["equipment_id"], name=op["name"], quantity=quantity,
                                      phase=phase, rework_id=rework_id, start_min=began, end_min=env.now))
            emit("resource_released", wo, op, quantity, phase, rework_id)
            progress(env.now, len(order_results))

        def production(wo):
            emit("order_started", wo)
            for op in wo["operations"]:
                failure = wo["failure"] if wo["failure"] and wo["failure"]["inspection_operation_id"] == op["operation_id"] else None
                rejected = failure["quantity"] if failure else 0
                outcome = {}

                def finish():
                    outcome["detail"] = mes.report(path, op["operation_id"], Report(
                        request_id=request_id(), operator="SimPy", good_quantity=wo["quantity"]-rejected,
                        rework_quantity=rejected, note="指定质检失败，进入返修" if rejected else "仿真整批合格",
                        rework_operation_id=failure["target_operation_id"] if failure else None))
                    if op["kind"] == "inspection":
                        emit("quality_result", wo, op, accepted_quantity=wo["quantity"]-rejected, rejected_quantity=rejected)

                yield from processing(wo, op, wo["quantity"], "normal",
                    lambda: mes.execute_operation(path, op["operation_id"], "start", note), finish)
                if failure:
                    task = outcome["detail"]["reworks"][-1]
                    rw_id = task["rework_id"]
                    target = next(o for o in wo["operations"] if o["operation_id"] == failure["target_operation_id"])
                    emit("rework_created", wo, target, rejected, "repair", rw_id,
                         source_operation_id=op["operation_id"])

                    def rw(action, equipment_id=None, passed=None):
                        return mes.execute_rework(path, rw_id, action, ReworkAction(
                            request_id=request_id(), operator="SimPy", note="指定失败后的返修与复检",
                            equipment_id=equipment_id, passed=passed))

                    yield from processing(wo, target, rejected, "repair",
                        lambda: rw("start", target["equipment_id"]), lambda: rw("finish"), rw_id)

                    def inspected():
                        rw("inspect-finish", passed=True)
                        emit("quality_result", wo, op, rejected, "reinspection", rw_id,
                             accepted_quantity=rejected, rejected_quantity=0)

                    yield from processing(wo, op, rejected, "reinspection",
                        lambda: rw("inspect-start", op["equipment_id"]), inspected, rw_id)
            completed = mes.inspect(path, wo["work_order_id"], Inspection(
                request_id=request_id(), operator="SimPy", rework_quantity=0, note="全部工序及复检合格，自动放行"))
            assert completed["status"] == completed["order_status"] == "completed"
            emit("order_completed", wo)
            order_results.append(dict(work_order_id=wo["work_order_id"], order_id=wo["order_id"],
                                      product_name=wo["product_name"], quantity=wo["quantity"],
                                      completed_min=env.now, waiting_min=waiting[wo["work_order_id"]],
                                      rework_quantity=wo["failure"]["quantity"] if wo["failure"] else 0,
                                      mes_status=completed["status"], erp_status=completed["order_status"]))
            progress(env.now, len(order_results))

        for wo in snapshot["orders"]:
            env.process(production(wo))
        env.run()
    if len(order_results) != len(snapshot["orders"]):
        raise RuntimeError("仿真事件已耗尽，但仍有工单未完成")
    order_results.sort(key=lambda w: [x["work_order_id"] for x in snapshot["orders"]].index(w["work_order_id"]))
    device_results = []
    for eq in snapshot["equipment"]:
        occupied = sum(i["end_min"]-i["start_min"] for i in intervals if i["equipment_id"] == eq["equipment_id"])
        device_results.append(dict(**eq, busy_min=occupied, utilization=round(100*occupied/env.now, 2) if env.now else 0))
    return dict(makespan_min=env.now, orders=order_results, equipment=device_results, intervals=intervals,
                completed_quantity=sum(w["quantity"] for w in order_results),
                rework_quantity=sum(w["rework_quantity"] for w in order_results),
                total_waiting_min=sum(waiting.values()), event_count=sequence)
