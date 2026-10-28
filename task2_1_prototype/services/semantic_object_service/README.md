# WP3 语义对象化服务

唯一输入为 WP2 的八字段 RawIngestMessage；每次映射再次执行 Pydantic 与 JSON Schema 校验。输出与历史每项均为恰好七字段 SemanticObject。服务不读取 transport_records.jsonl 或 rejected_messages.jsonl，不建立关系或图。

## 使用方式

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.services.semantic_object_service.pipeline --input task2_1_prototype/artifacts/wp2_smoke_20261028 --output task2_1_prototype/artifacts/my_wp3_run --repeat 2
```

输出目录须不存在、不得与 WP1/WP2/旧项目重叠。产物：semantic_objects.sqlite3、semantic_objects.jsonl（唯一对象当前快照）、semantic_state_history.jsonl（逐消息、逐目标对象历史）、semantic_summary.json。默认处理一次；repeat=2再次投递全部消息，验收持久幂等。任何无效消息/身份冲突/信号矛盾使该批次事务回滚，不输出成功摘要。

标准函数：

```python
from task2_1_prototype.services.semantic_object_service import semantic_mapping, MappingEngine, SQLiteStore

single_object = semantic_mapping(raw_message)  # 独立单消息投影，无隐藏全局状态；自动关闭临时内存库
store = SQLiteStore("my_output/semantic_objects.sqlite3")
try:
    engine = MappingEngine(store)
    result = engine.semantic_mapping(raw_message)  # 流处理必须复用同一 engine/store
    results = engine.map_batch(raw_message_list)   # 单事务原子批次
    current = store.query(object_type="equipment", level="device")
    history = store.query(history=True, object_id="CNC_01", start="2026-10-28T02:31:00Z")
finally:
    store.close()
```

模块级 semantic_mapping 和有状态 MappingEngine.semantic_mapping 均接受 dict 或 RawIngestMessage、返回单个七字段 dict。图片会在同一事务内更新设备和所属工序，返回其主目标设备对象；副目标从对象库/历史查询。返回对象的 context.history 标明消息、时钟及是否进入当前展示快照；重复消息返回第一次处理时保存的同一结果，而不把日后状态冒充原响应。

FastAPI 使用 `create_app(database_path)`；也可传 `create_app(engine=engine)` 由调用方管理存储生命周期。WP5 可 `app.include_router(create_router(lambda: engine))`，无需另开服务。

- `POST /semantic/object`：接收单条 Raw JSON 或列表，返回七字段对象或对象列表；列表原子提交。普通源信封不是有效请求。
- `GET /semantic/objects`：object_id/object_type/domain/level/start/end 过滤当前对象。
- `GET /semantic/objects/{object_id}/history`：时间窗口（两端包含）、object_type/domain/level/scope 过滤历史；scope区分wall与具体simulation运行。
- 契约/上下文错误422；message_id、批次/身份或观测冲突409；基础设施错误不伪装为正常业务响应。

## 对象映射和时间

| 类型 | domain / level | 说明 |
|---|---|---|
| order | enterprise / enterprise | ERP REST创建记录与DB当前快照按墙钟合并，保留两种协议来源和timestamp_basis |
| work_order | manufacturing / factory | 墙钟计划态和仿真执行态分轨存储；仿真轨优先展示，墙钟态保留在context.clock_states |
| process | manufacturing / line | 使用resolved_ids.process_instance_key；持久绑定process_id→work_order，拒绝重绑。源hint为device时按契约归一line |
| equipment | manufacturing / device | 设备级信号窗口、视觉元数据、日志和故障锁存 |
| resource | 使用源合法domain/level | inventory标注material；组织节点标注organization，保留parent；不发明物料用量关系 |

context规范提供enterprise/factory/line/equipment/process/work_order/order；存在则保留process_instance_key、legacy_ids、run_id、simulation_epoch、sim_time_s、phase、sequence、source_sequence、batch_id和timestamp_basis。原raw_context与最后来源摘要也在context；完整Raw在processed_messages.raw_json，避免把所有消息ID堆入对象顶层。

时间统一为UTC；仿真必须满足epoch+sim_time_s。数据库绑定一个WP2 batch_id及一个仿真run_id/epoch，其他场景应使用新库，防止复用演示别名污染。墙钟和仿真时间**分轨**比较，绝不按绝对时间把真实创建墙钟当成比仿真完成更晚的工单执行态。context.clock_states显式保存各轨状态、时间和时间基准。

旧于同轨水位的事件只写历史，不修改当前对象。非信号同刻事件另比较该协议/模态source_sequence；信号同刻各协议/OPC单点允许任意先后融合。历史记录late=true表示迟到投影，以该事件时刻以前的历史为基底；它不是重算全部后续历史，也不会悄悄回滚当前状态。下一次较新观测的窗口可以包含已补入的旧采样。

## 物理采样与故障规则

采样键是设备+仿真scope+精确UTC时刻；不按整数秒截断，因此支持亚秒采样。另校验工单/工序/phase/rework一致，避免同设备同时刻被绑定到不同任务。2160条协议表示是360个物理周期；OPC四单点合并，MQTT与OPC全精度优先于PLC定点，窗口默认最近10个不同周期。未齐四通道时signal_complete=false，可用已到高值触发故障，但不允许用不完整观测恢复。

PLC允许与全精度值相差半个量化步长（rpm=1、温度/振动=.01、load=.0001，加1e-7浮点容差）；超过容差拒收并回滚该消息。同协议同刻同指标也不能互相矛盾。state保留最新指标、latest_signal、window_features均值/峰值/最新值/计数、sample_count及窗口大小；context.observed_protocols记录采样协议集合。不是对六条消息做六次物理计数或六次平均。

默认规则（可通过MappingEngine.rules配置，但同库重启必须保持规则一致）：

- 温度>90或振动>8或load>.95：fault；否则load>.82：overload；否则温度>72或振动>3.8：warning。
- 完整观测且rpm<=1、load<.05：idle；其余正常观测running。温度/振动/load的等于阈值属于未超阈值。
- fault/overload锁存并累计故障次数；普通图片、INFO、正常数值甚至结构化running字段均不能清除。
- 只有明确INFO恢复日志state_action=fault_cleared，且同刻或后续完整数值正常，才恢复running并累计recovery_count。恢复后保留fault_count、recovered_at、recovery_log_timestamp、last_fault_codes。
- ERROR日志直接构成故障证据，正则提取INFO/WARN/ERROR和所有Code 0x...；恢复日志同时保留0x00与历史0x84。旧时间恢复日志不影响当前新故障。
- 诊断数值白名单不读取injected_fault/control_cmd；这些原字段仅在输入溯源存档里存在。synthetic_label也不触发设备fault，仅记录visual_inspection；明确annotation_source=synthetic_label，不声称使用视觉模型。

## 存储及WP4边界

SQLite表：semantic_objects、semantic_state_history、processed_messages；另有semantic_tracks、signal_cycles、semantic_metadata用于分时钟、跨协议融合与重启状态。每条消息和图片双目标更新同事务，message_id+指纹防重复/冲突；同库线程锁与BEGIN IMMEDIATE保证写入一致。数据库只接受专用文件名，拒绝源批次目录及旧项目目录，不迁移其他数据库。

storage.py的SemanticStore端口预留其他后端，open_store(backend="POSTGRESQL")明确抛未实现；本轮没有PostgreSQL语义持久化或实网验收。不要将WP2的DB-API接入分支等同于WP3存储后端。

WP4应读取七字段当前对象/历史，使用context中明确的业务ID和组织parent建边；按scope/run/时间取状态，迟到投影不可直接覆盖实时图。设备fault不会在WP3自动把工序/工单/订单改为故障或延期；跨层影响是WP4。物料已对象化，但源消息未提供BOM/领料关系，uses_resource需明确配置证据，不能仅因物料和工序同批次存在就宣称实际领用。
