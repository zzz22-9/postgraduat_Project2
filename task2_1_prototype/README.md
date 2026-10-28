# 任务2.1原型：共享契约、WP1—WP3

2026-10-28：本目录完成共享契约、多模态/多协议数据源、WP2多源接入与WP3语义对象化。WP4关系/图服务、WP5统一网关尚未实现。接入说明见[Connector](services/connector_service/README.md)；七字段映射、同刻信号融合、故障恢复、SQLite历史与FastAPI见[Semantic Object Service](services/semantic_object_service/README.md)。

## 从项目根目录运行

已有虚拟环境使用Python3.12。安装依赖（不要把`.venv`提交Git）：

```powershell
uv pip install --python '.\minimal_factory\.venv\Scripts\python.exe' -r task2_1_prototype/requirements-dev.txt
```

如果使用带pip的其他虚拟环境，可执行`python -m pip install -r task2_1_prototype/requirements-dev.txt`。

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m task2_1_prototype.simulators.simpy_factory --output task2_1_prototype/artifacts/my_wp1_run
```

输出目录必须不存在，防止覆盖旧实验。`--normal`关闭故障；`--config path.json`指定其他配置。运行无需外部数据库、MQTT Broker或OPC UA服务器，不占用HTTP监听端口。

默认单订单：复用原ERP POST /orders处理器 → MES dispatch/assign/release → source_snapshot → 原SimPy引擎。总时长6分钟，360条四路信号、9张960×600 PNG、8条日志，OP20第60—89秒注入温升/振动异常，第90秒明确恢复。

旧E-ASM在本次**演示映射**中对应CNC_01，旧设备类型仍为assembly；这是数值/视觉教学仿真，不是实机CNC物理模型。原订单和工单UUID不更改，独立映射表关联到ORDER_A/WO023/OP20。第二工单使用不同工序实例标识。旧练习库不写入。

## 文件与WP2读取边界

| 产物 | 内容 |
|---|---|
| config.json / input.json / id_registry.json | 本轮配置、真实MES工单快照、原始ID到演示ID的映射 |
| demo_erp.sqlite3 | 独立ERP/MES源库，orders/work_orders/operations/equipment/inventory表 |
| mes.db | 原引擎创建的仿真副本，完成反馈只写此副本 |
| events.jsonl / summary.json | 原引擎事件和结果，保留真实recorded_at |
| signals.jsonl | 四路数值、仿真时间、[start,end)上下文、合成注入真值 |
| sim_images/*.png / images.jsonl | Pillow实时绘图、内嵌PNG文本元数据、相对引用和SHA-256 |
| equipment.log / device_logs.jsonl | 原始设备行日志及上下文索引；明确fault/fault_cleared事件 |
| transport_records.jsonl | WP1源协议信封：sequence/source_system/protocol/modality/channel/timestamp/payload/context/object_hint |
| mqtt_received.jsonl | 内存订阅者实际收到的消息；不是仅打印发布意图 |
| plc_frames.bin / plc_frame_examples.json | 顺序18字节二进制帧及正常/截断/CRC错/未知地址样例 |
| run.json | completed/failed状态、计数与各协议运行模式 |

`REST_API`通过FastAPI TestClient在进程内调用真实ASGI路由；`SQLITE`是真实独立表；MQTT为内存发布/订阅Mock，OPC UA为命名节点存储/读取Mock，PLC为自定义二进制帧。不是实网协议验收。所有图像/信号是合成数据，图像标签不是视觉识别结果。

WP1 的 transport_records.jsonl 保持源信封格式；已实现的 WP2 Connector 将其解析为 raw_ingest_messages.jsonl 中的八字段 RawIngestMessage，并保留完整原始载荷。PLC帧不含时间与工单；接入器结合地址注册、采集时间和调度上下文核对后封装。详情见[接入说明](services/connector_service/README.md)、[契约](contracts/README.md)和[PLC字节约定](contracts/plc_frame.md)。

## 时间、复现与兼容

- 新标准时间为配置epoch加仿真秒，输出UTC ISO-8601字符串；context保留simulation_epoch、sim_time_s、sequence、run_id、legacy_ids。
- 仅在取得资源后的活动区间采样，不在排队或空闲时误绑工单。默认1秒；只给开始/加工中/质量/报警等节点出图，不伪造连续相机视频。
- 每工序/阶段使用独立`random.Random`；相同输入快照、映射、seed、epoch、run_id和相同Pillow版本重放时，新增信号、日志、PLC字节和PNG可逐字节复现。重新调用ERP创建订单会产生新的UUID和墙钟时间，不把这种新业务运行称为所有文件完全相同。
- 业务events.jsonl中的recorded_at是墙钟，天然不纳入字节复现。协议事件投影用仿真时间，原墙钟仍可从原事件追溯。
- 工序结束的Timeout先于采样任务创建；采样末次等待直接复用该完成事件，不创建结束后的Timeout。注意SimPy Timeout.triggered在构造时即为True，实际完成判断使用processed与时间边界。
- 观察器是观测扩展，不改变MES的业务状态机；故障信号不会实际阻塞排程。WP3现已提取并融合故障/恢复状态；跨层影响推理仍待WP4实现。

## 验收命令

```powershell
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s task2_1_prototype/tests -p 'test_wp3*.py' -v
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s task2_1_prototype/tests -p 'test_wp2*.py' -v
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s task2_1_prototype/tests -p 'test_wp1*.py' -v

# 旧系统采用顶层erp/mes导入。从根目录执行旧测试时需指定旧包路径。
$env:PYTHONPATH = (Resolve-Path '.\minimal_factory').Path
& '.\minimal_factory\.venv\Scripts\python.exe' -B -X utf8 -m unittest discover -s minimal_factory/tests -v
```

WP2 交付时本轮重新执行29项WP2、24项WP1测试和44项旧测试，全部通过，日志在`../md/agent_reports/evidence/2026-10-28_wp2_*`。一键生成测试与批次验收证据可运行 `python -m task2_1_prototype.tests.verify_wp2 --input <WP1目录> --output <新WP2目录> --evidence-prefix <证据文件前缀>`。旧事件金样本来自改造前提交d53fb7b：默认36事件、返修48事件。只归一化run_id、recorded_at和随机返修ID，不忽略事件序号、时刻、设备、工序、数量或先后顺序。

JSON Schema通过`python -m task2_1_prototype.contracts.export_schema`导出；三模型顶层字段严格固定，嵌套JSON禁止NaN/Inf。组织节点模型保持5类枚举。NetworkX仅完成环境准备，本轮没有建图服务。
