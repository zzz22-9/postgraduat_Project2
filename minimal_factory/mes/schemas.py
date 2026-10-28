"""MES 表单规则。这里只校验输入格式，业务状态检查在 service.py。

例如数量为负数由这里拒绝；尚未开工就报工，由 service.py 拒绝。
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
