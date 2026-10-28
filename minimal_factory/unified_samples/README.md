# 多模态统一样本库 V1

日期：2026-10-26（北京时间）。用户在图片和信号生成后要求“接着你的思路继续下去”。本轮完成本地文件接入、统一对象/观测表示、独立SQLite存储、规则关系与时间快照查询。它是组装小场景的可运行基线，不代表完整2.1软件或生产级平台已完成。

先看 [导入与查询结果](../data/unified_samples/examples/导入与查询结果.md)。

## 现在数据存在哪里

- 数据库：`minimal_factory/data/unified_samples/samples_v1.sqlite3`。
- 管理的图片文件：同级 `assets/`。6张原图按内容SHA-256命名并复制，数据库存相对路径、哈希和原始来源信息；不使用BLOB。
- 查询结果：同级 `examples/`，包括订单2跨模态查询、4个时刻的快照、总览及可阅读报告。
- 本轮验证记录：[验收记录](../data/unified_samples/验收记录.md)。

原图片清单和信号manifest是生成时的原始记录，保留其中“尚未绑定/未入库”字段。当前统一库中的新观测记录另存业务绑定与来源；不能把原清单中的旧状态当成当前入库状态。原ERP/MES数据库、保存的SimPy实验和原图片/信号文件均未改动。

## 场景怎么对应

全部配置见 `scenarios.json`。原实验只有正常组装过程；这里用其组装时间线建立三个**新的合成场景**。

| 场景编号 | 信号来源 | 第180秒图片 | 第359秒图片 |
|---|---|---|---|
| normal | 正常541条 | 未紧固，两视角 | 完整，两视角 |
| current_surge | 电流升高541条 | 未紧固，两视角 | 完整，两视角 |
| missing_bolt | 复用正常541条 | 未紧固，两视角 | 缺一螺栓，两视角 |

图片都在新场景中绑定到设备E-ASM、订单DEMO-ORD-2、工单DEMO-WO-2和组装工序DEMO-2-OP-1。180秒/359秒是**人为指定的教学观测时刻**，不是照片拍摄时间，也不是原实验已生成的相机帧。图像描述和螺栓标签来自渲染配置，尚未进行视觉识别。

三个场景独立，故意保留“缺件但电流正常”和“电流升高但装配齐全”的对照，不主张缺件会导致电流升高。只导入E-ASM的4条加工开始/结束事件；不导入旧实验的质检合格事件，避免把缺件场景自动判为合格。组装完成只表示工序时间结束，不证明质量合格。

实际存储：3场景、30对象、54状态记录、39关系记录、6图片资产、1647观测（1623信号+12图片+12事件）。1623=541×3，来自已有1082条独立信号样本的场景复用；12张图片观测引用6个文件，12条事件观测引用4条原始组装事件。不是新增同等数量的真实采集。

## 统一了哪些内容

| 数据表/输出 | 职责 | 关键字段 |
|---|---|---|
| scenarios | 场景命名空间与配置 | scenario_id、source_run_id、config_json |
| objects | 统一对象身份与静态属性 | object_id、object_type、domain、level、source_system、context_json |
| states | 按事件保存状态变化 | object_id、time_s、sequence、state、context_json |
| relations | 业务链、组织层级、动态加工关系 | source_id、target_id、relation_type、valid_from_s、valid_to_s、basis_json |
| assets | 文件引用与追溯 | asset_id、sha256、relative_path、source_json |
| observations | 信号、图片、事件共用的接入记录 | observation_id、scenario_id、object_id、业务对象编号、modality、time_s、payload_json、provenance_json |
| snapshot输出 | 某时刻统一对象与有效关系 | object_id / object_type / domain / level / state / context / timestamp |

例如设备对象编号是 `MM-V1:normal:Equipment:E-ASM`，换成缺件场景就是 `MM-V1:missing_bolt:Equipment:E-ASM`，不混用。同一设备上的多张图片和多条采样只产生多条观测，不反复创建设备对象。

原始值/原始图片清单条目保留在payload；provenance记录来源文件、行号或事件序号、哈希、原始run编号和人为绑定依据。源图片条目中的run_id仍为空是正确的：外层统一记录有新scenario和对象引用，二者语义不同。

组织层级为教学假设：TEACH-ENTERPRISE → TEACH-FACTORY → TEACH-LINE → E-ASM。`domain`为企业/制造域，`level`为enterprise/factory/line/device，`source_system`单独记录SimPy输入、回放或生成器，三者不混为一列。

## 时间和关系如何变化

- 秒是统一仿真时间基准；不捏造真实时间起点。快照的timestamp明确含clock=simulation、seconds和sequence；这是状态最近一次更新的时间，查询时刻另存as_of_sim_time_s。
- processing和serves_work_order采用[start,end)有效区间；时刻查询默认已经应用该时刻全部源事件。第180秒先结束订单1，再按源sequence启动订单2。
- 同一秒的观测顺序：源事件 → 信号 → 图片。信号与图片的sequence是接入排序，不冒充原事件sequence。
- 第179秒服务订单1；第180—359秒服务订单2；第360秒起设备空闲，没有有效的processing关系。图片只在180/359秒有观测，不伪造连续帧。
- 工序可以从waiting变为processing/completed；订单和工单总体状态保持unknown，因为只导入组装子流程，不能推断整条生产链完成。
- 观测查询的start/end均包含端点，关系区间的结束端点不包含；两种接口语义分别明确。

## 你可以亲手运行的第一条查询

在 `minimal_factory` 目录打开PowerShell，运行：

```powershell
python -B -X utf8 -m unified_samples query --scenario normal --work-order DEMO-WO-2 --start 180 --end 180
```

期望：`total=4`，包含1条事件、1条信号、2张图片。先看各记录的modality、time_s、work_order_object_id，再看payload原始内容和provenance来源。这是下一次教学的起点，执行成功仍不自动表示已掌握代码。

其他命令（均在minimal_factory目录执行；Python标准库即可，无新增安装）：

```powershell
python -B -X utf8 -m unified_samples overview
python -B -X utf8 -m unified_samples query --scenario missing_bolt --modality image --start 359
python -B -X utf8 -m unified_samples snapshot --scenario normal --at 180
python -B -X utf8 -m unified_samples snapshot --scenario normal --at 360
python -B -X utf8 -m unified_samples check
python -B -X utf8 -m unified_samples build
python -B -X utf8 -m unified_samples export
```

查询默认最多50条，返回总数与各模态数量；用 `--limit` / `--offset` 翻页，单次最多2000条。`--equipment E-ASM` 可指定设备；不存在的设备/工单返回空集合，未知场景报错。

重复build在代码、配置和来源未变时返回unchanged，数量不增加。若修改规则、源文件或导入代码，旧库拒绝静默覆盖，可用 `--db data/unified_samples_v2/samples.sqlite3` 创建新版本。原库与来源记录保留。导入先校验、暂存、完整性检查，再发布；失败不留下半成品目标库，可能保留可复用的内容寻址图片文件。

## 模块输入、输出、接口与部署状态

- 输入：保存的input/events、图片manifest与PNG、信号JSONL与来源哈希、场景配置。
- 输出：独立SQLite、受管理的图片副本、查询JSON、时间快照、报告。
- 接口：`build / query / snapshot / overview / check / export` 命令行；也可调用store.py对应Python函数。本轮未增加HTTP接口，未重启ERP/MES网页。
- 动态分析：加工关系按事件区间生效；`value > 4 A`作为独立示例规则，直接对信号数值查询，正常0点、缺件0点、电流升高12点，不使用注入标签判定阈值。
- Docker：未开展。本地正确性验证先完成，长期Connector/API/算法服务仍待继续。

## 验证及下一步

本轮新增9项测试通过，覆盖幂等、来源不变、跨模态查询/分页、时间切换、场景外键隔离、错误单位/上下文拒绝、图片损坏/缺失、配置变更保护和导入失败不发布。检查原ERP库SHA-256导入前后相同；未重跑旧业务测试或SimPy。

下一步先由用户执行上面的第180秒查询，解释四条记录怎样关联；随后再根据需求增加查询展示/HTTP接口或图像特征提取。当前基础规则与局部层级快照不能替代完整2.1算法服务、真实多模态融合或全流程验收。
