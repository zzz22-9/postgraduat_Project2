# 任务2.1 制造-企业跨域业务及资源深度感知：本周开发任务拆解与多Agent协同规范 (V0.1)

## 1. 项目背景与本周核心目标

### 1.1 课题背景与软件定位
本任务隶属于国家重点研发计划“制造企业业务集成和协同端对端工业互联技术”课题2（制造-企业业务集成的工业互联关键技术）中的 **任务2.1：制造-企业跨域业务及资源的深度感知**，同时为 **任务2.2：制造-企业跨域互联互通互操作** 提供多协议接入底座。

软件原型的核心目标是将 **“多源异构数据获取 $\rightarrow$ 语义对象化 $\rightarrow$ 动态关联分析 $\rightarrow$ 多层级时空建模 $\rightarrow$ 统一API服务”** 构建为一套可独立运行、可通过标准API调用、后续可平滑容器化（Docker/K8s）的跨域感知与建模服务底座。

### 1.2 现有系统基础（已搭建完成）
目前已完成三个基础系统的初步搭建与串联：
* **ERP 系统（企业经营层）**：支持输入订单编号、订单名称、需求量、交期等字段，创建订单并下发。
* **MES 系统（制造执行层）**：接收 ERP 下发的订单，生成对应工单（WorkOrder），并完成制造资源分配。
* **SimPy 生产仿真系统（制造现场层）**：接收 MES 工单并执行生产过程离散事件仿真，输出事件 ID、工序（Process）、仿真时间、事件状态（如工序完成）以及对应的工单编号与订单编号。

### 1.3 导师评审反馈与亟待解决的痛点
根据本周导师讨论录音及《任务2.1_制造企业跨域深度感知_软件原型整体设计》要求，当前系统存在以下三个核心短板，必须在**本周第一版（V0.1）**中解决：
1. **数据模态单一，缺乏异构性**：当前三个系统产生的数据全部为结构化文本/表格，且订单、工单、工序 ID 天然一一对应，无法体现“多模态异构数据归一化表征”的必要性。导师明确要求在 SimPy 生产过程仿真中针对具体机器和加工小过程，补充**机器传感器连续信号（时序数据）**、**现场加工状态图片（视觉数据）**和**设备运行/报警日志（非结构化文本）**。
2. **缺乏跨系统通信协议模拟**：当前 ERP、MES、SimPy 之间直接传递文本数据，未体现不同层级工业系统的通信协议差异（如上层 REST API / Database / MQTT、中下层 OPC UA 以及底层 PLC 私有帧协议），需引入轻量级工业协议模拟与多源接入转换。
3. **尚未落地任务2.1标准五层软件架构**：需要将现有脚本升级为符合《软件原型整体设计》的五个核心服务模块（`Data Connector` $\rightarrow$ `Semantic Object` $\rightarrow$ `Relation Service` $\rightarrow$ `Spatiotemporal Model` $\rightarrow$ `API Gateway`），并用简化版规则/图算法把第一版从头到尾跑通。

---

## 2. 整体软件架构与全局数据契约（全体Agent必须严格遵守）

所有协同 Agent 在开发各自模块时，必须严格遵守以下数据流向与 JSON 数据契约，禁止私自修改字段命名。

### 2.1 端到端软件架构流水线

| 架构层级 | 模块名称 | 核心职责 | 输入数据 | 输出数据 |
| :--- | :--- | :--- | :--- | :--- |
| **数据源层** | **ERP / MES / SimPy 多模态多协议仿真器** | 产生订单、工单、工序事件，以及机器信号、现场图片、运行日志，并通过异构协议发送 | 仿真配置与业务触发指令 | REST / DB / MQTT / OPC UA / 模拟PLC帧数据流 |
| **① 接入层** | **`Data Connector` 多源数据接入服务** | 适配四类以上异构数据源与协议，将工业侧和企业侧数据统一转换为内部原始消息格式 | 异构协议数据流与多模态载荷 | 内部标准原始消息 `RawIngestMessage` |
| **② 对象化层** | **`Semantic Object` 语义对象化服务** | 将 PLC 变量、传感器信号、图像/日志、MES 工单、ERP 订单转换为包含 7 个标准字段的统一业务对象 | `RawIngestMessage` | 统一语义对象 `SemanticObject`（5类核心对象） |
| **③ 关联层** | **`Relation Service` 动态关联分析服务** | 基于语义对象与上下文，生成并更新 `Order -> WorkOrder -> Process -> Equipment` 关联链及故障传播边 | `SemanticObject` 集合与上下文 | 标准关系三元组 `RelationEdge` |
| **④ 模型层** | **`Spatiotemporal Model` 多层级时空模型服务** | 构建 `Enterprise -> Factory -> Line -> Device` 四层动态时空图，支持事件驱动增量更新与影响链查询 | `SemanticObject` + `RelationEdge` | 多层级动态图快照与跨层影响分析结果 |
| **⑤ 接口层** | **`API Gateway` 统一服务接口** | 对外统一暴露 Object / State / Relation / Model / Event 五类标准 REST API | 外部或下游任务（2.2-2.5）HTTP请求 | 标准 JSON 响应 |

### 2.2 契约一：`Data Connector` 输出的内部统一原始消息格式 (`RawIngestMessage`)
`Data Connector` 接收到任何协议、任何模态的数据后，必须统一封装为如下 JSON 结构传递给 `Semantic Object Service`：

```json
{
  "message_id": "MSG_20261028_0001",
  "source_system": "SIMPY_PLC | MES | ERP",
  "protocol": "OPC_UA | MQTT | REST_API | POSTGRESQL | PLC_FRAME",
  "modality": "timeseries_signal | image | log_text | structured_record",
  "timestamp": "2026-10-28T10:30:00.125Z",
  "object_hint": {
    "raw_id": "DEV_CNC_01",
    "suggested_type": "equipment",
    "domain_hint": "manufacturing",
    "level_hint": "device"
  },
  "raw_payload": {
    "spindle_speed_rpm": 4200,
    "temperature_c": 78.5,
    "vibration_mm_s": 4.8,
    "load_ratio": 0.88,
    "plc_raw_frame": "01 01 03 E8 00 4E 01 E0 AA 55",
    "image_ref": "artifacts/sim_images/OP20_CNC01_frame01.png",
    "log_message": "[WARN] CNC_01 spindle temperature approaching threshold during OP20"
  },
  "raw_context": {
    "line_id": "LINE_01",
    "process_id": "OP20",
    "work_order_id": "WO023",
    "order_id": "ORDER_A"
  }
}
```

### 2.3 契约二：V1 统一语义对象模型 (`SemanticObject` —— 冻结核心规范)
`Semantic Object Service` 必须将所有输入转换为以下 **7 个标准字段** 的结构。
* **`object_type` 枚举值（5类核心对象）**：`equipment`（设备）、`process`（工序）、`work_order`（工单）、`order`（订单）、`resource`（资源）。
* **`domain` 枚举值**：`manufacturing`（制造侧）、`enterprise`（企业侧）。
* **`level` 枚举值**：`device`（设备层）、`line`（产线层）、`factory`（工厂层）、`enterprise`（企业层）。

| 字段名 | 类型 | 必填 | 含义说明 | 标准示例 |
| :--- | :--- | :--- | :--- | :--- |
| **`object_id`** | `string` | 是 | 全局唯一对象标识符（跨系统归一化后的标准ID） | `"CNC_01"` / `"OP20"` / `"WO023"` / `"ORDER_A"` |
| **`object_type`** | `string` | 是 | 对象类型（限定为5类核心对象之一） | `"equipment"` / `"process"` / `"work_order"` / `"order"` / `"resource"` |
| **`domain`** | `string` | 是 | 所属业务域 | `"manufacturing"` / `"enterprise"` |
| **`level`** | `string` | 是 | 所处空间/组织层级 | `"device"` / `"line"` / `"factory"` / `"enterprise"` |
| **`state`** | `object` | 是 | 动态运行状态（由信号、图像、日志或业务记录提炼） | `{"status": "running", "load": 0.65, "temp_c": 78.5, "health": "warning"}` |
| **`context`** | `object` | 是 | 业务上下文绑定关系（刻画与产线、工序、工单、订单的关联） | `{"factory": "FAC_01", "line": "LINE_01", "process": "OP20", "work_order": "WO023", "order": "ORDER_A"}` |
| **`timestamp`** | `string` | 是 | 归一化后的统一状态时间戳（ISO-8601） | `"2026-10-28T10:30:00.125Z"` |

### 2.4 契约三：统一动态关系模型 (`RelationEdge`)
`Relation Service` 产出的关联关系必须统一表达为 `source - relation - target` 结构，并附带权重、时间戳和上下文：

```json
{
  "relation_id": "REL_CNC01_OP20",
  "source": "CNC_01",
  "relation": "executes",
  "target": "OP20",
  "weight": 1.0,
  "timestamp": "2026-10-28T10:30:00.125Z",
  "context": {
    "work_order": "WO023",
    "order": "ORDER_A",
    "propagation_rule": "device_fault_blocks_process"
  }
}
```
**标准关系类型约定**：
* **业务执行与映射链**：`CNC_01 (equipment) —[executes]→ OP20 (process) —[belongs_to]→ WO023 (work_order) —[maps_to]→ ORDER_A (order)`
* **资源占用链**：`OP20 (process) —[uses_resource]→ RES_MAT_01 (resource)`
* **空间/组织层级隶属链**：`CNC_01 (device) —[located_in]→ LINE_01 (line) —[part_of]→ FAC_01 (factory) —[managed_by]→ ENT_01 (enterprise)`

---

## 3. 多Agent协同任务拆解（Work Packages）

以下是切分好的 5 个独立开发任务包，可分配给不同的 Coding Agent 并行开发或按序推进。

### 任务包 WP1：底层仿真系统多模态扩充与多协议模拟改造（Simulation & Protocol Agent）
* **背景与目标**：解决导师指出的“目前只有结构化文本、没有信号/图片/日志、没有体现不同系统通信协议”的问题。
* **具体开发内容**：
  1. **保留现有 ERP 与 MES 业务主线**：维持 ERP 创建订单（订单编号、名称、需求量、交期）$\rightarrow$ 下发 MES 生成工单与分配资源的逻辑（订单与工单本身不产生传感器信号）。
  2. **在 SimPy 生产过程仿真中新增 3 类多模态数据生成器**：
     * **机器传感器信号生成器（时序流）**：针对执行工序的具体设备（如 `CNC_01`、`ROBOT_02`），在 SimPy 工序推进过程中按采样周期生成连续物理信号（主轴转速 `rpm`、温度 `temp_c`、振动 `vibration`、负载率 `load`），支持正常波动模式与故障突变模式（如温度飙升、振动超标）。
     * **现场加工视觉图像生成器（图像模态）**：在工序开始、加工中、质检完成或设备报警节点，自动生成或关联对应的现场状态合成图像（可使用 `Pillow` / `matplotlib` 动态绘制带有工件编号、实时工艺参数叠加水印、正常/裂纹缺陷标记的现场监控模拟图，保存至本地并返回路径与图像元数据）。
     * **非结构化设备运行日志生成器（日志模态）**：实时生成类似工业控制器输出的行日志文本（如 `[2026-10-28 10:30:02] [PLC-01] [ERROR] Code 0x84: Spindle overload on CNC_01 while executing OP20 for WO023`）。
  3. **为三层系统挂载异构通信协议模拟接口**：
     * **ERP 协议模拟**：通过 **REST API（JSON）** 与 **PostgreSQL/SQLite 数据表** 暴露订单与库存记录。
     * **MES 协议模拟**：通过 **MQTT 消息发布**（或 REST API）对外推送工单排程与工序状态变更。
     * **SimPy 现场层协议模拟**：提供 **OPC UA 模拟节点 / MQTT 遥测流**，并专门封装一个**模拟底层 PLC 帧报文生成器**（将数据打包为包含 `帧头 + 设备地址 + 功能码 + 数据区 + 控制指令 + 心跳/校验` 的十六进制报文字符串，呼应导师关于 307 实验室 PLC 协议帧的要求）。
* **验收标准（DoD）**：运行仿真脚本后，能够同时产生结构化订单/工单、连续信号序列、加工现场图片文件、非结构化日志文本，且分别通过上述不同协议格式发出。

### 任务包 WP2：多源数据接入服务开发（`Data Connector` Agent）
* **对应架构模块**：架构图 ① `Data Connector - 多源数据接入服务`（对应 PDF 3.1 节）。
* **具体开发内容**：
  1. 开发模块化 Connector 基类 `BaseConnector` 及四类具体实现：
     * `RestApiConnector`：拉取或接收 ERP/MES 的 REST JSON 数据。
     * `DatabaseConnector`：轮询或监听 PostgreSQL/MySQL（本地开发支持 SQLite 平滑降级）中的 ERP 订单与库存表。
     * `MqttConnector`：订阅 MES 工单事件与现场传感器/日志 MQTT Topic（支持本地无 Broker 时的内存队列 Mock 模式，确保随时可单机演示）。
     * `OpcUaAndPlcConnector`：读取现场设备 OPC UA 变量节点，并内置**PLC 协议帧解析器（Frame Parser）**，将底层十六进制协议帧（地址段、功能码段、数据段、控制指令段）解析还原为键值对结构。
  2. 实现统一消息标准化封装器：为所有接入的数据自动打上 `source_system`、`protocol`、`modality`、`timestamp`、`object_hint` 与 `raw_context`，输出符合 **契约一（`RawIngestMessage`）** 的标准消息。
* **验收标准（DoD）**：四类 Connector 可独立运行；ERP 订单、MES 工单、PLC/设备信号、现场图片与日志能够同时进入接入层并输出标准化的 `RawIngestMessage`。

### 任务包 WP3：语义对象化服务开发（`Semantic Object Service` Agent —— 核心模块）
* **对应架构模块**：架构图 ② `Semantic Object - 语义对象化服务`（对应 PDF 3.2 节、第 4 节、第 5 节）。
* **具体开发内容**：
  1. **实现不同系统异构字段与编码的归一化映射**：
     * 将不同系统中的设备标识（如 PLC 地址 `0x01`、SCADA 编号 `DEV_CNC_01`、MES 资源码 `RES_M01`）统一映射为标准 `object_id`（如 `"CNC_01"`）。
     * 先识别输入数据所属的 `domain`（`enterprise` vs `manufacturing`）与 `level`（`enterprise` / `factory` / `line` / `device`），再映射至具体的 5 类 `object_type`（`equipment` / `process` / `work_order` / `order` / `resource`）。
  2. **实现多模态数据的状态提炼（Multi-Modal State Extraction）**：
     * **针对时序信号**：计算滑窗统计特征（均值、峰值、负载率 `load`），根据阈值判定设备运行状态（`idle` / `running` / `overload` / `fault`）写入 `state` 字段。
     * **针对现场图片**：提取图像检测结果标签或元数据（如 `{"inspection_result": "pass", "image_url": "..."}`）合并至对应工序或设备对象的 `state` 字段。
     * **针对运行日志**：通过正则/关键词规则解析日志中的故障码与严重等级，更新对应对象的 `state`。
  3. **实现博士算法标准接口封装**：
     * 严格按照 PDF 第 5 节接口约定，封装核心函数 `semantic_mapping(raw_data: dict) -> dict`，完成输入校验、异常处理、结果转换，并将生成的 `SemanticObject` 存入统一对象库（PostgreSQL / SQLite），对外暴露 `POST /semantic/object` 与 `GET /semantic/objects` 接口。
* **验收标准（DoD）**：任意一条来自 ERP、MES 或 SimPy（信号/图片/日志）的原始数据进入后，均能准确生成包含 7 个标准字段的 `SemanticObject`，且 `context` 上下文完整绑定了对应的产线、工序、工单和订单。

### 任务包 WP4：动态关联分析与多层级时空模型服务开发（`Relation & Spatiotemporal` Agent）
* **对应架构模块**：架构图 ③ `Relation Service` & ④ `Spatiotemporal Model`（对应 PDF 3.3 节、3.4 节）。
* **具体开发内容**：
  1. **开发 `Relation Service`（第一版简化算法实现）**：
     * 按照 PDF 第 5 节接口约定，封装 `relation_inference(objects: list, context: dict) -> list` 函数。
     * 基于对象的 `context` 绑定关系与跨尺度时间戳对齐，自动构建并动态更新 `CNC_01 —[executes]→ OP20 —[belongs_to]→ WO023 —[maps_to]→ ORDER_A` 的跨层业务链条，输出符合 **契约三（`RelationEdge`）** 的关系集合。
  2. **开发 `Spatiotemporal Model Service`（四层动态时空图）**：
     * 构建覆盖 **`Enterprise（企业） → Factory（工厂） → Line（产线） → Device（设备）`** 四个物理/组织层级的动态图结构（同时包含 `Order -> WorkOrder -> Process -> Equipment` 业务关系），支持 Neo4j 存储及 Python `NetworkX` 内存图双模式运行（保证无外部图数据库时也能直接跑通并可视化）。
     * **事件驱动的增量更新机制**：当某个设备或工单状态发生变化时，仅增量更新受影响的节点状态与关联边权重，并记录历史时间快照（Snapshot）。
  3. **实现跨层故障影响传播查询（Impact Propagation Analysis）**：
     * 当底层设备（如 `CNC_01`）因传感器信号超标或报错日志变为 `fault` 状态时，模型自动沿时空图向上遍历推理，输出完整的跨层影响链路：`CNC_01 (设备故障) -> OP20 (工序阻塞) -> WO023 (工单延期风险) -> ORDER_A (订单交付风险)`。
* **验收标准（DoD）**：支持查询任意时刻的四层时空图结构（节点+边），并能给定一个故障设备 ID 或变更订单 ID，实时返回跨层影响传播路径。

### 任务包 WP5：统一 API 网关封装与端到端演示系统集成（`Gateway & Demo` Agent）
* **对应架构模块**：架构图 ⑤ `API Gateway - 统一服务接口` 及端到端可视化/命令行 Demo（对应 PDF 3.5 节、第 8–10 节）。
* **具体开发内容**：
  1. **封装 5 类统一服务接口（FastAPI 实现）**：
     * **`Object API`**：`POST /api/v1/objects/ingest`（接入并转为语义对象）、`GET /api/v1/objects`（按类型/域/层级查询统一语义对象）。
     * **`State API`**：`GET /api/v1/states/{object_id}`（查询对象最新动态状态与多模态特征）、`GET /api/v1/states/{object_id}/history`（查询时序历史状态）。
     * **`Relation API`**：`GET /api/v1/relations`（查询跨层业务-资源关联三元组）、`POST /api/v1/relations/infer`（触发关联推理）。
     * **`Model API`**：`GET /api/v1/model/graph`（获取企业-工厂-产线-设备四层动态时空图全貌）、`GET /api/v1/model/impact/{object_id}`（查询异常跨层影响传播链）。
     * **`Event API`**：`POST /api/v1/events/trigger`（模拟注入设备故障、信号超标、紧急插单等动态事件并触发全链路更新）。
  2. **升级前端展示界面或一键端到端演示脚本（`run_e2e_demo.py`）**：
     * 将原有的 ERP/MES/SimPy 演示界面扩展，直观展示：**① 多协议原始报文与多模态数据（信号曲线、现场图片、日志） $\rightarrow$ ② 归一化后的 7 字段标准语义对象表 $\rightarrow$ ③ 企业-工厂-产线-设备四层时空关联拓扑图与故障影响传播高亮路径**。
  3. **预置 Docker 化配置文件**：
     * 编写基础 `Dockerfile` 与 `docker-compose.yml`（包含应用服务、PostgreSQL、Neo4j、MQTT Broker 定义），同时确保本地纯 Python 环境（`python run_e2e_demo.py`）无需安装重量级外部依赖即可一键启动演示。

---

## 4. 建议代码仓库目录结构

各协同 Agent 请按照以下目录结构组织代码，保持工程整洁与模块解耦：

```text
task2_1_prototype/
├── README.md                         # 快速启动说明与每周汇报五问对照表
├── requirements.txt                  # Python依赖包清单 (fastapi, uvicorn, simpy, networkx, pillow, pydantic等)
├── docker-compose.yml                # 阶段2容器化编排文件
├── simulators/                       # [WP1] 底层三系统与多模态多协议仿真器
│   ├── erp_simulator.py              # ERP订单与库存生成 (REST / DB模拟)
│   ├── mes_simulator.py              # MES工单与排程生成 (MQTT / REST模拟)
│   ├── simpy_factory.py              # SimPy生产过程仿真 (工序事件+连续信号+现场图片+设备日志)
│   └── protocol_emulator.py          # OPC UA / MQTT / 模拟PLC十六进制帧打包器
├── services/                         # [WP2-WP5] 任务2.1五个核心服务模块
│   ├── connector_service/            # ① Data Connector 多源数据接入服务
│   │   ├── connectors.py             # REST, DB, MQTT, OPC_UA/PLC_Frame 四类接入器
│   │   └── schema.py                 # RawIngestMessage 数据契约
│   ├── semantic_object_service/      # ② Semantic Object 语义对象化服务
│   │   ├── mapping_engine.py         # semantic_mapping(raw_data) 核心转换与多模态特征提取
│   │   └── models.py                 # 7字段 SemanticObject 模型与存储层
│   ├── relation_service/             # ③ Relation Service 动态关联分析服务
│   │   └── relation_engine.py        # relation_inference(objects, context) 关联生成
│   ├── spatiotemporal_model_service/ # ④ Spatiotemporal Model 多层级时空模型服务
│   │   └── graph_manager.py          # 企业-工厂-产线-设备四层动态图、增量更新与故障传播推理
│   └── api_gateway/                  # ⑤ API Gateway 统一服务接口
│       └── main.py                   # FastAPI 路由 (Object/State/Relation/Model/Event API)
├── artifacts/                        # 运行期生成的仿真图片、日志与快照文件
└── run_e2e_demo.py                   # 一键跑通“多源产生->接入->对象化->关联->时空图->API查询”脚本
```

---

## 5. 下周向导师汇报验收标准（严格对照PDF第9节“每周汇报五问”）

所有 Agent 完成协同开发后，最终运行效果必须能够直接回答导师规定的以下 5 个固定问题：

| 汇报问题序号 | 导师固定提问 | 本周 V0.1 汇报标准答案与演示内容 |
| :--- | :--- | :--- |
| **① 本周完成哪个模块** | 本周完成了什么可运行模块？ | 完成了底层仿真系统的**多模态（信号/图片/日志）与多协议改造**，并跑通了任务2.1 V0.1从 **`Data Connector` $\rightarrow$ `Semantic Object` $\rightarrow$ `Relation Service` $\rightarrow$ `Spatiotemporal Model` $\rightarrow$ `API Gateway`** 的端到端闭环。 |
| **② 输入是什么** | 模块的输入数据是什么？ | 1. **ERP（REST/DB）**：订单与交期数据；<br>2. **MES（MQTT/REST）**：工单与工序资源分配数据；<br>3. **SimPy现场层（OPC UA/模拟PLC帧）**：机器连续传感器信号（温度/振动/转速/负载）、现场加工合成图片、非结构化设备运行与故障日志。 |
| **③ 输出是什么** | 模块的输出结果是什么？ | 1. 包含 `object_id, object_type, domain, level, state, context, timestamp` 7个标准字段的 5 类统一语义对象（`Equipment/Process/WorkOrder/Order/Resource`）；<br>2. `source-relation-target` 动态关联链；<br>3. `Enterprise-Factory-Line-Device` 四层动态时空图及设备异常向上影响订单的传播链路。 |
| **④ 接口是什么** | 对外提供什么标准接口？ | 提供了 `POST /api/v1/objects/ingest`、`GET /api/v1/objects`、`GET /api/v1/states/{id}`、`GET /api/v1/relations`、`GET /api/v1/model/graph`、`GET /api/v1/model/impact/{id}` 等五类统一 API。 |
| **⑤ 是否已Docker化** | 当前处于容器化路线哪个阶段？ | 已完成 PDF 第6节规定的**“阶段1：Python 各服务在本地独立运行，功能测试通过，接口稳定”**，并已编写好面向阶段2的 `Dockerfile` 与 `docker-compose.yml` 配置文件。 |
