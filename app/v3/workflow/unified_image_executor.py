from __future__ import annotations

import json
import secrets
from typing import Any

from app.v3.contracts import Capability
from app.v3.zimage_temporal_executor import ZImageTemporalExecutor

from .contracts import StepActivityInput, StepActivityResult
from .production_cached_executor import CachedMaterializedDomainExecutor


_CHARACTER_PACKAGE_PHASES = {"costume", "turnaround"}


class UnifiedImageDomainExecutor(CachedMaterializedDomainExecutor):
    """One durable image operation with an explicit renderer boundary.

    Plain images and the staged character package are rendered by the repository's
    real Z-Image-Turbo workflow. Character package reference IDs remain attached
    as lineage/identity inputs, but they no longer cause the primary renderer to
    silently switch to the legacy SDXL reference checkpoint. The production
    worker applies the adopted face anchor after rendering through FaceFusion.

    Other reference-aware domains still use the configured reference workflow
    until they get a domain-specific Z-Image control implementation.
    """

    @staticmethod
    def _reference_phase(payload: dict[str, Any]) -> str:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return str(
            metadata.get("reference_phase")
            or payload.get("reference_phase")
            or ""
        ).strip().lower()

    @classmethod
    def _uses_zimage_primary(cls, payload: dict[str, Any], references: list[str]) -> bool:
        if not references:
            return True
        return cls._reference_phase(payload) in _CHARACTER_PACKAGE_PHASES

    def signature(self, project_id: str, operation: str, payload: dict[str, Any]) -> str | None:
        """Do not cache a character-package artifact before FaceFusion finishes.

        CachedMaterializedDomainExecutor writes its cache record immediately after
        the primary renderer returns. Hybrid character generation has a second
        mandatory identity stage, so caching at that boundary could persist a
        half-finished Z-Image artifact and later bypass FaceFusion entirely. Until
        the cache owns the whole two-stage transaction, hybrid requests deliberately
        bypass cross-task reuse.
        """
        if operation == "generation.image.generate_candidate":
            references = self._strings(payload, "reference_ids", required=False)
            if references and self._uses_zimage_primary(payload, references):
                return None
        return super().signature(project_id, operation, payload)

    async def _image_generate_candidate(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
    ) -> StepActivityResult:
        references = self._strings(payload, "reference_ids", required=False)
        phase = self._reference_phase(payload)
        use_zimage = self._uses_zimage_primary(payload, references)
        if references and not use_zimage:
            return await super()._image_generate_candidate(input, payload)

        hybrid_identity = bool(references) and phase in _CHARACTER_PACKAGE_PHASES
        job = self.jobs.get(input.project_id, input.step.idempotency_key)
        provider_id = str(job.get("provider_id") or "") if job else ""
        model_id = str(job.get("model_id") or "") if job else ""
        selected = self.base.providers.resolve(
            {Capability.image_generation},
            provider_id=provider_id or "local-zimage-image",
            model_id=model_id or "z-image-turbo",
        )
        if selected.spec.provider_id != "local-zimage-image" or selected.spec.model_id != "z-image-turbo":
            raise ValueError(
                "Z-Image production render must resolve to local-zimage-image:z-image-turbo"
            )
        adapter = self.adapter_factory(selected.spec)

        if job is None:
            positive = str(payload.get("positive_prompt") or "").strip()
            if not positive:
                raise ValueError("provider-ready positive_prompt is required before Z-Image dispatch")
            negative = str(payload.get("negative_prompt") or "").strip()
            seed = int(payload.get("seed") if payload.get("seed") is not None else -1)
            if seed < 0:
                seed = secrets.randbelow(2**63 - 1)
            width = int(payload.get("width") or 1024)
            height = int(payload.get("height") or 1024)
            print(
                "ZIMAGE_WORKER_INPUT "
                f"project_id={input.project_id} workflow={input.workflow_id} step={input.step.step_id} "
                f"phase={phase or 'plain'} refs={len(references)} hybrid_identity={hybrid_identity} "
                f"cfg=1.0 size={width}x{height} "
                f"positive={json.dumps(positive, ensure_ascii=False)} "
                f"negative={json.dumps(negative, ensure_ascii=False)}",
                flush=True,
            )
            queued = await ZImageTemporalExecutor(selected.spec, adapter).queue(
                positive_prompt=positive,
                negative_prompt=negative,
                width=width,
                height=height,
                seed=seed,
                filename_prefix=f"Xiaoduan/ZImageTurbo/{input.step.idempotency_key}",
            )
            job = self.jobs.put(
                input.project_id,
                input.step.idempotency_key,
                {
                    "kind": "image",
                    "state": "queued",
                    "prompt_id": str(queued["prompt_id"]),
                    "provider_id": selected.spec.provider_id,
                    "model_id": selected.spec.model_id,
                    "reference_ids": list(references),
                    "reference_phase": phase,
                    "identity_postprocess": "facefusion" if hybrid_identity else "",
                    "generation_params": {
                        "width": width,
                        "height": height,
                        "steps": 9,
                        "cfg": 1.0,
                        "seed": seed,
                        "sampler_name": "euler",
                        "scheduler": "simple",
                        "positive_prompt": positive,
                        "negative_prompt": negative,
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
            reference_ids=list(references),
            artifact_ref=artifact_ref,
            artifact_path=target,
            prompt_id=prompt_id,
            kind="image",
        )
        params = job.get("generation_params") if isinstance(job.get("generation_params"), dict) else {}
        return StepActivityResult(
            kind="completed",
            output_ref=artifact_ref,
            metadata={
                "executor": "v3-unified-image-domain-executor",
                "prompt_id": prompt_id,
                "provider_id": selected.spec.provider_id,
                "model_id": selected.spec.model_id,
                "artifact_path": str(target),
                "bytes_written": str(bytes_written),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
                "reference_count": str(len(references)),
                "reference_phase": phase,
                "runtime_image_backend": "z_image_turbo_facefusion" if hybrid_identity else "z_image_turbo",
                "identity_postprocess_required": "true" if hybrid_identity else "false",
                "width": str(params.get("width") or ""),
                "height": str(params.get("height") or ""),
                "steps": "9",
                "cfg": "1.0",
                "sampler": "euler/simple",
            },
        )


__all__ = ["UnifiedImageDomainExecutor"]
