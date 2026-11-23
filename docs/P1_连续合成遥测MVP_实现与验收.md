# P1 最小连续合成遥测：实现说明与验收边界（2026-11-23）

## 1. 范围与实际完成

- 在最小工厂已有 SimPy 加工过程内，按 **1 个虚拟秒**产生 CNC/Robot 派生时序记录。该间隔当前在 `simulation/continuous.py` 中固定，**并非可配置**；加工起始和结束点也保留，时长6分钟时共361个端点采样。
- 新增每批独立 `telemetry.jsonl`，不覆盖现有 `protocol_messages.jsonl`、`source_context.jsonl`、`events.jsonl` 或 `mes.db`。
- 新增分页读取 `GET /simulation/runs/{run_id}/telemetry?offset=0&limit=500`；单次 `limit` 1..1000，返回 `{run_id,available,total,offset,limit,items,next_offset}`。历史批次无 `telemetry.jsonl` 时返回 `available=false`，不伪造采样。
- Vision仍为离散PNG/检测结果；原MQTT/OPC UA/Modbus仅本地帧，不包含实际Broker/Server/Client；未开发 Data Connector。
- 现有工序末协议消息仍按原方式生成，新增遥测不会改变 MES 排产、故障延长和返修复检控制逻辑。

## 2. 数据含义：严格区分来源与生成方式

- `generation_method=deterministic_template_variation_v1`：在校验哈希的已有正常合成素材样本基础上，通过固定数学扰动构造**派生数值**，不是新增真实传感器观察，也不是经过物理模型校准的动态过程。
- `generation_method=existing_terminal_protocol_sample`：工序结束点直接使用既有正常/异常协议消息的数值，并通过 `terminal_message_id` 关联该消息。
- `synthetic=true`、`transport_mode=local_derived` 必填。`protocol` 表示载荷的OPC UA/Modbus风格，**不表示网络传输已经发生**。
- `sample_hash` 指向模板合成素材的SHA256，并非每个派生值自身的SHA256；要解释完整来源需结合 `generation_method` 和实际模板/终态消息。
- `virtual_minute` 是SimPy时间，`elapsed_s` 是当前加工阶段内经过的虚拟秒，`source_sample_time_s`/`source_time_s` 是素材内原有时间字段，不能混成现实时间。
- `sample_sequence` 在每个工序执行阶段（包括返修/复检）的CNC/Robot独立从0递增。消息身份含批次/工单/工序/阶段/返修号/序号；若同一工序阶段被重新执行，需要新增独立阶段执行ID才能确保跨重复执行绝对不冲突。
- 工序完成时如报告故障，最后一个终态样本才有 `is_anomaly=true`；**尚未实现持续故障窗口的高保真动态模拟**，不能把当前时序数据宣称为物理一致的故障振荡数据。

## 3. 2.1 Connector 所需示例读取流程

1. 用 `GET /simulation/runs` 发现批次，确认目标批次存在。
2. `GET /simulation/runs/{run_id}/telemetry?offset=0&limit=500` 读取第一页。
3. 按 `next_offset` 继续读取，直到返回 `null`。
4. 以 `run_id`、`work_order_id`、`operation_id`、`equipment_id`、`phase`、`sample_sequence` 确定上下文；保留完整原始遥测记录，切勿只留测量值。
5. 与 `GET /simulation/runs/{run_id}` 中的原始工序末协议消息按 `terminal_message_id` 关联；不可将其视为实时设备OPC UA/Modbus订阅。

## 4. 测试与限制

新增 `tests/test_continuous_telemetry.py`：正常6条既有协议消息/12虚拟分钟、三场景时间与返工、1 Hz 时序、元数据、Modbus字节一致性、HTTP分页与旧批次兼容等断言。隔离Python工作副本执行过3项专项测试（通过）；基于2026-11-07压缩包构造的独立副本也执行过原9项现场测试（通过），但**尚未在包含P0.4全部93项测试的远端新分支或用户Windows工作树执行完整回归**。完整验收需在用户项目新建隔离副本及虚拟环境执行：
```cmd
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_continuous_telemetry.py -v
.venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
```
注意 Windows 环境特定测试在Linux副本中不能直接作为正式完整回归；不得写入已有正式 `data/erp.db` 或旧仿真批次。

## 5. 下一步

不要继续扩展这套源头代码以推迟2.1。先从独立的REST Connector读这一页真实HTTP遥测，再定义内部统一消息V0.1、写入隔离存储和测试。真实MQTT/OPC UA/数据库连接源、连续采样可配置、故障窗口语义及高保真采样后续单独验收。
