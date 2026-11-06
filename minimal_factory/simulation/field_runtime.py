"""仅供新建仿真沙盒使用：协议落盘、设备故障轨迹与报警卡片。"""
import json

from mes import service as mes
from .field_protocols import DEVICES


def initialize_sandbox(conn):
    """在尚未种入业务数据的新库中扩展设备状态；不迁移生产库。"""
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
    def __init__(self, path, stream, equipment):
        self.path, self.stream = path, stream
        self.messages = []
        protocols = {"E-ASM": "OPC-UA-v1", "E-QC": "HTTP-Vision-v1", "E-MOVE": "Modbus-TCP-v1"}
        self.alerts = {eq["equipment_id"]: dict(
            equipment_id=eq["equipment_id"], equipment_name=eq["name"],
            device_code=DEVICES[eq["equipment_id"]], protocol=protocols[eq["equipment_id"]],
            status="normal", current_status="available", had_fault=False,
            alarm_message="", fault_records=[])
            for eq in equipment if eq["equipment_id"] in DEVICES}

    def event(self, action, detail, wo, op):
        with mes.transaction(self.path) as conn:
            mes.log(conn, action, json.dumps(detail, ensure_ascii=False), wo, op, operator="SimPy")

    def message(self, message, action, wo, op, emit, alarm_summary=""):
        raw = json.dumps(message, ensure_ascii=False, allow_nan=False)
        fields = ("message_id", "virtual_minute", "protocol", "work_order_id", "operation_id",
                  "equipment_id", "device_code", "is_anomaly")
        with mes.transaction(self.path) as conn:
            conn.execute("""INSERT INTO protocol_messages(message_id,virtual_minute,protocol,work_order_id,
                         operation_id,equipment_id,device_code,is_anomaly,alarm_summary,source_file,
                         sample_hash,raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                         tuple(message[k] for k in fields) +
                         (alarm_summary, message["source_file"], message["sample_hash"], raw))
            mes.log(conn, action, raw, wo, op, operator="SimPy")
        self.messages.append(message)
        if message["protocol"] != "MQTT-Dispatch-v1":
            alert = self.alerts[message["equipment_id"]]
            alert.update(latest_evidence=message["payload"], latest_message_id=message["message_id"])
            # 复检合格后仍保留最初缺陷图片；正常卡片也可直接展示采集数值/图像。
            if not alert["had_fault"]:
                alert.update(evidence=message["payload"], source_file=message["source_file"])
        self.stream.write(raw + "\n")
        self.stream.flush()
        emit(action, wo, op, action=action, detail=message)

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
        reason = f"检出法兰缺失螺栓({payload['detected_bolts']}/{payload['expected_bolts']})"
        treatment = "返工/复检"
    else:
        if code == "CNC_01":
            values = payload["values"]
            current, temperature, load = (values[k] for k in ("current_a", "temperature_c", "load_percent"))
        else:
            regs = payload["registers"]
            current, temperature, load = regs["40001"]/1000, regs["40002"]/10, regs["40003"]/10
        reason = f"电流/温度/负载异常({current:g}A/{temperature:g}℃/{load:g}%)"
        treatment = "停机检修"
    return (f"【{'质检' if code == 'Vision_01' else '设备'}报警】{code} {reason}，"
            f"设备进入 FAULT 状态，触发{treatment} +{extra_minutes:g} 虚拟分钟，"
            f"导致工单 {message['work_order_id']} 延误")
