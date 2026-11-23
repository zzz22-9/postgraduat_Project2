"""SimPy 控制时间和队列，MES 控制状态及数量；只操作每次运行的独立数据库。"""
import json
import random
from pathlib import Path

import simpy

from erp import database
from mes import service as mes
from mes.schemas import Inspection, Note, Report, ReworkAction
from . import field_protocols as field
from .field_runtime import FieldRecorder, alarm_text, initialize_sandbox
from .continuous import SAMPLE_PERIOD_S, SUPPORTED as CONTINUOUS_DEVICES, make_record
from .schemas import FieldScenario


class Interrupted(Exception):
    pass


def seed_database(path, snapshot):
    if path.exists():
        raise FileExistsError("仿真只允许创建新的沙盒mes.db，禁止覆盖既有数据库")
    database.initialize(path)
    mes.initialize(path)
    with mes.transaction(path) as conn:
        initialize_sandbox(conn)
        for eq in snapshot["equipment"]:
            conn.execute("""INSERT INTO equipment(equipment_id,name,kind,status,created_at)
                            VALUES (?,?,?,'available',?)""",
                         (eq["equipment_id"], eq["name"], eq["kind"], mes.now()))
        for wo in snapshot["orders"]:
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,'dispatched',?)", (
                wo["order_id"], wo["product_id"], wo["product_name"], wo["quantity"], wo["due_date"],
                wo.get("order_created_at")))  # 老快照缺少原订单时间时保存NULL，不套用工单时间。
            conn.execute("""INSERT INTO work_orders(work_order_id,order_id,status,priority,planned_date,created_at)
                            VALUES (?,?,'draft',?,?,?)""",
                         (wo["work_order_id"], wo["order_id"], wo["priority"], wo["planned_date"],
                          wo.get("work_order_created_at", wo["created_at"])))
            for op in wo["operations"]:
                conn.execute("""INSERT INTO operations(operation_id,work_order_id,sequence,name,kind,minutes,
                                equipment_id,status) VALUES (?,?,?,?,?,?,?,'pending')""",
                             (op["operation_id"], wo["work_order_id"], op["sequence"], op["name"],
                              op["kind"], op["minutes"], op["equipment_id"]))
            planned = sum(op["minutes"] * wo["quantity"] for op in wo["operations"])
            conn.execute("UPDATE work_orders SET planned_minutes=?,anomaly_status='NORMAL' WHERE work_order_id=?",
                         (planned, wo["work_order_id"]))
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
    available = {eq: env.event().succeed() for eq in resources}
    active_faults = {}
    scenarios = {wo["work_order_id"]: FieldScenario.model_validate(wo.get("field_protocols") or {})
                 for wo in snapshot["orders"]}
    samplers = {wo_id: random.Random(f"{config.seed}:{wo_id}") for wo_id, config in scenarios.items()}
    intervals, order_results = [], []
    waiting = {w["work_order_id"]: 0 for w in snapshot["orders"]}
    sequence = 0
    request_number = 0
    note = Note(operator="SimPy", note="仿真副本自动执行")

    def request_id():
        nonlocal request_number
        request_number += 1
        return f"{run_id}-{request_number}"

    with (folder / "events.jsonl").open("w", encoding="utf-8") as event_file, \
            (folder / "protocol_messages.jsonl").open("w", encoding="utf-8") as protocol_file, \
            (folder / "source_context.jsonl").open("w", encoding="utf-8") as source_file, \
            (folder / "telemetry.jsonl").open("w", encoding="utf-8") as telemetry_file:
        recorder = FieldRecorder(path, protocol_file, snapshot["equipment"], source_stream=source_file)
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

        def recover(wo, op):
            key = (wo["work_order_id"], op["operation_id"])
            if key in active_faults:
                recorder.recover(wo, op, env.now, active_faults.pop(key), emit)
                available[op["equipment_id"]].succeed()

        def processing(wo, op, quantity, phase, start, finish, rework_id=None, on_response=None):
            asked = env.now
            emit("resource_requested", wo, op, quantity, phase, rework_id)
            with resources[op["equipment_id"]].request() as request:
                yield request
                yield available[op["equipment_id"]]
                waiting[wo["work_order_id"]] += env.now - asked
                start()
                began = env.now
                emit("resource_acquired", wo, op, quantity, phase, rework_id, wait_min=began-asked)
                emit("processing_started", wo, op, quantity, phase, rework_id)
                supported = op["equipment_id"] in field.DEVICES
                if supported:
                    message, source = field.build_mqtt_dispatch_message(wo["work_order_id"], op["operation_id"],
                        op["equipment_id"], env.now, quantity, run_id=run_id, return_source=True)
                    recorder.message(message, "mes_to_field_mqtt_dispatched", wo, op, emit,
                                     quantity=quantity, phase=phase, rework_id=rework_id, source=source,
                                     expected_run_id=run_id, expected_virtual_minute=env.now)
                # In-process synthetic derived telemetry; preserve legacy end-of-operation frames.
                duration_s = op["minutes"] * quantity * 60
                tlm_context = dict(run_id=run_id, order_id=wo["order_id"],
                    work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                    phase=phase, rework_id=rework_id)
                tlm_count = [0]

                def write_tlm(protocol_message, provenance, elapsed_s, *, terminal=False):
                    item = make_record(protocol_message, provenance, tlm_context,
                        tlm_count[0], env.now, elapsed_s, duration_s, terminal=terminal)
                    telemetry_file.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
                    telemetry_file.flush()
                    tlm_count[0] += 1

                if op["equipment_id"] in CONTINUOUS_DEVICES:
                    tlm_builder = (field.build_cnc_opcua_message if op["equipment_id"] == "E-ASM"
                                   else field.build_robot_modbus_message)
                    tlm_template, tlm_source = tlm_builder(wo["work_order_id"], op["operation_id"],
                        env.now, is_anomaly=False, run_id=run_id, return_source=True)
                    write_tlm(tlm_template, tlm_source, 0)

                    def observe_tlm():
                        elapsed_s = 0
                        while elapsed_s + SAMPLE_PERIOD_S < duration_s - 1e-7:
                            yield env.timeout(SAMPLE_PERIOD_S / 60)
                            elapsed_s += SAMPLE_PERIOD_S
                            if stop.is_set():
                                raise Interrupted("服务关闭，连续观测中断")
                            write_tlm(tlm_template, tlm_source, elapsed_s)

                    env.process(observe_tlm())

                completed = env.timeout(op["minutes"] * quantity)
                if observer is not None:
                    observation_context = dict(run_id=run_id, order_id=wo["order_id"],
                        work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                        equipment_id=op["equipment_id"], quantity=quantity, phase=phase,
                        rework_id=rework_id, start_min=began, end_min=began + op["minutes"] * quantity)
                    env.process(observer.observe(env, observation_context, completed, stop))
                yield completed
                if supported:
                    config = scenarios[wo["work_order_id"]]
                    anomaly = phase == "normal" and op["equipment_id"] in config.anomaly_equipment_ids and (
                        config.mode == "anomaly" or (config.mode == "sampled" and
                        samplers[wo["work_order_id"]].random() < config.anomaly_probability))
                    builder, action = {
                        "E-ASM": (field.build_cnc_opcua_message, "field_opcua_signal_reported"),
                        "E-QC": (field.build_vision_http_message, "field_vision_http_reported"),
                        "E-MOVE": (field.build_robot_modbus_message, "field_modbus_reported"),
                    }[op["equipment_id"]]
                    options = {} if op["equipment_id"] == "E-QC" else {"sample_index": config.sample_index}
                    message, source = builder(wo["work_order_id"], op["operation_id"], env.now,
                                              is_anomaly=anomaly, run_id=run_id, return_source=True, **options)
                    extra = on_response(message) if on_response else 0
                    if message["is_anomaly"] and op["equipment_id"] != "E-QC":
                        extra = config.cnc_repair_minutes if op["equipment_id"] == "E-ASM" else config.robot_repair_minutes
                    alarm = alarm_text(message, extra) if message["is_anomaly"] else ""
                    recorder.message(message, action, wo, op, emit, alarm,
                                     quantity=quantity, phase=phase, rework_id=rework_id, source=source,
                                     expected_run_id=run_id, expected_virtual_minute=env.now)
                    if op["equipment_id"] in CONTINUOUS_DEVICES:
                        # Never persist a terminal sample until the original P0.3 relation check passes.
                        write_tlm(message, source, duration_s, terminal=True)
                    if message["is_anomaly"]:
                        fault = recorder.fault(message, wo, op, env.now, alarm, extra, emit)
                        active_faults[(wo["work_order_id"], op["operation_id"])] = fault
                        available[op["equipment_id"]] = env.event()
                        if op["equipment_id"] != "E-QC":
                            yield env.timeout(extra)  # 真正延长SimPy工序占用与工单完成时刻。
                            recover(wo, op)
                emit("processing_finished", wo, op, quantity, phase, rework_id)
                finish()
                intervals.append(dict(work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                                      equipment_id=op["equipment_id"], name=op["name"], quantity=quantity,
                                      phase=phase, rework_id=rework_id, start_min=began, end_min=env.now))
            emit("resource_released", wo, op, quantity, phase, rework_id)
            progress(env.now, len(order_results))

        def production(wo):
            order_start = env.now
            rework_quantity = 0
            emit("order_started", wo)
            for op in wo["operations"]:
                failure = wo["failure"] if wo["failure"] and wo["failure"]["inspection_operation_id"] == op["operation_id"] else None
                rejected = failure["quantity"] if failure else 0
                outcome = {}

                def observed(message):
                    nonlocal failure, rejected
                    if op["equipment_id"] == "E-QC" and message["is_anomaly"]:
                        if not failure:
                            target = next(o for o in reversed(wo["operations"])
                                if o["sequence"] < op["sequence"] and o["kind"] != "inspection")
                            failure = dict(inspection_operation_id=op["operation_id"],
                                           target_operation_id=target["operation_id"], quantity=1)
                        rejected = failure["quantity"]
                        target = next(o for o in wo["operations"] if o["operation_id"] == failure["target_operation_id"])
                        return (target["minutes"] + op["minutes"]) * rejected
                    return 0

                def finish():
                    outcome["detail"] = mes.report(path, op["operation_id"], Report(
                        request_id=request_id(), operator="SimPy", good_quantity=wo["quantity"]-rejected,
                        rework_quantity=rejected, note="质检异常，进入返工/复检" if rejected else "仿真整批合格",
                        rework_operation_id=failure["target_operation_id"] if failure else None))
                    if op["kind"] == "inspection":
                        emit("quality_result", wo, op, accepted_quantity=wo["quantity"]-rejected, rejected_quantity=rejected)

                yield from processing(wo, op, wo["quantity"], "normal",
                    lambda: mes.execute_operation(path, op["operation_id"], "start", note), finish,
                    on_response=observed)
                if failure:
                    rework_quantity += rejected
                    task = outcome["detail"]["reworks"][-1]
                    rw_id = task["rework_id"]
                    target = next(o for o in wo["operations"] if o["operation_id"] == failure["target_operation_id"])
                    emit("rework_created", wo, target, rejected, "repair", rw_id,
                         source_operation_id=op["operation_id"])

                    def rw(action, equipment_id=None, passed=None):
                        return mes.execute_rework(path, rw_id, action, ReworkAction(
                            request_id=request_id(), operator="SimPy", note="质检异常后的返工与复检",
                            equipment_id=equipment_id, passed=passed))

                    yield from processing(wo, target, rejected, "repair",
                        lambda: rw("start", target["equipment_id"]), lambda: rw("finish"), rw_id)
                    recover(wo, op)  # 返工已完成，视觉站解除故障，随后真实复检。

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
            planned = sum(op["minutes"] * wo["quantity"] for op in wo["operations"])
            actual = env.now - order_start
            had_fault = any(f["work_order_id"] == wo["work_order_id"]
                            for alert in recorder.alerts.values() for f in alert["fault_records"])
            timing = dict(planned_minutes=planned, actual_minutes=actual,
                          delay_minutes=max(0, actual-planned),
                          anomaly_status="DELAYED_BY_FAULT" if had_fault else "NORMAL")
            with mes.transaction(path) as conn:
                conn.execute("""UPDATE work_orders SET planned_minutes=?,actual_minutes=?,delay_minutes=?,
                             anomaly_status=? WHERE work_order_id=?""",
                             (*timing.values(), wo["work_order_id"]))
            order_results.append(dict(work_order_id=wo["work_order_id"], order_id=wo["order_id"],
                                      product_name=wo["product_name"], quantity=wo["quantity"],
                                      completed_min=env.now, waiting_min=waiting[wo["work_order_id"]],
                                      rework_quantity=rework_quantity, **timing,
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
                equipment_alerts=list(recorder.alerts.values()), protocol_messages=recorder.messages,
                delay_summary=[{k: wo[k] for k in ("work_order_id", "planned_minutes", "actual_minutes",
                               "delay_minutes", "anomaly_status")} for wo in order_results],
                completed_quantity=sum(w["quantity"] for w in order_results),
                rework_quantity=sum(w["rework_quantity"] for w in order_results),
                total_waiting_min=sum(waiting.values()), event_count=sequence)
