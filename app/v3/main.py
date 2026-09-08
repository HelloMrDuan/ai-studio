from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import get_settings

from .adapters.comfyui import ComfyUIAdapter
from .adapters.h3 import H3ReferenceFirstExecutor, H3WorkflowCompiler, H3WorkflowConfig
from .context_resolver import ContextResolver
from .contracts import Capability, ResolvedField
from .generation_contract import GenerationContract
from .generation_executor import ReferenceAssetStore, ReferenceFirstComfyExecutor
from .media.tts import TTSError, TTSRequest, build_tts_adapter
from .provider_catalog import build_provider_registry
from .provider_gateway import ProviderResolutionError
from .resource_store import ResourceStore, ResourceStoreError
from .skill_registry import SkillRegistry


settings = get_settings()
skill_registry = SkillRegistry()
context_resolver = ContextResolver(default_cultural_context="Chinese")
provider_registry = build_provider_registry(settings)
resource_store = ResourceStore(settings.data_dir)
reference_store = ReferenceAssetStore(settings.data_dir)
tts_artifact_root = Path(settings.data_dir) / "v3" / "media" / "tts"
tts_artifact_root.mkdir(parents=True, exist_ok=True)

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


class ComfyImageQueueRequest(BaseModel):
    provider_id: str = "local-comfyui-image"
    model_id: str = "configured-image-workflow"
    shot_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    reference_ids: list[str] = Field(min_length=1)
    entity_ids: list[str] = Field(default_factory=list)
    camera_direction: str = ""
    action: str = ""


class H3QueueRequest(BaseModel):
    provider_id: str = "local-h3-video"
    model_id: str = "minimax-h3"
    shot_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    first_frame_reference_id: str = Field(min_length=1)
    last_frame_reference_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    width: int = 768
    height: int = 448
    length: int = 124
    steps: int = 20
    seed: int = 0


class TTSGenerateRequest(BaseModel):
    provider_id: str | None = None
    model_id: str | None = None
    text: str = Field(min_length=1)
    voice: str = Field(min_length=1)
    response_format: str = "mp3"
    speed: float = Field(default=1.0, gt=0.0, le=4.0)
    instructions: str = ""


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
        "reference_first_generation": True,
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


@app.post("/api/v3/references/{reference_id}")
async def import_reference(
    reference_id: str,
    file: UploadFile = File(...),
    entity_id: str = Form(default=""),
) -> dict[str, Any]:
    """Register a canonical/adopted visual reference in private V3 storage."""
    suffix = Path(file.filename or "reference.png").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(status_code=400, detail="reference must be png/jpg/jpeg/webp")
    content = await file.read(20 * 1024 * 1024 + 1)
    if not content or len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="reference must be 1 byte..20 MB")
    staged = reference_store.root / f".upload-{secrets.token_hex(12)}{suffix}"
    try:
        staged.write_bytes(content)
        asset = reference_store.import_file(reference_id, staged, entity_id=entity_id)
        return {
            "reference_id": asset.reference_id,
            "entity_id": asset.entity_id,
            "sha256": asset.sha256,
            "mime_type": asset.mime_type,
            "private_storage": True,
        }
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        staged.unlink(missing_ok=True)
        await file.close()


@app.post("/api/v3/generation/comfy/image/queue")
async def queue_reference_first_image(request: ComfyImageQueueRequest) -> dict[str, Any]:
    """Queue a real ComfyUI workflow only when explicit reference slots are configured."""
    required = {Capability.image_generation, Capability.image_reference}
    if len(request.reference_ids) > 1:
        required.add(Capability.multi_reference)
    try:
        selected = provider_registry.resolve(
            required,
            provider_id=request.provider_id,
            model_id=request.model_id,
        )
        provider_registry.assert_reference_budget(selected, len(request.reference_ids))
        contract = GenerationContract(
            shot_id=request.shot_id,
            source_text=request.source_text,
            entity_ids=tuple(request.entity_ids),
            reference_ids=tuple(request.reference_ids),
            provider_reference_ids=tuple(request.reference_ids),
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            required_capabilities=frozenset(required),
            camera_direction=request.camera_direction,
            action=request.action,
            duration_seconds=3.0,
        )
        executor = ReferenceFirstComfyExecutor(
            adapter=ComfyUIAdapter(selected.spec),
            references=reference_store,
        )
        receipt = await executor.execute_provider_profile(contract)
        return {
            "prompt_id": receipt.prompt_id,
            "provider_id": receipt.provider_id,
            "model_id": receipt.model_id,
            "reference_ids": list(receipt.provider_reference_ids),
            "uploaded_reference_names": list(receipt.uploaded_reference_names),
        }
    except (ProviderResolutionError, ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v3/generation/h3/video/queue")
async def queue_reference_first_h3(request: H3QueueRequest) -> dict[str, Any]:
    """Queue MiniMax H3 using an adopted first frame and optional last frame."""
    required = {
        Capability.video_generation,
        Capability.image_reference,
        Capability.first_frame,
    }
    refs = [request.first_frame_reference_id]
    if request.last_frame_reference_id:
        required.update({Capability.last_frame, Capability.first_last_frame})
        refs.append(request.last_frame_reference_id)
    try:
        selected = provider_registry.resolve(
            required,
            provider_id=request.provider_id,
            model_id=request.model_id,
        )
        provider_registry.assert_reference_budget(selected, len(refs))
        contract = GenerationContract(
            shot_id=request.shot_id,
            source_text=request.prompt,
            entity_ids=tuple(request.entity_ids),
            reference_ids=tuple(refs),
            provider_reference_ids=tuple(refs),
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            required_capabilities=frozenset(required),
            camera_direction="",
            action="",
            duration_seconds=max(0.1, request.length / 24.0),
        )
        compiler = H3WorkflowCompiler(
            H3WorkflowConfig(
                fl2va_model=settings.h3_fl2va_model,
                ref2va_model=settings.h3_ref2va_model,
                text_encoder=settings.h3_text_encoder,
                video_vae=settings.h3_video_vae,
                audio_vae=settings.h3_audio_vae,
                sampler=settings.h3_sampler,
                scheduler=settings.h3_scheduler,
            )
        )
        receipt = await H3ReferenceFirstExecutor(
            adapter=ComfyUIAdapter(selected.spec),
            references=reference_store,
            compiler=compiler,
        ).execute_first_frame(
            contract,
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            length=request.length,
            steps=request.steps,
            seed=request.seed,
        )
        return {
            "prompt_id": receipt.prompt_id,
            "provider_id": receipt.provider_id,
            "model_id": receipt.model_id,
            "reference_ids": list(receipt.provider_reference_ids),
            "uploaded_reference_names": list(receipt.uploaded_reference_names),
        }
    except (ProviderResolutionError, ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v3/generation/tts")
async def generate_tts(request: TTSGenerateRequest) -> dict[str, Any]:
    """Generate speech through the capability-selected local or remote TTS provider."""
    response_format = request.response_format.strip().lower().lstrip(".")
    if response_format not in {"mp3", "wav", "flac", "opus", "aac", "pcm"}:
        raise HTTPException(status_code=400, detail="unsupported TTS response_format")
    try:
        selected = provider_registry.resolve(
            {Capability.tts},
            provider_id=request.provider_id,
            model_id=request.model_id,
        )
        adapter = build_tts_adapter(selected.spec)
        artifact_id = "tts_" + secrets.token_hex(12)
        target = tts_artifact_root / f"{artifact_id}.{response_format}"
        receipt = await adapter.synthesize(
            TTSRequest(
                text=request.text,
                voice=request.voice,
                model=selected.spec.model_id,
                response_format=response_format,
                speed=request.speed,
                instructions=request.instructions,
            ),
            target,
        )
        return {
            "artifact_id": artifact_id,
            "provider_id": receipt.provider_id,
            "model_id": receipt.model,
            "voice": receipt.voice,
            "response_format": receipt.response_format,
            "bytes_written": receipt.bytes_written,
            "download_path": f"/api/v3/media/tts/{artifact_id}",
            "private_storage": True,
        }
    except (ProviderResolutionError, TTSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v3/media/tts/{artifact_id}")
async def get_tts_artifact(artifact_id: str) -> FileResponse:
    if not artifact_id.startswith("tts_") or not artifact_id[4:].isalnum():
        raise HTTPException(status_code=400, detail="invalid TTS artifact_id")
    matches = list(tts_artifact_root.glob(f"{artifact_id}.*"))
    if len(matches) != 1 or not matches[0].is_file():
        raise HTTPException(status_code=404, detail="TTS artifact not found")
    return FileResponse(matches[0])


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
