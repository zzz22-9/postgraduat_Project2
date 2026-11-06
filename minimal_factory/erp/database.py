"""数据库层：负责订单的持久保存、查询和删除，不负责网页请求。

可以把 orders 表理解为一张表格：每行是一笔订单，每列是一个字段。
save_order 输入订单对象，写入磁盘；find_order 输入编号，返回该行数据。
SQLite 属于 Python 自带功能，不需要另外安装数据库服务器。
"""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from .schemas import Order


@contextmanager
def connect(database_path: Path):
    """每次操作打开一个连接，成功后提交，出错则回滚，最后关闭。"""
    # 连接到指定数据库文件；文件不存在时 SQLite 会创建它。
    connection = sqlite3.connect(database_path, timeout=10)
    # MES 的外键把工单、订单、工序关联起来，防止产生无来源的数据。
    connection.execute("PRAGMA foreign_keys = ON")
    # 让查询结果保留字段名，后面可以转换成 {"order_id": ..., ...} 的字典。
    connection.row_factory = sqlite3.Row
    try:
        # 这层 with 管理事务：正常结束则提交；发生异常则撤销未提交的写入。
        with connection:
            # 把连接交给调用方使用，调用方的 with 结束后再继续执行关闭连接。
            yield connection
    finally:
        connection.close()


def initialize(database_path: Path) -> None:
    """首次启动建立订单表；再次启动保留已有数据。"""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    # IF NOT EXISTS：只在表不存在时创建，所以重启不会丢掉已保存的订单。
    # PRIMARY KEY 限定订单号唯一；NOT NULL 限定必填；CHECK 限定有效值。
    with connect(database_path) as connection:
        old = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='orders'"
        ).fetchone()
        if old and "'dispatched'" not in old["sql"]:
            # SQLite 不能直接修改 CHECK 约束。先备份，再在一个事务内复制并换表。
            # 备份只创建一次，后续启动不会覆盖它；原有订单编号和数据均保留。
            backup_path = database_path.with_name(database_path.stem + '.before-mes.db')
            if not backup_path.exists():
                # sqlite3 的 with 只管理事务；closing 才会实际释放文件句柄。
                with closing(sqlite3.connect(backup_path)) as backup:
                    connection.backup(backup)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE orders_migrated (
                    order_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
                    due_date TEXT, status TEXT NOT NULL
                    CHECK(status IN ('pending','dispatched','in_progress','completed')),
                    created_at TEXT NOT NULL
                )
            """)
            connection.execute("INSERT INTO orders_migrated SELECT * FROM orders")
            connection.execute("DROP TABLE orders")
            connection.execute("ALTER TABLE orders_migrated RENAME TO orders")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                due_date TEXT,
                status TEXT NOT NULL CHECK (status IN ('pending','dispatched','in_progress','completed')),
                created_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS erp_dispatch_outbox (
                message_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL REFERENCES orders(order_id),
                request_json TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                acknowledged_at TEXT
            )
        """)
        if "http_json" not in {row[1] for row in connection.execute("PRAGMA table_info(erp_dispatch_outbox)")}:
            connection.execute("ALTER TABLE erp_dispatch_outbox ADD COLUMN http_json TEXT")


def save_order(database_path: Path, order: Order) -> None:
    """使用参数绑定写入数据：产品名称等输入不会被当成 SQL 执行。"""
    # INSERT INTO 表示新增一行，7 个问号依次对应下方的 7 个字段值。
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO orders
                (order_id, product_id, product_name, quantity, due_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.product_id,
                order.product_name,
                order.quantity,
                # SQLite 中把日期保存成 YYYY-MM-DD；未填写则保存为空值。
                order.due_date.isoformat() if order.due_date else None,
                order.status,
                order.created_at.isoformat(),
            ),
        )


def list_orders(database_path: Path) -> list[dict]:
    """返回订单字典组成的列表，最新创建的订单排在前面。"""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM orders ORDER BY created_at DESC, order_id"
        ).fetchall()
        # SELECT 读取记录，fetchall() 取出全部结果；不修改原来的订单。
        return [dict(row) for row in rows]


def find_order(database_path: Path, order_id: str) -> dict | None:
    """WHERE 按编号筛选；找到则返回字典，找不到则返回 None。"""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def delete_order(database_path: Path, order_id: str) -> bool:
    """按编号删除订单；确实删除一行返回 True，不存在返回 False。"""
    with connect(database_path) as connection:
        # WHERE 限定只删这一笔；参数绑定避免把编号中的文字当成 SQL。
        cursor = connection.execute(
            "DELETE FROM orders WHERE order_id = ?", (order_id,)
        )
        # rowcount 是本次删除的行数；退出 with 后提交，随后返回结果。
        return cursor.rowcount == 1
