# WP2 多源数据接入服务

仅负责接入、协议解析、完整性校验、ID 上下文核对、幂等与诊断隔离。不产生 SemanticObject，不判断 fault，不建立关系或图。

## CLI 与输出

在项目根目录执行（Python 使用现有虚拟环境）：

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.services.connector_service.pipeline --input task2_1_prototype/artifacts/wp1_smoke_20261028 --output task2_1_prototype/artifacts/my_wp2_run --exercise-invalid --repeat 2
```

输入为 WP1 完整批次：config.json、id_registry.json、input.json、transport_records.jsonl、demo_erp.sqlite3、PLC 二进制文件及 PNG。输出目录必须不存在，与输入不重叠，且不得位于 minimal_factory 内。

- `raw_ingest_messages.jsonl`：每行严格八字段 RawIngestMessage，每条均经 Pydantic 与导出 JSON Schema（含 format 校验）验证。
- `rejected_messages.jsonl`：错误 code、reason、connector、源 sequence、限长载荷/帧摘要；JSON 语法错误另带行号 locator。
- `connector_summary.json`：按 Connector、协议、模态计数，原批次/重复回放/补充数据/诊断样例分别统计；asset_root 给出图片引用基目录。
- `organization_reference.sqlite3`：从已批准配置生成的三个组织节点参考表，由 DatabaseConnector 只读接入。没有改动 WP1 源数据库。

默认批次原始 2202 条全部接入，另增加 1 条当前数据库订单快照与 3 条组织资源，共 2206 条。原 inventory 载荷与真实只读 DB 行逐字段核对；当前订单快照单列，不覆盖 ERP 创建订单时的历史 REST 载荷。订单快照 timestamp 是采集墙钟，created_at 原样保留在载荷，snapshot_kind/timestamp_basis 明确区分；组织配置使用 simulation_epoch。

`--repeat 2` 将原源记录再次投递，预期 2202 个 duplicates。`--exercise-invalid` 显式注入 6 个坏帧（截断、CRC、未知地址、头、功能码、长度）和 1 个错误 PNG 哈希引用，预期 7 个 rejected；不改输入文件。默认不注入。源消息拒收时退出 2；完整接入退出 0；文件系统/连接等基础设施错误使运行失败，且不生成成功摘要。退出 0 仍须查看 diagnostic_fixtures，不能解释为没有故意注入的坏数据。

## Python 接口

```python
from pathlib import Path
from task2_1_prototype.services.connector_service import IngestSession
from task2_1_prototype.services.connector_service.pipeline import load_batch, connectors_for, ingest_stream

folder = Path("task2_1_prototype/artifacts/wp1_smoke_20261028")
config, bindings, batch_id = load_batch(folder)
session = IngestSession(batch_id, folder, bindings)
connectors = connectors_for(session, folder / "demo_erp.sqlite3")
# records 可为生成器、WP1 ProtocolEmitter 返回值的流或已读取的源信封。
for raw in ingest_stream(records, connectors, session):
    downstream_input = raw.model_dump(mode="json")  # 留给 WP3 的唯一标准消息边界
print(session.summary(), session.rejections)
```

四类接入器均接受 `connector.ingest(source_envelope)`，成功返回模型，重复或拒收返回 None；应结合 session 计数与 rejections 区分。

| 接入器 | 独立采集入口 | 运行与约束 |
|---|---|---|
| RestApiConnector | `fetch(client_or_callback, path, metadata)` | GET；接收已配置 TestClient/HTTP client 或回调；单条 JSON 业务对象；非 2xx 拒收。分页/列表应由调用者逐对象提供稳定序号/事件键 |
| DatabaseConnector | `read_rows(table, protocol)`、`snapshot(table, metadata_factory, protocol)` | SQLite URI mode=ro + query_only，只读 orders/inventory/organizations；拒绝旧系统目录和原库别名；每次查询关闭连接 |
| MqttConnector | `subscribe(broker)`、`drain()`、`receive(delivery)` | MemoryMqttBroker 的 +/# 订阅或已收队列；核对 topic/channel；重叠订阅不重复输出 |
| OpcUaAndPlcConnector | `read_node(store, node_id, metadata)`、`read_frame(bytes_or_hex, metadata)` | OPC 单点节点输出，校验质量、NodeId/设备/指标与时间；PLC 调用 WP1 解码器且核对二进制偏移和帧外调度 |

metadata 使用源信封字段（payload 由采集接口填充）；timestamp、sequence、context、object_hint 必填。DB 工厂须提供 channel/event_key，event_key 应包含轮询标识，避免把后续状态变化当作同一条源记录。OPC 读取要求 metadata.timestamp 与 SourceTimestamp 相符。PLC 帧不能提供 timestamp/工单号，必须显式传入登记/调度上下文。

POSTGRESQL 使用 `DatabaseConnector(session, connection_factory=...)` 和 `snapshot(..., protocol="POSTGRESQL")`。工厂须返回独立、专用、具有 `set_session(readonly=True, autocommit=False)`、cursor、rollback、close 的 DB-API 连接（如 psycopg2），不能返回复用的业务事务。当前测试用受控 DB-API 替身验证真正不同的分支、SQL、只读事务与异常关闭；未安装驱动、未连接实网服务器。部署适配器须将驱动返回值转换为契约允许的 JSON 值（日期 ISO 文本、Decimal 精度保留文本等），本层会拒绝非 JSON 类型；不会偷偷将 SQLite 标记为 PostgreSQL。

## WP3 必须保留的边界

1. RawIngestMessage 直接复用 contracts，无新增顶层字段。语义层只能在现有 state/context 内扩展，不改变冻结模型。
2. `raw_payload.source_payload` 完整保留源载荷。其旁为方便读取的业务字段：REST body、DB row、MQTT 原字段、OPC NodeId/Value/StatusCode/SourceTimestamp 与单指标、PLC 四信号与 control_cmd/heartbeat_seq/device_address/function_code/plc_raw_frame。
3. `raw_context` 原字段不变；`_ingest` 保存 batch_id、source_sequence、channel、source_timestamp、event_key、resolved_ids。仿真 context.sequence 与传输 source_sequence 是不同序号，不能相互覆盖。
4. resolved_ids 仅来自显式登记，不是语义推断。process_instance_key 是 `[canonical_work_order_id, canonical_process_id]`；不得脱离工单按工序序号合并。原 UUID 仍在 legacy_ids 或原 context 中。
5. timestamp 统一 UTC；源时间字符串、仿真 epoch/time/run_id 保留。按到达顺序输出，不自动重排乱序事件；同刻消息不能因时间相同被删除。跨仿真和墙钟数据先按 clock/timestamp_basis 隔离，再由下游明确时间策略。
6. message_id 由 batch_id/source/protocol/channel/event_key（无则 sequence）确定；同身份相同内容仅输出一次，内容改变则 IDENTITY_CONFLICT。去重字典线程安全但仅限本 session；跨进程持久化需下游建立 message_id 唯一约束。重新采集的 DB 快照使用新采集时间的 event_key，属于新的源消息。
7. PNG 引用相对 asset_root；拒绝绝对路径、父目录、驱动器/UNC/流路径及符号链接越界；校验存在、SHA-256、Pillow verify/load。移动产物须一起移动源图片或显式重设可信基目录。
8. 图片 synthetic_label/annotation_source 是生成标签，非模型识别。源 injected_fault/control 不能替代 WP3 的数值/日志判据。
9. 日志原行、故障码与 state_action 原样保留。WP3 才能判定故障并融合状态；普通 INFO/图片不能隐式清除故障，明确 fault_cleared 才可作为恢复依据。
10. rejected_messages 永不传给 WP3。基础设施失败应终止/重试；有效的故障信号仍是合法正常接入消息。没有外部 Broker、OPC Server、HTTP 监听、CDC/持久队列或事务性断点续传承诺。
