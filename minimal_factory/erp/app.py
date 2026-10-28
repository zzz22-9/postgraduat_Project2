"""接口层：把“创建/查询/删除订单”开放给人或后续的 MES 调用。

阅读主线：create_order → database.save_order → 返回保存后的订单。
输入：浏览器或其他程序发来的请求。输出：JSON 格式的订单或错误说明。
本文件组织业务流程，具体的 SQL 语句在 database.py。
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from sqlite3 import IntegrityError
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import FileResponse, Response

from mes.api import create_router
from mes import service as mes_service
from simulation.api import create_router as create_simulation_router
from simulation.service import SimulationService

from . import database
from .schemas import Order, OrderCreate


def create_app(database_path: Path | None = None) -> FastAPI:
    """组装 ERP 应用。测试可传临时数据库路径，避免改动你的练习数据。"""
    # 以代码的位置为基准，所以从不同目录启动也不会误建另一份数据库。
    db_path = database_path or Path(__file__).resolve().parents[1] / "data" / "erp.db"
    simulation = SimulationService(db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 服务开始接收请求之前先准备数据库。已有表不会被清空。
        # yield 是启动准备与停止清理之间的分界；停止时保存并中断未结束的仿真。
        database.initialize(db_path)
        mes_service.initialize(db_path)
        simulation.startup()
        try:
            yield
        finally:
            simulation.shutdown()
    # app实例化
    app = FastAPI(
        title="最小工厂 · ERP 与 MES",
        description=(
            "本地教学系统：订单、工单、工序设备、报工、质量放行与追溯。"
            "创建订单不会自动生产；MES 执行完成并质量放行后反馈 ERP。"
        ),
        version="0.2.0",
        lifespan=lifespan,
    )

    # @app.get / @app.post 是“路由”：把网址与下面的处理函数对应起来。
    # 根网址显示中文操作页面；自动生成的接口文档仍在 /docs。
    #@app.get("/health") 的意思是：
    #如果有人通过 GET 请求访问 /health，就让 FastAPI 调用下面的 health() 函数。
    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "index.html")

    @app.get("/web/simulation.js", include_in_schema=False)
    def simulation_script():
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "simulation.js", media_type="text/javascript")

    @app.get("/web/simulation.css", include_in_schema=False)
    def simulation_styles():
        return FileResponse(Path(__file__).resolve().parents[1] / "web" / "simulation.css", media_type="text/css")

    @app.get("/health", tags=["服务状态"], summary="检查服务是否启动")
    def health():
        return {"status": "ok", "module": "erp+mes", "version": "0.2.0"}

    # POST /orders：创建一笔订单。201 是 HTTP 的“创建成功”状态码。
    # response_model=Order 指定返回结果的数据结构，并生成接口说明。
    @app.post(
        "/orders",
        response_model=Order,
        status_code=status.HTTP_201_CREATED,
        tags=["订单"],
        summary="创建并保存订单",
    )
    def create_order(order_input: OrderCreate):
        # 第 1 步：FastAPI 已按 OrderCreate 检查输入。
        # 如果数量不合格，就返回 422，此函数不会继续执行，数据库也不会新增。
        # 第 2 步：系统补充编号、状态和创建时间。
        order = Order(
            # model_dump() 将输入对象转换成字典；** 把字典各字段展开传入。
            **order_input.model_dump(),
            # uuid4 生成随机唯一标识，避免不同订单使用同一个编号。
            order_id=f"ORD-{uuid4().hex}",
            # 创建订单只登记需求；pending 是待下发，不代表生产已开始。
            status="pending",
            # 统一存 UTC 时间，避免不同机器的本地时区导致时间含义不同。
            created_at=datetime.now(timezone.utc),
        )
        # 第 3 步：保存到数据库。只有保存成功，才返回成功响应。
        database.save_order(db_path, order)
        # FastAPI 把这个对象转换成 JSON，显示在页面的 Response body 里。
        return order

    # GET /orders：查询所有订单，不会新增订单。
    @app.get("/orders", response_model=list[Order], tags=["订单"], summary="查询全部订单")
    def get_orders():
        return database.list_orders(db_path)

    # 大括号表示路径参数，例如 /orders/ORD-xxx 中的编号会交给 order_id。
    @app.get(
        "/orders/{order_id}",
        response_model=Order,
        tags=["订单"],
        summary="按订单编号查询",
    )
    def get_order(order_id: str):
        order = database.find_order(db_path, order_id)
        if order is None:
            # 404 表示“找不到这笔订单”，不是返回一笔空订单或伪造成功。
            raise HTTPException(status_code=404, detail="订单不存在")
        return order

    # DELETE /orders/{order_id}：按完整编号删除一笔订单。
    @app.delete(
        "/orders/{order_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        tags=["订单"],
        summary="按订单编号删除错误订单",
        description="永久删除未关联 MES 记录的订单。已有工单历史的订单禁止删除，以保留追溯。",
        responses={404: {"description": "订单不存在"}, 409: {"description": "订单已有 MES 记录"}},
    )
    def delete_order(order_id: str):
        # 数据库返回是否确实删除了一行；编号不存在时返回 404。
        try:
            deleted = database.delete_order(db_path, order_id)
        except IntegrityError as error:
            raise HTTPException(status_code=409, detail="该订单已有 MES 工单或追溯记录，不能删除") from error
        if not deleted:
            raise HTTPException(status_code=404, detail="订单不存在")
        # 204 表示删除成功，响应不包含 JSON 内容。
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # 两个模块共用一个服务与数据库，启动命令仍然是 erp.app:app。
    app.include_router(create_router(db_path))
    app.include_router(create_simulation_router(simulation))
    app.state.simulation = simulation
    return app


# 启动命令里的 erp.app:app：左边是这个模块，右边是下面的 app 变量。
app = create_app()
