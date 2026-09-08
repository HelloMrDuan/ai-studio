from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings

from .context_resolver import ContextResolver
from .contracts import Capability, ResolvedField
from .provider_catalog import build_provider_registry
from .provider_gateway import ProviderResolutionError
from .resource_store import ResourceStore, ResourceStoreError
from .skill_registry import SkillRegistry


settings = get_settings()
skill_registry = SkillRegistry()
context_resolver = ContextResolver(default_cultural_context="Chinese")
provider_registry = build_provider_registry(settings)
resource_store = ResourceStore(settings.data_dir)

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="xiaoduan映画 · Xiaoduan Studio V3", version="3.0.0-alpha")
app.mount("/v3-static", StaticFiles(directory=STATIC_DIR), name="v3-static")


class PlanRequest(BaseModel):
    requested_skills: list[str] = Field(min_length=1)


class ContextRequest(BaseModel):
    source_text: str
    user_override: str | None = None
    previous_locked: ResolvedField | None = None


class ProviderResolveRequest(BaseModel):
    capabilities: set[Capability] = Field(min_length=1)
    provider_id: str | None = None
    model_id: str | None = None


class CandidateRequest(BaseModel):
    logical_key: str
    generation_task_id: str
    provider_id: str
    model_id: str
    reference_ids: list[str] = Field(default_factory=list)
    prompt_contract_version: str | None = None
    continuity_version: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditRequest(BaseModel):
    passed: bool
    audit: dict[str, Any] = Field(default_factory=dict)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/v3/health")
async def health() -> dict[str, Any]:
    return {
        "product": "xiaoduan映画",
        "engineering_name": "Xiaoduan Studio",
        "version": "3.0.0-alpha",
        "legacy_stage_router": False,
        "skills": len(skill_registry.list()),
        "providers": len(provider_registry.list()),
        "remote_and_local_peer_routing": True,
        "silent_provider_fallback": False,
    }


@app.get("/api/v3/skills")
async def skills() -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in skill_registry.list()]


@app.post("/api/v3/plans")
async def build_plan(request: PlanRequest) -> dict[str, Any]:
    try:
        return skill_registry.build_plan(request.requested_skills).model_dump(mode="json")
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v3/context/resolve")
async def resolve_context(request: ContextRequest) -> dict[str, Any]:
    return context_resolver.resolve_cultural_context(
        request.source_text,
        user_override=request.user_override,
        previous_locked=request.previous_locked,
    ).model_dump(mode="json")


@app.get("/api/v3/providers")
async def providers() -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in provider_registry.list()]


@app.post("/api/v3/providers/resolve")
async def resolve_provider(request: ProviderResolveRequest) -> dict[str, Any]:
    try:
        selected = provider_registry.resolve(
            set(request.capabilities),
            provider_id=request.provider_id,
            model_id=request.model_id,
        )
    except ProviderResolutionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "provider": selected.spec.model_dump(mode="json"),
        "required_capabilities": sorted(item.value for item in selected.required_capabilities),
    }


@app.post("/api/v3/projects/{project_id}/resources/candidates")
async def create_candidate(project_id: str, request: CandidateRequest) -> dict[str, Any]:
    try:
        return resource_store.create_candidate(
            project_id,
            logical_key=request.logical_key,
            generation_task_id=request.generation_task_id,
            provider_id=request.provider_id,
            model_id=request.model_id,
            reference_ids=request.reference_ids,
            prompt_contract_version=request.prompt_contract_version,
            continuity_version=request.continuity_version,
            metadata=request.metadata,
        )
    except (ValueError, ResourceStoreError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v3/projects/{project_id}/resources/{resource_id}/audit")
async def audit_candidate(project_id: str, resource_id: str, request: AuditRequest) -> dict[str, Any]:
    try:
        return resource_store.set_audit_result(
            project_id,
            resource_id,
            passed=request.passed,
            audit=request.audit,
        )
    except ResourceStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v3/projects/{project_id}/resources/{resource_id}/adopt")
async def adopt_candidate(project_id: str, resource_id: str) -> dict[str, Any]:
    try:
        return resource_store.adopt(project_id, resource_id)
    except ResourceStoreError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v3/projects/{project_id}/resources/adopted")
async def adopted_resource(project_id: str, logical_key: str) -> dict[str, Any]:
    try:
        resource = resource_store.adopted(project_id, logical_key)
    except ResourceStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if resource is None:
        raise HTTPException(status_code=404, detail="no adopted resource for logical_key")
    return resource
