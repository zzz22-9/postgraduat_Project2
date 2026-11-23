# P1 连续合成遥测：Windows 本地集成与验收

2026-11-23（北京时间）。**本轮 P1 最小连续数据生成技术验收通过。** 原远程说明在本文末尾保留为历史；本结论取代其“尚未完成Windows完整回归”状态。依据是用户本轮完整任务及连续执行授权，不代表用户掌握或导师验收。交付后停止，后续工作须新授权。

## 1. 工作区、Git 与变更范围

- 原工作区：`D:\AAA-Myproject\postgraduate\Project 2`，`feat/p04-data-output-contract / f8547bdca288420a98fd3fc0d18485356151ca99`。起点27项未提交路径、空暂存区；逐路径清单与原字节哈希在本机 `baseline.json`。
- 独立工作树：`D:\AAA-Myproject\postgraduate\Project 2\.task_work\p1_local_20261123\worktree`；分支 `codex/p1-local-acceptance-20261123`；独立 Python：`D:\AAA-Myproject\postgraduate\Project 2\.task_work\p1_local_20261123\worktree\.venv\Scripts\python.exe`（Python 3.14.6）。未使用原venv。
- 仅 fetch `origin feat/p1-continuous-telemetry`，源提交 `447a5c6d409edbafbe59d9b030066e4ccbeacb40`。相对P0.4仅6个P1相关路径；未获取、合并或恢复被禁的REST功能分支。
- 本轮代码提交 `94da92597eef666c7dda9a6a00bff7b56e8537d6`。本报告及三份新摘要另作记录提交；最终交付SHA见远程分支HEAD与最终回复。唯一推送目标 [feat/p1-continuous-telemetry](https://github.com/zzz22-9/postgraduat_Project2/tree/feat/p1-continuous-telemetry)，普通快进，原工作分支与master不合并。

| 代码路径（相对minimal_factory） | 实际情况 |
|---|---|
| `simulation/continuous.py` | 原P1新增；本轮仅3行使Robot工程量从最终寄存器反算 |
| `simulation/engine.py` | 集成原P1在加工中采样及P0.3终态写前保护，本轮0修改 |
| `simulation/service.py` / `simulation/api.py` | 集成原P1每批JSONL分页读取，本轮0修改 |
| `tests/test_continuous_telemetry.py` | 增强原3项检查，新增真实HTTP及P0.3终态拒绝检查，共5方法 |
| `tools/accept_p1_telemetry.py` | 新增并实际执行的一次性独立验收工具，只操作显式临时库及新目录 |

无Data Connector、`task21/`、统一语义/关系层/知识图谱；无新数据库产品、真实协议Server/Broker或架构重构。依赖文件0修改，按现有requirements-dev安装，完整冻结在本机 `environment.txt`。Git信任仅通过每条命令的精确safe.directory，不改全局配置。

## 2. 三场景与采样实测

输入为临时SQLite备份内现有工单 `WO-b129387ecee44a8bb268596b0613d4cc`，订单 `ORD-585f82ea3684401b95f2f14dd8afd87e`，数量2；工序单件3/2/1分钟，与原基线相同。正式库仅只读备份；SQLite逻辑备份文件页布局哈希不同是正常现象，不宣称是字节复制。测试与新订单执行都在临时库/沙盒。

| 场景 | 实际run_id | 虚拟分钟 | 协议 / 来源 | 遥测 | 返工件数 |
|---|---|---:|---:|---:|---:|
| normal | SIM-16fd703a885b47fe8f929107e94aeafa | 12 | 6 / 6 | 482 | 0 |
| rework | SIM-35ae9c7393004fc395cf8206c74e3fe2 | 17 | 10 / 10 | 663 | 1 |
| all_faults | SIM-d7eb812dc577424f8719e3bbe1fb82d5 | 28 | 10 / 10 | 663 | 1 |

| 场景 | 设备 | phase | 虚拟分钟区间 | 点数 |
|---|---|---|---|---:|
| normal | CNC_01 | normal | 0–6 | 361 |
| normal | Robot_01 | normal | 10–12 | 121 |
| rework | CNC_01 | normal | 0–6 | 361 |
| rework | CNC_01 | repair | 10–13 | 181 |
| rework | Robot_01 | normal | 15–17 | 121 |
| all_faults | CNC_01 | normal | 0–6 | 361 |
| all_faults | CNC_01 | repair | 16–19 | 181 |
| all_faults | Robot_01 | normal | 21–23 | 121 |

每段含起点和终点，点数=加工虚拟秒数+1。`sample_sequence=0..N`、`elapsed_s=0..N`，相邻点1秒，`virtual_minute=start+sequence/60`，逐条误差<1e-8分钟；同工单/工序/phase/返工号执行段内不重复、不倒退。电流有确定性变化，四工程量齐全；只采CNC/Robot，不对Vision连续采样。

Robot逐条校验UInt16范围、17字节响应帧、MBAP长度、单元号/功能码/字节数以及四寄存器解码；工程量为寄存器除以1000/10/10和报警原值。素材行哈希、清单整文件哈希、上游哈希及素材时间已独立验证。所有run/订单/工单/工序/设备/device_code、phase和真实返工号核对input及沙盒SQLite。

## 3. 生产逻辑与P0.3保护

以同一输入、同一run_id、两个隔离目录分别执行P0.4原引擎与当前P1。P0.4源自 `f8547bdca288420a98fd3fc0d18485356151ca99` 的engine.py，文件SHA256=`bae70340212921d958618771472af2e17e963e11112b50a68e87fa4c48c7772b`。

三场景26条原协议及26条来源文件逐字节一致；完整summary和events仅排除实际 `recorded_at` 并映射两次独立生成的返工UUID后相等，其余字段全部比较。涵盖工序顺序、资源申请/释放与占用区间、实际故障和恢复时刻、6/5分钟维修延迟、MES报工/返工/复检/最终工单及ERP完成状态、env.run结束时间。沙盒完整性ok、外键0、结束设备available。

专项另注入CNC终态错误run_id，P0.3在原协议写入前拒绝；已存在360条合法加工前缀保留，错误终态遥测0，原协议仅保留MQTT。原24项现场回归及关联拒绝场景也在本轮98项中执行。无修改测试期望掩盖时长差异。

## 4. JSONL位置与真实HTTP

三批目录均为 `D:\AAA-Myproject\postgraduate\Project 2\.task_work\p1_local_20261123\proof\runs/<run_id>/`，各有独立 `telemetry.jsonl`、原 `protocol_messages.jsonl`、`source_context.jsonl`、events、mes.db、input、mapping、run与summary。后续批次运行后前批27文件哈希均未变化。JSONL每行可独立解析、尾换行完整；字段含本任务要求的全部11项。

独立实测地址 `http://127.0.0.1:53482`，真实uvicorn/FastAPI与urllib TCP通信，非TestClient替代。创建批次POST返回202；result及export返回200。遥测GET：

```text
/simulation/runs/SIM-16fd703a885b47fe8f929107e94aeafa/telemetry?offset=0&limit=100
```

limit100三场景分别5/7/7页，合并与JSONL逐条相等；next_offset最终null。offset越界200空列表；负offset、limit0/1001、非整数422；非法或不存在run_id为404。旧正式批次的临时副本无遥测时200、available=false、total0、items[]，7个旧文件哈希不变且未回填。专项还检查运行中不完整尾行不被当作完成记录。

独立验收记录HTTP 64次，状态分布 `{200: 44, 202: 3, 422: 15, 404: 2}`（含预期非法参数/缺失批次）。逐请求状态/响应哈希、各批完整示例与检查结果在本机 `proof/evidence.json`。验收服务已正常关闭，不宣称端口持续在线。

## 5. 两条实际连续采样JSON（完整原记录）

正常批次CNC第1秒：

```json
{
  "schema_version": "telemetry.v1",
  "telemetry_id": "TEL-040f8c43586ba5f2e72957383608f845",
  "run_id": "SIM-16fd703a885b47fe8f929107e94aeafa",
  "order_id": "ORD-585f82ea3684401b95f2f14dd8afd87e",
  "work_order_id": "WO-b129387ecee44a8bb268596b0613d4cc",
  "operation_id": "OP-827a967a1ec64885b0bb4fdcc13547e9",
  "equipment_id": "E-ASM",
  "device_code": "CNC_01",
  "phase": "normal",
  "rework_id": null,
  "virtual_minute": 0.016666667,
  "elapsed_s": 1,
  "sample_sequence": 1,
  "sample_period_s": 1,
  "protocol": "OPC-UA",
  "transport_mode": "local_derived",
  "synthetic": true,
  "is_anomaly": false,
  "metrics": {
    "current_a": 3.049036,
    "temperature_c": 30.595,
    "load_percent": 50.692,
    "alarm_code": 0
  },
  "payload": {
    "subscription_id": 1,
    "publishing_interval_ms": 1000,
    "nodes": [
      {
        "node_id": "ns=2;s=Channel1.CNC_01.SpindleCurrent",
        "data_type": "Float",
        "value": 3.049036,
        "status_code": "0x00000000 (Good)",
        "diagnostic_info": ""
      },
      {
        "node_id": "ns=2;s=Channel1.CNC_01.MotorTemperature",
        "data_type": "Float",
        "value": 30.595,
        "status_code": "0x00000000 (Good)",
        "diagnostic_info": ""
      },
      {
        "node_id": "ns=2;s=Channel1.CNC_01.SpindleLoad",
        "data_type": "Float",
        "value": 50.692,
        "status_code": "0x00000000 (Good)",
        "diagnostic_info": ""
      },
      {
        "node_id": "ns=2;s=Channel1.CNC_01.AlarmCode",
        "data_type": "UInt16",
        "value": 0,
        "status_code": "0x00000000 (Good)",
        "diagnostic_info": ""
      }
    ]
  },
  "source_file": "D:\\AAA-Myproject\\postgraduate\\Project 2\\.task_work\\p1_local_20261123\\worktree\\minimal_factory\\synthetic_signals\\generated_v1\\cnc_normal.jsonl",
  "sample_hash": "0c42a4c86891148f3bf4af497ce15dceff404e74b91769ecfd416f0e012c3c2c",
  "reference_sample_index": 0,
  "source_sample_time_s": 0,
  "source_time_s": 0,
  "generation_method": "deterministic_template_variation_v1",
  "terminal_message_id": null
}
```

同批Robot加工第1秒：

```json
{
  "schema_version": "telemetry.v1",
  "telemetry_id": "TEL-0f0587ea2fbc272efbf6abacfb5eab1a",
  "run_id": "SIM-16fd703a885b47fe8f929107e94aeafa",
  "order_id": "ORD-585f82ea3684401b95f2f14dd8afd87e",
  "work_order_id": "WO-b129387ecee44a8bb268596b0613d4cc",
  "operation_id": "OP-2b21ba3832c944579faac601b7587184",
  "equipment_id": "E-MOVE",
  "device_code": "Robot_01",
  "phase": "normal",
  "rework_id": null,
  "virtual_minute": 10.016666667,
  "elapsed_s": 1,
  "sample_sequence": 1,
  "sample_period_s": 1,
  "protocol": "Modbus-TCP",
  "transport_mode": "local_derived",
  "synthetic": true,
  "is_anomaly": false,
  "metrics": {
    "current_a": 2.439,
    "temperature_c": 28.5,
    "load_percent": 60.7,
    "alarm_code": 0
  },
  "payload": {
    "unit_id": 1,
    "function_code": 3,
    "start_address": 40001,
    "wire_start_address": 0,
    "register_count": 4,
    "raw_registers": [
      2439,
      285,
      607,
      0
    ],
    "request_frame_hex": "00 01 00 00 00 06 01 03 00 00 00 04",
    "response_frame_hex": "00 01 00 00 00 0B 01 03 08 09 87 01 1D 02 5F 00 00"
  },
  "source_file": "D:\\AAA-Myproject\\postgraduate\\Project 2\\.task_work\\p1_local_20261123\\worktree\\minimal_factory\\synthetic_signals\\generated_v1\\robot_normal.jsonl",
  "sample_hash": "090e5e86c80c4c95344280bc2085dfcb49e99b847a79ded41760ec2fc12af42b",
  "reference_sample_index": 0,
  "source_sample_time_s": 0,
  "source_time_s": 0,
  "generation_method": "deterministic_template_variation_v1",
  "terminal_message_id": null
}
```

## 6. 命令、修复与实际测试结果

工作目录为上述隔离工作树的 `minimal_factory`。TEMP/TMP由本机run_tests.py设为每次独立新目录，命令为：

```powershell
& ../.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -p test_continuous_telemetry.py -v
& ../.venv/Scripts/python.exe -X utf8 -B -m unittest discover -s tests -v
```

| 本轮执行 | 实际结果 | 本机证据（相对本轮.task_work） |
|---|---|---|
| 原P1受限专项 | 1方法完成后TestClient停滞，按精确PID停止；退出4294967295，100.228秒，不计通过 | original-special.log / -execution.json |
| 增强断言、修复前 | Ran3，3失败断言：两场景subTest与正常方法；原分页方法通过 | before-fix.log / -execution.json |
| 修复后专项 | 5通过，0失败0错误；unittest1.638秒，墙钟2.473秒 | fixed-special.log / -execution.json |
| 完整回归 | 98通过，0失败0错误；unittest28.636秒，墙钟29.821秒；内含46前端断言 | full.log / -execution.json |
| 自动测试外独立验收 | 三批+P0.4同输入对照+真实HTTP+历史兼容全部通过，进程退出0 | proof-execution.log / proof/evidence.json |

唯一运行时问题是Robot派生工程量比寄存器多出小数精度，例如28.534℃与28.5℃。先用增强断言复现失败，再在make_record内按最终payload反算工程量；既有终态协议/素材/时间/工单状态不变。首次pip安装因网络沙盒失败；同锁定文件受控安装成功。受限测试挂起按包含唯一路径与完整命令的PID35996/36496停止，未停止其他服务。Starlette对httpx的弃用提示保留，未为消除提示随意换依赖。所有失败日志留存。

独立验收命令（同工作目录，输出目录必须不存在）：

```powershell
& ../.venv/Scripts/python.exe -X utf8 -B tools/accept_p1_telemetry.py --source-db ../../proof-source.db --work-order-id WO-b129387ecee44a8bb268596b0613d4cc --output ../../proof --baseline-engine ../../p04_engine.py --legacy-run ../../legacy-copy/SIM-50cb01e187b741adadf46f023144df4f
```

该命令本轮已执行，复现须换全新output。source-db和legacy-run须为独立临时备份；不能填正式数据库或正式历史位置。所有日志、原始证据及DB留本机，不上传GitHub；在线交付源码、测试、验收工具、这一份必要报告及三份摘要。

## 7. 正式数据与用户文件保护

起点核查 `2026-11-23T14:27:30.777987+08:00`；本轮复核 `2026-11-23T14:43:26.451621+08:00`。792个既有文件逐个SHA256差异0，包括正式数据库/备份、合成信号与图像、正式旧SIM批次、此前.task_work历史成果、所有未提交用户文件。原分支/HEAD/暂存区未变，原工作目录业务及文档均未修改；新记录仅在隔离工作树。

正式库SHA256仍 `04097e34e1571f418eb7b6b84da05de2a45e186668293c7ef6ec595a07bf60f8`；只读计数4订单、9工单、3设备、27工序、20事件、收发箱各8，报工/检验/返工/返工动作0；完整性ok、外键0，正式历史仍1批 `SIM-50cb01e187b741adadf46f023144df4f`。最终交付收尾另在本机protection-after.json及最终回复核对。未reset/clean/checkout覆盖、删除用户数据或强推；未修改master。

## 8. 已知限制与停止点

- 当前是已有合成素材的确定性派生，非真实物理采集或校准模型。`sample_hash`是所引用素材行的哈希；中间点以generation_method标明派生，终态直接复用原协议样本。素材秒/上游秒和SimPy分钟明确分开。
- 采样周期固定1虚拟秒，含加工首尾；模板使用正常素材第0行，终态使用原场景配置选择的素材。虚拟分钟保存9位小数。
- 故障在原工序末发现，只有终态标记异常；没有持续故障高保真窗口，也不在停机维修等待期间生成加工遥测。返工phase=repair且有返工号；Vision复检仍是原离散事件。全故障2个CNC/Robot异常点，部分返工为Vision异常，CNC/Robot异常点0。
- sample_sequence在工单/工序/phase/返工号内重置；同设备接续工单可共享边界时间，不能把不同执行段的时间相等当重复采样。未来同一执行段重入的独立执行ID未实现。
- source_file为本机绝对路径；HTTP分页每次顺序扫描JSONL，小批次已验，不宣称大规模实时流性能。现场四协议仍是本地帧，无网络OPC UA/Modbus/MQTT服务。Vision只读PNG及标签回放，无模型/视频。

十项本次验收标准均通过；上述限制明确保留，当前无本轮验收阻塞。未来扩展只登记，不实现。教学停在“SimPy秒→阶段序列→素材来源→JSONL→现有工厂HTTP分页”；用户掌握及导师验收待确认。保留独立工作树和全部产物，验收交付后立即停止开发，等待下一条用户指令。

## 9. 可直接复制给Gemini的反馈Prompt

~~~text
请审阅 Project 2 的 P1 连续合成数据 Windows 本地验收。仅评审，不继续开发。
仓库 zzz22-9/postgraduat_Project2，唯一远程功能分支 feat/p1-continuous-telemetry；先读该分支的 AGENTS.md、md/项目索引.md、md/项目推进日志.md、minimal_factory/当前进度.md 和 docs/P1_连续合成遥测MVP_实现与验收.md，再看 continuous.py、engine.py、service.py、api.py、test_continuous_telemetry.py、tools/accept_p1_telemetry.py。
用户本轮一次授权隔离集成、必要修复、测试验收及提交GitHub，替代早期逐步确认，仅限P1。禁止task21/、Data Connector、真实协议Server/Broker、新数据库、语义层及无关重构。目标已在本地技术验收通过；用户掌握与导师验收仍待确认。
原工作区分支 feat/p04-data-output-contract，HEAD f8547bdca288420a98fd3fc0d18485356151ca99，原有27项未提交路径保持；独立工作树 D:\AAA-Myproject\postgraduate\Project 2\.task_work\p1_local_20261123\worktree，本地分支 codex/p1-local-acceptance-20261123。P1来源 447a5c6d409edbafbe59d9b030066e4ccbeacb40；本轮代码提交 94da92597eef666c7dda9a6a00bff7b56e8537d6，报告和三份摘要的提交以远程该分支最终HEAD及最终回复为准。本轮共两个提交（代码及记录）；不得把父提交当成最终HEAD。不使用被禁的REST分支，不合并master、不强推。
实际本地运行时代码仅 continuous.py 3行修复：Robot工程量由最终量化寄存器反算，消除28.534℃与28.5℃等矛盾。增强原专项并新增真实TCP HTTP和P0.3拒绝终态保护两项，另新增已执行的独立验收脚本；engine/service/api按原P1内容集成，无本轮修改。无新依赖；Python3.14.6，新venv按原requirements-dev安装，环境冻结留本机。
本轮增强断言修复前Ran3、3个失败断言（两场景subTest和正常），原HTTP分页方法通过；修复后专项5/5、2.473秒墙钟，完整98/98、29.821秒墙钟、0失败0错误，内含46前端断言。首次pip网络受限失败；首次受限专项在TestClient停滞，按唯一完整命令确认PID35996/36496后停止，100.228秒、退出4294967295，不计通过。受控复跑全部通过；失败日志未删、未降低断言或改期望。
自动测试外，以正式库只读SQLite备份为临时源，HTTP实际创建三批：normal12分钟/6协议/482遥测；rework17/10/663；all_faults28/10/663，工单2件。CNC正常0–6分钟361点；返工10–13或16–19分钟181点；Robot10–12、15–17、21–23分钟各121点。每段含首尾，1虚拟秒，序号0起；业务ID、phase/返工号、素材行/清单/上游哈希和所有工程量独立核对。全故障仅两个设备终态报警；部分返工是Vision异常，因此CNC/Robot无异常点。同输入同run_id、不同隔离目录与P0.4引擎对照，26协议及26来源逐字节一致，完整summary/events仅移除recorded_at并映射独立返工UUID后相等，资源/延迟/报工/最终完成和P0.3检查保留。
独立实测HTTP http://127.0.0.1:53482，分页limit100分别5/7/7页，与JSONL逐条相等，越界offset为空，负offset、limit0/1001及非整数422，无效/缺失批次404；正式旧历史的临时副本返回available=false且7文件不变；已有result/export正常。服务验收后已关闭，不宣称端口持续在线。新三批27文件在后续批次运行后未覆盖。数据保存在 D:\AAA-Myproject\postgraduate\Project 2\.task_work\p1_local_20261123\proof\runs，证据/日志/临时DB不上传；两条完整实际JSON在报告中，验收工具与必要报告已准备随同分支交付。
正式业务状态核查 2026-11-23T14:43:26.451621+08:00：4订单、9工单、3设备、27工序、20事件、收发各8、1正式历史批次，完整性ok、外键0。792个原有保护文件SHA256差异0（含正式DB、所有素材、历史产物、用户未提交文件）；正式库SHA256 04097e34e1571f418eb7b6b84da05de2a45e186668293c7ef6ec595a07bf60f8，原HEAD/分支/暂存区未变。该状态是本轮核查，最终收尾见最终回复/本机protection-after.json。
素材为已有合成素材的真实文件读取；中间点是确定性数学派生，terminal复用原协议素材；sample_hash是素材原行哈希，不是派生值哈希。OPC UA/Modbus/MQTT/Vision现场报文仍为local_frame，无真实网络协议发布；只有工厂HTTP在验收中真实通信。Vision是PNG及标签回放，无视频/模型。已有故障→延迟/维修/返工/复检/恢复在沙盒实际执行；未实现故障持续窗口的高保真信号，也不在维修停机等待期间采样。采样固定1秒，阶段内序号，不承诺重入同一执行阶段的全局身份。素材绝对路径、模板固定第0正常行、终态按配置选素材、HTTP逐次扫描JSONL的规模限制均明确保留。
请核查十项验收标准和证据边界，指出具体文件/行与未覆盖风险，勿把合成派生说成真实采集，勿把最小故障终态说成故障全窗口模拟。教学停在SimPy秒→逐阶段序列→模板来源→JSONL→工厂分页HTTP。验收后立即停止开发；下一步须用户另行明确授权，此Prompt不授权任何业务操作，也未自动发送Gemini。
~~~

## 10. 原远程实现说明（历史，已由上方本地验收更新）

以下为447a5c6中的原说明，仅保留来源及当时状态；其中未来Connector建议不是本轮授权，本轮不执行。

> # P1 最小连续合成遥测：实现说明与验收边界（2026-11-23）
>
> ## 1. 范围与实际完成
>
> - 在最小工厂已有 SimPy 加工过程内，按 **1 个虚拟秒**产生 CNC/Robot 派生时序记录。该间隔当前在 `simulation/continuous.py` 中固定，**并非可配置**；加工起始和结束点也保留，时长6分钟时共361个端点采样。
> - 新增每批独立 `telemetry.jsonl`，不覆盖现有 `protocol_messages.jsonl`、`source_context.jsonl`、`events.jsonl` 或 `mes.db`。
> - 新增分页读取 `GET /simulation/runs/{run_id}/telemetry?offset=0&limit=500`；单次 `limit` 1..1000，返回 `{run_id,available,total,offset,limit,items,next_offset}`。历史批次无 `telemetry.jsonl` 时返回 `available=false`，不伪造采样。
> - Vision仍为离散PNG/检测结果；原MQTT/OPC UA/Modbus仅本地帧，不包含实际Broker/Server/Client；未开发 Data Connector。
> - 现有工序末协议消息仍按原方式生成，新增遥测不会改变 MES 排产、故障延长和返修复检控制逻辑。
>
> ## 2. 数据含义：严格区分来源与生成方式
>
> - `generation_method=deterministic_template_variation_v1`：在校验哈希的已有正常合成素材样本基础上，通过固定数学扰动构造**派生数值**，不是新增真实传感器观察，也不是经过物理模型校准的动态过程。
> - `generation_method=existing_terminal_protocol_sample`：工序结束点直接使用既有正常/异常协议消息的数值，并通过 `terminal_message_id` 关联该消息。
> - `synthetic=true`、`transport_mode=local_derived` 必填。`protocol` 表示载荷的OPC UA/Modbus风格，**不表示网络传输已经发生**。
> - `sample_hash` 指向模板合成素材的SHA256，并非每个派生值自身的SHA256；要解释完整来源需结合 `generation_method` 和实际模板/终态消息。
> - `virtual_minute` 是SimPy时间，`elapsed_s` 是当前加工阶段内经过的虚拟秒，`source_sample_time_s`/`source_time_s` 是素材内原有时间字段，不能混成现实时间。
> - `sample_sequence` 在每个工序执行阶段（包括返修/复检）的CNC/Robot独立从0递增。消息身份含批次/工单/工序/阶段/返修号/序号；若同一工序阶段被重新执行，需要新增独立阶段执行ID才能确保跨重复执行绝对不冲突。
> - 工序完成时如报告故障，最后一个终态样本才有 `is_anomaly=true`；**尚未实现持续故障窗口的高保真动态模拟**，不能把当前时序数据宣称为物理一致的故障振荡数据。
>
> ## 3. 2.1 Connector 所需示例读取流程
>
> 1. 用 `GET /simulation/runs` 发现批次，确认目标批次存在。
> 2. `GET /simulation/runs/{run_id}/telemetry?offset=0&limit=500` 读取第一页。
> 3. 按 `next_offset` 继续读取，直到返回 `null`。
> 4. 以 `run_id`、`work_order_id`、`operation_id`、`equipment_id`、`phase`、`sample_sequence` 确定上下文；保留完整原始遥测记录，切勿只留测量值。
> 5. 与 `GET /simulation/runs/{run_id}` 中的原始工序末协议消息按 `terminal_message_id` 关联；不可将其视为实时设备OPC UA/Modbus订阅。
>
> ## 4. 测试与限制
>
> 新增 `tests/test_continuous_telemetry.py`：正常6条既有协议消息/12虚拟分钟、三场景时间与返工、1 Hz 时序、元数据、Modbus字节一致性、HTTP分页与旧批次兼容等断言。隔离Python工作副本执行过3项专项测试（通过）；基于2026-11-07压缩包构造的独立副本也执行过原9项现场测试（通过），但**尚未在包含P0.4全部93项测试的远端新分支或用户Windows工作树执行完整回归**。完整验收需在用户项目新建隔离副本及虚拟环境执行：
> ```cmd
> .venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -p test_continuous_telemetry.py -v
> .venv\Scripts\python.exe -X utf8 -B -m unittest discover -s tests -v
> ```
> 注意 Windows 环境特定测试在Linux副本中不能直接作为正式完整回归；不得写入已有正式 `data/erp.db` 或旧仿真批次。
>
> ## 5. 下一步
>
> 不要继续扩展这套源头代码以推迟2.1。先从独立的REST Connector读这一页真实HTTP遥测，再定义内部统一消息V0.1、写入隔离存储和测试。真实MQTT/OPC UA/数据库连接源、连续采样可配置、故障窗口语义及高保真采样后续单独验收。
