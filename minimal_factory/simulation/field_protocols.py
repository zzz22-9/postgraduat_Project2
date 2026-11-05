"""读取固定素材并返回四类项目协议字典，命令行入口只向终端打印JSON。

信号sample_hash为选中JSONL行原始字节的SHA256（不含换行）。
图像sample_hash为PNG文件SHA256，MQTT的sample_hash为素材清单SHA256。
Vision的detected_bolts取场景标签，payload明确标注判定来源。
这些函数封装项目报文契约，协议网络收发由后续调用方负责。
"""

import argparse  # 解析独立自测命令的参数。
import hashlib  # 校验素材内容，并生成可重复的消息编号。
import json  # 读取JSON/JSONL，向终端打印协议JSON。
import math  # 拒绝NaN和无穷大时间或特征值。
from pathlib import Path  # 以脚本所在目录定位素材，兼容不同终端工作目录。


ASSET_ROOT = Path(__file__).resolve().parents[1]  # minimal_factory绝对目录。
SIGNAL_DIR = Path("synthetic_signals/generated_v1")  # 信号素材相对位置。
IMAGE_DIR = Path("synthetic_images/rendered_v1")  # 图片素材相对位置。
DEVICES = {"E-ASM": "CNC_01", "E-QC": "Vision_01", "E-MOVE": "Robot_01"}  # 固定设备绑定。


def _json_file(path):
    """真实读取文件，返回解析结果与原始字节。"""
    raw = path.read_bytes()  # 文件缺失时抛出异常，调用方能看到具体路径。
    return json.loads(raw), raw  # JSON解析失败时保留原异常信息。


def _finite_number(value, name):
    """时间和特征必须是有限数字，布尔值不作为数值接受。"""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name}必须是有限数字")  # 在返回报文前拒绝非法数据。


def _envelope(protocol, work_order_id, operation_id, equipment_id, virtual_minute,
              is_anomaly, source_file, sample_hash, payload, run_id):
    """统一追踪头，消费素材时由调用方传入当前业务编号和虚拟时间。"""
    if not isinstance(work_order_id, str) or not work_order_id.strip():
        raise ValueError("work_order_id不能为空")  # 每条消息必须能追到工单。
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("operation_id不能为空")  # 每条消息必须能追到工序。
    if equipment_id not in DEVICES:
        raise ValueError("equipment_id必须为E-ASM、E-QC或E-MOVE")  # 防止设备串线。
    _finite_number(virtual_minute, "virtual_minute")  # 记录SimPy虚拟分钟。
    if virtual_minute < 0:
        raise ValueError("virtual_minute不能为负数")  # 虚拟时间从零开始。
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise ValueError("提供run_id时必须是非空字符串")  # 未来不同批次使用各自编号。
    message = {  # 四类协议共享这组追踪字段。
        "protocol": protocol, "virtual_minute": virtual_minute,
        "work_order_id": work_order_id, "operation_id": operation_id,
        "equipment_id": equipment_id, "device_code": DEVICES[equipment_id],
        "is_anomaly": is_anomaly, "source_file": str(source_file.resolve()),
        "sample_hash": sample_hash, "payload": payload, "run_id": run_id,
    }
    canonical = json.dumps(message, sort_keys=True, ensure_ascii=False, allow_nan=False)  # 固定字段排序。
    message_id = "MSG-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]  # 不调用随机数。
    return {"message_id": message_id, **message}  # 相同上下文和样本得到相同消息号。


def _signal_sample(equipment_id, is_anomaly, sample_index, asset_root):
    """按设备、场景和明确行号读取样本，并验证清单、身份和异常标签。"""
    if type(is_anomaly) is not bool:
        raise ValueError("is_anomaly必须是bool")  # 避免字符串false被当作True。
    if type(sample_index) is not int or sample_index < 0:
        raise ValueError("sample_index必须是非负整数")  # 不隐式随机抽样或循环取模。
    prefix = {"E-ASM": "cnc", "E-MOVE": "robot"}[equipment_id]  # 设备决定文件前缀。
    scenario = "overload" if is_anomaly else "normal"  # 选择磁盘上已存在的场景文件。
    folder = Path(asset_root) / SIGNAL_DIR  # 支持测试时显式指定另一份素材根目录。
    manifest, _ = _json_file(folder / "manifest.json")  # 清单提供文件哈希和报警阈值。
    filename = f"{prefix}_{scenario}.jsonl"  # 文件名由受控设备及场景组成。
    entries = [entry for entry in manifest["field_library"]["files"] if entry["path"] == filename]
    if len(entries) != 1:
        raise ValueError(f"清单缺少唯一文件记录：{filename}")  # 防止重复或缺失配置。
    entry = entries[0]  # 唯一的清单记录。
    path = folder / filename  # 即将真实读取的JSONL文件。
    raw = path.read_bytes()  # 每次调用读取磁盘内容，后续修改可被校验发现。
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError(f"信号文件哈希不符：{path}")  # 不封装被修改或损坏的素材。
    lines = raw.splitlines()  # 保留每条记录的原始字节用于sample_hash。
    if len(lines) != entry["sample_count"] or sample_index >= len(lines):
        raise ValueError("信号文件行数不符或sample_index越界")  # 越界立即报错。
    sample = json.loads(lines[sample_index])  # 只按指定行读取具体记录。
    if (sample["equipment_id"] != equipment_id or sample["equipment_name"] != DEVICES[equipment_id]
            or sample["scenario"] != scenario or entry["equipment_id"] != equipment_id):
        raise ValueError("信号设备或场景与请求不匹配")  # 拒绝把Robot数据当CNC数据。
    reasons = []  # 根据真实字段和清单阈值逐项形成异常原因。
    for key in ("current_a", "temperature_c", "load_percent"):
        _finite_number(sample[key], key)  # 只接受有限工程数值。
        limit = entry["thresholds"][key]  # 从清单读取当前设备的对应阈值。
        _finite_number(limit, key + "阈值")  # 防止坏阈值漏报。
        if sample[key] > limit:
            reasons.append(f"{key}={sample[key]}超过阈值{limit}")  # 保留具体数值及判据。
    if type(sample["alarm_code"]) is not int or sample["alarm_code"] not in (0, 1):
        raise ValueError("样本alarm_code必须为整数0或1")  # 明确报警码范围。
    if bool(reasons) != is_anomaly or bool(sample["alarm_code"]) != is_anomaly:
        raise ValueError("样本数值、报警码与场景不一致")  # 数据决定是否异常。
    return sample, path, hashlib.sha256(lines[sample_index]).hexdigest(), reasons  # 返回记录和来源证据。


def build_mqtt_dispatch_message(work_order_id, operation_id, equipment_id, virtual_minute,
                                planned_qty, *, command="START", qos=1, run_id=None,
                                asset_root=ASSET_ROOT):
    """输入当前任务，读取设备素材清单，返回MQTT-Dispatch-v1命令字典。"""
    if equipment_id not in DEVICES:
        raise ValueError("下发设备编号不支持")  # 在读取清单前校验设备。
    if type(planned_qty) is not int or planned_qty <= 0:
        raise ValueError("planned_qty必须为正整数")  # 计划件数不能为零或小数。
    if type(qos) is not int or qos not in (0, 1, 2):
        raise ValueError("qos必须为0、1或2")  # MQTT消息服务质量等级。
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command不能为空")  # 命令由调用方显式指定。
    folder = IMAGE_DIR if equipment_id == "E-QC" else SIGNAL_DIR  # 清单与目标设备相对应。
    path = Path(asset_root) / folder / "manifest.json"  # 记录命令引用的素材库位置。
    manifest, raw = _json_file(path)  # 真实读取清单，命令本身不预报设备故障。
    payload = {"topic": f"factory/line1/{DEVICES[equipment_id]}/dispatch", "qos": qos,
               "command": command, "planned_qty": planned_qty,
               "asset_dataset_id": manifest["field_library"]["dataset_id"],
               "evidence_kind": "asset_manifest"}  # 清单哈希与观测样本哈希分别标识来源。
    return _envelope("MQTT-Dispatch-v1", work_order_id, operation_id, equipment_id,
                     virtual_minute, False, path, hashlib.sha256(raw).hexdigest(), payload, run_id)


def build_cnc_opcua_message(work_order_id, operation_id, virtual_minute, *, is_anomaly=False,
                            sample_index=0, run_id=None, asset_root=ASSET_ROOT):
    """读取CNC指定行，返回OPC-UA-v1节点和值及异常原因。"""
    sample, path, sample_hash, reasons = _signal_sample("E-ASM", is_anomaly, sample_index, asset_root)
    keys = ("current_a", "temperature_c", "load_percent", "alarm_code")  # 四个观测字段。
    payload = {
        "node_id": {key: f"ns=2;s=CNC_01.{key}" for key in keys},  # 项目OPC UA节点命名约定。
        "values": {key: sample[key] for key in keys},  # 数值直接取自磁盘样本。
        "sample_id": sample["sample_id"], "sample_index": sample_index,
        "anomaly_reasons": reasons, "synthetic": sample["synthetic"],
    }
    return _envelope("OPC-UA-v1", work_order_id, operation_id, "E-ASM", virtual_minute,
                     bool(reasons), path, sample_hash, payload, run_id)  # 同时携带上下文和原始行哈希。


def build_vision_http_message(work_order_id, operation_id, virtual_minute, *, is_anomaly=False,
                              view="front", run_id=None, asset_root=ASSET_ROOT):
    """读取图片清单和PNG，返回HTTP-Vision-v1的本地标签回放结果。"""
    if type(is_anomaly) is not bool or view not in ("front", "side"):
        raise ValueError("is_anomaly须为bool，view须为front或side")  # 限定已有素材选择。
    folder = Path(asset_root) / IMAGE_DIR  # 固定图片素材目录。
    manifest_path = folder / "manifest.json"  # 图片标签及哈希来自该清单。
    manifest, _ = _json_file(manifest_path)  # 读取磁盘上的真实图片描述。
    label = "defect" if is_anomaly else "normal"  # 选择正常或缺陷条目。
    entries = [row for row in manifest["field_samples"] if row["label"] == label and row["view"] == view]
    if len(entries) != 1:
        raise ValueError("清单缺少唯一的视觉样本")  # 拒绝缺项或歧义。
    entry = entries[0]  # 取选定的唯一图片条目。
    path = (folder / entry["image_path"]).resolve()  # 将清单中的路径变为实际文件位置。
    if not path.is_relative_to(folder.resolve()):
        raise ValueError("图片路径超出素材目录")  # 清单只能引用素材目录内的文件。
    raw = path.read_bytes()  # 真实读取PNG内容，而不只返回一个文件名。
    sample_hash = hashlib.sha256(raw).hexdigest()  # 图像证据是完整PNG的哈希。
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or sample_hash != entry["image_sha256"]:
        raise ValueError("图片格式或哈希不符")  # 拒绝缺失/损坏/被替换的图片。
    if entry["equipment_id"] != "E-QC" or entry["equipment_name"] != "Vision_01":
        raise ValueError("图片设备身份不符")  # 保证图像绑定视觉站。
    detected = entry["bolt_count"]  # 这是已有场景标签中的螺栓数。
    if type(detected) is not int or detected != (3 if is_anomaly else 4):
        raise ValueError("图片标签与合格/缺陷选择不一致")  # 按本素材库约定校验标签。
    payload = {
        "request_path": "/vision/inspect", "method": "POST", "image_path": str(path),
        "expected_bolts": 4, "detected_bolts": detected, "defect_detected": detected < 4,
        "defect_type": entry["defect_type"], "sample_id": entry["sample_id"],
        "manifest_path": str(manifest_path.resolve()),
        "result_source": entry["annotation_source"], "synthetic": entry["synthetic"],
    }
    return _envelope("HTTP-Vision-v1", work_order_id, operation_id, "E-QC", virtual_minute,
                     payload["defect_detected"], path, sample_hash, payload, run_id)


def build_robot_modbus_message(work_order_id, operation_id, virtual_minute, *, is_anomaly=False,
                               sample_index=0, slave_id=1, run_id=None, asset_root=ASSET_ROOT):
    """读取Robot指定行及寄存器值，返回Modbus-TCP-v1字典。"""
    if type(slave_id) is not int or not 1 <= slave_id <= 247:
        raise ValueError("slave_id必须为1—247的整数")  # 当前教学模式的单元编号范围。
    sample, path, sample_hash, reasons = _signal_sample("E-MOVE", is_anomaly, sample_index, asset_root)
    registers = sample["holding_registers"]  # 寄存器映射直接来自JSONL。
    expected = {"40001": int(round(sample["current_a"] * 1000)),
                "40002": int(round(sample["temperature_c"] * 10)),
                "40003": int(round(sample["load_percent"] * 10)), "40004": sample["alarm_code"]}
    if (registers != expected or any(type(value) is not int or not 0 <= value <= 65535
                                    for value in registers.values())):
        raise ValueError("寄存器与特征值不一致或超出uint16范围")  # 先检查缩放和边界。
    payload = {
        "slave_id": slave_id, "function_code": 3, "registers": dict(registers),
        "start_address": 0, "register_count": 4,  # 03功能码读取保持寄存器偏移0—3。
        "addressing": "40001..40004 reference labels map to wire offsets 0..3",
        "alarm_code": sample["alarm_code"], "anomaly_reasons": reasons,
        "sample_id": sample["sample_id"], "sample_index": sample_index, "synthetic": sample["synthetic"],
    }
    return _envelope("Modbus-TCP-v1", work_order_id, operation_id, "E-MOVE", virtual_minute,
                     bool(reasons), path, sample_hash, payload, run_id)


def main():
    """一行命令读取素材并打印正常/异常各四种JSON；业务编号由命令参数提供。"""
    parser = argparse.ArgumentParser(description=__doc__)  # 独立入口只依赖Python标准库。
    parser.add_argument("--work-order-id", required=True)  # 指定要演示关联的工单。
    parser.add_argument("--operations", nargs=3, required=True, metavar=("CNC_OP", "VISION_OP", "ROBOT_OP"))
    parser.add_argument("--quantity", type=int, default=2)  # MQTT命令中的计划数量。
    parser.add_argument("--virtual-minute", type=float, default=0)  # 自测时间由用户明确给定。
    parser.add_argument("--mode", choices=("normal", "anomaly", "both"), default="both")
    args = parser.parse_args()  # 命令行参数转成Python对象。
    modes = ("normal", "anomaly") if args.mode == "both" else (args.mode,)  # 默认两组都打印。
    cnc_op, vision_op, robot_op = args.operations  # 与三台设备的工序逐一对应。
    for mode in modes:
        anomaly = mode == "anomaly"  # CLI场景选择只决定读取哪个已落盘样本。
        messages = [
            build_mqtt_dispatch_message(args.work_order_id, cnc_op, "E-ASM", args.virtual_minute, args.quantity),
            build_cnc_opcua_message(args.work_order_id, cnc_op, args.virtual_minute, is_anomaly=anomaly),
            build_vision_http_message(args.work_order_id, vision_op, args.virtual_minute, is_anomaly=anomaly),
            build_robot_modbus_message(args.work_order_id, robot_op, args.virtual_minute, is_anomaly=anomaly),
        ]
        for message in messages:
            print(json.dumps({"mode": mode, "message": message}, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()  # 直接执行文件时才打印，import时不读取素材、不运行自测。
