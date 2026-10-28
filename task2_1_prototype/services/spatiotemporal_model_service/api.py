"""WP4 route factory; a later gateway can mount it without changing prefixes."""
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException

from task2_1_prototype.services.relation_service.api import create_router as relation_router


def create_router(manager_provider):
    router = APIRouter(prefix="/model", tags=["Spatiotemporal model"])

    @router.get("/graph")
    def graph(as_of: str | None = None, clock: Literal["simulation", "acquisition_wall_clock"] = "simulation"):
        try:
            return manager_provider().snapshot(as_of, clock=clock)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/impact/{object_id}")
    def impact(object_id: str, as_of: str | None = None,
               direction: Literal["upstream", "downstream", "auto"] = "auto",
               clock: Literal["simulation", "acquisition_wall_clock"] = "simulation"):
        try:
            return manager_provider().impact(object_id, as_of, direction, clock)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="object_id is not in the graph") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/snapshots")
    def snapshots():
        return manager_provider().store.snapshots()
    return router


def create_app(manager):
    """Convenience ASGI app; caller owns manager/store lifetime. This is not the WP5 gateway."""
    app = FastAPI(title="Task 2.1 WP4 relations and spatiotemporal model")
    app.include_router(relation_router(lambda: manager))
    app.include_router(create_router(lambda: manager))
    return app
