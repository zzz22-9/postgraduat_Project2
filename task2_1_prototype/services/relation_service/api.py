"""Mountable relation routes, with strict SemanticObject inputs and RelationEdge outputs."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from task2_1_prototype.contracts.models import RelationEdge, SemanticObject
from .relation_engine import relation_inference


class InferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objects: list[SemanticObject]
    context: dict | None = None


def create_router(manager_provider):
    router = APIRouter(tags=["Relations"])

    @router.get("/relations", response_model=list[RelationEdge])
    def relations(source: str | None = None, target: str | None = None, relation: str | None = None,
                  active: bool | None = None, as_of: str | None = None, clock: str = "simulation"):
        try:
            return manager_provider().relations(source=source, target=target, relation=relation, active=active, as_of=as_of, clock=clock)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/relations/infer", response_model=list[RelationEdge])
    def infer(body: InferenceRequest):
        try:
            return relation_inference(body.objects, body.context)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return router
