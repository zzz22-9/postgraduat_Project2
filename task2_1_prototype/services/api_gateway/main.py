"""FastAPI lifecycle and the ten explicitly specified WP5 standard endpoints."""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from task2_1_prototype.contracts.models import Domain, Level, ObjectType, RelationEdge, SemanticObject, require_json
from task2_1_prototype.services.semantic_object_service.api import create_router as semantic_router
from task2_1_prototype.services.relation_service.api import create_router as relation_router
from task2_1_prototype.services.relation_service import relation_inference
from task2_1_prototype.services.spatiotemporal_model_service.api import create_router as model_router
from .runtime import GatewayRuntime

Clock = Literal["simulation", "acquisition_wall_clock"]
Direction = Literal["auto", "upstream", "downstream"]


class TriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    event_id: str = Field(min_length=1, max_length=160)
    kind: Literal["fault", "fault_cleared", "business_update"]
    object_id: str = Field(min_length=1)
    sim_time_s: float = Field(ge=0, le=8640000)
    signals: dict[str, float] | None = None
    fault_code: str = Field(default="0x84", pattern=r"^0x[0-9a-fA-F]+$")
    status: Literal["pending", "dispatched", "released", "running", "completed", "cancelled"] | None = None

    @field_validator("signals")
    @classmethod
    def four_signals(cls, values):
        if values is not None:
            require_json(values)
            if set(values) != {"spindle_speed_rpm", "temperature_c", "vibration_mm_s", "load_ratio"}:
                raise ValueError("signals must contain exactly the four physical channels")
        return values


class InferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    as_of: float | str | None = None
    clock: Clock = "simulation"
    objects: list[SemanticObject] | None = None
    context: dict | None = None


def create_gateway_app(workspace_dir, **config):
    @asynccontextmanager
    async def lifespan(app):
        runtime = GatewayRuntime(workspace_dir, **config)
        app.state.runtime = runtime
        try:
            if runtime.config.get("demo_workspace"):
                if not hasattr(app.state, "demo_app"):
                    app.state.demo_app = create_gateway_app(runtime.config["demo_workspace"])
                    app.mount("/demo", app.state.demo_app)
                async with app.state.demo_app.router.lifespan_context(app.state.demo_app):
                    yield
            else:
                yield
        finally:
            runtime.close()

    app = FastAPI(title="任务2.1 · 本地统一服务网关", version="0.1.0", lifespan=lifespan)

    def runtime():
        return app.state.runtime

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail={"reason": "required identity or context is missing", "field": str(exc)}) from exc
        except (ValueError, TypeError, SchemaError, OverflowError) as exc:
            raise HTTPException(status_code=422, detail={"code": getattr(exc, "code", "INVALID_INPUT"), "reason": str(exc)[:1200]}) from exc

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard():
        path = runtime().config.get("dashboard_file")
        if path and Path(path).is_file():
            return FileResponse(path, media_type="text/html; charset=utf-8")
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>任务2.1</title><h1>本地网关已启动</h1><p>当前会话未配置端到端看板。</p><a href="/docs">查看交互式 API 文档</a></html>')

    @app.post("/api/v1/objects/ingest", response_model=SemanticObject | list[SemanticObject], tags=["Object"])
    def ingest(body: dict | list[dict] = Body(...)):
        return call(runtime().ingest, body)

    @app.get("/api/v1/objects", response_model=list[SemanticObject], tags=["Object"])
    def objects(object_id: str | None = None, object_type: ObjectType | None = None, domain: Domain | None = None,
                level: Level | None = None, start: str | None = None, end: str | None = None):
        return call(runtime().store.query, object_id=object_id, object_type=object_type, domain=domain, level=level, start=start, end=end)

    @app.get("/api/v1/states/{object_id}", response_model=SemanticObject, tags=["State"])
    def state(object_id: str):
        obj = runtime().store.latest(object_id)
        if obj is None:
            raise HTTPException(status_code=404, detail="object not found")
        return obj

    @app.get("/api/v1/states/{object_id}/history", response_model=list[SemanticObject], tags=["State"])
    def history(object_id: str, start: str | None = None, end: str | None = None, scope: str | None = None):
        return call(runtime().store.query, history=True, object_id=object_id, start=start, end=end, scope=scope)

    @app.get("/api/v1/relations", response_model=list[RelationEdge], tags=["Relation"])
    def relations(source: str | None = None, target: str | None = None, relation: str | None = None,
                  active: bool | None = None, as_of: str | None = None, clock: Clock = "simulation"):
        return call(runtime().manager.relations, source=source, target=target, relation=relation, active=active, as_of=as_of, clock=clock)

    @app.post("/api/v1/relations/infer", response_model=list[RelationEdge], tags=["Relation"])
    def infer(body: InferRequest = Body(default=InferRequest())):
        if body.objects is not None:
            return call(relation_inference, body.objects, body.context)
        snapshot = call(runtime().manager.snapshot, body.as_of, clock=body.clock)
        if body.context is None:
            return snapshot["edges"]
        context = {**runtime().manager.rules, **body.context, "timestamp": snapshot["timestamp"]}
        if body.clock == "simulation":
            context.update(as_of=snapshot["as_of"], execution_windows=runtime().manager.windows)
        return call(relation_inference, snapshot["nodes"], context)

    @app.get("/api/v1/model/graph", tags=["Model"])
    def graph(as_of: str | None = None, clock: Clock = "simulation"):
        return call(runtime().manager.snapshot, as_of, clock=clock)

    @app.get("/api/v1/model/impact/{object_id}", tags=["Model"])
    def impact(object_id: str, as_of: str | None = None, clock: Clock = "simulation", direction: Direction = "auto"):
        if object_id not in runtime().manager.catalog:
            raise HTTPException(status_code=404, detail="object not found")
        return call(runtime().impact, object_id, as_of, direction, clock)

    @app.get("/api/v1/model/snapshots", tags=["Model"])
    def snapshots():
        return runtime().graph_store.snapshots()

    @app.post("/api/v1/events/trigger", tags=["Event"])
    def trigger(body: TriggerRequest):
        if body.object_id not in runtime().manager.catalog:
            raise HTTPException(status_code=404, detail="object not found")
        return call(runtime().trigger, body)

    # Compatibility writes use a runtime adapter, so /semantic/object cannot bypass acquisition or the graph.
    app.include_router(semantic_router(runtime))
    app.include_router(relation_router(lambda: runtime().manager))
    app.include_router(model_router(lambda: runtime().manager))
    return app


def main():
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser(description="Serve an existing isolated WP5 gateway workspace")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    uvicorn.run(create_gateway_app(args.workspace), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
