from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from temporalio.client import Client

from app.models import GPUOwner, TaskStatus
from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.generation_contract import visual_contract_fields
from app.v3.legacy_reference_bridge import ReferenceAwareLegacyCandidateV3Bridge
from app.v3.quality_policy import apply_smart_candidate_params, infer_quality_tier, profile_for_shot
from app.v3.storyboard.contracts import ShotSupportRegion
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.temporal import ProductionWorkflow


_ASPECT_SIZES: dict[str, tuple[int, int]] = {
    "16:9": (1024, 576),
    "9:16": (576, 1024),
    "4:3": (1024, 768),
    "3:4": (768, 1024),
    "4:5": (1024, 1280),
    "1:1": (1024, 1024),
}


class ProductionReadyLegacyBridge(ReferenceAwareLegacyCandidateV3Bridge):
    """Original workbench UX backed by the production-quality V3 media path."""

    @staticmethod
    def _image_dimensions(params: dict[str, Any]) -> tuple[int, int]:
        if int(params.get("width") or 0) > 0 and int(params.get("height") or 0) > 0:
            return int(params["width"]), int(params["height"])
        return _ASPECT_SIZES.get(str(params.get("aspect_ratio") or "16:9"), (1024, 576))

    def _adopted_previous_shot(self, project_id: str, formal: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the immediately preceding, adopted frame in this continuity chain."""
        link = formal.get("continuity_link") if isinstance(formal.get("continuity_link"), dict) else {}
        if link.get("mode") != "inherit":
            return None
        previous_id = str(link.get("from_shot_id") or "").strip()
        if not previous_id or previous_id == str(formal.get("shot_id") or ""):
            raise ValueError("分镜连续性链缺少合法的上一镜头 ID")
        state = self.shot_authoring._load(project_id)
        previous = next((row for row in state.get("shots") or [] if row.get("shot_id") == previous_id), None)
        if not isinstance(previous, dict) or previous.get("scene_id") != formal.get("scene_id"):
            raise ValueError("分镜连续性链引用了不存在或不同场景的镜头")
        logical_key = self._logical_key(previous_id, "image")
        adopted = self.resources.adopted(project_id, logical_key)
        if adopted is None:
            return None
        resource_id = str(adopted.get("resource_id") or "")
        candidate = next((row for row in self.legacy._wb_load_candidates(project_id)
                          if str(row.get("v3_resource_id") or "") == resource_id
                          and str(row.get("v3_logical_key") or "") == logical_key
                          and str(row.get("confirmed_asset_id") or "")), None)
        if candidate is None:
            raise ValueError("上一镜头 V3 资源虽已采用，但找不到对应的正式图片资产")
        asset_id = str(candidate["confirmed_asset_id"])
        asset = self.legacy.director.production.get_asset(project_id, asset_id)
        if self._status_value(asset.get("status")) != "ready" or self._status_value(asset.get("dependency_state")) == "stale":
            raise ValueError("上一镜头已采用图片失效，请先重新生成并采用上一镜头")
        path = self._asset_path(project_id, asset_id)
        ref_id = f"shot-adopted:{project_id}:{asset_id}"
        self.references.import_file(ref_id, path, role="previous_shot", entity_type="shot")
        covered = {str(x) for x in previous.get("character_entity_ids") or [] if str(x)}
        covered.update(str(x) for x in previous.get("prop_entity_ids") or [] if str(x))
        scene = next((row for row in state.get("scenes") or [] if row.get("scene_id") == previous.get("scene_id")), {})
        if scene.get("location_entity_id"):
            covered.add(str(scene["location_entity_id"]))
        return {"reference_id": ref_id, "shot_id": previous_id,
                "asset_id": asset_id, "covered_entity_ids": sorted(covered)}

    def _approved_scene_plate(self, project_id: str, formal: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve one adopted, revision-bound camera plate from the existing asset graph."""
        shot_id = str(formal.get("shot_id") or "").strip()
        contract = self.shot_authoring.active_contract(project_id, shot_id)
        contract_id = str((contract or {}).get("asset_id") or "")
        rows = [asset for asset in self.legacy.director.production.list_assets(project_id, active_only=True)
                if str(asset.get("logical_key") or "") == f"studio:shot:{shot_id}:scene-plate"
                and str(asset.get("asset_role") or "") == "shot_scene_plate"
                and self._status_value(asset.get("status")) == "ready"
                and self._status_value(asset.get("dependency_state")) != "stale"
                and str((asset.get("metadata") or {}).get("shot_contract_asset_id") or "") == contract_id
                and str((asset.get("metadata") or {}).get("storyboard_source_sha256") or "")
                    == str(formal.get("storyboard_source_sha256") or "")
                and str((asset.get("metadata") or {}).get("manual_revision_id") or "")
                    == str((formal.get("manual_revision") or {}).get("revision_id") or "")]
        if not rows:
            return None
        rows.sort(key=lambda asset: int(asset.get("version") or 0))
        asset = rows[-1]
        metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
        regions = [ShotSupportRegion.model_validate(item).model_dump(mode="json")
                   for item in metadata.get("support_regions") or []]
        if not regions:
            raise ValueError("已采用场景底图缺少可站立区域，不能生成双人分镜")
        path = self._asset_path(project_id, str(asset["asset_id"]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(metadata.get("sha256") or ""):
            raise ValueError("场景底图文件与已采用资产版本的 SHA256 不一致")
        return {"asset_id": str(asset["asset_id"]), "sha256": digest,
                "url": self.legacy.director.production.asset_url(project_id, asset["asset_id"]),
                "support_regions": regions}

    def _shot_reference_manifest(self, project_id: str, target: dict[str, Any], formal: dict[str, Any],
                                 *, use_scene_plate: bool = False) -> tuple[list[str], dict[str, Any] | None, list[str]]:
        """Prefer an adopted continuity frame when it covers the current identities.

        A multi-person shot without a usable continuity frame still binds every
        canonical reference to the spatial ControlNet path.
        """
        required = self._relevant_entity_ids(project_id, target)
        canonical = self._candidate_reference_ids(project_id, target)
        previous = None if use_scene_plate else self._adopted_previous_shot(project_id, formal)
        covered = set(previous["covered_entity_ids"]) & required if previous else set()
        needed = required - covered
        by_entity = {self.references.resolve(ref_id).entity_id: ref_id for ref_id in canonical}
        missing = required - set(by_entity) - covered
        if missing:
            raise ValueError(f"当前分镜缺少 canonical 参考图：{sorted(missing)}")
        refs = ([str(previous["reference_id"])] if previous else []) + [by_entity[eid] for eid in sorted(needed)]
        if len(refs) > 3:
            if len(formal.get("character_entity_ids") or []) > 1:
                missing = required - set(by_entity)
                if missing:
                    raise ValueError(f"当前分镜缺少 canonical 参考图：{sorted(missing)}")
                return [by_entity[eid] for eid in sorted(required)], None, sorted(required)
            raise ValueError(
                f"当前镜头需要 {len(refs)} 张同时生效的参考图，Qwen Image Edit 工作流只有 3 个输入槽。"
                "请先采用连续链的上一镜头，或在 Stage04 将这一画面拆成可生成的镜头；禁止分两轮假装五张同时约束。"
            )
        if not refs or len(set(refs)) != len(refs):
            raise ValueError("分镜参考图清单为空或包含重复绑定")
        return refs, previous, sorted(required)

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
        metadata.update({
            "quality_tier": infer_quality_tier(formal),
            "quality_mode": "smart",
            "quality_stage": str(params.get("quality_stage") or "preview"),
        })
        next_payload["metadata"] = metadata
        return next_payload

    def _reference_ids_from_assets(self, project_id: str, asset_ids: list[str]) -> list[str]:
        production = self.legacy.director.production
        refs: list[str] = []
        for asset_id in asset_ids:
            asset_id = str(asset_id or "").strip()
            if not asset_id:
                continue
            asset = production.get_asset(project_id, asset_id)
            if str(asset.get("asset_type") or "").upper() != "IMAGE":
                raise ValueError("角色锁脸锚点必须是图片资产")
            if self._status_value(asset.get("status")) != "ready":
                raise ValueError("角色锁脸锚点尚未采用，不能生成三视图")
            if self._status_value(asset.get("dependency_state")) == "stale":
                raise ValueError("角色锁脸锚点已经过期，请重新生成并采用")
            path = self._asset_path(project_id, asset_id)
            entity_id = next((str(x).strip() for x in asset.get("entity_ids") or [] if str(x).strip()), "")
            ref_id = f"face-anchor:{project_id}:{asset_id}"
            self.references.import_file(
                ref_id,
                path,
                entity_id=entity_id,
                role=str(asset.get("asset_role") or "character_face_anchor"),
                entity_type="character",
            )
            refs.append(ref_id)
        if not refs:
            raise ValueError("三视图生成缺少已采用的角色锁脸锚点")
        return refs

    async def _execute_reference_first_target(
        self,
        project_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        reference_asset_ids: list[str],
    ) -> dict[str, Any]:
        prompt_asset_id = str(payload.get("prompt_asset_id") or "").strip()
        prompt = self._prompt_text(project_id, prompt_asset_id)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        reference_ids = self._reference_ids_from_assets(project_id, reference_asset_ids)
        width, height = self._image_dimensions(params)

        task_id = "v3task_" + secrets.token_hex(10)
        workflow_id = "studio-v3-ref-" + secrets.token_hex(10)
        target_asset_id = str(target.get("asset_id") or "").strip()
        logical_key = f"studio-v3:reference:{target_asset_id}:image"
        step_key = f"{workflow_id}:image-generate"
        seed = int(params.get("seed") or 0)
        if seed < 0:
            seed = secrets.randbelow(2_147_483_646) + 1

        step_payload = {
            "logical_key": logical_key,
            "provider_id": "local-comfyui-image",
            "model_id": "configured-image-workflow",
            "shot_id": f"reference-{target_asset_id}",
            "source_text": prompt,
            **visual_contract_fields({**payload, **params}),
            "reference_ids": reference_ids,
            "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
            "camera_direction": "neutral character reference",
            "action": "neutral standing turnaround",
            "duration_seconds": 1.0,
            "width": width,
            "height": height,
            "steps": int(params.get("steps") or 36),
            "cfg": float(params.get("cfg") or 6.0),
            "seed": seed,
            "sampler_name": str(params.get("sampler_name") or params.get("sampler") or "dpmpp_2m"),
            "scheduler": str(params.get("scheduler") or "karras"),
            "metadata": {
                "source": "character_face_anchor_reference_first",
                "legacy_target_asset_id": target_asset_id,
                "reference_phase": str(params.get("reference_phase") or "turnaround"),
                "face_anchor_asset_ids": reference_asset_ids,
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
        request = ProductionWorkflowInput(workflow_id=workflow_id, project_id=project_id, steps=(step,))
        task_record = self.legacy.store.create(
            task_id=task_id,
            module="新版工作流",
            operation="角色定装图生成" if params.get("reference_phase") == "costume" else "角色三视图生成",
            title=str(target.get("name") or "角色三视图"),
            params={
                "v3_workflow_id": workflow_id,
                "v3_logical_key": logical_key,
                "legacy_target_asset_id": target_asset_id,
                "reference_phase": str(params.get("reference_phase") or "turnaround"),
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
            dependencies=[prompt_asset_id, *reference_asset_ids],
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
            "producer": "v3_temporal_reference_first",
            "manual_adoption_required": True,
            "reference_ids": reference_ids,
            "face_anchor_asset_ids": reference_asset_ids,
        }

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
                params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
                reference_asset_ids = [str(x).strip() for x in params.get("reference_asset_ids") or [] if str(x).strip()]
                if reference_asset_ids:
                    return await self._execute_reference_first_target(project_id, target, payload, reference_asset_ids)
            return await self.original_execute(project_id, payload)

        target = self.shot_authoring.bind_active_contract_to_target(project_id, target)
        formal = self._formal_shot(project_id, target)
        next_payload = self._smart_payload(payload, formal=formal, capability=capability)

        shot_manifest = None
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
            if str(target.get("asset_role") or "") == "shot_keyframe":
                scene_plate = self._approved_scene_plate(project_id, formal)
                if len(formal.get("character_entity_ids") or []) > 1 and scene_plate is None:
                    raise ValueError("双人分镜缺少已采用的场景机位底图与可站立区域；当前地点图无法证明人物脚点，已阻断生成")
                shot_manifest = self._shot_reference_manifest(
                    project_id, target, formal, use_scene_plate=bool(scene_plate))

                reference_ids, previous_shot, _required = shot_manifest
                canonical_names = {
                    str(entity.get("entity_id") or ""): str(entity.get("name") or "")
                    for entity in self.legacy.director.production.list_entities(project_id)
                }
                semantics = []
                for reference_id in reference_ids:
                    reference = self.references.resolve(reference_id)
                    semantics.append({
                        "reference_id": reference_id,
                        "entity_id": reference.entity_id,
                        "entity_type": reference.entity_type,
                        "role": reference.role,
                        "name": canonical_names.get(reference.entity_id, ""),
                        **({"previous_shot_id": previous_shot["shot_id"],
                            "covered_entity_ids": previous_shot["covered_entity_ids"],
                            "adopted_asset_id": previous_shot["asset_id"]}
                           if previous_shot and reference_id == previous_shot["reference_id"] else {}),
                    })
                source_prompt_asset_id = str(next_payload.get("prompt_asset_id") or "")
                visual = await self.shot_authoring.ensure_visual_plan(
                    project_id, formal,
                    source_prompt_asset_id=source_prompt_asset_id,
                    reference_semantics=semantics,
                    scene_plate=scene_plate,
                )
                await self.legacy.gpu.ensure_ready(GPUOwner.comfyui)
                plan_id = str(visual["asset_id"])
                prompt_text = self.shot_authoring.render_visual_plan(project_id, visual["plan"], formal=formal)
                next_payload["shot_visual_plan"] = visual["plan"]
                next_payload["shot_prompt_context"] = {
                    "shot_id": str(formal["shot_id"]),
                    "formal_shot_fingerprint": hashlib.sha256(
                        json.dumps(formal, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    ).hexdigest(),
                    "visual_plan_asset_id": plan_id,
                    "reference_ids": reference_ids,
                }
                prompt_key = f"studio:shot:{formal['shot_id']}:visual-prompt"
                prompt_assets = [row for row in self.legacy.director.production.list_assets(project_id, active_only=True)
                                 if str(row.get("logical_key") or "") == prompt_key
                                 and str((row.get("metadata") or {}).get("visual_plan_asset_id") or "") == plan_id
                                 and self._status_value(row.get("status")) == "ready"
                                 and self._status_value(row.get("dependency_state")) != "stale"]
                visual_prompt_id = ""
                if prompt_assets:
                    prompt_assets.sort(key=lambda row: int(row.get("version") or 0))
                    current_prompt = prompt_assets[-1]
                    if self.legacy.director.production.read_text_asset(
                        project_id, current_prompt["asset_id"]
                    ) == prompt_text:
                        visual_prompt_id = str(current_prompt["asset_id"])
                if not visual_prompt_id:
                    prompt_asset = self.legacy.director.production.create_text_asset(
                        project_id, stage="04", skill="xiaoduan-storyboard-director",
                        logical_key=prompt_key, asset_role="shot_visual_prompt",
                        name=f"镜头 {formal['shot_id']} · 单帧画面提示词",
                        content=prompt_text, entity_ids=list(_required),
                        parent_asset_ids=[plan_id],
                        metadata={"shot_id": formal["shot_id"], "visual_plan_asset_id": plan_id},
                    )
                    visual_prompt_id = str(prompt_asset["asset_id"])
                next_payload["prompt_asset_id"] = visual_prompt_id

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
        common_metadata.update({"source": "original_workbench_v3_bridge", "legacy_target_asset_id": target_asset_id, "shot_id": shot_id})

        if capability == "image":
            shot_keyframe = str(target.get("asset_role") or "") == "shot_keyframe"
            previous_shot = None
            required_reference_entity_ids: list[str] = []
            if shot_keyframe:
                reference_ids, previous_shot, required_reference_entity_ids = shot_manifest
            else:
                reference_ids = self._candidate_reference_ids(project_id, target)
            canonical_names = {
                str(entity.get("entity_id") or ""): str(entity.get("name") or "")
                for entity in self.legacy.director.production.list_entities(project_id)
            }
            reference_semantics = []
            for reference_id in reference_ids:
                reference = self.references.resolve(reference_id)
                reference_semantics.append({
                    "reference_id": reference_id,
                    "entity_id": reference.entity_id,
                    "entity_type": reference.entity_type,
                    "role": reference.role,
                    "name": canonical_names.get(reference.entity_id, ""),
                    **({"previous_shot_id": previous_shot["shot_id"],
                        "covered_entity_ids": previous_shot["covered_entity_ids"],
                        "adopted_asset_id": previous_shot["asset_id"]}
                       if previous_shot and reference_id == previous_shot["reference_id"] else {}),
                })
            width, height = self._image_dimensions(params)
            profile = profile_for_shot(formal)
            quality_stage = str(params.get("quality_stage") or "preview")
            steps = int(params.get("steps") or (profile.image_final_steps if quality_stage == "final" else profile.image_candidate_steps))
            operation = "generation.image.generate_candidate"
            step_payload = {
                "logical_key": logical_key,
                "provider_id": ("local-zimage-shot-controlnet" if len(formal.get("character_entity_ids") or []) > 1 and not previous_shot
                                else "local-qwen-image-edit") if shot_keyframe else "local-comfyui-image",
                "model_id": ("z-image-turbo-fun-controlnet-union" if len(formal.get("character_entity_ids") or []) > 1 and not previous_shot
                             else "qwen-image-edit-2511") if shot_keyframe else "configured-image-workflow",
                "shot_id": shot_id,
                "source_text": prompt,
                **visual_contract_fields({**next_payload, **params}),
                "reference_ids": reference_ids,
                "reference_semantics": reference_semantics,
                "shot_visual_plan": next_payload.get("shot_visual_plan") if shot_keyframe else None,
                "scene_plate": scene_plate if shot_keyframe else None,
                "identity_names": {eid: canonical_names.get(eid, eid)
                                   for eid in required_reference_entity_ids} if shot_keyframe else {},
                "required_reference_entity_ids": required_reference_entity_ids,
                "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
                "camera_direction": str(formal.get("camera") or ""),
                "scene_environment": str(formal.get("environment") or ""),
                "shot_size": str(formal.get("shot_size") or ""),
                "action": str(formal.get("action") or ""),
                "continuity": str(formal.get("continuity") or ""),
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
                    "reference_phase": "shot_keyframe" if shot_keyframe else "",
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
                "metadata": {**common_metadata, "legacy_first_frame_asset_id": first_frame_asset_id},
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
        request = ProductionWorkflowInput(workflow_id=workflow_id, project_id=project_id, steps=(step,))
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
            self.legacy.store.update(task_id, status=TaskStatus.running, progress=5, message="正在生成候选")
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
                (item for item in reversed(versions) if str(item.get("generation_task_id") or "") == step_key),
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
            # Temporal wraps the Activity exception in WorkflowFailureError.
            # Surface the actual worker failure, not only "Workflow execution failed".
            parts: list[str] = []
            cause: BaseException | None = exc
            seen: set[int] = set()
            while cause is not None and id(cause) not in seen and len(parts) < 5:
                seen.add(id(cause))
                label = str(cause).strip()
                if label:
                    parts.append(f"{type(cause).__name__}: {label}")
                cause = getattr(cause, "cause", None) or cause.__cause__
            detail = " → ".join(parts)[-1600:] or "未知错误"
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
