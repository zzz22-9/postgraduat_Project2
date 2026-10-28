"""MES 的业务与存储：订单 → 工单 → 工序执行 → 质量放行 → ERP 完工。

每个写操作都使用 BEGIN IMMEDIATE：先取得 SQLite 写锁，再检查状态，
最后一起提交工单、设备占用、订单状态和追溯事件。中途失败会全部回滚。
因此两个请求不能同时占用同一设备，也不能重复下发同一笔订单。
工序整批顺序流转；这里记录人工操作，不模拟物理设备或虚拟时间。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from erp.database import connect


class BusinessError(Exception):
    """可向使用者解释的错误；api.py 把它转换成 404 或 409。"""

    def __init__(self, message, code=409):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}-{uuid4().hex}"


@contextmanager
def transaction(path):
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        yield conn


def initialize(path):
    """建立 MES 表，并准备三台明确标注为教学用途的设备；不会创建订单。"""
    with connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS equipment (
                equipment_id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK(kind IN ('assembly','inspection','transport')),
                status TEXT NOT NULL CHECK(status IN ('available','maintenance')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_orders (
                work_order_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                status TEXT NOT NULL CHECK(status IN
                    ('draft','released','in_progress','awaiting_quality','completed','cancelled')),
                priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 3),
                planned_date TEXT, created_at TEXT NOT NULL,
                released_at TEXT, completed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_work_order
                ON work_orders(order_id) WHERE status != 'cancelled';
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
                sequence INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
                minutes INTEGER NOT NULL CHECK(minutes > 0),
                equipment_id TEXT REFERENCES equipment(equipment_id),
                status TEXT NOT NULL CHECK(status IN ('pending','running','paused','completed')),
                good_quantity INTEGER NOT NULL DEFAULT 0 CHECK(good_quantity >= 0),
                rework_attempts INTEGER NOT NULL DEFAULT 0 CHECK(rework_attempts >= 0),
                started_at TEXT, completed_at TEXT,
                UNIQUE(work_order_id, sequence)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_busy_operation
                ON operations(equipment_id) WHERE status IN ('running','paused');
            CREATE TABLE IF NOT EXISTS production_reports (
                report_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                operator TEXT NOT NULL, good_quantity INTEGER NOT NULL,
                rework_quantity INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inspections (
                inspection_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
                operator TEXT NOT NULL, accepted_quantity INTEGER NOT NULL,
                rework_quantity INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                order_id TEXT REFERENCES orders(order_id),
                work_order_id TEXT REFERENCES work_orders(work_order_id),
                operation_id TEXT REFERENCES operations(operation_id),
                equipment_id TEXT REFERENCES equipment(equipment_id),
                action TEXT NOT NULL, operator TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_by_work_order ON events(work_order_id, event_id);
            CREATE TABLE IF NOT EXISTS reworks (
                rework_id TEXT PRIMARY KEY,
                work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
                source_operation_id TEXT REFERENCES operations(operation_id),
                target_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('pending','repairing','awaiting_reinspection','inspecting','completed')),
                equipment_id TEXT REFERENCES equipment(equipment_id),
                cycle INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rework_actions (
                request_id TEXT PRIMARY KEY,
                rework_id TEXT NOT NULL REFERENCES reworks(rework_id),
                action TEXT NOT NULL, operator TEXT NOT NULL, note TEXT NOT NULL,
                equipment_id TEXT, passed INTEGER, created_at TEXT NOT NULL
            );
        """)
        # Additive migration: historical reports remain unchanged; new routing is explicit.
        for table in ("production_reports", "inspections"):
            if "rework_operation_id" not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN rework_operation_id TEXT REFERENCES operations(operation_id)")
        for equipment_id, name, kind in [
            ("E-ASM", "教学组装工位", "assembly"),
            ("E-QC", "教学质检工位", "inspection"),
            ("E-MOVE", "教学搬运资源", "transport"),
        ]:
            conn.execute(
                "INSERT OR IGNORE INTO equipment VALUES (?, ?, ?, 'available', ?)",
                (equipment_id, name, kind, now()),
            )


def require(condition, message, code=409):
    if not condition:
        raise BusinessError(message, code)


def row(conn, sql, args=()):
    result = conn.execute(sql, args).fetchone()
    return dict(result) if result is not None else None


def rows(conn, sql, args=()):
    return [dict(item) for item in conn.execute(sql, args).fetchall()]


def work_order(conn, work_order_id):
    result = row(conn, """
        SELECT w.*, o.product_id, o.product_name, o.quantity, o.due_date,
               o.status AS order_status
        FROM work_orders w JOIN orders o ON o.order_id=w.order_id
        WHERE w.work_order_id=?
    """, (work_order_id,))
    require(result is not None, "工单不存在", 404)
    return result


def operation(conn, operation_id):
    result = row(conn, "SELECT * FROM operations WHERE operation_id=?", (operation_id,))
    require(result is not None, "工序不存在", 404)
    return result


def equipment(conn, equipment_id):
    result = row(conn, "SELECT * FROM equipment WHERE equipment_id=?", (equipment_id,))
    require(result is not None, "设备不存在", 404)
    return result


def log(conn, action, detail, wo=None, op=None, equipment_id=None, operator="计划员"):
    # 同一条事件保留所有关联编号，后续研究可直接据此关联不同来源的数据。
    conn.execute("""
        INSERT INTO events(created_at,order_id,work_order_id,operation_id,equipment_id,action,operator,detail)
        VALUES (?,?,?,?,?,?,?,?)
    """, (now(), wo["order_id"] if wo else None, wo["work_order_id"] if wo else None,
          op["operation_id"] if op else None,
          equipment_id or (op["equipment_id"] if op else None), action, operator, detail))


def detail(conn, work_order_id):
    result = work_order(conn, work_order_id)
    result["operations"] = rows(conn, """
        SELECT op.*, e.name AS equipment_name FROM operations op
        LEFT JOIN equipment e ON e.equipment_id=op.equipment_id
        WHERE work_order_id=? ORDER BY sequence
    """, (work_order_id,))
    result["reports"] = rows(conn, """
        SELECT r.*, op.sequence, op.name AS operation_name
        FROM production_reports r JOIN operations op ON op.operation_id=r.operation_id
        WHERE op.work_order_id=? ORDER BY r.created_at, r.report_id
    """, (work_order_id,))
    result["inspections"] = rows(conn,
        "SELECT * FROM inspections WHERE work_order_id=? ORDER BY created_at, inspection_id", (work_order_id,))
    result["events"] = rows(conn,
        "SELECT * FROM events WHERE work_order_id=? ORDER BY event_id", (work_order_id,))
    result["reworks"] = rows(conn, """
        SELECT r.*, t.name AS target_name, t.kind AS target_kind,
               s.name AS source_name, e.name AS equipment_name
        FROM reworks r JOIN operations t ON t.operation_id=r.target_operation_id
        LEFT JOIN operations s ON s.operation_id=r.source_operation_id
        LEFT JOIN equipment e ON e.equipment_id=r.equipment_id
        WHERE r.work_order_id=? ORDER BY r.created_at, r.rework_id
    """, (work_order_id,))
    result["rework_actions"] = rows(conn, """
        SELECT a.* FROM rework_actions a JOIN reworks r ON r.rework_id=a.rework_id
        WHERE r.work_order_id=? ORDER BY a.created_at, a.request_id
    """, (work_order_id,))
    total = result["quantity"] * len(result["operations"])
    result["progress"] = round(sum(op["good_quantity"] for op in result["operations"]) / total * 100)
    return result


def get_work_order(path, work_order_id):
    with connect(path) as conn:
        conn.execute("BEGIN")  # 一次详情里的多个查询读取同一份快照。
        return detail(conn, work_order_id)


def list_work_orders(path):
    with connect(path) as conn:
        return rows(conn, """
            SELECT w.*, o.product_id,o.product_name,o.quantity,o.due_date,
                   ROUND(100.0*SUM(op.good_quantity)/(o.quantity*COUNT(op.operation_id))) AS progress
            FROM work_orders w JOIN orders o ON o.order_id=w.order_id
            JOIN operations op ON op.work_order_id=w.work_order_id
            GROUP BY w.work_order_id
            ORDER BY w.priority, w.planned_date IS NULL, w.planned_date, w.created_at DESC
        """)


def list_equipment(path):
    with connect(path) as conn:
        return rows(conn, """
            SELECT e.*, op.operation_id AS occupied_by, op.work_order_id,
                   op.name AS operation_name, op.status AS operation_status
            FROM equipment e LEFT JOIN (
                SELECT equipment_id, operation_id, work_order_id, name, status
                FROM operations WHERE status IN ('running','paused')
                UNION ALL
                SELECT r.equipment_id, r.rework_id, r.work_order_id,
                    CASE WHEN r.status='repairing' THEN '返修：' ELSE '复检：' END || t.name, r.status
                FROM reworks r JOIN operations t ON t.operation_id=r.target_operation_id
                WHERE r.status IN ('repairing','inspecting')
            ) op ON op.equipment_id=e.equipment_id
            ORDER BY e.created_at,e.equipment_id
        """)


def dispatch(path, data):
    with transaction(path) as conn:
        order = row(conn, "SELECT * FROM orders WHERE order_id=?", (data.order_id,))
        require(order is not None, "来源订单不存在", 404)
        require(order["status"] == "pending", "这笔订单已经下发，不能重复生成工单")
        wo_id = new_id("WO")
        conn.execute("""
            INSERT INTO work_orders(work_order_id,order_id,status,priority,planned_date,created_at)
            VALUES (?,?,'draft',?,?,?)
        """, (wo_id, data.order_id, data.priority, str(data.planned_date) if data.planned_date else None, now()))
        for sequence, step in enumerate(data.steps, start=1):
            conn.execute("""
                INSERT INTO operations(operation_id,work_order_id,sequence,name,kind,minutes,status)
                VALUES (?,?,?,?,?,?,'pending')
            """, (new_id("OP"), wo_id, sequence, step.name, step.kind, step.minutes))
        conn.execute("UPDATE orders SET status='dispatched' WHERE order_id=?", (data.order_id,))
        wo = work_order(conn, wo_id)
        log(conn, "dispatched", f"ERP 订单下发；建立 {len(data.steps)} 道工序", wo)
        return detail(conn, wo_id)


def update_plan(path, wo_id, data):
    with transaction(path) as conn:
        wo = work_order(conn, wo_id)
        require(wo["status"] in ("draft", "released"), "开工后不能修改生产计划")
        conn.execute("UPDATE work_orders SET priority=?,planned_date=? WHERE work_order_id=?",
                     (data.priority, str(data.planned_date) if data.planned_date else None, wo_id))
        log(conn, "plan_updated", f"优先级 {data.priority}；计划日期 {data.planned_date or '未指定'}", wo)
        return detail(conn, wo_id)


def assign(path, op_id, data):
    with transaction(path) as conn:
        op = operation(conn, op_id)
        wo = work_order(conn, op["work_order_id"])
        require(wo["status"] not in ("completed", "cancelled"), "已关闭工单不能分配设备")
        require(op["status"] == "pending", "只能为尚未执行的工序分配设备")
        eq = equipment(conn, data.equipment_id)
        require(eq["status"] == "available", "设备处于维护状态")
        require(eq["kind"] == op["kind"], "设备类型不符合工序要求")
        conn.execute("UPDATE operations SET equipment_id=? WHERE operation_id=?", (data.equipment_id, op_id))
        log(conn, "assigned", f"工序 {op['sequence']} 分配给 {eq['name']}", wo, op, data.equipment_id)
        return detail(conn, wo["work_order_id"])


def release(path, wo_id):
    with transaction(path) as conn:
        wo = work_order(conn, wo_id)
        require(wo["status"] == "draft", "只有待排产工单可以下达执行")
        ops = rows(conn, "SELECT * FROM operations WHERE work_order_id=?", (wo_id,))
        for op in ops:
            require(op["equipment_id"], "请先给每道工序分配设备")
            eq = equipment(conn, op["equipment_id"])
            require(eq["status"] == "available", f"{eq['name']} 正在维护")
        conn.execute("UPDATE work_orders SET status='released',released_at=? WHERE work_order_id=?", (now(), wo_id))
        log(conn, "released", "计划已下达，可以从第一道工序开工", wo)
        return detail(conn, wo_id)


def cancel(path, wo_id, data):
    with transaction(path) as conn:
        wo = work_order(conn, wo_id)
        started = row(conn, "SELECT 1 FROM operations WHERE work_order_id=? AND started_at IS NOT NULL", (wo_id,))
        require(wo["status"] in ("draft", "released") and not started, "仅允许撤回尚未开工的工单")
        conn.execute("UPDATE work_orders SET status='cancelled' WHERE work_order_id=?", (wo_id,))
        conn.execute("UPDATE orders SET status='pending' WHERE order_id=?", (wo["order_id"],))
        log(conn, "cancelled", data.note, wo, operator=data.operator)
        return detail(conn, wo_id)


def execute_operation(path, op_id, action, data):
    with transaction(path) as conn:
        op = operation(conn, op_id)
        wo = work_order(conn, op["work_order_id"])
        require(wo["status"] in ("released", "in_progress"), "工单尚未下达或已经结束，不能执行工序")
        require(not active_rework(conn, wo["work_order_id"]), "请先完成返修任务及复检，再继续正常工序")
        if action == "start":
            require(op["status"] == "pending", "只有待执行工序可以开工")
            earlier = row(conn, """
                SELECT 1 FROM operations WHERE work_order_id=? AND sequence<? AND status!='completed'
            """, (wo["work_order_id"], op["sequence"]))
            require(not earlier, "前一道工序尚未完成，不能越序开工")
            require(op["equipment_id"], "请先分配设备")
            eq = equipment(conn, op["equipment_id"])
            require(eq["status"] == "available", "设备正在维护，不能开工")
            busy = equipment_busy(conn, op["equipment_id"])
            require(not busy, "设备已被其他工序占用，请等待该工序完成")
            conn.execute("UPDATE operations SET status='running',started_at=COALESCE(started_at,?) WHERE operation_id=?", (now(), op_id))
            conn.execute("UPDATE work_orders SET status='in_progress' WHERE work_order_id=?", (wo["work_order_id"],))
            conn.execute("UPDATE orders SET status='in_progress' WHERE order_id=?", (wo["order_id"],))
        elif action == "pause":
            require(op["status"] == "running", "只有执行中的工序可以暂停")
            conn.execute("UPDATE operations SET status='paused' WHERE operation_id=?", (op_id,))
        elif action == "resume":
            require(op["status"] == "paused", "只有已暂停的工序可以恢复")
            conn.execute("UPDATE operations SET status='running' WHERE operation_id=?", (op_id,))
        else:
            raise BusinessError("未知的工序操作", 404)
        log(conn, action, data.note, wo, op, operator=data.operator)
        return detail(conn, wo["work_order_id"])


def same_request(existing, data, fields):
    # 已处理的报工请求只能按原内容重试；相同编号却修改数量必须报错。
    require(all(existing[key] == getattr(data, key) for key in fields), "请求编号已被其他内容使用，请刷新后重新操作")


def active_rework(conn, wo_id):
    return row(conn, "SELECT 1 FROM reworks WHERE work_order_id=? AND status!='completed'", (wo_id,))


def equipment_busy(conn, eq_id):
    return row(conn, """
        SELECT 1 FROM operations WHERE equipment_id=? AND status IN ('running','paused')
        UNION ALL
        SELECT 1 FROM reworks WHERE equipment_id=? AND status IN ('repairing','inspecting')
        LIMIT 1
    """, (eq_id, eq_id))


def create_rework(conn, wo, source, data):
    require(data.rework_operation_id, "质检不合格必须指定返回哪道工序返修")
    target = operation(conn, data.rework_operation_id)
    require(target["work_order_id"] == wo["work_order_id"], "返修工序必须属于本工单")
    require(target["kind"] != "inspection" and target["status"] == "completed",
            "请选择已经完成的加工或搬运工序作为返修目标")
    require(source is None or target["sequence"] < source["sequence"], "只能返回当前质检之前的工序")
    require(not active_rework(conn, wo["work_order_id"]), "本工单已有未完成的返修任务")
    rework_id = new_id("RW")
    conn.execute("""
        INSERT INTO reworks(rework_id,work_order_id,source_operation_id,target_operation_id,
                            quantity,reason,status,created_at)
        VALUES (?,?,?,?,?,?,'pending',?)
    """, (rework_id, wo["work_order_id"], source["operation_id"] if source else None,
          target["operation_id"], data.rework_quantity, data.note, now()))
    log(conn, "rework_created", f"返修任务 {rework_id}：{data.rework_quantity} 件返回 {target['name']}；{data.note}",
        wo, target, operator=data.operator)


def execute_rework(path, rework_id, action, data):
    """A repair and its reinspection reserve the same resources as normal operations.

    One failed lot is handled together. Reinspection failure repeats the repair cycle;
    only a passed reinspection credits the source inspection, exactly once.
    """
    with transaction(path) as conn:
        task = row(conn, "SELECT * FROM reworks WHERE rework_id=?", (rework_id,))
        require(task is not None, "返修任务不存在", 404)
        wo = work_order(conn, task["work_order_id"])
        existing = row(conn, "SELECT * FROM rework_actions WHERE request_id=?", (data.request_id,))
        if existing:
            require(existing["rework_id"] == rework_id and existing["action"] == action, "请求编号已用于其他返修操作")
            same_request(existing, data, ["operator", "note", "equipment_id", "passed"])
            return detail(conn, wo["work_order_id"])
        require(wo["status"] == "in_progress", "工单不在生产中，不能执行返修")
        target = operation(conn, task["target_operation_id"])
        transitions = {"start": ("pending", "repairing"),
                       "finish": ("repairing", "awaiting_reinspection"),
                       "inspect-start": ("awaiting_reinspection", "inspecting"),
                       "inspect-finish": ("inspecting", "completed" if data.passed else "pending")}
        require(action in transitions, "未知返修操作", 404)
        previous, next_status = transitions[action]
        require(task["status"] == previous, "返修状态已变化，请刷新页面后按步骤操作")
        require(action == "inspect-finish" or data.passed is None, "仅复检结果操作可以提交合格判定")
        eq_id = task["equipment_id"]
        if action in ("start", "inspect-start"):
            require(data.equipment_id, "请指定本次使用的设备")
            eq = equipment(conn, data.equipment_id)
            kind = target["kind"] if action == "start" else "inspection"
            require(eq["kind"] == kind, "设备类型不符合返修或复检要求")
            require(eq["status"] == "available", "设备正在维护")
            require(not equipment_busy(conn, eq["equipment_id"]), "设备已被其他工序或返修任务占用，请等待释放")
            eq_id = eq["equipment_id"]
        else:
            require(data.equipment_id is None, "结束操作不能更换正在使用的设备")
        if action == "inspect-finish":
            require(data.passed is not None, "请明确填写复检是否全部合格")
            require(data.passed or data.note.strip(), "复检不合格必须填写原因")
        conn.execute("""UPDATE reworks SET status=?, equipment_id=?, completed_at=?, cycle=cycle+?
                        WHERE rework_id=?""", (
            next_status, eq_id if next_status in ("repairing", "inspecting") else None,
            now() if next_status == "completed" else None,
            int(action == "inspect-finish" and not data.passed), rework_id))
        conn.execute("INSERT INTO rework_actions VALUES (?,?,?,?,?,?,?,?)", (
            data.request_id, rework_id, action, data.operator, data.note, data.equipment_id, data.passed, now()))
        labels = {"start": "返修开工", "finish": "返修完成，释放设备，等待复检",
                  "inspect-start": "复检开工", "inspect-finish": "复检通过" if data.passed else "复检失败，释放设备，整批返修件再次返修"}
        log(conn, "rework_" + action.replace("-", "_"),
            f"{rework_id} · 第 {task['cycle']} 轮 · {task['quantity']} 件 · {labels[action]}；{data.note}",
            wo, target, equipment_id=eq_id, operator=data.operator)
        if next_status == "completed":
            if task["source_operation_id"]:
                source = operation(conn, task["source_operation_id"])
                good = source["good_quantity"] + task["quantity"]
                require(good <= wo["quantity"], "复检数量超过工单数量")
                conn.execute("UPDATE operations SET good_quantity=?,status=?,completed_at=? WHERE operation_id=?", (
                    good, "completed" if good == wo["quantity"] else "pending",
                    now() if good == wo["quantity"] else None, source["operation_id"]))
            unfinished = row(conn, "SELECT 1 FROM operations WHERE work_order_id=? AND status!='completed'", (wo["work_order_id"],))
            if not unfinished:
                conn.execute("UPDATE work_orders SET status='awaiting_quality' WHERE work_order_id=?", (wo["work_order_id"],))
        return detail(conn, wo["work_order_id"])


def report(path, op_id, data):
    with transaction(path) as conn:
        op = operation(conn, op_id)
        wo = work_order(conn, op["work_order_id"])
        existing = row(conn, "SELECT * FROM production_reports WHERE request_id=?", (data.request_id,))
        if existing:
            require(existing["operation_id"] == op_id, "请求编号已用于另一道工序")
            same_request(existing, data, ["operator", "good_quantity", "rework_quantity", "note", "rework_operation_id"])
            return detail(conn, wo["work_order_id"])
        require(op["status"] == "running" and wo["status"] == "in_progress", "工序必须处于执行中才能报工")
        require(not active_rework(conn, wo["work_order_id"]), "请先完成返修与复检")
        require(not data.rework_operation_id or (op["kind"] == "inspection" and data.rework_quantity),
                "只有质检发现不合格时才选择返回工序；其他工序的返工在本工位处理")
        remaining = wo["quantity"] - op["good_quantity"]
        require(data.good_quantity + data.rework_quantity <= remaining, f"本次报工总数不能超过剩余的 {remaining} 件")
        if op["kind"] == "inspection" and data.rework_quantity:
            create_rework(conn, wo, op, data)
        conn.execute("""
            INSERT INTO production_reports VALUES (?,?,?,?,?,?,?,?,?)
        """, (new_id("RPT"), data.request_id, op_id, data.operator, data.good_quantity, data.rework_quantity, data.note, now(), data.rework_operation_id))
        good = op["good_quantity"] + data.good_quantity
        finished = good == wo["quantity"]
        conn.execute("""
            UPDATE operations SET good_quantity=?,rework_attempts=rework_attempts+?,status=?,completed_at=?
            WHERE operation_id=?
        """, (good, data.rework_quantity, "completed" if finished else "running", now() if finished else None, op_id))
        log(conn, "reported", f"合格 {data.good_quantity} 件，待返工 {data.rework_quantity} 件；{data.note}", wo, op, operator=data.operator)
        if op["kind"] == "inspection" and data.rework_quantity:
            conn.execute("UPDATE operations SET status='pending' WHERE operation_id=?", (op_id,))
            log(conn, "inspection_held", "不合格件进入返修，释放质检设备；正常流转等待复检", wo, op, operator=data.operator)
        if finished:
            log(conn, "operation_completed", "本工序合格数量已达计划，释放设备", wo, op, operator=data.operator)
            unfinished = row(conn, "SELECT 1 FROM operations WHERE work_order_id=? AND status!='completed'", (wo["work_order_id"],))
            if not unfinished:
                conn.execute("UPDATE work_orders SET status='awaiting_quality' WHERE work_order_id=?", (wo["work_order_id"],))
                log(conn, "awaiting_quality", "全部工序完成，等待最终整批质量放行", wo)
        return detail(conn, wo["work_order_id"])


def inspect(path, wo_id, data):
    with transaction(path) as conn:
        wo = work_order(conn, wo_id)
        existing = row(conn, "SELECT * FROM inspections WHERE request_id=?", (data.request_id,))
        if existing:
            require(existing["work_order_id"] == wo_id, "请求编号已用于另一张工单")
            same_request(existing, data, ["operator", "rework_quantity", "note", "rework_operation_id"])
            return detail(conn, wo_id)
        require(wo["status"] == "awaiting_quality", "全部工序完成后才能进行最终质检")
        require(not active_rework(conn, wo_id), "返修及复检尚未完成，不能放行")
        require(data.rework_quantity or not data.rework_operation_id, "整批通过时不应指定返修工序")
        require(data.rework_quantity <= wo["quantity"], "返工数量不能超过计划数量")
        if data.rework_quantity:
            create_rework(conn, wo, None, data)
        conn.execute("INSERT INTO inspections VALUES (?,?,?,?,?,?,?,?,?)", (
            new_id("QC"), data.request_id, wo_id, data.operator,
            wo["quantity"] - data.rework_quantity, data.rework_quantity, data.note, now(), data.rework_operation_id))
        if data.rework_quantity:
            conn.execute("UPDATE work_orders SET status='in_progress' WHERE work_order_id=?", (wo_id,))
        else:
            conn.execute("UPDATE work_orders SET status='completed',completed_at=? WHERE work_order_id=?", (now(), wo_id))
            conn.execute("UPDATE orders SET status='completed' WHERE order_id=?", (wo["order_id"],))
            log(conn, "completed", f"整批 {wo['quantity']} 件质量放行，ERP 订单同步完工；{data.note}", wo, operator=data.operator)
        return detail(conn, wo_id)


def create_equipment(path, data):
    with transaction(path) as conn:
        require(not row(conn, "SELECT 1 FROM equipment WHERE name=?", (data.name,)), "设备名称已存在")
        eq_id = new_id("EQ")
        conn.execute("INSERT INTO equipment VALUES (?,?,?,'available',?)", (eq_id, data.name, data.kind, now()))
        log(conn, "equipment_created", f"登记设备：{data.name}", equipment_id=eq_id)
        return equipment(conn, eq_id)


def set_equipment_state(path, eq_id, data):
    with transaction(path) as conn:
        eq = equipment(conn, eq_id)
        busy = equipment_busy(conn, eq_id)
        require(not busy, "设备仍被执行中或暂停的工序占用，不能改变维护状态")
        require(eq["status"] != data.status, "设备已经处于此状态")
        conn.execute("UPDATE equipment SET status=? WHERE equipment_id=?", (data.status, eq_id))
        log(conn, "equipment_state", f"{eq['status']} → {data.status}；{data.reason}", equipment_id=eq_id)
        return equipment(conn, eq_id)


def export_trace(path, wo_id):
    result = get_work_order(path, wo_id)
    # 原始字段与关联编号完整保留，适合作为后续数据统一表示的输入样本。
    return {"format_version": 1, "source": "local_mes", "exported_at": now(), "work_order": result}


def dashboard(path):
    with connect(path) as conn:
        conn.execute("BEGIN")
        counts = {item["status"]: item["n"] for item in rows(conn, "SELECT status,COUNT(*) AS n FROM work_orders GROUP BY status")}
        order_count = row(conn, "SELECT COUNT(*) AS n FROM orders WHERE status='pending'")["n"]
        completed = row(conn, """
            SELECT COALESCE(SUM(o.quantity),0) AS n FROM work_orders w
            JOIN orders o ON o.order_id=w.order_id WHERE w.status='completed'
        """)["n"]
        busy = row(conn, """SELECT
            (SELECT COUNT(*) FROM operations WHERE status IN ('running','paused')) +
            (SELECT COUNT(*) FROM reworks WHERE status IN ('repairing','inspecting')) AS n""")["n"]
        return {"counts": counts, "pending_orders": order_count, "completed_quantity": completed,
                "busy_equipment": busy,
                "recent_events": rows(conn, "SELECT * FROM events ORDER BY event_id DESC LIMIT 20")}
