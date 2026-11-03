"""第一步的第一个小改动：登记目标设备与原业务资源的对应关系。

这里只建立业务骨架绑定，不表示已经建成 CNC、视觉或机器人设备模型。
当前独立验证；尚未接入主仿真器，需用户理解并确认后再继续。
"""

# 外层字典的键是用户指定的目标设备编号；内层字典描述它复用哪个原业务位置。
# assembly 仍是原“组装”工序，不能仅换一个 CNC 名称就声称已有机加工工艺。
DEVICE_BINDINGS = {
    "CNC_01": {
        "legacy_equipment_id": "E-ASM",
        "operation_kind": "assembly",
        "operation_name": "组装",
    },
    "Vision_01": {
        "legacy_equipment_id": "E-QC",
        "operation_kind": "inspection",
        "operation_name": "过程质检",
    },
    "Robot_01": {
        "legacy_equipment_id": "E-MOVE",
        "operation_kind": "transport",
        "operation_name": "搬运",
    },
}


def get_device_binding(device_id: str) -> dict:
    """【输入 Input】目标设备编号，例如 "Vision_01"。
    【Python 语法与转换逻辑】用字典按编号查找；未知编号报错，防止数据挂错设备。
    【输出 Output】设备、原资源和工序组成的新字典；只返回内存数据，不写数据库。
    """
    if device_id not in DEVICE_BINDINGS:  # not in：检查这个编号是否没有登记。
        raise ValueError(f"未登记的目标设备：{device_id}")

    binding = DEVICE_BINDINGS[device_id]  # 用键查字典，例如查到 E-QC 与过程质检。
    # **binding 把内层键值展开到一个新字典；调用者修改返回值不会改掉登记表。
    return {"device_id": device_id, **binding}
