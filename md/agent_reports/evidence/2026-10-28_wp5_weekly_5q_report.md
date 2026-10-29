# 任务2.1 · 导师每周汇报五问

本次运行：wp5_e2e_20261028；北京时间：2026-10-29T10:43:50+08:00。

## ① 本周完成哪个模块？

完成阶段1的五层本地原型：多源接入、语义对象化、动态关联、四层时空模型、统一API网关及离线演示看板。复用真实ERP创建订单、MES下达/分配与SimPy生产仿真。

## ② 输入是什么？

本次从零生成2202条源协议记录、360条四路信号、9张PNG和8条设备日志。接入后共2206条八字段RawIngestMessage，7条坏样本拒收，2202条源重复消息去重。

## ③ 输出是什么？

12个七字段语义对象、2215条历史、360个对齐物理周期、19条七字段关系及4个时空快照。

| 仿真时刻 | CNC_01诊断 | 活跃风险 | 路径 |
|---:|---|---:|---|
| 59s | running | 0 | 空 |
| 75s | fault | 1 | CNC_01 → OP20 → WO023 → ORDER_A |
| 95s | running | 0 | 空 |
| 360s | idle | 0 | 空 |

风险为诊断评估，未改变MES实际排程。恢复后仍保留历史审计；物料关系依据显式教学BOM。

## ④ 接口是什么？

Object / State / Relation / Model / Event五类API，共10条标准路由，兼容原semantic/relations/model路由；本次38次HTTP响应断言通过。

- `/api/v1/events/trigger`
- `/api/v1/model/graph`
- `/api/v1/model/impact/{object_id}`
- `/api/v1/model/snapshots`
- `/api/v1/objects`
- `/api/v1/objects/ingest`
- `/api/v1/relations`
- `/api/v1/relations/infer`
- `/api/v1/states/{object_id}`
- `/api/v1/states/{object_id}/history`

独立在线事件分支状态：[{'kind': 'fault', 'status': 'fault', 'active_impacts': 1}, {'kind': 'fault_cleared', 'status': 'running', 'active_impacts': 0}]。源协议记录真实经过WP2→WP3→WP4，未直接改图。

## ⑤ 当前运行阶段是什么？

本次状态：**completed**。阶段1为本地Python独立运行、功能与接口实测；默认脚本执行后退出，可选--serve启动本地Uvicorn。**阶段2 Docker化按用户要求暂缓，尚未进入。**

MQTT/OPC UA为本地模拟；SQLite为真实本地数据库；Neo4j仅参数化离线导出。合成图标签不代表视觉深度学习识别。

查看：e2e_dashboard.html；计数及验收：e2e_summary.json；在线事件：online_event_evidence.json。
