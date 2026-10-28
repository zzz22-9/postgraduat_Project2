# 设备电流合成信号 V1

> 后续状态（2026-10-26）：本批信号已导入[统一样本库](../unified_samples/README.md)，与图片及组装事件按新合成场景关联。原manifest保留生成时database_ingested=false和image_binding=null，当前入库/绑定信息由统一库管理。下文“未入库/图片未绑定”保留为生成节点的历史描述。

日期：2026-10-26（北京时间）。来源：用户指出图片已有、仍缺信号数据。本轮补充组装设备E-ASM的模拟电流幅值序列。信号类型、1秒采样及数值参数为AI实施选择，不是老师规定的设备参数。

## 数据是什么

读取已保存实验 `SIM-97bdfc0915964e5ba0b0a33c3466a301/events.jsonl`，从加工开始/结束事件推导设备时间线，再另外生成电流。原实验未重跑、原事件未修改；这些值不是2026-10-20采集的真实信号。

| 仿真区间 | 组装设备状态 | 来源业务上下文 | 电流生成设定 |
|---|---|---|---|
| [0,3)分钟 | 加工 | DEMO-ORD-1 / DEMO-WO-1 / DEMO-1-OP-1 | 3 A + 缓慢波动 + 小幅噪声 |
| [3,6)分钟 | 加工 | DEMO-ORD-2 / DEMO-WO-2 / DEMO-2-OP-1 | 同上 |
| [6,9]分钟 | 空闲 | 工单、订单、工序字段为空 | 0.2 A + 小幅噪声 |

区间采用左闭右开，在第180秒采样时已经切到订单2，第360秒已经空闲。0—540秒包含两个端点，因此每组541条，其中加工360条、空闲181条。空闲181个采样点不等于空闲持续181秒。订单2等待期间，设备仍为订单1加工，不能生成空闲信号。

这是1Hz采样的**低频电流幅值/包络示意**，不是交流电瞬时波形，也不是经标定的RMS测量值。所有幅值、噪声、周期和阈值均为教学假设。

## 两组样本

- [normal.jsonl](generated_v1/normal.jsonl)：正常基线。加工值约3 A，空闲约0.2 A。
- [injected_surge.jsonl](generated_v1/injected_surge.jsonl)：与基线使用相同随机序列，仅在第240—251秒（[4.0,4.2)分钟，12秒）额外增加2.5 A。用于验证简单阈值规则，非原实验已发生故障。
- [preview.png](generated_v1/preview.png)：两条曲线，展示业务区间、采样值与4 A演示阈值。
- `timeline.json`：从源事件提取的区间及事件序号；`manifest.json`：参数、来源哈希、脚本哈希、生成时间和时间语义；`summary.json`：统计；`validation.json`：本轮验证。

`injected_anomaly` 是按注入规则写出的真值，不是故障诊断结果；`above_4A_count` 是简单数值统计，不代表诊断算法已完成。缺螺栓不必然导致电流升高，本批信号与图片没有建立因果关系或同步对应。

## 如何读一条记录

每行是一个独立JSON对象：

- `sample_id`：组内唯一采样编号；`dataset_id`：新合成数据集。
- `run_id`：新合成场景编号，正常/异常分别隔离；`source_run_id`：作为时间线来源的旧实验编号。
- `source_type=synthetic_event_replay`、`synthetic=true`：明确生成来源。
- `equipment_id`：E-ASM；`sim_time_s`：整数仿真秒，是采样时间的精确基准；`sim_time_min`：换算的分钟。
- `signal=current_envelope`、`value`、`unit=A`：电流幅值、数值、单位。
- `equipment_state`、`order_id/work_order_id/operation_id`：从源时间线导入的加工上下文，空闲时业务编号为空。
- `source_start_event_sequence`：追溯该加工区间的源事件序号；`injected_anomaly`：是否位于人为注入窗口。

工单编号是源实验回放引用，不表示重新推进原工单。`generated_at` 在manifest中记录实际生成UTC时间，与仿真时间分开。尚未统一入库；图片清单未更改，仍未绑定业务或时间。

## 重复生成

数据生成只用Python标准库，预览使用本机已有Pillow和微软雅黑。项目ERP/MES依赖未修改。

在项目根目录的PowerShell中运行：

```powershell
& 'C:\Users\18456\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -X utf8 '.\minimal_factory\synthetic_signals\generate_signals.py'
```

如果没有该环境，可使用普通Python并加 `--no-preview`，仍能生成全部JSON数据与验证报告。默认覆盖本组件自己的 `generated_v1` 生成文件；可用 `--output 新目录` 保留旧批次。源事件路径固定为本项目保存的默认实验。随机种子固定为20261026；数值公式和参数见脚本顶部及manifest。本轮验证同一环境重新生成记录完全一致，跨Python版本的一致性未验证。

## 本轮验证与边界

实际生成两组各541条，共1082条；JSONL写出再读回一致；全部数值有限且非负；核对180秒/360秒业务边界；重复生成一致；两组差异严格限于240—251秒且每点增加2.5 A；源events文件运行前后SHA-256一致。正常组超过4 A为0条，注入组为12条。已查看曲线，未重跑业务测试或SimPy。

当前完成信号素材与回放上下文，未完成传感器采集、协议通信、图片时间绑定、统一数据库和完整语义转换。下一步结合用户理解整理图片/信号/事件的字段与关联规则，再推进统一接入。
