"""教学第一步：只读已有仿真实验，打印同一订单的三类原始样本。

不启动服务、不运行仿真、不修改数据库。这里只提取，不做统一表示。
"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


# 1. 固定使用已保存的默认实验；订单编号只在本实验范围内解释。
RUN_ID = "SIM-97bdfc0915964e5ba0b0a33c3466a301"
ORDER_ID = "DEMO-ORD-2"
folder = Path(__file__).resolve().parents[1] / "data" / "erp.simulations" / RUN_ID

# 2. mode=ro 表示只读数据库。execute 的 SELECT 表示查询，不改数据。
with closing(sqlite3.connect((folder / "mes.db").as_uri() + "?mode=ro", uri=True)) as conn:
    conn.row_factory = sqlite3.Row  # 查询结果保留字段名。
    order = dict(conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (ORDER_ID,)
    ).fetchone())
    work_order = dict(conn.execute(
        "SELECT * FROM work_orders WHERE order_id = ?", (ORDER_ID,)
    ).fetchone())
    operations = [dict(row) for row in conn.execute(
        "SELECT * FROM operations WHERE work_order_id = ? ORDER BY sequence",
        (work_order["work_order_id"],),
    )]

# 3. JSONL 文件每一行是一条事件；找出这笔订单的第一条加工开始事件。
events = [json.loads(line) for line in
          (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
event = next(item for item in events
             if item["order_id"] == ORDER_ID and item["event_type"] == "processing_started")

# 4. 包装键是教学分类，里面的字段和值直接来自原始记录。
samples = {
    "experiment_run_id": RUN_ID,
    "erp_order_final_snapshot": order,
    "mes_work_order_final_snapshot": work_order,
    "mes_operations_final_snapshot": operations,
    "simpy_processing_started_event": event,
}
print(json.dumps(samples, ensure_ascii=False, indent=2))

# 注意：数据库是实验结束后的状态；事件是仿真第0分钟发生的历史记录。
# 二者时间不同，不能把 completed 当成第0分钟的状态。
