"""Mountable WP3 FastAPI routes. No WP5 gateway or implicit legacy DB initialization."""
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError

from task2_1_prototype.contracts.models import Domain, Level, ObjectType, RawIngestMessage, SemanticObject
from .mapping_engine import MappingEngine
from .models import MappingError
from .storage import SQLiteStore


def create_router(engine_provider):
    """engine_provider is a zero-argument callable, suitable for a gateway-owned engine."""
    router = APIRouter(prefix="/semantic", tags=["Semantic objects"])

    @router.post("/object", response_model=SemanticObject | list[SemanticObject])
    def ingest(body: RawIngestMessage | list[RawIngestMessage]):
        try:
            engine = engine_provider()
            return engine.map_batch(body) if isinstance(body, list) else engine.semantic_mapping(body)
        except MappingError as exc:
            status = 409 if exc.code in ("IDENTITY_CONFLICT", "SCOPE_CONFLICT", "SAMPLE_CONTEXT", "SIGNAL_CONFLICT") else 422
            raise HTTPException(status_code=status, detail={"code": exc.code, "reason": str(exc)}) from exc
        except (ValueError, ValidationError, SchemaError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/objects", response_model=list[SemanticObject])
    def objects(object_id: str | None = None, object_type: ObjectType | None = None,
                domain: Domain | None = None, level: Level | None = None, start: str | None = None, end: str | None = None):
        try:
            return engine_provider().store.query(object_id=object_id, object_type=object_type, domain=domain,
                                                  level=level, start=start, end=end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/objects/{object_id}/history", response_model=list[SemanticObject])
    def history(object_id: str, object_type: ObjectType | None = None, domain: Domain | None = None,
                level: Level | None = None, start: str | None = None, end: str | None = None, scope: str | None = None):
        try:
            return engine_provider().store.query(history=True, object_id=object_id, object_type=object_type,
                                                  domain=domain, level=level, start=start, end=end, scope=scope)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return router


def create_app(database_path=":memory:", *, engine=None):
    @asynccontextmanager
    async def lifespan(app):
        if engine is None:
            store = SQLiteStore(database_path)
            try:
                app.state.semantic_engine = MappingEngine(store)
                yield
            finally:
                store.close()
        else:
            app.state.semantic_engine = engine
            yield

    app = FastAPI(title="Task 2.1 WP3 Semantic Object Service", lifespan=lifespan)
    app.include_router(create_router(lambda: app.state.semantic_engine))
    return app
