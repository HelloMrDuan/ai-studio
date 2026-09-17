from __future__ import annotations

from typing import Any

from app.v3.contracts import Capability
from app.v3.generation_contract import GenerationContract, visual_contract_fields
from app.v3.generation_executor import ReferenceFirstComfyExecutor

from .contracts import StepActivityInput, StepActivityResult
from .materialized_generation import MaterializedDomainExecutor


class SmartMaterializedDomainExecutor(MaterializedDomainExecutor):
    """Materialized image executor with frozen quality/canvas parameters.

    The base durable job/checkpoint behavior remains unchanged.  Only the image
    contract construction is specialized so the original workbench aspect ratio
    and smart quality policy reach explicit Comfy nodes instead of being UI-only.
    """

    @staticmethod
    def _int(payload: dict[str, Any], name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(payload.get(name) if payload.get(name) is not None else default)
        except Exception:
            value = default
        return max(low, min(high, value))

    @staticmethod
    def _float(payload: dict[str, Any], name: str, default: float, low: float, high: float) -> float:
        try:
            value = float(payload.get(name) if payload.get(name) is not None else default)
        except Exception:
            value = default
        return max(low, min(high, value))

    @staticmethod
    def _dimension(payload: dict[str, Any], name: str, default: int) -> int:
        value = SmartMaterializedDomainExecutor._int(payload, name, default, 512, 1536)
        # SDXL workflows are most stable on 64-pixel aligned latent dimensions.
        return max(512, min(1536, int(round(value / 64.0)) * 64))

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
            sampler = str(payload.get("sampler_name") or "dpmpp_2m").strip()
            scheduler = str(payload.get("scheduler") or "karras").strip()
            allowed_samplers = {
                "euler", "euler_ancestral", "heun", "dpm_2", "dpm_2_ancestral",
                "lms", "dpm_fast", "dpm_adaptive", "dpmpp_2s_ancestral",
                "dpmpp_sde", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc",
            }
            allowed_schedulers = {"normal", "karras", "exponential", "simple", "ddim_uniform", "sgm_uniform", "beta"}
            if sampler not in allowed_samplers:
                sampler = "dpmpp_2m"
            if scheduler not in allowed_schedulers:
                scheduler = "karras"

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
                width=self._dimension(payload, "width", 1216),
                height=self._dimension(payload, "height", 704),
                steps=self._int(payload, "steps", 21, 8, 50),
                cfg=self._float(payload, "cfg", 5.5, 1.0, 12.0),
                seed=self._int(payload, "seed", 0, 0, 2_147_483_647),
                sampler_name=sampler,
                scheduler=scheduler,
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
                    "contract": {
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
        contract_meta = job.get("contract") if isinstance(job.get("contract"), dict) else {}
        return StepActivityResult(
            kind="completed",
            output_ref=artifact_ref,
            metadata={
                "executor": "v3-smart-materialized-domain-executor",
                "prompt_id": prompt_id,
                "artifact_path": str(target),
                "bytes_written": str(bytes_written),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
                "width": str(contract_meta.get("width") or ""),
                "height": str(contract_meta.get("height") or ""),
                "steps": str(contract_meta.get("steps") or ""),
                "quality_tier": str((payload.get("metadata") or {}).get("quality_tier") or ""),
            },
        )


__all__ = ["SmartMaterializedDomainExecutor"]
