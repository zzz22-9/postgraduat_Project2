from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Minutes = Annotated[int, Field(strict=True, gt=0, le=10000)]


class Failure(Input):
    inspection_operation_id: str = Field(min_length=1, max_length=100)
    target_operation_id: str = Field(min_length=1, max_length=100)
    quantity: PositiveInt


class FieldScenario(Input):
    """本地素材回放配置；正常模式不引入额外耗时。"""
    mode: Literal["normal", "anomaly", "sampled"] = "normal"
    anomaly_equipment_ids: list[Literal["E-ASM", "E-QC", "E-MOVE"]] = Field(
        default_factory=lambda: ["E-ASM", "E-QC", "E-MOVE"], max_length=3)
    sample_index: Annotated[int, Field(strict=True, ge=0, le=11)] = 0
    seed: Annotated[int, Field(strict=True, ge=0)] = 0
    anomaly_probability: float = Field(default=.5, ge=0, le=1, allow_inf_nan=False)
    cnc_repair_minutes: Minutes = 6
    robot_repair_minutes: Minutes = 5


class Override(Input):
    quantity: PositiveInt | None = None
    minutes: dict[str, Minutes] = Field(default_factory=dict, max_length=12)
    failure: Failure | None = None
    field_protocols: FieldScenario | None = None


class RunRequest(Input):
    source: Literal["demo", "work_orders", "replay"] = "demo"
    work_order_ids: list[str] = Field(default_factory=list, max_length=50)
    replay_run_id: str | None = None
    overrides: dict[str, Override] = Field(default_factory=dict, max_length=50)

    @model_validator(mode="after")
    def source_fields(self):
        if self.source == "work_orders":
            if not self.work_order_ids or len(set(self.work_order_ids)) != len(self.work_order_ids):
                raise ValueError("请选择 1—50 张不同的工单")
        elif self.work_order_ids:
            raise ValueError("只有现有工单来源可以提供工单编号")
        if (self.source == "replay") != bool(self.replay_run_id):
            raise ValueError("复制实验必须提供原运行编号，其他来源不要填写")
        return self
