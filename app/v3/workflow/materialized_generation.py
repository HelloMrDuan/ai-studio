from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import Settings
from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.adapters.h3 import H3ReferenceFirstExecutor, H3WorkflowCompiler, H3WorkflowConfig
from app.v3.contracts import Capability, ProviderModelSpec
from app.v3.generation_contract import GenerationContract, visual_contract_fields
from app.v3.generation_executor import (
    ComfyWorkflowBindingError,
    ReferenceAssetError,
    ReferenceFirstComfyExecutor,
)
from app.v3.provider_gateway import ProviderResolutionError
from app.v3.resource_store import ResourceStoreError

from .contracts import StepActivityInput, StepActivityResult
from .domain_executor import DomainStepExecutor


_SAFE_KEY = re.compile(r"[A-Za-z0-9._:-]{3,160}")
_MATERIALIZED_OPERATIONS = {
    "generation.image.generate_candidate",
    "generation.h3.generate_candidate",
}


class ComfyExecutionError(RuntimeError):
    """The queued Comfy workflow reached a terminal execution error."""


class GenerationJobStore:
    """Persistent queue/materialization ledger for expensive visual Activities.

    Temporal Activities are at-least-once. Persisting the Comfy prompt ID before
    polling lets retries resume the already queued job instead of immediately
    submitting another expensive image/video generation.
    """

    schema_version = "xiaoduan_generation_job_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "workflow-generation-jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate(value: str, name: str) -> str:
        cleaned = str(value or "").strip()
        if not _SAFE_KEY.fullmatch(cleaned):
            raise ValueError(f"invalid {name}")
        return cleaned

    def _path(self, project_id: str, idempotency_key: str) -> Path:
        project = self._validate(project_id, "project_id")
        key = self._validate(idempotency_key, "idempotency_key")
        folder = self.root / project
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return folder / f"{digest}.json"

    def get(self, project_id: str, idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(project_id, idempotency_key)
        if not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("schema_version") != self.schema_version:
            raise ValueError("unsupported generation job schema")
        if str(record.get("idempotency_key") or "") != idempotency_key:
            raise ValueError("generation job idempotency collision")
        return record

    def put(self, project_id: str, idempotency_key: str, values: dict[str, Any]) -> dict[str, Any]:
        path = self._path(project_id, idempotency_key)
        record = dict(values)
        record.update(
            {
                "schema_version": self.schema_version,
                "project_id": project_id,
                "idempotency_key": idempotency_key,
            }
        )
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return record


AdapterFactory = Callable[[ProviderModelSpec], ComfyUIAdapter]


class MaterializedDomainExecutor:
    """Adds durable visual generation to the existing V3 domain dispatcher.

    `generation.*.queue` remains available for API-style asynchronous submission.
    These materialized operations are the Temporal production path: queue once,
    resume by prompt ID on retry, wait for a real Comfy artifact, download it to
    private V3 storage, and create an auditable Resource candidate.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        base: DomainStepExecutor | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.settings = settings
        self.base = base or DomainStepExecutor(settings)
        self.jobs = GenerationJobStore(settings.data_dir)
        self.adapter_factory = adapter_factory or (lambda spec: ComfyUIAdapter(spec))
        self.image_root = Path(settings.data_dir) / "v3" / "media" / "image"
        self.video_root = Path(settings.data_dir) / "v3" / "media" / "video"
        self.image_root.mkdir(parents=True, exist_ok=True)
        self.video_root.mkdir(parents=True, exist_ok=True)

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
        if operation not in _MATERIALIZED_OPERATIONS:
            return await self.base(input)

        cached = self.base.results.get(input.project_id, input.step.idempotency_key)
        if cached is not None:
            return cached

        try:
            payload = self.base.payloads.resolve(input.project_id, input.step.payload_ref)
            if operation == "generation.image.generate_candidate":
                payload = self.base.prepare_image_payload(input.project_id, payload)
                result = await self._image_generate_candidate(input, payload)
            else:
                result = await self._h3_generate_candidate(input, payload)
        except ProviderResolutionError as exc:
            result = self._semantic("PROVIDER_RESOLUTION_FAILED", exc)
        except (ReferenceAssetError, FileNotFoundError) as exc:
            result = self._semantic("REFERENCE_OR_PAYLOAD_UNAVAILABLE", exc)
        except ComfyWorkflowBindingError as exc:
            result = self._semantic("COMFY_WORKFLOW_BINDING_ERROR", exc)
        except ComfyExecutionError as exc:
            result = self._semantic("COMFY_EXECUTION_FAILED", exc)
        except ResourceStoreError as exc:
            result = self._semantic("RESOURCE_STATE_CONFLICT", exc)
        except ValueError as exc:
            result = self._semantic("INVALID_STEP_PAYLOAD", exc)

        self.base.results.put(input.project_id, input.step.idempotency_key, result)
        return result

    @staticmethod
    def _semantic(code: str, exc: Exception) -> StepActivityResult:
        return StepActivityResult(
            kind="semantic_failure",
            error_code=code,
            message=f"{type(exc).__name__}: {exc}",
            metadata={"executor": "v3-materialized-domain-executor"},
        )

    @staticmethod
    def _required(payload: dict[str, Any], name: str) -> str:
        value = str(payload.get(name) or "").strip()
        if not value:
            raise ValueError(f"{name} is required")
        return value

    @staticmethod
    def _optional(payload: dict[str, Any], name: str) -> str | None:
        value = str(payload.get(name) or "").strip()
        return value or None

    @staticmethod
    def _strings(payload: dict[str, Any], name: str, *, required: bool = False) -> list[str]:
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

    @staticmethod
    def _artifact_id(kind: str, idempotency_key: str) -> str:
        prefix = "img" if kind == "image" else "vid"
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
        return f"{prefix}_{digest}"

    @staticmethod
    def _first_artifact(item: dict[str, Any]) -> dict[str, Any] | None:
        for output in (item.get("outputs") or {}).values():
            if not isinstance(output, dict):
                continue
            for field in ("images", "videos", "gifs"):
                for artifact in output.get(field, []) or []:
                    if isinstance(artifact, dict) and str(artifact.get("filename") or "").strip():
                        return dict(artifact)
        return None

    async def _wait_for_artifact(
        self,
        adapter: ComfyUIAdapter,
        prompt_id: str,
        *,
        timeout_seconds: float,
        poll_seconds: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            history = await adapter.history(prompt_id)
            item = history.get(prompt_id)
            if isinstance(item, dict):
                status = item.get("status") if isinstance(item.get("status"), dict) else {}
                if str(status.get("status_str") or "").lower() == "error":
                    messages = status.get("messages") or item.get("status") or {}
                    detail = json.dumps(messages, ensure_ascii=False)[-2000:]
                    raise ComfyExecutionError(f"prompt {prompt_id} failed: {detail}")
                artifact = self._first_artifact(item)
                if artifact is not None:
                    return artifact
            await asyncio.sleep(poll_seconds)
        # Timeout is infrastructure-level: let Temporal retry and resume the same
        # persisted prompt instead of converting it into a semantic failure.
        raise TimeoutError(f"Comfy prompt timed out while waiting for artifact: {prompt_id}")

    @staticmethod
    def _suffix(filename: str, kind: str) -> str:
        suffix = Path(filename).suffix.lower()
        allowed = (
            {".png", ".jpg", ".jpeg", ".webp"}
            if kind == "image"
            else {".mp4", ".webm", ".mov", ".mkv", ".gif"}
        )
        if suffix not in allowed:
            raise ValueError(f"unexpected {kind} artifact extension: {suffix or '(none)'}")
        return suffix

    async def _download_artifact(
        self,
        adapter: ComfyUIAdapter,
        artifact: dict[str, Any],
        target: Path,
    ) -> int:
        filename = str(artifact.get("filename") or "").strip()
        if not filename:
            raise ValueError("Comfy artifact filename is missing")
        params = {
            "filename": filename,
            "subfolder": str(artifact.get("subfolder") or ""),
            "type": str(artifact.get("type") or "output"),
        }
        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            response = await client.get(f"{adapter.base_url}/view", params=params)
            response.raise_for_status()
            content = bytes(response.content)
        if not content:
            raise RuntimeError(f"Comfy returned an empty artifact: {filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.tmp")
        temp.write_bytes(content)
        temp.replace(target)
        return len(content)

    def _candidate_once(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
        *,
        provider_id: str,
        model_id: str,
        reference_ids: list[str],
        artifact_ref: str,
        artifact_path: Path,
        prompt_id: str,
        kind: str,
    ) -> dict[str, Any]:
        logical_key = self._required(payload, "logical_key")
        generation_task_id = input.step.idempotency_key
        for existing in self.base.resources.list_versions(input.project_id, logical_key):
            if str(existing.get("generation_task_id") or "") == generation_task_id:
                metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
                if metadata.get("artifact_ref") and metadata.get("artifact_ref") != artifact_ref:
                    raise ResourceStoreError("generation_task_id already points to a different artifact")
                return existing

        metadata = dict(payload.get("metadata") or {}) if isinstance(payload.get("metadata"), dict) else {}
        metadata.update(
            {
                "artifact_ref": artifact_ref,
                "artifact_path": str(artifact_path),
                "prompt_id": prompt_id,
                "media_kind": kind,
                "temporal_workflow_id": input.workflow_id,
                "temporal_step_id": input.step.step_id,
            }
        )
        return self.base.resources.create_candidate(
            input.project_id,
            logical_key=logical_key,
            generation_task_id=generation_task_id,
            provider_id=provider_id,
            model_id=model_id,
            reference_ids=reference_ids,
            prompt_contract_version=self._optional(payload, "prompt_contract_version"),
            continuity_version=(
                int(payload["continuity_version"])
                if payload.get("continuity_version") is not None
                else None
            ),
            metadata=metadata,
        )

    def _adopted_reference(
        self,
        project_id: str,
        logical_key: str,
        *,
        entity_id: str = "",
    ) -> tuple[str, dict[str, Any]]:
        adopted = self.base.resources.adopted(project_id, logical_key)
        if adopted is None:
            raise ResourceStoreError(f"no adopted resource for logical_key: {logical_key}")
        metadata = adopted.get("metadata") if isinstance(adopted.get("metadata"), dict) else {}
        artifact_path = Path(str(metadata.get("artifact_path") or ""))
        if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
            raise FileNotFoundError(f"adopted resource artifact is unavailable: {logical_key}")
        reference_id = f"resource:{adopted['resource_id']}"
        self.base.references.import_file(reference_id, artifact_path, entity_id=entity_id)
        return reference_id, adopted

    async def _image_generate_candidate(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
    ) -> StepActivityResult:
        references = self._strings(payload, "reference_ids", required=True)
        required = {Capability.image_generation, Capability.image_reference}
        if len(references) > 1:
            required.add(Capability.multi_reference)

        job = self.jobs.get(input.project_id, input.step.idempotency_key)
        provider_id = str(job.get("provider_id") or "") if job else ""
        model_id = str(job.get("model_id") or "") if job else ""
        selected = self.base.providers.resolve(
            required,
            provider_id=provider_id or self._optional(payload, "provider_id") or "local-comfyui-image",
            model_id=model_id or self._optional(payload, "model_id") or "configured-image-workflow",
        )
        self.base.providers.assert_reference_budget(selected, len(references))
        adapter = self.adapter_factory(selected.spec)

        if job is None:
            contract = GenerationContract(
                shot_id=self._required(payload, "shot_id"),
                source_text=self._required(payload, "source_text"),
                **visual_contract_fields(payload),
                entity_ids=tuple(self._strings(payload, "entity_ids")),
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
                adapter=adapter,
                references=self.base.references,
            ).execute_provider_profile(contract)
            job = self.jobs.put(
                input.project_id,
                input.step.idempotency_key,
                {
                    "kind": "image",
                    "state": "queued",
                    "prompt_id": receipt.prompt_id,
                    "provider_id": receipt.provider_id,
                    "model_id": receipt.model_id,
                    "reference_ids": list(receipt.provider_reference_ids),
                },
            )
        elif str(job.get("kind") or "") != "image":
            raise ValueError("generation job kind mismatch")

        prompt_id = self._required(job, "prompt_id")
        artifact = job.get("artifact") if isinstance(job.get("artifact"), dict) else None
        if artifact is None:
            artifact = await self._wait_for_artifact(
                adapter,
                prompt_id,
                timeout_seconds=float(self.settings.comfyui_task_timeout_seconds),
            )
            job["artifact"] = artifact
            job["state"] = "generated"
            job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

        artifact_id = self._artifact_id("image", input.step.idempotency_key)
        suffix = self._suffix(str(artifact.get("filename") or ""), "image")
        target = self.image_root / f"{artifact_id}{suffix}"
        if not target.is_file() or target.stat().st_size <= 0:
            bytes_written = await self._download_artifact(adapter, artifact, target)
        else:
            bytes_written = target.stat().st_size
        artifact_ref = f"artifact://image/{artifact_id}"
        job.update(
            {
                "state": "materialized",
                "artifact_ref": artifact_ref,
                "artifact_path": str(target),
                "bytes_written": bytes_written,
            }
        )
        self.jobs.put(input.project_id, input.step.idempotency_key, job)

        candidate = self._candidate_once(
            input,
            payload,
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            reference_ids=references,
            artifact_ref=artifact_ref,
            artifact_path=target,
            prompt_id=prompt_id,
            kind="image",
        )
        return StepActivityResult(
            kind="completed",
            output_ref=artifact_ref,
            metadata={
                "executor": "v3-materialized-domain-executor",
                "prompt_id": prompt_id,
                "artifact_path": str(target),
                "bytes_written": str(bytes_written),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
            },
        )

    async def _h3_generate_candidate(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
    ) -> StepActivityResult:
        first_direct = self._optional(payload, "first_frame_reference_id")
        first_logical = self._optional(payload, "first_frame_logical_key")
        if bool(first_direct) == bool(first_logical):
            raise ValueError("set exactly one of first_frame_reference_id or first_frame_logical_key")
        entity_ids = self._strings(payload, "entity_ids")
        if first_logical:
            first_ref, first_resource = self._adopted_reference(
                input.project_id,
                first_logical,
                entity_id=(entity_ids[0] if entity_ids else ""),
            )
        else:
            first_ref = str(first_direct)
            first_resource = None

        last_direct = self._optional(payload, "last_frame_reference_id")
        last_logical = self._optional(payload, "last_frame_logical_key")
        if last_direct and last_logical:
            raise ValueError("set only one of last_frame_reference_id or last_frame_logical_key")
        last_resource = None
        if last_logical:
            last_ref, last_resource = self._adopted_reference(input.project_id, last_logical)
        else:
            last_ref = last_direct

        references = [first_ref] + ([str(last_ref)] if last_ref else [])
        required = {Capability.video_generation, Capability.image_reference, Capability.first_frame}
        if last_ref:
            required.update({Capability.last_frame, Capability.first_last_frame})

        job = self.jobs.get(input.project_id, input.step.idempotency_key)
        provider_id = str(job.get("provider_id") or "") if job else ""
        model_id = str(job.get("model_id") or "") if job else ""
        selected = self.base.providers.resolve(
            required,
            provider_id=provider_id or self._optional(payload, "provider_id") or "local-h3-video",
            model_id=model_id or self._optional(payload, "model_id") or "minimax-h3",
        )
        self.base.providers.assert_reference_budget(selected, len(references))
        adapter = self.adapter_factory(selected.spec)

        if job is None:
            prompt = self._required(payload, "prompt")
            contract = GenerationContract(
                shot_id=self._required(payload, "shot_id"),
                source_text=prompt,
                entity_ids=tuple(entity_ids),
                reference_ids=tuple(references),
                provider_reference_ids=tuple(references),
                provider_id=selected.spec.provider_id,
                model_id=selected.spec.model_id,
                required_capabilities=frozenset(required),
                duration_seconds=max(0.1, int(payload.get("length") or 56) / 24.0),
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
                adapter=adapter,
                references=self.base.references,
                compiler=compiler,
            ).execute_first_frame(
                contract,
                prompt=prompt,
                width=int(payload.get("width") or 512),
                height=int(payload.get("height") or 320),
                length=int(payload.get("length") or 56),
                steps=int(payload.get("steps") or 12),
                seed=int(payload.get("seed") or 0),
            )
            job = self.jobs.put(
                input.project_id,
                input.step.idempotency_key,
                {
                    "kind": "h3",
                    "state": "queued",
                    "prompt_id": receipt.prompt_id,
                    "provider_id": receipt.provider_id,
                    "model_id": receipt.model_id,
                    "reference_ids": list(receipt.provider_reference_ids),
                    "first_frame_resource_id": (
                        str(first_resource.get("resource_id") or "") if first_resource else ""
                    ),
                    "last_frame_resource_id": (
                        str(last_resource.get("resource_id") or "") if last_resource else ""
                    ),
                },
            )
        elif str(job.get("kind") or "") != "h3":
            raise ValueError("generation job kind mismatch")

        prompt_id = self._required(job, "prompt_id")
        artifact = job.get("artifact") if isinstance(job.get("artifact"), dict) else None
        if artifact is None:
            artifact = await self._wait_for_artifact(
                adapter,
                prompt_id,
                timeout_seconds=float(self.settings.h3_task_timeout_seconds),
            )
            job["artifact"] = artifact
            job["state"] = "generated"
            job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

        artifact_id = self._artifact_id("video", input.step.idempotency_key)
        suffix = self._suffix(str(artifact.get("filename") or ""), "video")
        target = self.video_root / f"{artifact_id}{suffix}"
        if not target.is_file() or target.stat().st_size <= 0:
            bytes_written = await self._download_artifact(adapter, artifact, target)
        else:
            bytes_written = target.stat().st_size
        artifact_ref = f"artifact://video/{artifact_id}"
        job.update(
            {
                "state": "materialized",
                "artifact_ref": artifact_ref,
                "artifact_path": str(target),
                "bytes_written": bytes_written,
            }
        )
        self.jobs.put(input.project_id, input.step.idempotency_key, job)

        candidate = self._candidate_once(
            input,
            payload,
            provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id,
            reference_ids=references,
            artifact_ref=artifact_ref,
            artifact_path=target,
            prompt_id=prompt_id,
            kind="video",
        )
        return StepActivityResult(
            kind="completed",
            output_ref=artifact_ref,
            metadata={
                "executor": "v3-materialized-domain-executor",
                "prompt_id": prompt_id,
                "artifact_path": str(target),
                "bytes_written": str(bytes_written),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
                "first_frame_resource_id": str(job.get("first_frame_resource_id") or ""),
            },
        )
