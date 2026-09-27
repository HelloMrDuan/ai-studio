from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.v3.contracts import Capability
from app.v3.generation_contract import GenerationContract, visual_contract_fields
from app.v3.generation_executor import ReferenceFirstComfyExecutor

from .contracts import StepActivityInput, StepActivityResult
from .full_pipeline_executor import FullPipelineExecutor
from .materialized_generation import MaterializedDomainExecutor


_MATERIALIZED = {
    "generation.image.generate_candidate": "image",
    "generation.h3.generate_candidate": "video",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class MediaContentCache:
    """Project-scoped cache for immutable image/video materializations.

    Temporal idempotency protects retries of one task.  This cache goes one step
    further: a brand-new task may reuse an older artifact only when the complete
    production signature is identical, including reference file checksums,
    provider/workflow identity, seed and media parameters.
    """

    schema_version = "xiaoduan_media_content_cache_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "media-content-cache"
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        path = self.root / str(project_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, project_id: str, signature: str) -> Path:
        return self._project_dir(project_id) / f"{signature}.json"

    def get(self, project_id: str, signature: str) -> dict[str, Any] | None:
        path = self.path(project_id, signature)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if record.get("schema_version") != self.schema_version or record.get("signature") != signature:
            return None
        artifact = Path(str(record.get("artifact_path") or ""))
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            return None
        expected = str(record.get("artifact_sha256") or "")
        if expected and _file_sha(artifact) != expected:
            return None
        return record

    def put(self, project_id: str, signature: str, record: dict[str, Any]) -> None:
        artifact = Path(str(record.get("artifact_path") or ""))
        if not artifact.is_file() or artifact.stat().st_size <= 0:
            return
        payload = dict(record)
        payload.update(
            {
                "schema_version": self.schema_version,
                "signature": signature,
                "artifact_sha256": _file_sha(artifact),
            }
        )
        target = self.path(project_id, signature)
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)


class CachedMaterializedDomainExecutor(MaterializedDomainExecutor):
    """Materialized production with exact media reuse and real image parameters."""

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        super().__init__(settings, **kwargs)
        self.content_cache = MediaContentCache(settings.data_dir)

    def _profile_fingerprint(self) -> dict[str, str]:
        result: dict[str, str] = {}
        profile = Path(self.settings.data_dir) / "comfyui_reference_profile.v3.json"
        if profile.is_file():
            result["profile_sha256"] = _file_sha(profile)
            try:
                data = json.loads(profile.read_text(encoding="utf-8"))
                workflow = Path(str(data.get("reference_workflow_path") or ""))
                if workflow.is_file():
                    result["workflow_sha256"] = _file_sha(workflow)
            except Exception:
                pass
        return result

    def _reference_signature(self, reference_id: str) -> dict[str, str]:
        asset = self.base.references.resolve(reference_id)
        return {
            "reference_id": reference_id,
            "sha256": asset.sha256,
            "entity_id": asset.entity_id,
        }

    def _logical_frame_signature(self, project_id: str, logical_key: str) -> dict[str, str]:
        adopted = self.base.resources.adopted(project_id, logical_key)
        if adopted is None:
            raise FileNotFoundError(f"没有已采用的首尾帧：{logical_key}")
        metadata = adopted.get("metadata") if isinstance(adopted.get("metadata"), dict) else {}
        path = Path(str(metadata.get("artifact_path") or ""))
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"已采用首尾帧文件不存在：{logical_key}")
        return {
            "logical_key": logical_key,
            "resource_id": str(adopted.get("resource_id") or ""),
            "sha256": _file_sha(path),
        }

    def signature(self, project_id: str, operation: str, payload: dict[str, Any]) -> str | None:
        seed = int(payload.get("seed") or 0)
        # A negative seed means "random every time".  Reusing it would silently
        # change user intent, therefore random requests deliberately bypass the
        # cross-task cache.
        if seed < 0:
            return None

        if operation == "generation.image.generate_candidate":
            payload = self.base.prepare_image_payload(project_id, payload)
            references = [self._reference_signature(str(ref)) for ref in payload.get("reference_ids") or []]
            signature = {
                "schema": "xiaoduan_image_signature_v3",
                "visual_context": payload.get("visual_context", {}),
                "visual_direction": payload.get("visual_direction", {}),
                "character_appearances": payload.get("character_appearances", []),
                "positive_prompt": payload.get("positive_prompt", ""),
                "negative_prompt": payload.get("negative_prompt", ""),
                "kind": "image",
                "provider_id": str(payload.get("provider_id") or "local-comfyui-image"),
                "model_id": str(payload.get("model_id") or "configured-image-workflow"),
                "source_text": str(payload.get("source_text") or ""),
                "reference_files": references,
                "width": int(payload.get("width") or 1024),
                "height": int(payload.get("height") or 1024),
                "steps": int(payload.get("steps") or 28),
                "cfg": float(payload.get("cfg") or 5.5),
                "seed": seed,
                "sampler_name": str(payload.get("sampler_name") or payload.get("sampler") or "dpmpp_2m"),
                "scheduler": str(payload.get("scheduler") or "karras"),
                "quality_stage": str((payload.get("metadata") or {}).get("quality_stage") or "preview"),
                "provider_profile": self._profile_fingerprint(),
            }
            return _sha(signature)

        if operation == "generation.h3.generate_candidate":
            first_logical = str(payload.get("first_frame_logical_key") or "").strip()
            first_direct = str(payload.get("first_frame_reference_id") or "").strip()
            last_logical = str(payload.get("last_frame_logical_key") or "").strip()
            last_direct = str(payload.get("last_frame_reference_id") or "").strip()
            first = (
                self._logical_frame_signature(project_id, first_logical)
                if first_logical
                else self._reference_signature(first_direct)
            )
            last = None
            if last_logical:
                last = self._logical_frame_signature(project_id, last_logical)
            elif last_direct:
                last = self._reference_signature(last_direct)
            signature = {
                "schema": "xiaoduan_h3_signature_v2",
                "kind": "video",
                "provider_id": str(payload.get("provider_id") or "local-h3-video"),
                "model_id": str(payload.get("model_id") or "minimax-h3"),
                "prompt": str(payload.get("prompt") or ""),
                "first_frame": first,
                "last_frame": last,
                "width": int(payload.get("width") or 512),
                "height": int(payload.get("height") or 320),
                "length": int(payload.get("length") or 56),
                "steps": int(payload.get("steps") or 12),
                "seed": seed,
                "h3_runtime": {
                    "fl2va_model": str(self.settings.h3_fl2va_model),
                    "ref2va_model": str(self.settings.h3_ref2va_model),
                    "text_encoder": str(self.settings.h3_text_encoder),
                    "video_vae": str(self.settings.h3_video_vae),
                    "audio_vae": str(self.settings.h3_audio_vae),
                    "sampler": str(self.settings.h3_sampler),
                    "scheduler": str(self.settings.h3_scheduler),
                },
            }
            return _sha(signature)
        return None

    def _cache_hit_result(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
        signature: str,
        record: dict[str, Any],
    ) -> StepActivityResult:
        artifact_path = Path(str(record["artifact_path"]))
        local_payload = dict(payload)
        metadata = dict(local_payload.get("metadata") or {})
        metadata.update(
            {
                "media_content_hash": signature,
                "media_cache_hit": True,
                "reused_from_generation_task_id": str(record.get("generation_task_id") or ""),
                "quality_stage": str(record.get("quality_stage") or metadata.get("quality_stage") or "preview"),
            }
        )
        local_payload["metadata"] = metadata
        candidate = self._candidate_once(
            input,
            local_payload,
            provider_id=str(record.get("provider_id") or ""),
            model_id=str(record.get("model_id") or ""),
            reference_ids=[str(x) for x in record.get("reference_ids") or []],
            artifact_ref=str(record.get("artifact_ref") or ""),
            artifact_path=artifact_path,
            prompt_id=f"cache:{signature[:24]}",
            kind=str(record.get("kind") or "image"),
        )
        result = StepActivityResult(
            kind="completed",
            output_ref=str(record.get("artifact_ref") or ""),
            metadata={
                "executor": "v3-media-content-cache",
                "media_content_hash": signature,
                "media_cache_hit": "true",
                "artifact_path": str(artifact_path),
                "bytes_written": str(artifact_path.stat().st_size),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
            },
        )
        self.base.results.put(input.project_id, input.step.idempotency_key, result)
        return result

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
        if operation not in _MATERIALIZED:
            return await super().__call__(input)
        cached_task = self.base.results.get(input.project_id, input.step.idempotency_key)
        if cached_task is not None:
            return cached_task

        payload = self.base.payloads.resolve(input.project_id, input.step.payload_ref)
        signature = self.signature(input.project_id, operation, payload)
        if signature:
            record = self.content_cache.get(input.project_id, signature)
            if record is not None:
                return self._cache_hit_result(input, payload, signature, record)

        result = await super().__call__(input)
        if signature and result.kind == "completed":
            artifact_path = Path(str(result.metadata.get("artifact_path") or ""))
            if artifact_path.is_file() and artifact_path.stat().st_size > 0:
                resource_id = str(result.metadata.get("resource_id") or "")
                refs: list[str] = []
                provider_id = str(payload.get("provider_id") or ("local-comfyui-image" if operation.endswith("image.generate_candidate") else "local-h3-video"))
                model_id = str(payload.get("model_id") or ("configured-image-workflow" if operation.endswith("image.generate_candidate") else "minimax-h3"))
                if operation == "generation.image.generate_candidate":
                    refs = [str(x) for x in payload.get("reference_ids") or []]
                else:
                    # Keep exact refs from the materialized Resource when possible.
                    logical_key = str(payload.get("logical_key") or "")
                    for version in reversed(self.base.resources.list_versions(input.project_id, logical_key)):
                        if str(version.get("resource_id") or "") == resource_id:
                            refs = [str(x) for x in version.get("reference_ids") or []]
                            provider_id = str(version.get("provider_id") or provider_id)
                            model_id = str(version.get("model_id") or model_id)
                            break
                self.content_cache.put(
                    input.project_id,
                    signature,
                    {
                        "kind": _MATERIALIZED[operation],
                        "operation": operation,
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "reference_ids": refs,
                        "artifact_ref": result.output_ref,
                        "artifact_path": str(artifact_path),
                        "generation_task_id": input.step.idempotency_key,
                        "quality_stage": str((payload.get("metadata") or {}).get("quality_stage") or "preview"),
                    },
                )
        return result

    async def _image_generate_candidate(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
    ) -> StepActivityResult:
        """Same durable image materialization, but all UI media params are real."""
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
                width=int(payload.get("width") or 1024),
                height=int(payload.get("height") or 1024),
                steps=int(payload.get("steps") or 28),
                cfg=float(payload.get("cfg") or 5.5),
                seed=int(payload.get("seed") or 0),
                sampler_name=str(payload.get("sampler_name") or payload.get("sampler") or "dpmpp_2m"),
                scheduler=str(payload.get("scheduler") or "karras"),
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
                    "generation_params": {
                        **visual_contract_fields(payload),
                        "width": contract.width,
                        "height": contract.height,
                        "steps": contract.steps,
                        "cfg": contract.cfg,
                        "seed": contract.seed,
                        "sampler_name": contract.sampler_name,
                        "scheduler": contract.scheduler,
                    },
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
                "executor": "v3-cached-materialized-domain-executor",
                "prompt_id": prompt_id,
                "artifact_path": str(target),
                "bytes_written": str(bytes_written),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
                "width": str(job.get("generation_params", {}).get("width") or ""),
                "height": str(job.get("generation_params", {}).get("height") or ""),
                "steps": str(job.get("generation_params", {}).get("steps") or ""),
            },
        )


class ProductionCachedFullPipelineExecutor(FullPipelineExecutor):
    """Full executor used by the production worker."""

    def __init__(self, settings: Settings, **kwargs: Any) -> None:
        visual = kwargs.pop("visual", None) or CachedMaterializedDomainExecutor(settings)
        super().__init__(settings, visual=visual, **kwargs)


__all__ = [
    "MediaContentCache",
    "CachedMaterializedDomainExecutor",
    "ProductionCachedFullPipelineExecutor",
]
