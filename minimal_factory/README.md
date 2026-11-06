# 最小工厂：ERP + MES + SimPy + 异构协议数据源

这是后续“多源接入与统一字段转换”的干净源头。ERP负责订单与HTTP下发；MES保留排产、设备、报工、返工/复检及状态管理；SimPy在独立沙盒执行三工序，生成各自原生的现场帧。转换模块尚未新建。

## 启动与测试

在 minimal_factory 目录双击 `启动演示.cmd`，或运行：

```powershell
.\.venv\Scripts\python.exe -m uvicorn erp.app:app --host 127.0.0.1 --port 8000
```

访问 http://127.0.0.1:8000/ ，接口文档 http://127.0.0.1:8000/docs 。代码更新后需重启旧服务。`汇报前自检.cmd`只检查访问状态。旧离线导出已删除。

首次安装：Python 3.12，`python -m pip install -r requirements.txt -r requirements-dev.txt`。前端测试另需Node.js，页面运行无需构建工具。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试全部使用临时数据库。每次正式仿真仅更新自己的 `data/erp.simulations/SIM-*/mes.db`，不写回原工单。已有生产数据库与历史批次保留，不为展示刷新业务状态。

## 保留的目录

```text
minimal_factory/
├─ erp/                  # 订单、dispatch.py、http_trace.py、feed.py
├─ mes/                  # HTTP接收、工单/工序/设备/返工状态机
├─ simulation/           # engine.py、field_protocols.py、field_runtime.py、assets.py等
├─ synthetic_signals/
│  └─ generated_v1/      # 完整保留的合成信号库
├─ synthetic_images/
│  └─ rendered_v1/       # 完整保留的图像、标签与清单；两套素材共36文件
├─ web/                  # index.html、simulation.js、simulation.css
├─ tools/
│  ├─ prepare_field_assets.py
│  └─ demo_launcher.py
├─ tests/                # ERP/MES/HTTP、原生帧、SimPy/异常级联与前端回归
├─ data/                 # 本机业务库/历史批次；protocol_acceptance.json为本轮验收
├─ logs/                 # 运行时日志（不提交Git）
├─ .venv/                # 本机运行环境（不提交Git）
├─ .vscode/              # 启动任务配置
├─ .gitignore
├─ requirements.txt
├─ requirements-dev.txt
├─ README.md
├─ 当前进度.md
├─ 启动演示.cmd
└─ 汇报前自检.cmd
```

外围原型、旧统一库、学习/离线演示、临时目录已清理；源码历史在Git的 `backup-before-clean`。不在工作区保留第二套备份目录。

## ERP 的 Connector 源头（只读）

- `GET /orders`：订单列表；`GET /orders/{order_id}`：详情，未找到返回404。
- `GET /erp/feed` 与 `GET /orders/dispatch-outbox`：同一结果，含 `orders` 主数据数组与 `dispatch_outbox` 原消息数组。
- 每条发件箱记录含 message_id、timestamp、order_id、原生payload、完整envelope、endpoint、已记录的http请求/响应、acknowledged_at与delivery_state。
- feed以SQLite `mode=ro`、`query_only=ON`及单个读事务查询，不下发、不改变状态、不执行仿真。
- 历史报文原样返回：没有保存过的HTTP寻址/响应不能伪装成已观测201，对应值返回null。`unconfirmed`不等于未发送；例如MES已接收但ERP丢失响应，需要按同一message_id重试。

新服务首次启动仅为outbox幂等增加 `http_json` 列，保存以后的实际交互；不会清空订单或历史记录。

## 五类协议的原生结构

共享追溯信息（工单/工序、源文件、哈希、虚拟分钟）放在帧外层，`payload`保留协议差异。

| 类型 | 寻址 | 原生结构 | 传输事实 |
|---|---|---|---|
| HTTP-REST | POST http://127.0.0.1:8000/mes/dispatch | IT JSON：order_id/product_code/qty/route_steps | ERP真实HTTP POST，成功201；HTTP头与状态在outbox和日志 |
| MQTT | mqtt://127.0.0.1:1883；factory/line1/{device_code}/command | PUBLISH：topic/qos/retain/packet_id/payload_json/payload_bytes_hex | 本地帧，未连接broker |
| OPC-UA | opc.tcp://127.0.0.1:4840/freeopcua/server/ | nodes订阅列表：node_id/data_type/value/status_code | 本地帧，未连接OPC server |
| HTTP-Vision-Webhook | POST http://127.0.0.1:8000/mes/vision/webhook | camera_ip/image_url/detections(label/count/expected/PASS) | 本地帧，未发起webhook请求 |
| Modbus-TCP | modbus://192.168.10.30:502；unit=1/FC03/40001/count=4 | raw_registers整数数组与request/response_frame_hex | 本地帧，未连接PLC |

MES_BASE_URL可覆盖ERP目标服务地址，实际目标随报文保存。旧HTTP-REST/ISA95-Lite-v1仍支持，历史幂等与订单快照保留。OPC UA使用有效BadOutOfRange状态码0x803C0000并注明Overload诊断；不把通用Bad码误称BadOverload。

Modbus载荷故意不带物理量英文名。后续Connector必须依据以下点表解释；当前报警与设备卡片的少量本地读取不代表已经完成通用转换模块。

| 点表编号 | 线上偏移 | 含义 | 解码 |
|---|---|---|---|
| 40001 | 0 | 电流 | uint16 / 1000 → A |
| 40002 | 1 | 温度 | uint16 / 10 → ℃ |
| 40003 | 2 | 负载 | uint16 / 10 → % |
| 40004 | 3 | 故障码 | uint16；当前素材0正常/1过载 |

MBAP的Length含unit_id；请求PDU使用零偏移而非把40001写进线上地址。实际异常样本为 `[4421,886,1105,1]`，对应4.421A、88.6℃、110.5%、故障1；不为凑示例改造原素材的故障码。

## 素材、级联与落盘

`tools/prepare_field_assets.py`根据已保留的原始合成素材确定性整理现场样本；运行时真实读盘并校验清单/哈希。信号非实测，Robot部分特征为教学派生；Vision取场景标签，不做图像识别。四PNG通过 `/static/rendered_v1/{normal|defect}_{front|side}.png` 只读白名单提供。

在仿真页选择现有MES工单后，Normal/Anomaly/Sampled均需点击才开始。法兰2件、每件3/2/1分钟：Normal为12分钟；三设备异常导致CNC维修+6、Vision返工3/复检2、Robot维修+5，共28分钟、延误16。设备事件当刻实际写fault，恢复后写available，红灯保留曾发生故障的轨迹。

每批保留input.json、mes.db、events.jsonl、protocol_messages.jsonl、summary.json及运行记录；协议完整JSON同时进入沙盒表、事件detail与JSONL。网页协议列表直接展示类型、地址和原始payload，ERP额外显示实际请求头与状态码。

本轮完整实际证据在 [data/protocol_acceptance.json](data/protocol_acceptance.json)，包括临时8000服务HTTP 201、正常/异常结果、五帧、设备当刻状态、36素材哈希及原主库只读核对。该文件有明确时间，是验收快照，不是实时业务状态。完整Gemini反馈见 [项目推进日志](../md/项目推进日志.md)。
