from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from temporalio.client import Client

from app.models import TaskStatus
from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.generation_contract import visual_contract_fields
from app.v3.legacy_reference_bridge import ReferenceAwareLegacyCandidateV3Bridge
from app.v3.quality_policy import apply_smart_candidate_params, infer_quality_tier, profile_for_shot
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.temporal import ProductionWorkflow


_ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "1:1": (1024, 1024),
}


class ProductionReadyLegacyBridge(ReferenceAwareLegacyCandidateV3Bridge):
    """Original workbench UX backed by the production-quality V3 media path."""

    @staticmethod
    def _image_dimensions(params: dict[str, Any]) -> tuple[int, int]:
        if int(params.get("width") or 0) > 0 and int(params.get("height") or 0) > 0:
            return int(params["width"]), int(params["height"])
        return _ASPECT_SIZES.get(str(params.get("aspect_ratio") or "16:9"), (1024, 576))

    def _smart_payload(
        self,
        payload: dict[str, Any],
        *,
        formal: dict[str, Any],
        capability: str,
    ) -> dict[str, Any]:
        next_payload = dict(payload)
        raw = dict(payload.get("params") or {}) if isinstance(payload.get("params"), dict) else {}
        quality_stage = str(raw.get("quality_stage") or "preview").strip().lower()
        if capability == "image" and quality_stage != "final":
            for key in ("steps", "cfg", "sampler", "sampler_name", "scheduler"):
                raw.pop(key, None)
            raw.pop("model_key", None)

        params = apply_smart_candidate_params(raw, shot=formal, capability=capability)
        # A normal preview is a new creative attempt, so freeze a fresh concrete
        # seed at submission time. A final refine explicitly carries the preview
        # seed and therefore remains reproducible and cacheable.
        if capability == "image" and quality_stage != "final":
            seed_value = raw.get("seed")
            try:
                seed = int(seed_value) if seed_value is not None else -1
            except Exception:
                seed = -1
            if seed < 0:
                seed = secrets.randbelow(2_147_483_646) + 1
            params["seed"] = seed

        next_payload["params"] = params
        metadata = dict(next_payload.get("metadata") or {}) if isinstance(next_payload.get("metadata"), dict) else {}
        metadata.update(
            {
                "quality_tier": infer_quality_tier(formal),
                "quality_mode": "smart",
                "quality_stage": str(params.get("quality_stage") or "preview"),
            }
        )
        next_payload["metadata"] = metadata
        return next_payload

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability = str(payload.get("capability") or "").strip().lower()
        target_asset_id = str(payload.get("target_asset_id") or "").strip()
        if capability not in {"image", "video"} or not target_asset_id:
            return await self.original_execute(project_id, payload)

        target = self.legacy.director.production.get_asset(project_id, target_asset_id)
        shot_id = self._shot_id(target)
        if not shot_id:
            if capability == "image":
                payload = MediaGenerationPipeline().prepare_candidate(self.legacy.director.production, project_id, payload)
            return await self.original_execute(project_id, payload)

        target = self.shot_authoring.bind_active_contract_to_target(project_id, target)
        formal = self._formal_shot(project_id, target)
        next_payload = self._smart_payload(payload, formal=formal, capability=capability)

        if capability == "image":
            try:
                self._candidate_reference_ids(project_id, target)
            except ValueError as missing:
                prepared = await self.reference_bootstrap.generate_missing(project_id)
                submitted = list(prepared.get("submitted_entity_ids") or [])
                waiting = list(prepared.get("waiting_adoption_entity_ids") or [])
                if submitted:
                    raise ValueError(
                        f"当前作品缺少已采用参考图，系统已批量开始生成 {len(submitted)} 个一致性参考候选。"
                        "候选完成后统一预览并采用，再生成分镜画面。"
                    ) from missing
                if waiting:
                    raise ValueError(
                        f"当前有 {len(waiting)} 个一致性参考候选等待采用。请先统一预览并采用，再生成分镜画面。"
                    ) from missing
                raise

        if capability == "image":
            next_payload = MediaGenerationPipeline().prepare_candidate(self.legacy.director.production, project_id, next_payload)
        prompt_asset_id = str(next_payload.get("prompt_asset_id") or "").strip()
        prompt = self._prompt_text(project_id, prompt_asset_id)
        params = next_payload.get("params") if isinstance(next_payload.get("params"), dict) else {}
        dependencies = [prompt_asset_id]
        task_id = "v3task_" + secrets.token_hex(10)
        workflow_id = "studio-v3-" + secrets.token_hex(12)
        logical_key = self._logical_key(shot_id, capability)
        step_key = f"{workflow_id}:{capability}-generate"
        common_metadata = dict(next_payload.get("metadata") or {})
        common_metadata.update(
            {
                "source": "original_workbench_v3_bridge",
                "legacy_target_asset_id": target_asset_id,
                "shot_id": shot_id,
            }
        )

        if capability == "image":
            reference_ids = self._candidate_reference_ids(project_id, target)
            width, height = self._image_dimensions(params)
            profile = profile_for_shot(formal)
            quality_stage = str(params.get("quality_stage") or "preview")
            steps = int(
                params.get("steps")
                or (profile.image_final_steps if quality_stage == "final" else profile.image_candidate_steps)
            )
            operation = "generation.image.generate_candidate"
            step_payload = {
                "logical_key": logical_key,
                "provider_id": "local-comfyui-image",
                "model_id": "configured-image-workflow",
                "shot_id": shot_id,
                "source_text": prompt,
                **visual_contract_fields({**next_payload, **params}),
                "reference_ids": reference_ids,
                "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
                "camera_direction": str((target.get("metadata") or {}).get("camera_direction") or ""),
                "action": str((target.get("metadata") or {}).get("action") or ""),
                "duration_seconds": float((target.get("metadata") or {}).get("duration_seconds") or 3.0),
                "width": width,
                "height": height,
                "steps": steps,
                "cfg": float(params.get("cfg") or 5.5),
                "seed": int(params.get("seed") or 0),
                "sampler_name": str(params.get("sampler_name") or params.get("sampler") or "dpmpp_2m"),
                "scheduler": str(params.get("scheduler") or "karras"),
                "metadata": {
                    **common_metadata,
                    "quality_stage": quality_stage,
                    "quality_tier": str(params.get("quality_tier") or profile.tier),
                    "preview_candidate_id": str(params.get("preview_candidate_id") or ""),
                },
            }
            output_asset_type = "IMAGE"
        else:
            first_frame_asset_id = str(next_payload.get("first_frame_asset_id") or "").strip()
            if not first_frame_asset_id:
                raise ValueError("视频生成必须先选择并采用视频首帧")
            dependencies.append(first_frame_asset_id)
            first_frame_logical_key = self._mirror_adopted_first_frame(project_id, shot_id, first_frame_asset_id)
            operation = "generation.h3.generate_candidate"
            step_payload = {
                "logical_key": logical_key,
                "provider_id": "local-h3-video",
                "model_id": "minimax-h3",
                "shot_id": shot_id,
                "prompt": prompt,
                "first_frame_logical_key": first_frame_logical_key,
                "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
                "width": int(params.get("width") or 768),
                "height": int(params.get("height") or 448),
                "length": int(params.get("length") or 124),
                "steps": int(params.get("steps") or 20),
                "seed": int(params.get("seed") or 0),
                "metadata": {
                    **common_metadata,
                    "legacy_first_frame_asset_id": first_frame_asset_id,
                },
            }
            output_asset_type = "VIDEO"

        payload_ref = self.payloads.put(project_id, f"{workflow_id}-{capability}", step_payload)
        step = ProductionStep(
            step_id=f"{capability}-generate",
            skill_id="image_direction" if capability == "image" else "video_direction",
            operation=operation,
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
            operation="图片候选生成" if capability == "image" else "视频候选生成",
            title=str(target.get("name") or "镜头候选"),
            params={
                "v3_workflow_id": workflow_id,
                "v3_logical_key": logical_key,
                "legacy_target_asset_id": target_asset_id,
                "quality_tier": str(params.get("quality_tier") or "B"),
                "quality_stage": str(params.get("quality_stage") or "preview"),
            },
            input_files=[],
        )
        task = task_record.model_dump(mode="json")
        candidate = self._append_candidate(
            project_id=project_id,
            target=target,
            payload=next_payload,
            task=task,
            output_asset_type=output_asset_type,
            dependencies=dependencies,
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
            "producer": "v3_temporal",
            "manual_adoption_required": True,
            "quality_tier": str(params.get("quality_tier") or "B"),
            "quality_stage": str(params.get("quality_stage") or "preview"),
        }

    async def _run_v3(
        self,
        *,
        project_id: str,
        candidate_id: str,
        task_id: str,
        request: ProductionWorkflowInput,
        logical_key: str,
        step_key: str,
    ) -> None:
        try:
            self.legacy.store.update(
                task_id,
                status=TaskStatus.running,
                progress=5,
                message="正在生成候选",
            )
            client = await Client.connect(self.temporal_address, namespace=self.temporal_namespace)
            handle = await client.start_workflow(
                ProductionWorkflow.run,
                request,
                id=request.workflow_id,
                task_queue=self.task_queue,
            )
            result = await handle.result()
            if result.status != "completed":
                raise RuntimeError(result.message or result.error_code or "工作流生成失败")

            versions = self.resources.list_versions(project_id, logical_key)
            resource = next(
                (
                    item for item in reversed(versions)
                    if str(item.get("generation_task_id") or "") == step_key
                ),
                None,
            )
            if resource is None:
                raise RuntimeError("工作流完成但没有找到对应候选资源")
            metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
            path = Path(str(metadata.get("artifact_path") or ""))
            if not path.is_file() or path.stat().st_size <= 0:
                raise FileNotFoundError("候选媒体文件不存在")
            output_url = self._url_for_path(path)
            self.legacy.store.update(
                task_id,
                status=TaskStatus.completed,
                progress=100,
                message="候选生成完成，等待你预览并采用",
                output_files=[output_url],
                error=None,
            )
            self._update_candidate(
                project_id,
                candidate_id,
                status="completed",
                progress=100,
                message="候选生成完成，等待你预览并采用",
                output_files=[output_url],
                v3_resource_id=str(resource.get("resource_id") or ""),
            )
        except Exception as exc:
            detail = str(exc).strip() or "未知错误"
            message = f"候选生成失败：{detail}"
            try:
                self.legacy.store.update(
                    task_id,
                    status=TaskStatus.failed,
                    progress=100,
                    message="候选生成失败",
                    error=message,
                )
            except Exception:
                pass
            self._update_candidate(
                project_id,
                candidate_id,
                status="failed",
                progress=100,
                message="候选生成失败",
                error=message,
            )


__all__ = ["ProductionReadyLegacyBridge"]
