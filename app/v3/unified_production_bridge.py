from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.generation_contract import visual_contract_fields
from app.v3.production_legacy_bridge import ProductionReadyLegacyBridge
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput


logger = logging.getLogger(__name__)


class UnifiedProductionBridge(ProductionReadyLegacyBridge):
    """Close the last legacy-only image branch.

    ProductionReadyLegacyBridge already sends reference-conditioned images and
    shot images through Temporal. This subclass routes reference-free reusable
    image assets (notably character Face Anchor) through the exact same durable
    generation.image.generate_candidate operation instead of falling back to the
    archived V2 workbench executor.
    """

    async def _execute_zimage_temporal_target(
        self,
        project_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        prompt_asset_id = str(payload.get("prompt_asset_id") or "").strip()
        prompt = self._prompt_text(project_id, prompt_asset_id)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        positive = str(params.get("positive_prompt") or "").strip()
        negative = str(params.get("negative_prompt") or "").strip()
        if not positive:
            raise ValueError("Z-Image 生成缺少已经冻结的 positive_prompt")
        width, height = self._image_dimensions(params)

        task_id = "v3task_" + secrets.token_hex(10)
        workflow_id = "studio-v3-zimage-" + secrets.token_hex(10)
        target_asset_id = str(target.get("asset_id") or "").strip()
        logical_key = f"studio-v3:reference:{target_asset_id}:image"
        step_key = f"{workflow_id}:image-generate"
        seed = int(params.get("seed") if params.get("seed") is not None else -1)
        phase = str(params.get("reference_phase") or "")

        logger.info(
            "ZIMAGE_FROZEN_INPUT project_id=%s workflow_id=%s target_asset_id=%s phase=%s cfg=1.0 size=%sx%s positive=%s negative=%s",
            project_id,
            workflow_id,
            target_asset_id,
            phase,
            width,
            height,
            json.dumps(positive, ensure_ascii=False),
            json.dumps(negative, ensure_ascii=False),
        )

        step_payload = {
            "logical_key": logical_key,
            "provider_id": "local-zimage-image",
            "model_id": "z-image-turbo",
            "shot_id": f"reference-{target_asset_id}",
            "source_text": prompt,
            **visual_contract_fields({**payload, **params}),
            "positive_prompt": positive,
            "negative_prompt": negative,
            "reference_ids": [],
            "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
            "camera_direction": "neutral identity reference",
            "action": "neutral identity portrait",
            "duration_seconds": 1.0,
            "width": width,
            "height": height,
            "steps": 9,
            "cfg": 1.0,
            "seed": seed,
            "sampler_name": "euler",
            "scheduler": "simple",
            "metadata": {
                "source": "unified_temporal_txt2img",
                "legacy_target_asset_id": target_asset_id,
                "reference_phase": phase,
                "runtime_image_backend": "z_image_turbo",
                "provider_ready_prompt_frozen": True,
            },
        }
        payload_ref = self.payloads.put(project_id, f"{workflow_id}-image", step_payload)
        step = ProductionStep(
            step_id="image-generate",
            skill_id="image_direction",
            operation="generation.image.generate_candidate",
            payload_ref=payload_ref,
            idempotency_key=step_key,
        )
        request = ProductionWorkflowInput(
            workflow_id=workflow_id,
            project_id=project_id,
            steps=(step,),
        )
        task_record = self.legacy.store.create(
            task_id=task_id,
            module="新版工作流",
            operation="Z-Image 图片候选生成",
            title=str(target.get("name") or "图片候选"),
            params={
                "v3_workflow_id": workflow_id,
                "v3_logical_key": logical_key,
                "legacy_target_asset_id": target_asset_id,
                "reference_phase": phase,
                "provider_id": "local-zimage-image",
                "model_id": "z-image-turbo",
                "width": width,
                "height": height,
            },
            input_files=[],
        )
        task = task_record.model_dump(mode="json")
        candidate = self._append_candidate(
            project_id=project_id,
            target=target,
            payload=payload,
            task=task,
            output_asset_type="IMAGE",
            dependencies=[prompt_asset_id],
            v3_workflow_id=workflow_id,
            v3_logical_key=logical_key,
            v3_step_key=step_key,
        )
        background = asyncio.create_task(
            self._run_v3(
                project_id=project_id,
                candidate_id=str(candidate["candidate_id"]),
                task_id=task_id,
                request=request,
                logical_key=logical_key,
                step_key=step_key,
            )
        )
        self._tasks.add(background)
        background.add_done_callback(self._tasks.discard)
        return {
            "candidate": candidate,
            "task": task,
            "producer": "v3_temporal_zimage",
            "manual_adoption_required": True,
            "provider_id": "local-zimage-image",
            "model_id": "z-image-turbo",
            "reference_ids": [],
        }

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability = str(payload.get("capability") or "").strip().lower()
        target_asset_id = str(payload.get("target_asset_id") or "").strip()
        if capability == "image" and target_asset_id:
            target = self.legacy.director.production.get_asset(project_id, target_asset_id)
            if not self._shot_id(target):
                prepared = MediaGenerationPipeline().prepare_candidate(
                    self.legacy.director.production,
                    project_id,
                    payload,
                )
                params = prepared.get("params") if isinstance(prepared.get("params"), dict) else {}
                references = [
                    str(item).strip()
                    for item in params.get("reference_asset_ids") or []
                    if str(item).strip()
                ]
                if not references:
                    return await self._execute_zimage_temporal_target(project_id, target, prepared)
                # The parent bridge owns the proven reference-conditioned path.
                return await super().execute_candidate(project_id, prepared)
        return await super().execute_candidate(project_id, payload)


__all__ = ["UnifiedProductionBridge"]
