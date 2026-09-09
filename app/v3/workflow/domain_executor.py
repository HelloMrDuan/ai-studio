from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.config import Settings
from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.adapters.h3 import H3ReferenceFirstExecutor, H3WorkflowCompiler, H3WorkflowConfig
from app.v3.contracts import Capability
from app.v3.generation_contract import GenerationContract, visual_contract_fields
from app.v3.generation_executor import (
    ComfyWorkflowBindingError,
    ReferenceAssetError,
    ReferenceAssetStore,
    ReferenceFirstComfyExecutor,
)
from app.v3.media.tts import TTSRequest, build_tts_adapter
from app.v3.provider_catalog import build_provider_registry
from app.v3.provider_gateway import ProviderRegistry, ProviderResolutionError
from app.v3.resource_store import ResourceStore, ResourceStoreError

from .contracts import StepActivityInput, StepActivityResult


_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{3,160}")


def _safe_id(value: str, name: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise ValueError(f"invalid {name}")
    return cleaned


def _required(payload: dict[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _optional(payload: dict[str, Any], name: str) -> str | None:
    value = str(payload.get(name) or "").strip()
    return value or None


def _string_list(payload: dict[str, Any], name: str, *, required: bool = False) -> list[str]:
    raw = payload.get(name)
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError(f"{name} must be an array")
    result = [str(item or "").strip() for item in raw]
    if any(not item for item in result):
        raise ValueError(f"{name} cannot contain empty values")
    if required and not result:
        raise ValueError(f"{name} requires at least one value")
    return result


def _metadata(**values: Any) -> dict[str, str]:
    return {key: str(value) for key, value in values.items() if value is not None}


class WorkflowPayloadStore:
    """Small durable payload store referenced by Temporal workflow steps.

    Production payloads stay outside Temporal history. Workflow steps only carry
    payload:// IDs, while the worker resolves the actual JSON from the V3 data
    directory owned by the project.
    """

    schema_version = "xiaoduan_workflow_payload_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "workflow-payloads"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        project = _safe_id(project_id, "project_id")
        path = self.root / project
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, project_id: str, payload_id: str, payload: dict[str, Any]) -> str:
        pid = _safe_id(payload_id, "payload_id")
        if not isinstance(payload, dict):
            raise ValueError("workflow payload must be an object")
        path = self._project_dir(project_id) / f"{pid}.json"
        record = {
            "schema_version": self.schema_version,
            "project_id": project_id,
            "payload_id": pid,
            "payload": payload,
        }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return f"payload://{pid}"

    def resolve(self, project_id: str, payload_ref: str) -> dict[str, Any]:
        ref = str(payload_ref or "").strip()
        if not ref.startswith("payload://"):
            raise ValueError("production payload_ref must use payload://")
        payload_id = _safe_id(ref[len("payload://"):], "payload_id")
        path = self._project_dir(project_id) / f"{payload_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"workflow payload not found: {ref}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema_version") != self.schema_version:
            raise ValueError("unsupported workflow payload schema")
        if str(record.get("project_id") or "") != project_id:
            raise ValueError("workflow payload project mismatch")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("workflow payload body must be an object")
        return payload


class ActivityResultStore:
    """Persistent idempotency ledger for Temporal Activity results."""

    schema_version = "xiaoduan_activity_result_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "workflow-idempotency"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str, idempotency_key: str) -> Path:
        project = _safe_id(project_id, "project_id")
        key = _safe_id(idempotency_key, "idempotency_key")
        folder = self.root / project
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return folder / f"{digest}.json"

    def get(self, project_id: str, idempotency_key: str) -> StepActivityResult | None:
        path = self._path(project_id, idempotency_key)
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema_version") != self.schema_version:
            raise ValueError("unsupported activity result schema")
        if str(record.get("idempotency_key") or "") != idempotency_key:
            raise ValueError("activity idempotency collision")
        raw = record.get("result")
        if not isinstance(raw, dict):
            raise ValueError("activity result record is invalid")
        return StepActivityResult(
            kind=str(raw.get("kind") or ""),
            output_ref=str(raw.get("output_ref") or ""),
            error_code=str(raw.get("error_code") or ""),
            message=str(raw.get("message") or ""),
            metadata={str(k): str(v) for k, v in (raw.get("metadata") or {}).items()},
        )

    def put(self, project_id: str, idempotency_key: str, result: StepActivityResult) -> None:
        path = self._path(project_id, idempotency_key)
        record = {
            "schema_version": self.schema_version,
            "idempotency_key": idempotency_key,
            "result": asdict(result),
        }
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)


class DomainStepExecutor:
    """Production-domain dispatcher used by the Temporal Activity boundary."""

    def __init__(
        self,
        settings: Settings,
        *,
        providers: ProviderRegistry | None = None,
        resources: ResourceStore | None = None,
        references: ReferenceAssetStore | None = None,
    ) -> None:
        self.settings = settings
        self.providers = providers or build_provider_registry(settings)
        self.resources = resources or ResourceStore(settings.data_dir)
        self.references = references or ReferenceAssetStore(settings.data_dir)
        self.payloads = WorkflowPayloadStore(settings.data_dir)
        self.results = ActivityResultStore(settings.data_dir)
        self.tts_root = Path(settings.data_dir) / "v3" / "media" / "tts"
        self.tts_root.mkdir(parents=True, exist_ok=True)

    def prepare_image_payload(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from app.services.production_assets import ProductionAssetService
        from app.services.media_generation_pipeline import MediaGenerationPipeline
        if payload.get("generation_contract_id") and payload.get("positive_prompt"):
            return payload
        # Standalone V3 API jobs retain their existing non-Director project IDs;
        # their GenerationContract is still compiled at the Comfy boundary.
        if not re.fullmatch(r"[a-f0-9]{24}", project_id):
            return payload
        production = ProductionAssetService(self.settings.data_dir)
        target_id = (payload.get("metadata") or {}).get("legacy_target_asset_id") or payload.get("asset_id")
        if not target_id:
            logical = str(payload.get("logical_key") or payload.get("shot_id") or "image")
            target = next((a for a in production.list_assets(project_id, active_only=True)
                           if a.get("logical_key") == logical), None)
            if target is None:
                target = production.declare_asset(
                    project_id, stage="05", skill="v3-image-generation", logical_key=logical,
                    asset_type="IMAGE", asset_role="shot_keyframe", name=str(payload.get("shot_id") or logical),
                    entity_ids=list(payload.get("entity_ids") or []), metadata={"visual_context": payload.get("visual_context") or {}},
                )
            target_id = target["asset_id"]
        target = production.ensure_visual_context(project_id, target_id)
        source = production.create_text_asset(
            project_id, stage="05", skill="v3-image-generation", logical_key=f"{target_id}:source-prompt",
            asset_role="image_prompt", name="已确认图片生成要求", content=str(payload.get("source_text") or ""),
            entity_ids=list(payload.get("entity_ids") or []),
            metadata={"visual_context": target["metadata"]["visual_context"]},
        )
        prepared = MediaGenerationPipeline().prepare_candidate(production, project_id, {
            "target_asset_id": target_id, "prompt_asset_id": source["asset_id"],
            "params": {"negative_prompt": payload.get("negative_prompt", "")},
        })
        return {**payload, **visual_contract_fields({**prepared, **prepared["params"]})}

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        step = input.step
        cached = self.results.get(input.project_id, step.idempotency_key)
        if cached is not None:
            return cached

        try:
            payload = self.payloads.resolve(input.project_id, step.payload_ref)
            result = await self._dispatch(input, payload)
        except ProviderResolutionError as exc:
            result = self._semantic("PROVIDER_RESOLUTION_FAILED", exc)
        except (ReferenceAssetError, FileNotFoundError) as exc:
            result = self._semantic("REFERENCE_OR_PAYLOAD_UNAVAILABLE", exc)
        except ComfyWorkflowBindingError as exc:
            result = self._semantic("COMFY_WORKFLOW_BINDING_ERROR", exc)
        except ResourceStoreError as exc:
            result = self._semantic("RESOURCE_STATE_CONFLICT", exc)
        except ValueError as exc:
            result = self._semantic("INVALID_STEP_PAYLOAD", exc)

        self.results.put(input.project_id, step.idempotency_key, result)
        return result

    @staticmethod
    def _semantic(code: str, exc: Exception) -> StepActivityResult:
        return StepActivityResult(
            kind="semantic_failure",
            error_code=code,
            message=f"{type(exc).__name__}: {exc}",
            metadata={"executor": "v3-domain-step-executor"},
        )

    async def _dispatch(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        operation = input.step.operation
        if operation == "tts.generate":
            return await self._tts_generate(input, payload)
        if operation == "resource.candidate.create":
            return self._resource_candidate(input, payload)
        if operation == "resource.audit_latest":
            return self._resource_audit_latest(input, payload)
        if operation == "resource.adopt_latest":
            return self._resource_adopt_latest(input, payload)
        if operation == "generation.image.queue":
            return await self._image_queue(input, payload)
        if operation == "generation.h3.queue":
            return await self._h3_queue(input, payload)
        return StepActivityResult(
            kind="semantic_failure",
            error_code="UNSUPPORTED_OPERATION",
            message=f"V3 production executor has no operation: {operation}",
            metadata={"executor": "v3-domain-step-executor"},
        )

    async def _tts_generate(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        provider_id = _optional(payload, "provider_id")
        model_id = _optional(payload, "model_id")
        selected = self.providers.resolve(
            {Capability.tts},
            provider_id=provider_id,
            model_id=model_id,
        )
        response_format = str(payload.get("response_format") or "mp3").strip().lower().lstrip(".")
        if response_format not in {"mp3", "wav", "flac", "opus", "aac", "pcm"}:
            raise ValueError("unsupported TTS response_format")
        artifact_id = "tts_" + hashlib.sha256(input.step.idempotency_key.encode("utf-8")).hexdigest()[:24]
        target = self.tts_root / f"{artifact_id}.{response_format}"
        if not target.is_file() or target.stat().st_size <= 0:
            adapter = build_tts_adapter(selected.spec)
            receipt = await adapter.synthesize(
                TTSRequest(
                    text=_required(payload, "text"),
                    voice=_required(payload, "voice"),
                    model=selected.spec.model_id,
                    response_format=response_format,
                    speed=float(payload.get("speed") or 1.0),
                    instructions=str(payload.get("instructions") or ""),
                ),
                target,
            )
            bytes_written = receipt.bytes_written
        else:
            bytes_written = target.stat().st_size
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://tts/{artifact_id}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                provider_id=selected.spec.provider_id,
                model_id=selected.spec.model_id,
                artifact_id=artifact_id,
                bytes_written=bytes_written,
                download_path=f"/api/v3/media/tts/{artifact_id}",
            ),
        )

    def _resource_candidate(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        record = self.resources.create_candidate(
            input.project_id,
            logical_key=_required(payload, "logical_key"),
            generation_task_id=str(payload.get("generation_task_id") or input.workflow_id),
            provider_id=_required(payload, "provider_id"),
            model_id=_required(payload, "model_id"),
            reference_ids=_string_list(payload, "reference_ids"),
            prompt_contract_version=_optional(payload, "prompt_contract_version"),
            continuity_version=(int(payload["continuity_version"]) if payload.get("continuity_version") is not None else None),
            metadata=(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
        )
        return StepActivityResult(
            kind="completed",
            output_ref=f"resource://{record['resource_id']}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                logical_key=record.get("logical_key"),
                resource_id=record.get("resource_id"),
                version=record.get("version"),
                state=record.get("state"),
            ),
        )

    def _resource_audit_latest(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        logical_key = _required(payload, "logical_key")
        versions = self.resources.list_versions(input.project_id, logical_key)
        if not versions:
            raise ResourceStoreError(f"no resource versions for logical_key: {logical_key}")
        latest = max(versions, key=lambda item: int(item.get("version") or 0))
        passed = bool(payload.get("passed"))
        state = str(latest.get("state") or "")
        if (passed and state in {"candidate_ready", "adopted"}) or (not passed and state == "audit_failed"):
            audited = latest
        else:
            audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
            audited = self.resources.set_audit_result(
                input.project_id,
                str(latest["resource_id"]),
                passed=passed,
                audit=audit,
            )
        return StepActivityResult(
            kind="completed",
            output_ref=f"resource://{audited['resource_id']}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                logical_key=logical_key,
                resource_id=audited.get("resource_id"),
                state=audited.get("state"),
            ),
        )

    def _resource_adopt_latest(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        logical_key = _required(payload, "logical_key")
        versions = self.resources.list_versions(input.project_id, logical_key)
        if not versions:
            raise ResourceStoreError(f"no resource versions for logical_key: {logical_key}")
        ready = [item for item in versions if item.get("state") == "candidate_ready"]
        if ready:
            target = max(ready, key=lambda item: int(item.get("version") or 0))
            adopted = self.resources.adopt(input.project_id, str(target["resource_id"]))
        else:
            already = [item for item in versions if item.get("state") == "adopted"]
            if not already:
                raise ResourceStoreError(f"no candidate_ready resource for logical_key: {logical_key}")
            adopted = max(already, key=lambda item: int(item.get("version") or 0))
        return StepActivityResult(
            kind="completed",
            output_ref=f"resource://{adopted['resource_id']}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                logical_key=logical_key,
                resource_id=adopted.get("resource_id"),
                state=adopted.get("state"),
            ),
        )

    async def _image_queue(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        payload = self.prepare_image_payload(input.project_id, payload)
        references = _string_list(payload, "reference_ids", required=True)
        required = {Capability.image_generation, Capability.image_reference}
        if len(references) > 1:
            required.add(Capability.multi_reference)
        selected = self.providers.resolve(
            required,
            provider_id=_optional(payload, "provider_id") or "local-comfyui-image",
            model_id=_optional(payload, "model_id") or "configured-image-workflow",
        )
        self.providers.assert_reference_budget(selected, len(references))
        contract = GenerationContract(
            shot_id=_required(payload, "shot_id"),
            source_text=_required(payload, "source_text"),
            **visual_contract_fields(payload),
            entity_ids=tuple(_string_list(payload, "entity_ids")),
            reference_ids=tuple(references),
            provider_reference_ids=tuple(references),
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            required_capabilities=frozenset(required),
            camera_direction=str(payload.get("camera_direction") or ""),
            action=str(payload.get("action") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 3.0),
        )
        receipt = await ReferenceFirstComfyExecutor(
            adapter=ComfyUIAdapter(selected.spec),
            references=self.references,
        ).execute_provider_profile(contract)
        return StepActivityResult(
            kind="completed",
            output_ref=f"comfy://prompt/{receipt.prompt_id}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                prompt_id=receipt.prompt_id,
                provider_id=receipt.provider_id,
                model_id=receipt.model_id,
                reference_count=len(receipt.provider_reference_ids),
            ),
        )

    async def _h3_queue(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        first_ref = _required(payload, "first_frame_reference_id")
        last_ref = _optional(payload, "last_frame_reference_id")
        refs = [first_ref] + ([last_ref] if last_ref else [])
        required = {Capability.video_generation, Capability.image_reference, Capability.first_frame}
        if last_ref:
            required.update({Capability.last_frame, Capability.first_last_frame})
        selected = self.providers.resolve(
            required,
            provider_id=_optional(payload, "provider_id") or "local-h3-video",
            model_id=_optional(payload, "model_id") or "minimax-h3",
        )
        self.providers.assert_reference_budget(selected, len(refs))
        prompt = _required(payload, "prompt")
        contract = GenerationContract(
            shot_id=_required(payload, "shot_id"),
            source_text=prompt,
            entity_ids=tuple(_string_list(payload, "entity_ids")),
            reference_ids=tuple(refs),
            provider_reference_ids=tuple(refs),
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            required_capabilities=frozenset(required),
            duration_seconds=max(0.1, int(payload.get("length") or 124) / 24.0),
        )
        compiler = H3WorkflowCompiler(
            H3WorkflowConfig(
                fl2va_model=self.settings.h3_fl2va_model,
                ref2va_model=self.settings.h3_ref2va_model,
                text_encoder=self.settings.h3_text_encoder,
                video_vae=self.settings.h3_video_vae,
                audio_vae=self.settings.h3_audio_vae,
                sampler=self.settings.h3_sampler,
                scheduler=self.settings.h3_scheduler,
            )
        )
        receipt = await H3ReferenceFirstExecutor(
            adapter=ComfyUIAdapter(selected.spec),
            references=self.references,
            compiler=compiler,
        ).execute_first_frame(
            contract,
            prompt=prompt,
            width=int(payload.get("width") or 768),
            height=int(payload.get("height") or 448),
            length=int(payload.get("length") or 124),
            steps=int(payload.get("steps") or 20),
            seed=int(payload.get("seed") or 0),
        )
        return StepActivityResult(
            kind="completed",
            output_ref=f"comfy://prompt/{receipt.prompt_id}",
            metadata=_metadata(
                executor="v3-domain-step-executor",
                prompt_id=receipt.prompt_id,
                provider_id=receipt.provider_id,
                model_id=receipt.model_id,
                reference_count=len(receipt.provider_reference_ids),
            ),
        )
