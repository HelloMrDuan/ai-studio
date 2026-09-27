from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from PIL import Image

from app.services.production_assets import ProductionAssetService
from app.v3.contracts import Capability
from app.v3.generation_executor import ReferenceAsset, ReferenceAssetError
from app.v3.storyboard.contracts import ShotSupportRegion, ShotVisualPlan, validate_shot_support
from app.v3.zimage_temporal_executor import (
    ZImageTemporalExecutor,
    compile_character_front_prompt,
    compose_shot_control_layout,
)

from .contracts import StepActivityInput, StepActivityResult
from .production_cached_executor import CachedMaterializedDomainExecutor


_CHARACTER_PACKAGE_PHASES = {"costume", "turnaround"}
_DOMAIN_REFERENCE_PHASES = {"prop_identity", "location_identity"}


def build_shot_scene_plate_prompt(plan: ShotVisualPlan, environment: str) -> str:
    foot_points = [
        (round((row.screen_box[0] + row.screen_box[2]) / 2, 2),
         round(row.screen_box[3], 2))
        for row in plan.subject_positions
    ]
    return (
        "Empty filming location plate. The adopted location image is authoritative for "
        "topology, architecture, terrain materials, weather and historical period. "
        f"Formal shot environment: {environment.strip()}. "
        f"Standing surface in this shot: {plan.standing_surface}. "
        f"Show physically supported ground directly below the future foot positions {foot_points}; "
        "these positions are empty now. Preserve the source location's actual paths, stairs, "
        "cliff edges and ground materials. If the source camera cannot show both standing "
        "positions, choose a plausible nearby camera angle within the same location. "
        "Do not replace a narrow path, stair or cliff with a broad level plaza, a generic "
        "tiled floor or newly invented architecture. Retain all specified snow or other "
        "weather effects on the standing surface. The whole frame is unoccupied: no "
        "people, human shapes, statues, animals, props, loose objects, writing or collage."
    )


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

    def _approved_scene_plate_path(self, project_id: str, plan: ShotVisualPlan,
                                   plate: dict[str, Any]) -> Path:
        """Reopen the exact ready asset instead of trusting a submitted file path."""
        asset_id = str(plate.get("asset_id") or "")
        asset = ProductionAssetService(self.settings.data_dir).get_asset(project_id, asset_id)
        metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
        if (not asset.get("active") or str(asset.get("asset_role") or "") != "shot_scene_plate"
                or str(asset.get("status") or "").lower() != "ready"
                or str(asset.get("dependency_state") or "").lower() == "stale"
                or str(metadata.get("shot_id") or "") != plan.shot_id
                or str(metadata.get("location_entity_id") or "") != plan.location_entity_id):
            raise ValueError("场景机位底图不是当前正式镜头已采用的有效资产")
        url = str((asset.get("storage") or {}).get("url") or "")
        if not url.startswith("/files/") or url != str(plate.get("url") or ""):
            raise ValueError("场景机位底图的资产 URL 与本轮提交不一致")
        root = Path(self.settings.data_dir).resolve()
        path = (root / url.removeprefix("/files/")).resolve()
        if root not in path.parents or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("场景机位底图不存在或越出平台数据目录")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != str(metadata.get("sha256") or "") or digest != str(plate.get("sha256") or ""):
            raise ValueError("场景机位底图的文件版本与本轮提交不一致")
        if metadata.get("support_regions") != plate.get("support_regions"):
            raise ValueError("场景机位底图的可站立区域与本轮提交不一致")
        regions = [ShotSupportRegion.model_validate(item) for item in plate.get("support_regions") or []]
        validate_shot_support(plan, regions)
        return path

    @classmethod
    def _uses_zimage_primary(cls, payload: dict[str, Any], references: list[str]) -> bool:
        if not references:
            return True
        return cls._reference_phase(payload) in _CHARACTER_PACKAGE_PHASES

    def _costume_reference(self, references: list[str]) -> ReferenceAsset:
        for reference_id in references:
            asset = self.base.references.resolve(reference_id)
            role = str(asset.role or "").strip().lower()
            if role in {"character_costume_reference", "character_costume", "costume_reference"}:
                return asset
        raise ReferenceAssetError(
            "角色三视图缺少已采用的 character_costume_reference 图像条件"
        )

    def _face_reference(self, references: list[str]) -> ReferenceAsset:
        for reference_id in references:
            asset = self.base.references.resolve(reference_id)
            role = str(asset.role or "").strip().lower()
            if role in {"character_face_anchor", "face_anchor", "character_identity"}:
                return asset
        raise ReferenceAssetError("角色三视图缺少已采用的 character_face_anchor 图像条件")

    async def _person_boxes(self, image_path: str) -> list[list[float]]:
        """Count people before a plate or candidate can enter the asset graph."""
        model = self.settings.person_segmentation_model
        python = self.settings.identity_runtime_python
        if not model.is_file() or not python.is_file():
            raise ValueError("人物分割预检模型或运行环境不存在，不能无校验地生成多人物镜头")
        script = (
            "import json,sys;from ultralytics import YOLO;"
            "r=YOLO(sys.argv[1]).predict(sys.argv[2],device='cpu',conf=0.25,verbose=False)[0];"
            "print('PERSON_BOXES='+json.dumps([[float(x) for x in box] "
            "for box in r.boxes.xyxy.tolist()]))"
        )
        process = await asyncio.create_subprocess_exec(
            str(python), "-c", script, str(model), str(image_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("人物分割预检超时，镜头图像不进入生产") from None
        if process.returncode != 0:
            raise ValueError(f"人物分割预检失败：{stderr.decode(errors='replace')[-1000:]}")
        lines = [line.removeprefix("PERSON_BOXES=") for line in stdout.decode(errors="replace").splitlines()
                 if line.startswith("PERSON_BOXES=")]
        if len(lines) != 1:
            raise ValueError("人物分割预检没有返回可信的人数")
        return json.loads(lines[0])

    async def _person_wrist(
        self, cutout_path: str, screen_box: tuple[float, float, float, float],
        hand_side: str, width: int, height: int,
    ) -> tuple[float, float]:
        """Map an adopted character's anatomical wrist into the control canvas."""
        model = self.settings.person_pose_model
        python = self.settings.identity_runtime_python
        if not model.is_file() or not python.is_file():
            raise ValueError("person pose model is unavailable; cannot place a held prop")
        script = (
            "import json,sys;from ultralytics import YOLO;"
            "r=YOLO(sys.argv[1]).predict(sys.argv[2],device='cpu',conf=0.25,verbose=False)[0];"
            "print('WRISTS='+json.dumps({'count':len(r.boxes),'points':"
            "r.keypoints.data[0,[9,10],:].tolist() if r.keypoints is not None and len(r.boxes)==1 else []}))"
        )
        process = await asyncio.create_subprocess_exec(
            str(python), "-c", script, str(model), cutout_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("person pose detection timed out before shot rendering") from None
        if process.returncode != 0:
            raise ValueError(f"person pose detection failed: {stderr.decode(errors='replace')[-1000:]}")
        lines = [line.removeprefix("WRISTS=") for line in stdout.decode(errors="replace").splitlines()
                 if line.startswith("WRISTS=")]
        if len(lines) != 1:
            raise ValueError("person pose detection returned no trustworthy wrist coordinates")
        measured = json.loads(lines[0])
        if measured.get("count") != 1 or len(measured.get("points") or []) != 2:
            raise ValueError("adopted character reference has no unique pose for a held prop")
        wrist = measured["points"][0 if hand_side == "left" else 1]
        if len(wrist) < 3 or float(wrist[2]) < 0.65:
            raise ValueError(f"adopted character {hand_side} wrist is not visible enough")
        with Image.open(cutout_path) as opened:
            cutout = opened.convert("RGBA")
            bounds = cutout.getbbox()
        if bounds is None:
            raise ValueError("adopted character cutout has no visible foreground")
        left, top, right, bottom = screen_box
        box_width, box_height = round((right - left) * width), round((bottom - top) * height)
        crop_width, crop_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
        scale = min(box_width / crop_width, box_height / crop_height)
        resized_width = max(1, round(crop_width * scale))
        resized_height = max(1, round(crop_height * scale))
        dest_x = round(left * width + (box_width - resized_width) / 2)
        dest_y = round(bottom * height) - resized_height
        x = (dest_x + (float(wrist[0]) - bounds[0]) * resized_width / crop_width) / width
        y = (dest_y + (float(wrist[1]) - bounds[1]) * resized_height / crop_height) / height
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("detected wrist falls outside the shot control canvas")
        return x, y
    async def _person_cutout(self, source_path: str, body_view: str, target_path: str) -> None:
        """Extract one adopted character view without copying studio backdrop into the shot."""
        model = self.settings.person_segmentation_model
        python = self.settings.identity_runtime_python
        if not model.is_file() or not python.is_file():
            raise ValueError("人物分割模型或运行环境不存在，不能以灰背景裁切替代身份遮罩")
        script = "\n".join((
            "import sys, numpy as np",
            "from PIL import Image, ImageFilter",
            "from ultralytics import YOLO",
            "source, view, target, model = sys.argv[1:]",
            "image = Image.open(source).convert('RGB')",
            "if image.width >= image.height * 1.9:",
            "    panel_width = image.width // 3",
            "    panel = {'front': 0, 'left_profile': 1, 'right_profile': 1, 'back': 2}[view]",
            "    image = image.crop((panel*panel_width, 0, image.width if panel == 2 else (panel+1)*panel_width, image.height))",
            "    if view == 'right_profile': image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)",
            "elif view != 'front': raise ValueError('adopted side/back view requires a turnaround sheet')",
            "result = YOLO(model).predict(np.asarray(image), device='cpu', conf=0.35, verbose=False)[0]",
            "if result.masks is None or len(result.boxes) != 1: raise ValueError('character reference must contain exactly one detectable person')",
            "if float(result.boxes.conf[0]) < 0.6: raise ValueError('character reference person mask confidence is too low')",
            "mask = result.masks.data[0].cpu().numpy()",
            "alpha = Image.fromarray((mask*255).astype('uint8')).resize(image.size, Image.Resampling.BILINEAR)",
            "alpha = alpha.point(lambda value: 255 if value > 90 else 0).filter(ImageFilter.GaussianBlur(0.6))",
            "coverage = float(np.count_nonzero(np.asarray(alpha))) / (image.width * image.height)",
            "if not 0.02 < coverage < 0.65: raise ValueError('character person mask has implausible coverage')",
            "cutout = image.convert('RGBA'); cutout.putalpha(alpha); cutout.save(target)",
        ))
        process = await asyncio.create_subprocess_exec(
            str(python), "-c", script, source_path, body_view, target_path, str(model),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError("人物参考图分割超时，未生成分镜控制图") from None
        if process.returncode != 0:
            raise ValueError(f"人物参考图分割失败：{stderr.decode(errors='replace')[-1000:]}")

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
            if self._reference_phase(payload) == "character_master":
                # A master is a two-prompt transaction (front render, then
                # identity-conditioned side/back).  Reusing an older single
                # txt2img sheet would bypass the controlled-view workflow.
                return None
            if self._reference_phase(payload) in _DOMAIN_REFERENCE_PHASES:
                # The durable job includes a second image-conditioned edit;
                # a cached first-pass render is not the finished candidate.
                return None
            if self._reference_phase(payload) == "shot_keyframe":
                # The final image may require two dependent Qwen edits. Reuse
                # only the durable job for this submission, never an old shot.
                return None
            if references and self._uses_zimage_primary(payload, references):
                return None
        return super().signature(project_id, operation, payload)

    async def _shot_generate_candidate(
        self, input: StepActivityInput, payload: dict[str, Any],
        references: list[str],
    ) -> StepActivityResult:
        visual_plan = payload.get("shot_visual_plan")
        if not isinstance(visual_plan, dict) or not visual_plan.get("composition"):
            raise ValueError("正式分镜缺少当前提交的结构化单帧画面合同")
        parsed_plan = ShotVisualPlan.model_validate(visual_plan)
        semantics = payload.get("reference_semantics")
        if not isinstance(semantics, list) or len(semantics) != len(references):
            raise ValueError("正式分镜缺少逐张参考图的 canonical 身份与类型")
        has_previous = any(isinstance(item, dict) and item.get("entity_type") == "shot"
                           for item in semantics)
        controlled = len(visual_plan.get("subject_positions") or []) > 1 and not has_previous
        if not 1 <= len(references) <= (6 if controlled else 3):
            raise ValueError("分镜执行图的 canonical 参考图数量超出当前后端能力")
        records = {str(item.get("reference_id") or ""): item for item in semantics if isinstance(item, dict)}
        if set(records) != set(references):
            raise ValueError("正式分镜参考图身份与实际 reference_ids 不一致")
        by_kind: dict[str, list[str]] = {"shot": [], "location": [], "character": [], "prop": []}
        covered: set[str] = set()
        for ref_id in references:
            actual = self.base.references.resolve(ref_id)
            declared = records[ref_id]
            kind = str(declared.get("entity_type") or "").lower()
            if kind not in by_kind or kind != actual.entity_type or str(declared.get("entity_id") or "") != actual.entity_id:
                raise ValueError(f"正式分镜参考图类型或 canonical entity 不一致：{ref_id}")
            if kind == "shot":
                if str(declared.get("role") or "") != "previous_shot" or not declared.get("previous_shot_id"):
                    raise ValueError("上一镜头参考图缺少已采用镜头的来源证明")
                covered.update(str(eid) for eid in declared.get("covered_entity_ids") or [] if str(eid))
            else:
                covered.add(actual.entity_id)
            by_kind[kind].append(ref_id)
        if len(by_kind["shot"]) > 1 or (not by_kind["shot"] and len(by_kind["location"]) != 1):
            raise ValueError("分镜必须绑定一个地点参考图，或一个已采用的连续链前镜")
        if controlled and (by_kind["shot"] or len(by_kind["location"]) != 1
                           or len(by_kind["character"]) != len(visual_plan["subject_positions"])):
            raise ValueError("双人镜头的布局控制必须绑定地点及每位角色的已采用参考图")
        required = {str(eid) for eid in payload.get("required_reference_entity_ids") or [] if str(eid)}
        if not required or required - covered:
            raise ValueError(f"分镜参考图未覆盖所有 canonical 身份：{sorted(required - covered)}")
        ordered = by_kind["shot"] + by_kind["location"] + by_kind["character"] + by_kind["prop"]
        if len(ordered) != len(references) or len(set(ordered)) != len(references):
            raise ValueError("分镜参考图清单与实际输入不一致")
        stage1 = ordered
        stage2: list[str] = []

        job = self.jobs.get(input.project_id, input.step.idempotency_key)
        selected = self.base.providers.resolve(
            {Capability.image_generation, Capability.image_reference, Capability.multi_reference},
            provider_id=str(job.get("provider_id") or "") if job else (
                "local-zimage-shot-controlnet" if controlled else "local-qwen-image-edit"),
            model_id=str(job.get("model_id") or "") if job else (
                "z-image-turbo-fun-controlnet-union" if controlled else "qwen-image-edit-2511"),
        )
        self.base.providers.assert_reference_budget(selected, len(references))
        adapter = self.adapter_factory(selected.spec)
        if not controlled:
            await adapter.require_models({
                "UNETLoader": ("unet_name", "qwen_image_edit_2511_int8_convrot.safetensors"),
                "CLIPLoader": ("clip_name", "qwen_2.5_vl_7b_nvfp4.safetensors"),
                "VAELoader": ("vae_name", "qwen_image_vae.safetensors"),
            })
        renderer = ZImageTemporalExecutor(selected.spec, adapter)
        positive = str(payload.get("positive_prompt") or "").strip()
        if not positive:
            raise ValueError("正式分镜缺少已编译的 positive_prompt")
        negative = str(payload.get("negative_prompt") or "").strip()
        camera = str(payload.get("camera_direction") or "").strip()
        shot_size = str(payload.get("shot_size") or "").strip()

        def role_instruction(ids: list[str]) -> str:
            directions = []
            for index, ref_id in enumerate(ids, start=1):
                record = records[ref_id]
                kind = record["entity_type"]
                name = record.get("name") or record["entity_id"]
                if kind == "character":
                    rule = "copy this exact person's face, hair, garment design and garment color; do not recolor the costume"
                elif kind == "shot":
                    rule = "preserve its established environment, camera, people and visible props; apply only the new shot's explicit changes"
                elif kind == "prop":
                    rule = "copy the object's design; place it according to the holder and position contract, not its display pose in this reference"
                else:
                    rule = "preserve the established location identity"
                directions.append(f"Image {index} is the adopted {kind} reference for {name}; {rule}. ")
            return "".join(directions)

        common = (
            f"Formal shot: {positive} Camera: {camera}. Shot size: {shot_size}. "
            f"Avoid: {negative}. No extra people, duplicate props, floating garments, collage or text."
        )
        identity_names = {str(key): str(value) for key, value in
                          (payload.get("identity_names") or {}).items()}
        placements = []
        for row in visual_plan.get("subject_positions") or []:
            if not isinstance(row, dict) or not row.get("entity_id"):
                raise ValueError("单帧画面合同的人物位置无效")
            name = identity_names.get(str(row["entity_id"])) or str(row["entity_id"])
            placements.append(f"{name}: {row.get('screen_position')}; {row.get('pose_and_gaze')}.")
        for row in visual_plan.get("prop_placements") or []:
            if not isinstance(row, dict) or not row.get("entity_id"):
                raise ValueError("单帧画面合同的道具位置无效")
            name = identity_names.get(str(row["entity_id"])) or str(row["entity_id"])
            holder_id = str(row.get("holder_entity_id") or "")
            holder = identity_names.get(holder_id) or holder_id or "no holder"
            placements.append(
                f"Exactly one {name}: {row.get('screen_position')}; {row.get('visible_state')}; "
                f"held by {holder}."
            )
        layout_instruction = (
            f"MANDATORY FINAL-FRAME LAYOUT: {visual_plan['composition']}. "
            + " ".join(placements)
            + " These are target-image positions, not descriptions of the input images. "
              "Recompose the source frame when its existing subject positions conflict. "
              "A held prop must visibly be in the stated person's hand, not hanging from clothing. "
        )
        first_prompt = (
            role_instruction(stage1)
            + layout_instruction
            + " Compose one coherent frame at the specified instant. Do not make a posed portrait. "
            + common
        )
        second_prompt = ""
        if job is None:
            seed = int(payload.get("seed") if payload.get("seed") is not None else -1)
            if seed < 0:
                seed = secrets.randbelow(2**63 - 1)
            if controlled:
                scene_plate = payload.get("scene_plate")
                if not isinstance(scene_plate, dict):
                    raise ValueError("双人分镜缺少已采用的可站立场景机位底图")
                plate_path = self._approved_scene_plate_path(input.project_id, parsed_plan, scene_plate)
                job = self.jobs.put(input.project_id, input.step.idempotency_key, {
                    "kind": "image", "state": "shot_scene_plate_ready",
                    "provider_id": selected.spec.provider_id, "model_id": selected.spec.model_id,
                    "reference_ids": list(references), "scene_plate_asset_id": scene_plate["asset_id"],
                    "scene_plate_sha256": scene_plate["sha256"],
                    "scene_plate_path": str(plate_path), "seed": seed,
                })
            else:
                queued = await renderer.queue_shot_edit(
                    image_paths=[self.base.references.resolve(ref_id).path for ref_id in stage1],
                    prompt=first_prompt, seed=seed,
                    filename_prefix=f"Xiaoduan/QwenShot/{input.step.idempotency_key}",
                )
                job = self.jobs.put(input.project_id, input.step.idempotency_key, {
                    "kind": "image", "state": "shot_stage1_queued",
                    "provider_id": selected.spec.provider_id, "model_id": selected.spec.model_id,
                    "reference_ids": list(references),
                    "stage1_reference_ids": list(stage1), "stage2_reference_ids": list(stage2),
                    "stage1_bindings": queued.get("reference_bindings") or [queued.get("layout_binding")],
                    "runtime_image_backend": "qwen_image_edit_2511_single_bound_shot",
                    "stage1_prompt_id": str(queued["prompt_id"]), "prompt_id": str(queued["prompt_id"]),
                    "generation_params": {"seed": seed, "positive_prompt": positive,
                                          "negative_prompt": negative, "stage1_prompt": first_prompt},
                })
        elif list(job.get("reference_ids") or []) != list(references):
            raise ValueError("持久化分镜任务的参考图与本轮提交不一致")

        if controlled and job.get("state") == "shot_scene_plate_ready":
            scene_plate = payload.get("scene_plate")
            if not isinstance(scene_plate, dict) or scene_plate.get("asset_id") != job.get("scene_plate_asset_id"):
                raise ValueError("持久化任务的场景机位底图与本轮提交不一致")
            plate_path = self._approved_scene_plate_path(input.project_id, parsed_plan, scene_plate)
            if hashlib.sha256(plate_path.read_bytes()).hexdigest() != job.get("scene_plate_sha256"):
                raise ValueError("持久化任务的场景机位底图文件已变化")
            with Image.open(plate_path) as opened:
                if opened.width < 512 or opened.height < 512:
                    raise ValueError("镜头空场景底图分辨率不足")
            plate_people = await self._person_boxes(str(plate_path))
            if plate_people:
                raise ValueError(f"镜头空场景底图出现 {len(plate_people)} 人，已阻断进入分镜控制图")
            seed = int(job["seed"])
            if controlled:
                subject_rows = {str(row["entity_id"]): row for row in visual_plan["subject_positions"]}
                character_refs = {records[ref_id]["entity_id"]: ref_id for ref_id in by_kind["character"]}
                if set(subject_rows) != set(character_refs):
                    raise ValueError("布局合同角色和实际 canonical 参考图不一致")
                prop_rows = {str(row["entity_id"]): row for row in visual_plan.get("prop_placements") or []}
                prop_refs = {records[ref_id]["entity_id"]: ref_id for ref_id in by_kind["prop"]}
                if set(prop_rows) != set(prop_refs):
                    raise ValueError("布局合同道具和实际 canonical 参考图不一致")
                dimensions = (int(payload.get("width") or 1392), int(payload.get("height") or 752))
                subject_cutouts = []
                for index, (eid, row) in enumerate(subject_rows.items()):
                    source_path = self.base.references.resolve(character_refs[eid]).path
                    cutout_path = self.image_root / f"{self._artifact_id('image', input.step.idempotency_key)}-subject-{index}.png"
                    if not cutout_path.is_file() or cutout_path.stat().st_size <= 0:
                        await self._person_cutout(str(source_path), str(row["body_view"]), str(cutout_path))
                    subject_cutouts.append({"reference_id": character_refs[eid],
                                            "body_view": str(row["body_view"]), "path": str(cutout_path),
                                            "screen_box": row["screen_box"]})
                cutouts_by_entity = dict(zip(subject_rows, subject_cutouts))
                layout_props = []
                prop_wrist_bindings = []
                for eid, row in prop_rows.items():
                    box = tuple(float(value) for value in row["screen_box"])
                    if row["attachment"] == "hand":
                        holder_id = str(row["holder_entity_id"])
                        side = str(row.get("hand_side") or "")
                        if holder_id not in cutouts_by_entity or side not in {"left", "right"}:
                            raise ValueError("hand prop has no visible canonical holder or anatomical side")
                        wrist = await self._person_wrist(
                            cutouts_by_entity[holder_id]["path"],
                            tuple(float(value) for value in subject_rows[holder_id]["screen_box"]),
                            side, *dimensions,
                        )
                        anchor = tuple(float(value) for value in row["attachment_anchor"])
                        dx, dy = wrist[0] - anchor[0], wrist[1] - anchor[1]
                        box = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                        if not (0 <= box[0] < box[2] <= 1 and 0 <= box[1] < box[3] <= 1):
                            raise ValueError("wrist-bound prop would fall outside the shot frame")
                        prop_wrist_bindings.append({
                            "entity_id": eid, "holder_entity_id": holder_id,
                            "hand_side": side, "wrist": [round(v, 4) for v in wrist],
                            "effective_screen_box": [round(v, 4) for v in box],
                        })
                    layout_props.append((
                        self.base.references.resolve(prop_refs[eid]).path, box,
                        str(row["layer"]), str(row["attachment"])
                    ))
                layout_png = compose_shot_control_layout(
                    location_path=plate_path,
                    subjects=[
                        (cutout["path"], tuple(float(value) for value in cutout["screen_box"]), "front")
                        for cutout in subject_cutouts
                    ],
                    props=layout_props,
                    width=dimensions[0], height=dimensions[1],
                )
                queued = await renderer.queue_shot_controlnet(
                    layout_png=layout_png,
                    positive_prompt=(layout_instruction + common +
                                     " Held props are physically attached to the measured anatomical wrist in the control image. "
                                     "Preserve that grip and the exact canonical costumes. "),
                    seed=seed, width=dimensions[0], height=dimensions[1],
                    filename_prefix=f"Xiaoduan/ControlledShot/{input.step.idempotency_key}",
                )
            job.update({
                "state": "shot_stage1_queued",
                "scene_plate_path": str(plate_path),
                "character_cutout_bindings": subject_cutouts,
                "prop_wrist_bindings": prop_wrist_bindings,
                "stage1_reference_ids": list(stage1), "stage2_reference_ids": list(stage2),
                "stage1_bindings": queued.get("reference_bindings") or [queued.get("layout_binding")],
                "runtime_image_backend": "zimage_fun_union_controlnet",
                "stage1_prompt_id": str(queued["prompt_id"]), "prompt_id": str(queued["prompt_id"]),
                "generation_params": {"seed": seed, "positive_prompt": positive,
                                      "negative_prompt": negative, "stage1_prompt": first_prompt,
                                      "scene_plate_asset_id": job["scene_plate_asset_id"]},
            })
            job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

        stage1_artifact = job.get("stage1_artifact") if isinstance(job.get("stage1_artifact"), dict) else None
        if stage1_artifact is None:
            stage1_artifact = await self._wait_for_artifact(
                adapter, str(job["stage1_prompt_id"]),
                timeout_seconds=float(self.settings.comfyui_task_timeout_seconds),
            )
            job["stage1_artifact"] = stage1_artifact
            job["state"] = "shot_stage1_completed"
            job = self.jobs.put(input.project_id, input.step.idempotency_key, job)
        artifact_id = self._artifact_id("image", input.step.idempotency_key)
        base_path = self.image_root / f"{artifact_id}-shot-stage1{self._suffix(str(stage1_artifact.get('filename') or ''), 'image')}"
        if not base_path.is_file() or base_path.stat().st_size <= 0:
            await self._download_artifact(adapter, stage1_artifact, base_path)

        artifact = stage1_artifact

        suffix = self._suffix(str(artifact.get("filename") or ""), "image")
        target = self.image_root / f"{artifact_id}{suffix}"
        if not target.is_file() or target.stat().st_size <= 0:
            bytes_written = await self._download_artifact(adapter, artifact, target)
        else:
            bytes_written = target.stat().st_size
        if controlled:
            people = await self._person_boxes(str(target))
            expected = len(visual_plan["subject_positions"])
            if len(people) != expected:
                raise ValueError(f"分镜候选人物数量 {len(people)} 与正式镜头 {expected} 不一致，已阻断入库")
        artifact_ref = f"artifact://image/{artifact_id}"
        job.update({
            "state": "materialized", "artifact_ref": artifact_ref,
            "artifact_path": str(target), "bytes_written": bytes_written,
        })
        self.jobs.put(input.project_id, input.step.idempotency_key, job)
        candidate = self._candidate_once(
            input, payload, provider_id=selected.spec.provider_id,
            model_id=selected.spec.model_id, reference_ids=list(references),
            artifact_ref=artifact_ref, artifact_path=target,
            prompt_id=str(job["prompt_id"]), kind="image",
        )
        return StepActivityResult(
            kind="completed", output_ref=artifact_ref,
            metadata={
                "executor": "v3-unified-image-domain-executor",
                "runtime_image_backend": "zimage_fun_union_controlnet" if controlled else "qwen_image_edit_2511_single_bound_shot",
                "provider_id": selected.spec.provider_id,
                "model_id": selected.spec.model_id,
                "stage1_prompt_id": str(job["stage1_prompt_id"]),
                "stage2_prompt_id": str(job.get("stage2_prompt_id") or ""),
                "reference_count": str(len(references)),
                "stage1_reference_count": str(len(stage1)),
                "stage2_reference_count": str(len(stage2)),
                "resource_id": str(candidate.get("resource_id") or ""),
                "logical_key": str(candidate.get("logical_key") or ""),
                "artifact_path": str(target),
            },
        )

    async def _image_generate_candidate(
        self,
        input: StepActivityInput,
        payload: dict[str, Any],
    ) -> StepActivityResult:
        references = self._strings(payload, "reference_ids", required=False)
        phase = self._reference_phase(payload)
        if phase == "shot_keyframe":
            return await self._shot_generate_candidate(input, payload, references)
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
        controlled_master = phase == "character_master"
        domain_reference_edit = phase in _DOMAIN_REFERENCE_PHASES

        if controlled_master or phase == "turnaround":
            await adapter.require_models({
                "UNETLoader": ("unet_name", "qwen_image_edit_2511_int8_convrot.safetensors"),
                "CLIPLoader": ("clip_name", "qwen_2.5_vl_7b_nvfp4.safetensors"),
                "VAELoader": ("vae_name", "qwen_image_vae.safetensors"),
                "LoraLoaderModelOnly": (
                    "lora_name", "qwen-image-edit-2511-multiple-angles-lora.safetensors"
                ),
            })
        elif domain_reference_edit:
            await adapter.require_models({
                "UNETLoader": ("unet_name", "qwen_image_edit_2511_int8_convrot.safetensors"),
                "CLIPLoader": ("clip_name", "qwen_2.5_vl_7b_nvfp4.safetensors"),
                "VAELoader": ("vae_name", "qwen_image_vae.safetensors"),
            })

        if job is None:
            positive = str(payload.get("positive_prompt") or "").strip()
            if not positive:
                raise ValueError("provider-ready positive_prompt is required before Z-Image dispatch")
            negative = str(payload.get("negative_prompt") or "").strip()
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            identity = str(metadata.get("reference_identity") or "").strip()
            if domain_reference_edit and not identity:
                raise ValueError("道具或场景参考图缺少正式稳定设定，不能进行图像条件生成")
            seed = int(payload.get("seed") if payload.get("seed") is not None else -1)
            if seed < 0:
                seed = secrets.randbelow(2**63 - 1)
            width = int(payload.get("width") or 1024)
            height = int(payload.get("height") or 1024)
            if controlled_master:
                # The first and only free generation is the canonical front
                # costume view.  Side/back are generated below from this exact
                # image with identity LoRA plus deterministic pose controls.
                width, height = 768, 1024
                positive = compile_character_front_prompt(positive)
            if phase == "costume":
                # A square full-body render made the face too small for stable
                # identity post-processing. This portrait canvas was verified on
                # the real Z-Image workflow and keeps the full costume plus a
                # materially larger face region.
                width, height = max(width, 1536), max(height, 2048)
            print(
                "ZIMAGE_WORKER_INPUT "
                f"project_id={input.project_id} workflow={input.workflow_id} step={input.step.step_id} "
                f"phase={phase or 'plain'} refs={len(references)} hybrid_identity={hybrid_identity} "
                f"cfg=1.0 size={width}x{height} "
                f"positive={json.dumps(positive, ensure_ascii=False)} "
                f"negative={json.dumps(negative, ensure_ascii=False)}",
                flush=True,
            )
            zimage = ZImageTemporalExecutor(selected.spec, adapter)
            if phase == "turnaround":
                costume = self._costume_reference(references)
                face = self._face_reference(references)
                queued = await zimage.queue_turnaround(
                    adopted_costume_path=costume.path,
                    adopted_face_path=face.path,
                    positive_prompt=positive,
                    negative_prompt=negative,
                    seed=seed,
                    filename_prefix=f"Xiaoduan/QwenEdit/{input.step.idempotency_key}",
                )
                width, height = 2304, 1024
            else:
                queued = await zimage.queue(
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
                    "controlled_master": controlled_master,
                    "domain_reference_edit": domain_reference_edit,
                    "reference_identity": identity,
                    "generation_params": {
                        "width": width,
                        "height": height,
                        "steps": 40 if phase == "turnaround" else 9,
                        "cfg": 4.0 if phase == "turnaround" else 1.0,
                        "seed": seed,
                        "sampler_name": "euler",
                        "scheduler": "simple",
                        "positive_prompt": positive,
                        "negative_prompt": negative,
                        "image_conditioning": (
                            "clean_zimage_front_then_qwen_reference_camera_edits"
                            if controlled_master
                            else "adopted_costume_qwen_reference_camera_edits" if phase == "turnaround"
                            else ""
                        ),
                        "angle_editor_model": (
                            "qwen-image-edit-2511-int8+multiple-angles-lora"
                            if phase == "turnaround" else ""
                        ),
                    },
                },
            )
        elif str(job.get("kind") or "") != "image":
            raise ValueError("generation job kind mismatch")

        if controlled_master:
            zimage = ZImageTemporalExecutor(selected.spec, adapter)
            front_prompt_id = str(job.get("front_prompt_id") or job.get("prompt_id") or "").strip()
            if not front_prompt_id:
                raise ValueError("controlled character master is missing its front prompt id")
            front_artifact = job.get("front_artifact") if isinstance(job.get("front_artifact"), dict) else None
            if front_artifact is None:
                front_artifact = await self._wait_for_artifact(
                    adapter,
                    front_prompt_id,
                    timeout_seconds=float(self.settings.comfyui_task_timeout_seconds),
                )
                job["front_artifact"] = front_artifact
                job["state"] = "front_generated"
                job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

            artifact_id = self._artifact_id("image", input.step.idempotency_key)
            front_suffix = self._suffix(str(front_artifact.get("filename") or ""), "image")
            front_path = self.image_root / f"{artifact_id}-front{front_suffix}"
            if not front_path.is_file() or front_path.stat().st_size <= 0:
                await self._download_artifact(adapter, front_artifact, front_path)
            face_path = self.image_root / f"{artifact_id}-face.png"
            if not face_path.is_file() or face_path.stat().st_size <= 0:
                with Image.open(front_path) as front_image:
                    rgb = front_image.convert("RGB")
                    left = round(rgb.width * 0.18)
                    right = round(rgb.width * 0.82)
                    bottom = min(rgb.height, round(rgb.width * 0.64))
                    rgb.crop((left, 0, right, bottom)).resize(
                        (512, 512), Image.Resampling.LANCZOS,
                    ).save(face_path)

            turnaround_prompt_id = str(job.get("turnaround_prompt_id") or "").strip()
            if not turnaround_prompt_id:
                params = job.get("generation_params") if isinstance(job.get("generation_params"), dict) else {}
                queued = await zimage.queue_turnaround(
                    adopted_costume_path=front_path,
                    adopted_face_path=face_path,
                    positive_prompt=str(params.get("positive_prompt") or ""),
                    negative_prompt=str(params.get("negative_prompt") or ""),
                    seed=int(params.get("seed") or 0),
                    filename_prefix=f"Xiaoduan/QwenEdit/{input.step.idempotency_key}-master",
                )
                turnaround_prompt_id = str(queued["prompt_id"])
                job.update({
                    "front_prompt_id": front_prompt_id,
                    "front_artifact_path": str(front_path),
                    "face_crop_path": str(face_path),
                    "turnaround_prompt_id": turnaround_prompt_id,
                    "prompt_id": turnaround_prompt_id,
                    "artifact": None,
                    "state": "separate_views_queued",
                })
                params.update({
                    "width": 2304,
                    "height": 1024,
                    "steps": 40,
                    "cfg": 4.0,
                    "angle_editor_model": "qwen-image-edit-2511-int8+multiple-angles-lora",
                })
                job["generation_params"] = params
                job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

        if domain_reference_edit:
            zimage = ZImageTemporalExecutor(selected.spec, adapter)
            base_prompt_id = str(job.get("base_prompt_id") or job.get("prompt_id") or "").strip()
            if not base_prompt_id:
                raise ValueError("domain reference is missing its Z-Image base prompt id")
            base_artifact = job.get("base_artifact") if isinstance(job.get("base_artifact"), dict) else None
            if base_artifact is None:
                base_artifact = await self._wait_for_artifact(
                    adapter, base_prompt_id,
                    timeout_seconds=float(self.settings.comfyui_task_timeout_seconds),
                )
                job["base_artifact"] = base_artifact
                job["state"] = "base_generated"
                job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

            artifact_id = self._artifact_id("image", input.step.idempotency_key)
            base_suffix = self._suffix(str(base_artifact.get("filename") or ""), "image")
            base_path = self.image_root / f"{artifact_id}-base{base_suffix}"
            if not base_path.is_file() or base_path.stat().st_size <= 0:
                await self._download_artifact(adapter, base_artifact, base_path)

            edit_prompt_id = str(job.get("edit_prompt_id") or "").strip()
            if not edit_prompt_id:
                identity = str(job.get("reference_identity") or "").strip()
                params = job.get("generation_params") if isinstance(job.get("generation_params"), dict) else {}
                queued = await zimage.queue_domain_reference_edit(
                    source_path=base_path, identity=identity,
                    kind="prop" if phase == "prop_identity" else "location",
                    seed=int(params.get("seed") or 0),
                    filename_prefix=f"Xiaoduan/QwenEdit/{input.step.idempotency_key}-domain",
                )
                edit_prompt_id = str(queued["prompt_id"])
                job.update({
                    "base_prompt_id": base_prompt_id,
                    "base_artifact_path": str(base_path),
                    "edit_prompt_id": edit_prompt_id,
                    "prompt_id": edit_prompt_id,
                    "artifact": None,
                    "state": "domain_edit_queued",
                })
                job = self.jobs.put(input.project_id, input.step.idempotency_key, job)

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

        requires_identity_postprocess = hybrid_identity or controlled_master
        candidate = {} if requires_identity_postprocess else self._candidate_once(
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
                "runtime_image_backend": (
                    "z_image_turbo_front_qwen_edit_angles_facefusion"
                    if controlled_master
                    else "qwen_image_edit_angles_facefusion" if phase == "turnaround"
                    else "z_image_turbo_qwen_domain_reference_edit" if domain_reference_edit
                    else "z_image_turbo_facefusion" if hybrid_identity
                    else "z_image_turbo"
                ),
                "identity_postprocess_required": "true" if requires_identity_postprocess else "false",
                "identity_source_path": str(job.get("face_crop_path") or "") if controlled_master else "",
                "view_structure_control": "fixed_front_plus_reference_bound_side_and_back_camera_edits" if controlled_master else "",
                "front_prompt_id": str(job.get("front_prompt_id") or ""),
                "base_prompt_id": str(job.get("base_prompt_id") or ""),
                "width": str(params.get("width") or ""),
                "height": str(params.get("height") or ""),
                "steps": str(params.get("steps") or ""),
                "cfg": str(params.get("cfg") or ""),
                "sampler": "euler/simple",
            },
        )


__all__ = ["UnifiedImageDomainExecutor"]
