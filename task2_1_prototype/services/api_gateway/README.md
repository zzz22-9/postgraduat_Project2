# WP5 本地统一网关与演示

本轮完成阶段1的纯Python本地功能与接口集成。没有Dockerfile或docker-compose。三大冻结契约、旧ERP/MES/SimPy与WP1—WP4源码不变。用户清单实际列出10条标准路由，全部实现。

## 一步运行

在项目根目录执行（output必须是新目录）：

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 run_e2e_demo.py --output task2_1_prototype/artifacts/my_wp5_run
```

从`task2_1_prototype`子目录也可执行`..\minimal_factory\.venv\Scripts\python.exe -B -X utf8 run_e2e_demo.py --output task2_1_prototype/artifacts/my_other_run`；相对output统一以项目根目录解释。模块入口为`python -m task2_1_prototype.run_e2e_demo`。

默认执行完退出。输出目录内的`e2e_dashboard.html`可直接双击打开：嵌入全部PNG、JSON、CSS和JavaScript，不依赖CDN/网络；可切换59/75/95/360秒、查看七字段原始JSON和图片。`weekly_5q_report.md`是可汇报摘要，`e2e_summary.json`保留计数/HTTP验收证据。

新运行并启动本机服务：

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 run_e2e_demo.py --output task2_1_prototype/artifacts/my_wp5_live --serve --host 127.0.0.1 --port 8010
```

浏览器打开`http://127.0.0.1:8010/`，接口文档为`/docs`，Ctrl+C关闭服务。已有产物不需要重建订单，可直接恢复网关：

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.services.api_gateway.main --workspace task2_1_prototype/artifacts/my_wp5_run/gateway --host 127.0.0.1 --port 8010
```

在线按钮使用`/demo/api/v1/events/trigger`的独立回放分支；主会话保留完整360秒结果。每次操作把分支当前时间增加5秒，只有设备在活动工序窗口内才接受异常/恢复，超过窗口返回诊断。不要在已完工主会话向过去75秒注入新事件。

## 十条标准路由

| 类别 | 方法与路径 | 输入或查询 |
|---|---|---|
| Object | POST /api/v1/objects/ingest | 单个或数组SourceEnvelope/RawIngestMessage |
| Object | GET /api/v1/objects | object_id、object_type、domain、level、start、end |
| State | GET /api/v1/states/{object_id} | 返回最新七字段对象；不存在404 |
| State | GET /api/v1/states/{object_id}/history | start、end、scope |
| Relation | GET /api/v1/relations | source、target、relation、active、as_of、clock |
| Relation | POST /api/v1/relations/infer | `{as_of,clock}`，或`{objects,context}`；纯推理 |
| Model | GET /api/v1/model/graph | as_of、clock；七字段nodes/edges及统计、风险附加视图 |
| Model | GET /api/v1/model/impact/{object_id} | as_of、clock、direction；活跃风险/反向依赖/impact_events |
| Model | GET /api/v1/model/snapshots | 已保存的快照列表 |
| Event | POST /api/v1/events/trigger | event_id、kind、object_id、sim_time_s，可带signals/fault_code/status |

兼容挂载WP3的`/semantic/object`、`/semantic/objects`、`/semantic/objects/{object_id}/history`及WP4的`/relations`、`/relations/infer`、`/model/graph`、`/model/impact/{object_id}`、`/model/snapshots`。兼容写入也经过WP2→WP3→WP4，不绕过网关协调层。

Object/State列表的start/end是带时区ISO-8601，history的scope使用WP3原始scope字符串。Model/Relation默认clock=simulation，as_of是epoch后的秒；clock=acquisition_wall_clock时使用ISO时间。未知impact对象404，非法载荷/CRC/哈希/上下文/时间等422，全部正常对象/关系保持7字段。

独立分支的触发示例：

```json
{"event_id":"manual-fault-001","kind":"fault","object_id":"CNC_01","sim_time_s":85}
```

恢复用新event_id、`kind="fault_cleared"`和更晚时间；默认生成正常四路值与INFO恢复日志。也可显式传入四个signals键；若恢复时数值仍异常，WP3故障继续锁存。业务更新用`kind="business_update"`、order/work_order对象及status；订单沿WP3规则写采集墙钟轨，工单写仿真轨。event_id用于持久幂等，同ID异内容拒绝。

Event响应含`objects`（唯一更新对象的7字段最新快照）、`relations`（当前完整7字段关系列表，含更新关联）、`impact`、实际`source_records`、各层计数。不会将风险值写入真实业务status。返回的impact_events按as_of限制，不提前透露未来恢复时间。

## 生命周期与一致性

```python
from task2_1_prototype.services.api_gateway import create_gateway_app
app = create_gateway_app(
    "new_session",
    source_dir="new_run/wp1",
    catalog_file="new_run/wp3/semantic_objects.jsonl",
    bootstrap_raw_file="new_run/wp2/raw_ingest_messages.jsonl",  # 可选
    bootstrap_until=59,  # 可选：仅回放到指定仿真秒，墙钟/组织记录保留
)
```

FastAPI lifespan初始化并持有同一IngestSession、MappingEngine(SQLiteStore)、GraphManager(SQLiteGraphStore)，关闭时释放连接。`gateway_config.json`固定会话输入与规则；重新打开用`create_gateway_app("new_session")`。输出拒绝进入旧系统或WP1—WP4产物目录。配置源资产只读，语义/图分别存于会话内semantic/graph子目录。

Raw输入不仅验证8字段模型，还必须保留WP2的source_payload和_ingest来源信息。网关重建SourceEnvelope，经原WP2 Connector重新检查地址、CRC、PNG路径/哈希、工单作用域，再比对重建Raw的全部字段，拒绝伪造的message_id、decoded值或resolved_ids。合法消息调用WP3映射，按新增history_id将所有目标（含图片双目标）交给WP4.apply_event。

批量上限5000，本地单进程用锁串行协调；请求前为两个SQLite创建内存检查点，失败时恢复两库、接入身份和内存图。重启根据processed_messages重建接入身份，并从语义历史对图幂等补齐。这是阶段1本地设计，未提供分布式事务、多个Uvicorn worker写同一会话或任意进程崩溃下的严格跨库原子性保证。

图先注册WP3完整身份目录，再接收动态历史；早期缺少动态观测的节点仍按WP4策略标not_started/reference_only。新对象/新批次/新规则用新会话，不擅自扩张已有图身份。关系纯推理接口不保存边或快照。

## 端到端与验收边界

A调用真实WP1创建ERP/MES订单并仿真；B调用WP2完整接入、坏样本与重复；C调用WP3映射；D调用WP4图/快照/离线Cypher；E用ASGI实际请求重新经过网关链路并验证独立事件分支；F从实际结果生成JSON、五问Markdown和HTML。既有模块manifest的历史“下一层未实现”scope文字原样保留，最终集成状态以顶层e2e_summary为准。

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s task2_1_prototype/tests -p 'test_wp5*.py' -v
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.tests.verify_wp5 --output task2_1_prototype/artifacts/my_wp5_acceptance --evidence-prefix md/agent_reports/evidence/my_wp5_acceptance
```

验收器执行31项WP5与既有158项测试、根目录真实CLI，独立复核Pydantic/JSON Schema、HTML内嵌数据与九张PNG、原ERP和WP1—WP4共40个验收文件的历史哈希。MQTT/OPC为本地模拟、Neo4j为离线导出，Docker明确暂缓，不宣称已完成实网工业协议或生产部署验收。
