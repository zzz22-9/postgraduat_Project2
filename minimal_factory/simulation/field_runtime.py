"""仅供新建仿真沙盒使用：协议落盘、设备故障轨迹与报警卡片。"""
import json

from mes import service as mes
from .field_protocols import DEVICES


def initialize_sandbox(conn):
    """在尚未种入业务数据的新库中扩展设备状态；不迁移生产库。"""
    if conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
        raise ValueError("只允许初始化空的仿真沙盒，禁止重建已有订单表")
    # 仅新沙盒允许原订单时间未知（旧input.json没有该信息）；正式库约束不变。
    conn.execute("DROP TABLE orders")
    conn.execute("""CREATE TABLE orders (
        order_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
        product_name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
        due_date TEXT, status TEXT NOT NULL
        CHECK(status IN ('pending','dispatched','in_progress','completed')),
        created_at TEXT)""")
    conn.execute("DELETE FROM equipment")
    conn.execute("DROP TABLE equipment")
    conn.execute("""CREATE TABLE equipment (
        equipment_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK(kind IN ('assembly','inspection','transport')),
        status TEXT NOT NULL CHECK(status IN ('available','maintenance','fault')),
        created_at TEXT NOT NULL, alarm_message TEXT, fault_virtual_minute REAL,
        recovered_virtual_minute REAL)""")
    for name, sql_type in (("planned_minutes", "REAL"), ("actual_minutes", "REAL"),
                           ("delay_minutes", "REAL"), ("anomaly_status", "TEXT")):
        conn.execute(f"ALTER TABLE work_orders ADD COLUMN {name} {sql_type}")
    conn.execute("""CREATE TABLE protocol_messages (
        message_id TEXT PRIMARY KEY, virtual_minute REAL NOT NULL,
        protocol TEXT NOT NULL, work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
        operation_id TEXT NOT NULL REFERENCES operations(operation_id),
        equipment_id TEXT NOT NULL REFERENCES equipment(equipment_id), device_code TEXT NOT NULL,
        is_anomaly INTEGER NOT NULL CHECK(is_anomaly IN (0,1)), alarm_summary TEXT NOT NULL,
        source_file TEXT NOT NULL, sample_hash TEXT NOT NULL, raw_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX protocols_by_order ON protocol_messages(work_order_id,virtual_minute)")


class FieldRecorder:
    def __init__(self, path, stream, equipment, *, source_stream):
        self.path, self.stream, self.source_stream = path, stream, source_stream
        self.messages = []
        protocols = {"E-ASM": "OPC-UA", "E-QC": "HTTP-Vision-Webhook", "E-MOVE": "Modbus-TCP"}
        self.alerts = {eq["equipment_id"]: dict(
            equipment_id=eq["equipment_id"], equipment_name=eq["name"],
            device_code=DEVICES[eq["equipment_id"]], protocol=protocols[eq["equipment_id"]],
            status="normal", current_status="available", had_fault=False,
            alarm_message="", fault_records=[])
            for eq in equipment if eq["equipment_id"] in DEVICES}

    def event(self, action, detail, wo, op):
        with mes.transaction(self.path) as conn:
            mes.log(conn, action, json.dumps(detail, ensure_ascii=False), wo, op, operator="SimPy")

    def message(self, message, action, wo, op, emit, alarm_summary="", *,
                quantity, phase, rework_id, source, expected_run_id, expected_virtual_minute):
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False)
        source_raw = json.dumps(source, ensure_ascii=False, allow_nan=False)
        fields = ("message_id", "virtual_minute", "protocol", "work_order_id", "operation_id",
                  "equipment_id", "device_code", "is_anomaly")
        with mes.transaction(self.path) as conn:
            mes.require(message["work_order_id"] == wo["work_order_id"],
                        "协议 work_order_id 与本次执行工单不一致")
            actual_op = mes.operation(conn, message["operation_id"])
            mes.require(actual_op["work_order_id"] == wo["work_order_id"],
                        "协议 operation_id 不属于本次执行工单")
            mes.require(message["operation_id"] == op["operation_id"],
                        "协议 operation_id 与本次执行工序不一致")
            mes.require(actual_op["equipment_id"] == op["equipment_id"] == message["equipment_id"],
                        "协议 equipment_id 与工序实际设备不一致")
            mes.require(message["device_code"] == DEVICES[actual_op["equipment_id"]],
                        "协议 device_code 与实际设备映射不一致")
            mes.require(message["run_id"] == expected_run_id, "协议 run_id 与当前仿真批次不一致")
            mes.require(message["virtual_minute"] == expected_virtual_minute,
                        "协议 virtual_minute 与 SimPy 当前时间不一致")
            mes.require(source["message_id"] == message["message_id"],
                        "来源 message_id 与当前协议消息不一致")
            conn.execute("""INSERT INTO protocol_messages(message_id,virtual_minute,protocol,work_order_id,
                         operation_id,equipment_id,device_code,is_anomaly,alarm_summary,source_file,
                         sample_hash,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         tuple(message[k] for k in fields) +
                         (alarm_summary, message["source_file"], message["sample_hash"], raw))
            mes.log(conn, action, raw, wo, op, operator="SimPy")
        self.messages.append(message)
        if message["protocol"] != "MQTT":
            alert = self.alerts[message["equipment_id"]]
            alert.update(latest_evidence=message["payload"], latest_message_id=message["message_id"])
            # 复检合格后仍保留最初缺陷图片；正常卡片也可直接展示采集数值/图像。
            if not alert["had_fault"]:
                alert.update(evidence=message["payload"], source_file=message["source_file"])
        self.stream.write(raw + "\n")
        self.stream.flush()
        self.source_stream.write(source_raw + "\n")
        self.source_stream.flush()
        emit(action, wo, op, quantity=quantity, phase=phase, rework_id=rework_id,
             action=action, detail=message)

    def fault(self, message, wo, op, now, alarm, extra_minutes, emit):
        detail = dict(virtual_minute=now, status="fault", alarm_message=alarm,
                      extra_minutes=extra_minutes, protocol=message["protocol"],
                      message_id=message["message_id"], source_file=message["source_file"],
                      sample_hash=message["sample_hash"], evidence=message["payload"])
        with mes.transaction(self.path) as conn:
            conn.execute("""UPDATE equipment SET status='fault',alarm_message=?,
                fault_virtual_minute=?,recovered_virtual_minute=NULL WHERE equipment_id=?""",
                         (alarm, now, op["equipment_id"]))
            conn.execute("UPDATE work_orders SET anomaly_status='DELAYED_BY_FAULT' WHERE work_order_id=?",
                         (wo["work_order_id"],))
            mes.log(conn, "device_anomaly_affected_work_order", json.dumps(detail, ensure_ascii=False),
                    wo, op, operator="SimPy")
        alert = self.alerts[op["equipment_id"]]
        alert.update(status="fault", current_status="fault", had_fault=True,
                     alarm_message=alarm, evidence=message["payload"], source_file=message["source_file"])
        fault = dict(detail, work_order_id=wo["work_order_id"], operation_id=op["operation_id"],
                     recovered_virtual_minute=None)
        alert["fault_records"].append(fault)
        emit("device_anomaly_affected_work_order", wo, op,
             action="device_anomaly_affected_work_order", detail=detail)
        return fault

    def recover(self, wo, op, now, fault, emit):
        detail = dict(virtual_minute=now, status="available", previous_status="fault",
                      alarm_message="维修/返工完成，设备恢复", fault_virtual_minute=fault["virtual_minute"])
        with mes.transaction(self.path) as conn:
            conn.execute("UPDATE equipment SET status='available',recovered_virtual_minute=? WHERE equipment_id=?",
                         (now, op["equipment_id"]))
            mes.log(conn, "device_recovered", json.dumps(detail, ensure_ascii=False), wo, op, operator="SimPy")
        fault["recovered_virtual_minute"] = now
        self.alerts[op["equipment_id"]]["current_status"] = "available"
        emit("device_recovered", wo, op, action="device_recovered", detail=detail)


def alarm_text(message, extra_minutes):
    code, payload = message["device_code"], message["payload"]
    if code == "Vision_01":
        detection = payload["detections"][0]
        reason = f"检出法兰缺失螺栓({detection['count']}/{detection['expected']})"
        treatment = "返工/复检"
    else:
        if code == "CNC_01":
            values = {node["node_id"].rsplit(".", 1)[-1]: node["value"] for node in payload["nodes"]}
            current, temperature, load = (values[k] for k in ("SpindleCurrent", "MotorTemperature", "SpindleLoad"))
        else:
            regs = payload["raw_registers"]
            current, temperature, load = regs[0]/1000, regs[1]/10, regs[2]/10
        reason = f"电流/温度/负载异常({current:g}A/{temperature:g}℃/{load:g}%)"
        treatment = "停机检修"
    return (f"【{'质检' if code == 'Vision_01' else '设备'}报警】{code} {reason}，"
            f"设备进入 FAULT 状态，触发{treatment} +{extra_minutes:g} 虚拟分钟，"
            f"导致工单 {message['work_order_id']} 延误")
