# WP4 动态关联服务

输入仅为通过冻结契约的七字段 `SemanticObject`。`relation_inference(objects, context=None)` 返回七字段 `RelationEdge` 列表；两端节点必须真实存在。Pydantic 与导出 JSON Schema 均参与运行时验证。

```python
from task2_1_prototype.services.relation_service import relation_inference

edges = relation_inference(objects, context=rules)
```

工序使用 `context.process_instance_key == [context.work_order, object_id]`，设备与订单身份来自显式 `equipment/order`。不从 OP 编号推断次序。组织关系使用资源 `parent` 和业务上下文中的 `line/factory/enterprise`。

| relation | source → target | 活动依据 |
|---|---|---|
| executes | equipment → process | 工序实际执行窗口 |
| belongs_to | process → work_order | 工序实际执行窗口 |
| maps_to | work_order → order | 工单 running 等生命周期状态 |
| located_in | 设备/业务对象/物料 → 组织资源 | 显式上下文或配置 |
| part_of / managed_by | line → factory / factory → enterprise | 组织 parent |
| uses_resource | process/work_order → material | 显式 BOM 规则及源对象活动状态 |

活动边 `weight=1.0`，非活动边 `weight=0.0`；`context.active/phase` 区分执行、未开始、完成和结构关系。relation_id 按批次与三元组确定，状态改变不改变身份。已有关系冲突、悬空目标、工单串连、非法时间与多批次混入均拒绝。

历史查询应先由 `GraphManager.snapshot(as_of)` 取得指定时刻对象，再进行推理。单独调用本函数不能由最终对象倒推过去状态。图服务传入的 `execution_windows` 来自工序生命周期，采用 `[start_s,end_s)`；函数也支持只用已选时刻的对象状态判断活动。

`config/relation_rules.json` 提供本演示新增的显式规则：ORDER_A→ENT_01、RES_MAT_01→FAC_01，以及 WO023/OP20 使用 RES_MAT_01 的教学 BOM。边标记 `evidence_source="configured_bom_rule"`、`actual_consumption_asserted=false`。不传该规则时不生成物料边；配置不代表实际领料。

## 路由挂载

```python
from task2_1_prototype.services.relation_service.api import create_router
app.include_router(create_router(lambda: manager))
```

- `GET /relations?source=CNC_01&relation=executes&active=true&as_of=75`；还支持 target、clock。
- `POST /relations/infer` 接收 `{"objects": [...], "context": {...}}`，返回关系数组。本端点只推理，不暗中注册节点或更新图；图状态更新由明确的 `manager.apply_event` 完成。

时间参数、持久化、风险边界和批次命令见[时空图服务](../spatiotemporal_model_service/README.md)。
