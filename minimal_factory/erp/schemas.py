"""订单的数据规则：用户输入什么，接口返回什么。

本文件相当于“订单表单的填写规范”，不负责把订单存进数据库。
输入：用户提交的 JSON。输出：通过校验的 OrderCreate 或 Order 对象。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    """创建订单时，用户填写的内容。"""

    # BaseModel 来自 Pydantic，它会按下面的字段规则检查输入。
    # 去掉字符串首尾空格；拒绝未声明的字段，例如用户自己填写 status。
    # example 只用于接口页面展示，不会自动创建这笔订单。
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "product_id": "P001",
                "product_name": "简易装配件",
                "quantity": 2,
                "due_date": "2026-10-23",
            }
        },
    )

    # str 表示文字，int 表示整数。Field 用来补充长度、范围等约束。
    product_id: str = Field(min_length=1, max_length=50, description="产品编号")
    product_name: str = Field(min_length=1, max_length=100, description="产品名称")
    # gt=0 表示必须大于 0；strict=True 不允许把字符串 "2" 或小数当整数。
    quantity: int = Field(strict=True, gt=0, description="生产数量，必须是正整数")
    # date | None 表示“日期或空值”；default=None 表示用户可以不填写交期。
    due_date: date | None = Field(default=None, description="交期，可不填")


class Order(OrderCreate):
    """已保存的订单：在用户输入之上，增加系统生成的信息。"""

    # 继承 OrderCreate：保留上面的四个字段，再补上下面的系统字段。
    # 响应示例补全编号、状态和时间，便于区分“提交内容”和“保存后的结果”。
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "product_id": "P001",
            "product_name": "简易装配件",
            "quantity": 2,
            "due_date": "2026-10-23",
            "order_id": "ORD-0123456789abcdef0123456789abcdef",
            "status": "pending",
            "created_at": "2026-10-16T06:00:00Z",
        }
    })

    order_id: str
    # 状态由 MES 的事务更新，不能通过创建订单的输入自行指定。
    status: Literal["pending", "dispatched", "in_progress", "completed"] = Field(
        description="待下发 / 已下发 MES / 生产中 / 已完工"
    )
    created_at: datetime = Field(description="创建时间，使用带时区的 UTC 时间")
