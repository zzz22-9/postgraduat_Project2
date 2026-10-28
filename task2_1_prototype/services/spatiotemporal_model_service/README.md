# WP4 四层时空模型服务

使用 NetworkX `MultiDiGraph` 与独立 `spatiotemporal_model.sqlite3`，读取 WP3 七字段对象/历史，融合设备—工序—工单—订单与设备—产线—工厂—企业两条链。风险作为图响应的 `risk_overlays`，不改写 SemanticObject 的业务状态。

## 批次与验收

从项目根目录执行，输出目录必须不存在：

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.services.spatiotemporal_model_service.pipeline --input task2_1_prototype/artifacts/wp3_smoke_20261028 --output task2_1_prototype/artifacts/my_wp4_run --repeat 2
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s task2_1_prototype/tests -p 'test_wp4*.py' -v
```

默认规则文件是 `config/relation_rules.json`，可通过 `--rules` 替换；`--repeat 2` 验证事件重放幂等。输入只读 WP3 的 semantic_objects.jsonl、semantic_state_history.jsonl 与 semantic_summary.json。目录保护拒绝覆盖/嵌套到 WP1/WP2/WP3 或旧系统目录。

| 产物 | 用途 |
|---|---|
| relations.jsonl | 当前最终时刻的关系列表，完成的执行边保留但不活动 |
| graph_snapshots.json | 59、75、95、360 秒完整七字段节点/边与风险视图 |
| impact_report.json | 四时刻故障传播、订单/工单反查、已解除风险审计 |
| spatiotemporal_summary.json | 对象/层级/边统计、幂等、模式与数据库计数 |
| spatiotemporal_model.sqlite3 | 身份目录、事件、关系、快照、风险审计 |
| neo4j_export.json | 参数化 Cypher 与节点/边参数，离线导出 |

`python -m task2_1_prototype.tests.verify_wp4 --wp1 <WP1目录> --wp2 <WP2目录> --input <WP3目录> --output <新WP4目录> --evidence-prefix <证据前缀>` 执行全部测试、真实批次、独立双契约/API验证及历史哈希比对。

## Python 接口与增量更新

```python
from task2_1_prototype.services.spatiotemporal_model_service import GraphManager, SQLiteGraphStore

store = SQLiteGraphStore("my_output/spatiotemporal_model.sqlite3")
manager = GraphManager(objects, store=store, rules=rules)
manager.replay(semantic_history)      # 按 WP3 历史到达顺序处理
manager.apply_event(semantic_event)  # 新事件 True，重复事件 False
graph = manager.snapshot(75, save=True)
risk = manager.impact("CNC_01", 75)
dependencies = manager.impact("ORDER_A", 75)
store.close()
```

重开：`GraphManager(store=SQLiteGraphStore(path))` 从持久身份目录/配置/事件重建。每个库固定一个批次和仿真 epoch/scope。先注册完整身份目录，再流式更新节点；新增未知身份、改变类型/域/层级、跨批次、工序重绑均拒绝。`GraphStore` 是后端端口，SQLite 已实现事务、持久幂等、回滚后一致重建。调用者负责在应用生命周期末尾关闭 store。

## 时间与风险语义

- `clock="simulation"`（默认）：`as_of` 为 epoch 后的非负有限秒，可用小数。默认查询最新仿真秒。工序执行区间从真实状态转换重建，不猜工序顺序。本批次 OP20 `[0,180)`、OP30 `[180,300)`、OP40 `[300,360)`。
- `clock="acquisition_wall_clock"`：`as_of` 使用带时区 ISO-8601，独立查询采集状态。WO023 的墙钟 released 不覆盖仿真 completed。
- 历史带 late 或比同对象同轨最新时刻更旧的事件仅记录审计，不倒退当前图；本版也不把这些迟到记录回填到过去快照，不声称重算未来因果。
- 身份目录可提供未开始节点的拓扑，缺失该时刻动态证据时剥离未来状态，标注 `context.model_projection`。设备/工序/工单为 not_started，组织为 configured；仅墙钟可用的订单/物料在仿真视图为 reference_only，真实状态在墙钟视图查询。拓扑是事后完整身份目录，不代表提前获知所有动态事件。
- 上溯仅从 fault/overload 设备沿 active 的 executes→belongs_to→maps_to 边传播；每路径带已访问集合，环路停止并诊断。超过 10000 次路径扩展拒绝继续。
- `blocked_risk/delay_risk/delivery_risk` 是诊断推演，不修改工序 running、工单 running 等事实；`mes_schedule_changed=false`。同一路径/故障起点只形成一个风险事件，恢复或关联结束时关闭。
- 反查 ORDER_A/WO023 会返回工序、设备、显式配置物料和组织节点，保留非活动历史分配，便于完工后追溯。

本演示 59/75/95/360 秒活跃风险数分别为 0/1/0/0；75 秒路径为 CNC_01→OP20→WO023→ORDER_A，故障码 0x84，审计区间 `[60,90)`。工序/工单在 360 秒为 completed，不把新设备故障永久挂到已完成订单。

## 路由与 Neo4j

`api.create_router(lambda: manager)` 返回 `/model` 路由，可供 WP5 挂载：

- `GET /model/graph?as_of=75`：节点、边、层级统计、风险附加视图。
- `GET /model/impact/CNC_01?as_of=75&direction=auto`：equipment 自动向上；order/work_order 自动向下；也支持 upstream/downstream。
- `GET /model/snapshots`：已保存的快照 ID、revision、clock、as_of。

以上时刻查询都支持 clock 参数，错误时间返回 422，未知对象返回 404。`api.create_app(manager)` 是挂载关系/模型路由的测试便利应用；本轮没有实现 WP5 统一网关，也未启动常驻 HTTP 服务。

`neo4j_export.export_neo4j(snapshot)` 仅生成参数化 UNWIND/MERGE。节点/关系标识都是参数，关系标签来自固定白名单，嵌套 state/context 编码为 JSON 字符串属性。输出明确 `mode="offline_parameterized_export"`、`neo4j_connected=false`；没有联网 Neo4j 验收。跨批次导入真实 Neo4j 前仍需由后续适配器添加命名空间策略。
