# 共享契约 V0.1（2026-10-28 用户批准）

`models.py` 是唯一模型定义，`schemas/*.schema.json` 是JSON Schema Draft 2020-12导出物。
运行 `python -m task2_1_prototype.contracts.export_schema` 可重新导出。

- RawIngestMessage固定8字段；SemanticObject恰好7字段；RelationEdge固定7字段。所有字段必填，模型启用strict与extra=forbid。
- 嵌套payload/context允许JSON扩展字段，但不接受NaN/Inf、非字符串键、bytes、tuple等非JSON值；不能将来源信息添加成第8个语义对象顶层字段。
- 时间是带时区ISO-8601字符串，不接受纯日期、无时区或不可能日期。标准序列化用UTC Z，保留微秒；配置epoch可带+08:00。
- protocol枚举：OPC_UA / MQTT / REST_API / POSTGRESQL / SQLITE / PLC_FRAME。SQLITE是本次用户明确批准的本地后端补充值，不标成PostgreSQL。
- object_type固定equipment / process / work_order / order / resource；组织节点使用resource，context.resource_kind=organization，level区分enterprise/factory/line。
- source_system为ERP / MES / SIMPY_PLC；四种modality为structured_record / timeseries_signal / image / log_text。
- 关系类型固定executes / belongs_to / maps_to / uses_resource / located_in / part_of / managed_by；weight为[0,1]有限数值。
- JSON文本必须由标准JSON解析器拒绝NaN/Infinity；Python入口额外递归拒绝非有限数。JSON Schema的date-time需启用FormatChecker，不能只检查pattern。
- 时间来自simulation_epoch + sim_time_min*60秒；raw_context/context保留clock=simulation、sim_time_s、sequence、run_id和原始业务ID。
- 后续WP3须逐字段合并多模态并保存来源；普通图片/INFO不得解除fault，必须有明确恢复证据。本轮只冻结约束，不实现语义映射服务。

WP1的transport_records.jsonl是协议源消息，不是已完成Connector的输出；契约示例仅用于验证接口约定。
