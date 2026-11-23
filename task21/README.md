# 任务2.1：Data Connector V0.1（第一块，非五层完成）

本目录独立于 `minimal_factory` 生产者。依据《任务2.1制造-企业跨域业务及资源深度感知 软件原型整体设计》，正式目标为至少四种独立连接源：**REST API、MQTT、OPC UA、PostgreSQL/MySQL**。本次仅开始 REST Connector；其他三种尚未实现。

## 当前最小模块

- 文件：`task21/rest_connector.py`。
- 输入：HTTP(S) 基地址、`run_id`、分页大小；也可读取 `GET /erp/feed`。
- 真实传输：Python 标准库 `urllib` 以 HTTP GET 访问目标，读取 JSON，处理 HTTP/网络/格式错误。
- 输出：`connector.v0.1` Python 字典；保留 `raw_data` 原始遥测，增加 `source_system`、`source_protocol=HTTP-REST`、`source_endpoint`、`received_at_utc`、`object_hint` 与 `context`。
- 注意：`raw_data.protocol=OPC-UA` 或 `Modbus-TCP` 仅说明设备数据的本地模拟格式，**本连接器实际使用HTTP**，不冒称完成其他协议网络客户端。
- 解析仍处于Connector统一消息封装，不创建正式 `Equipment` 对象，不写数据库，不运行图谱算法。
- 旧仿真批次无连续数据、网络失败或分页错误会显式报错，不把缺失数据当正常空结果。

## 使用

从仓库根目录、在已启动对应最小工厂应用之后：

```python
from task21.rest_connector import RESTConnector
connector = RESTConnector("http://127.0.0.1:8000")
print(connector.read_erp_feed())
run_id = "<填入实际存在且已生成telemetry的SIM-id>"
for item in connector.iter_telemetry(run_id, page_size=500):
    print(item["context"], item["raw_data"]["metrics"])
```

独立的本地HTTP测试（不使用正式ERP数据库）：

```bash
python -m unittest discover -s task21/tests -v
```

## 下一小块

先做统一内部消息契约的schema及数据库选型/原始与规范化存储设计，再做真正的PostgreSQL/MySQL读取器与MQTT、OPC UA网络读取器。遵守每模块“输入→处理→输出→测试→代码讲解”流程，不复制原工厂生成逻辑。

**验证状态**：已将代码与网络测试文件提交 GitHub 分支；正式用户本机/新分支全套测试尚待运行核验。P1连续遥测的专项样例测试来自隔离旧快照，不等同远端P0.4全量回归。
