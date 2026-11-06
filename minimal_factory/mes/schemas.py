"""MES 表单与 ERP 下发报文规则。业务状态检查在 service.py。

例如数量为负数由这里拒绝；尚未开工就报工，由 service.py 拒绝。
"""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_serializer, model_validator

Kind = Literal["assembly", "inspection", "transport"]


class Input(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class RouteStep(Input):
    name: str = Field(min_length=1, max_length=80)
    kind: Kind
    minutes: int = Field(strict=True, gt=0, le=10000, description="计划每件分钟数，不是实测时间")


class Dispatch(Input):
    order_id: str = Field(min_length=1, max_length=100)
    priority: int = Field(default=2, strict=True, ge=1, le=3)
    planned_date: date | None = None
    # 默认流程是教学案例，用户可在下发时修改，保存后冻结为该工单的工艺快照。
    steps: list[RouteStep] = Field(default_factory=lambda: [
        RouteStep(name="组装", kind="assembly", minutes=3),
        RouteStep(name="过程质检", kind="inspection", minutes=2),
        RouteStep(name="搬运", kind="transport", minutes=1),
    ], min_length=1, max_length=12)


class ERPOrderSnapshot(Input):
    """ERP 在下发时提供的完整订单快照。"""

    order_id: str = Field(min_length=1, max_length=100, description="ERP 来源订单编号")
    product_id: str = Field(min_length=1, max_length=50, description="产品编号")
    product_name: str = Field(min_length=1, max_length=100, description="产品名称")
    quantity: int = Field(strict=True, gt=0, description="本批生产件数")
    due_date: date | None = Field(description="交期，未指定时显式填写 null")
    status: Literal["pending"] = Field(description="ERP 发起下发时的订单状态")
    created_at: AwareDatetime = Field(description="ERP 订单创建时间，必须携带时区")


class ProtocolRouteStep(RouteStep):
    """复用工序名称、类型和每件分钟数，补充明确的执行设备。"""

    equipment_id: str = Field(min_length=1, max_length=60, description="MES 设备编号，如 E-ASM")


class DispatchPayload(Input):
    """下发载荷，将订单快照和本次工艺路线放在一起。"""

    order: ERPOrderSnapshot
    # 列表顺序就是工序顺序，由 ERP 显式填写每道工序和设备。
    route: list[ProtocolRouteStep] = Field(min_length=1, max_length=12)
    priority: int = Field(default=2, strict=True, ge=1, le=3, description="排产优先级，1 最高")
    planned_date: date | None = Field(default=None, description="计划生产日期")


class NativeDispatchPayload(Input):
    """IT 层原生订单 JSON；属性仅供 MES 内部兼容已有建单逻辑。"""

    order_id: str = Field(min_length=1, max_length=100)
    product_code: str = Field(min_length=1, max_length=50)
    product_name: str = Field(min_length=1, max_length=100)
    qty: int = Field(strict=True, gt=0)
    due_date: date | None
    created_at: AwareDatetime
    route_steps: list[ProtocolRouteStep] = Field(min_length=1, max_length=12)
    priority: int = Field(default=2, strict=True, ge=1, le=3)
    planned_date: date | None = None

    @property
    def order(self):
        return ERPOrderSnapshot(order_id=self.order_id, product_id=self.product_code,
                                product_name=self.product_name, quantity=self.qty,
                                due_date=self.due_date, created_at=self.created_at, status="pending")

    @property
    def route(self):
        return self.route_steps


class DispatchHTTP(Input):
    url: str
    method: Literal["POST"] = "POST"
    request_headers: dict[str, str] = Field(default_factory=lambda: {"Content-Type": "application/json"})


class DispatchEnvelope(Input):
    """项目自定义的 ISA95-Lite-v1 精简下发契约，使用 HTTP REST 传输 JSON。"""

    protocol: Literal["HTTP-REST", "HTTP-REST/ISA95-Lite-v1"]
    message_id: str = Field(min_length=1, max_length=100, description="本次下发的消息编号，重试沿用")
    timestamp: AwareDatetime = Field(description="ERP 封装报文的时间，必须携带时区")
    source: Literal["ERP"]
    target: Literal["MES"]
    action: Literal["DISPATCH_ORDER"]
    payload: NativeDispatchPayload | DispatchPayload
    http: DispatchHTTP | None = None

    @model_serializer(mode="wrap")
    def serialize_envelope(self, handler):
        value = handler(self)
        if self.http is None:
            value.pop("http", None)
        return value


class ERPDispatchRequest(Dispatch):
    """网页提交给 ERP 的下发指令，同一弹窗重试沿用消息编号。"""

    message_id: str = Field(min_length=1, max_length=100)


class Plan(Input):
    priority: int = Field(strict=True, ge=1, le=3)
    planned_date: date | None = None


class Assignment(Input):
    equipment_id: str = Field(min_length=1, max_length=60)


class EquipmentCreate(Input):
    name: str = Field(min_length=1, max_length=80)
    kind: Kind


class EquipmentState(Input):
    status: Literal["available", "maintenance"]
    reason: str = Field(min_length=1, max_length=300)


class Note(Input):
    operator: str = Field(min_length=1, max_length=50)
    note: str = Field(min_length=1, max_length=300)


class Report(Input):
    # 同一请求重试时沿用此编号，避免网络重试重复增加产量。
    request_id: str = Field(min_length=8, max_length=80)
    operator: str = Field(min_length=1, max_length=50)
    good_quantity: int = Field(strict=True, ge=0)
    rework_quantity: int = Field(default=0, strict=True, ge=0)
    note: str = Field(default="", max_length=300)
    rework_operation_id: str | None = None

    @model_validator(mode="after")
    def check_quantities(self):
        if self.good_quantity + self.rework_quantity == 0:
            raise ValueError("合格数量与待返工数量不能同时为 0")
        if self.rework_quantity and not self.note:
            raise ValueError("有待返工数量时必须填写原因")
        return self


class Inspection(Input):
    request_id: str = Field(min_length=8, max_length=80)
    operator: str = Field(min_length=1, max_length=50)
    rework_quantity: int = Field(strict=True, ge=0, description="整批检验中的返工数量；0 表示全部合格")
    note: str = Field(default="", max_length=300)
    rework_operation_id: str | None = None

    @model_validator(mode="after")
    def check_reason(self):
        if self.rework_quantity and not self.note:
            raise ValueError("质检不合格必须填写原因")
        return self


class ReworkAction(Note):
    note: str = Field(default="", max_length=300)
    request_id: str = Field(min_length=8, max_length=80)
    equipment_id: str | None = None
    passed: bool | None = Field(default=None, strict=True)
