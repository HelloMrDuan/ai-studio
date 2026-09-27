from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from PIL import Image

from app.models import GPUOwner
from app.v3.storyboard.contracts import ShotSupportRegion, ShotVisualPlan, snap_shot_support, validate_shot_support


_EDITABLE_FIELDS = (
    "title",
    "summary",
    "duration_seconds",
    "composition",
    "shot_size",
    "camera",
    "camera_move",
    "action",
    "performance",
    "environment",
    "dialogue",
    "narration",
    "sound",
    "music",
    "continuity",
    "representative_state",
    "video_start_state",
    "video_end_state",
    "image_prompt",
    "video_start_prompt",
    "video_prompt",
    "character_entity_ids",
    "prop_entity_ids",
)
_SHOT_MEDIA_ROLES = {
    "shot_keyframe",
    "shot_video_start_frame",
    "shot_clip",
    "shot_image_processed",
    "shot_video_processed",
    "shot_scene_plate",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


def _story_prop_holders(
    state: dict[str, Any], formal: dict[str, Any],
    prop_ids: list[str], character_ids: list[str],
) -> dict[str, str]:
    """Resolve canonical prop ownership from source-grounded continuity events."""
    scene_id = _clean(formal.get("scene_id"))
    authority: dict[str, str] = {}
    for prop_id in prop_ids:
        events = [
            row for row in state.get("events") or []
            if isinstance(row, dict)
            and _clean(row.get("target_type")) == "prop"
            and _clean(row.get("target_id")) == prop_id
            and _clean(row.get("source_kind")) == "story"
            and _clean(row.get("scope")) == "persistent"
            and _clean((row.get("patch") or {}).get("holder_entity_id"))
        ]
        local = [row for row in events if _clean(row.get("scene_id")) == scene_id]
        relevant = local or events
        holders = {_clean(row["patch"]["holder_entity_id"]) for row in relevant}
        if len(holders) > 1:
            raise ValueError(f"canonical prop {prop_id} has conflicting story holder events")
        if holders:
            holder = next(iter(holders))
            if holder not in character_ids:
                raise ValueError(f"canonical prop {prop_id} is held by a character outside the shot")
            authority[prop_id] = holder
    return authority


def _reconcile_prop_holders(
    raw: dict[str, Any], expected: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bind visual semantics to canonical prop IDs by the frozen holder relation."""
    result = json.loads(json.dumps(raw))
    rows = result.get("prop_placements")
    if not isinstance(rows, list):
        raise ValueError("visual plan has no typed prop placements")
    original = {str(row.get("entity_id") or ""): row for row in rows if isinstance(row, dict)}
    if len(original) != len(rows):
        raise ValueError("visual plan has duplicate or invalid prop identities")
    corrected = []
    used: set[int] = set()
    changes: list[dict[str, Any]] = []
    for prop_id in original:
        holder = expected.get(prop_id)
        current = original[prop_id]
        if holder is None:
            if current.get("attachment") != "world" or _clean(current.get("holder_entity_id")):
                raise ValueError(f"canonical prop {prop_id} has no source-grounded holder")
            chosen = current
        else:
            matches = [row for row in rows if isinstance(row, dict)
                       and _clean(row.get("holder_entity_id")) == holder
                       and id(row) not in used]
            exact = [row for row in matches if _clean(row.get("entity_id")) == prop_id]
            if exact:
                chosen = exact[0]
            elif len(matches) == 1:
                chosen = matches[0]
            else:
                raise ValueError(f"canonical prop {prop_id} cannot be bound to its story holder")
        used.add(id(chosen))
        rendered = json.loads(json.dumps(chosen))
        if _clean(rendered.get("entity_id")) != prop_id:
            changes.append({
                "entity_id": prop_id,
                "model_entity_id": _clean(rendered.get("entity_id")),
                "rule": "source_grounded_prop_holder_rebinding",
            })
            rendered["entity_id"] = prop_id
        corrected.append(rendered)
    if len(used) != len(rows):
        raise ValueError("visual plan has unclaimed prop placement rows")
    result["prop_placements"] = corrected
    return result, changes

def _normalize_back_prop_geometry(raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Derive back-carried prop depth and size from the holder's typed view."""
    result = json.loads(json.dumps(raw))
    subjects = {
        str(row.get("entity_id") or ""): row
        for row in result.get("subject_positions") or [] if isinstance(row, dict)
    }
    corrections: list[dict[str, Any]] = []
    for prop in result.get("prop_placements") or []:
        if not isinstance(prop, dict) or prop.get("attachment") != "back":
            continue
        owner = subjects.get(str(prop.get("holder_entity_id") or ""))
        if not owner or owner.get("body_view") not in {"front", "back", "left_profile", "right_profile"}:
            continue
        box = owner.get("screen_box")
        source_box = prop.get("screen_box")
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or not isinstance(source_box, (list, tuple)) or len(source_box) != 4):
            continue
        try:
            left, top, right, bottom = (float(value) for value in box)
            prop_left, _, prop_right, _ = (float(value) for value in source_box)
        except (TypeError, ValueError):
            continue
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            continue
        span_x, span_y = right - left, bottom - top
        anchor = prop.get("attachment_anchor")
        proposed_x = float(anchor[0]) if isinstance(anchor, (list, tuple)) and len(anchor) == 2 else (left + right) / 2
        center_x = ((left + right) / 2 if owner["body_view"] == "back"
                    else min(right - 0.15 * span_x, max(left + 0.15 * span_x, proposed_x)))
        prop_width = min(0.52 * span_x, max(0.28 * span_x, prop_right - prop_left))
        prop_top = top + 0.10 * span_y
        prop_bottom = top + 0.55 * span_y
        prop["screen_box"] = [
            round(max(0.0, center_x - prop_width / 2), 4), round(prop_top, 4),
            round(min(1.0, center_x + prop_width / 2), 4), round(prop_bottom, 4),
        ]
        prop["attachment_anchor"] = [round(center_x, 4), round(top + 0.20 * span_y, 4)]
        prop["layer"] = "front_of_subject" if owner["body_view"] == "back" else "behind_subject"
        corrections.append({
            "entity_id": str(prop.get("entity_id") or ""),
            "holder_entity_id": str(prop.get("holder_entity_id") or ""),
            "rule": "typed_back_attachment_geometry",
            "screen_box": prop["screen_box"], "layer": prop["layer"],
        })
    return result, corrections

class ShotAuthoringService:
    """Edit one formal shot without reopening the whole storyboard stage.

    A manual shot edit is persisted as a new versioned shot-contract asset.
    Only media bound to that shot and their real descendants are invalidated.
    The existing Stage04 generation routes remain the production gate: after an
    edit, image/video generation still has to satisfy the strict shot contract.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self.root = Path(settings.data_dir) / "story_continuity"

    def _path(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        return self.root / f"{project_id}.json"

    def _load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            raise FileNotFoundError("当前作品还没有正式分镜数据")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("分镜数据格式无效")
        if not isinstance(value.get("shots"), list):
            value["shots"] = []
        return value

    def _save(self, project_id: str, state: dict[str, Any]) -> None:
        path = self._path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _shot(state: dict[str, Any], shot_id: str) -> dict[str, Any]:
        wanted = _clean(shot_id)
        for item in state.get("shots") or []:
            if isinstance(item, dict) and _clean(item.get("shot_id")) == wanted:
                return item
        raise FileNotFoundError(f"正式镜头不存在：{shot_id}")

    @staticmethod
    def _scene(state: dict[str, Any], scene_id: str) -> dict[str, Any]:
        wanted = _clean(scene_id)
        for item in state.get("scenes") or []:
            if isinstance(item, dict) and _clean(item.get("scene_id")) == wanted:
                return item
        return {}

    @staticmethod
    def contract_key(shot_id: str) -> str:
        return f"studio:shot:{shot_id}:manual-contract"

    def active_contract(self, project_id: str, shot_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == self.contract_key(shot_id)
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _profile_parent(self, project_id: str, entity_id: str) -> str:
        key = f"studio:authoring:{entity_id}:profile"
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == key
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return _clean((rows[-1] if rows else {}).get("asset_id"))

    async def ensure_visual_plan(
        self,
        project_id: str,
        formal: dict[str, Any],
        *,
        source_prompt_asset_id: str,
        reference_semantics: list[dict[str, Any]],
        scene_plate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Author only the drawable frame, after Stage04 narrative is frozen.

        The visual author may describe composition and placement but cannot
        change the shot ID or any canonical character/location/prop identity.
        Its versioned asset is the prompt compiler's input for this submission.
        """
        shot_id = _clean(formal.get("shot_id"))
        state = self._load(project_id)
        scene = self._scene(state, _clean(formal.get("scene_id")))
        location_id = _clean(scene.get("location_entity_id"))
        char_ids = [_clean(x) for x in formal.get("character_entity_ids") or [] if _clean(x)]
        prop_ids = [_clean(x) for x in formal.get("prop_entity_ids") or [] if _clean(x)]
        if not shot_id or not location_id or not _clean(formal.get("representative_state")):
            raise ValueError("正式镜头缺少 shot_id、canonical 地点或静态代表状态")
        if len(char_ids) != len(set(char_ids)) or len(prop_ids) != len(set(prop_ids)):
            raise ValueError("正式镜头的 canonical 角色或道具 ID 重复")
        identities = {
            _clean(row.get("entity_id")): {"entity_id": _clean(row.get("entity_id")),
                "entity_type": _clean(row.get("entity_type")), "name": _clean(row.get("name"))}
            for row in self.production.list_entities(project_id)
        }
        for entity_id, kind in [(location_id, "location"), *[(x, "character") for x in char_ids],
                                *[(x, "prop") for x in prop_ids]]:
            if identities.get(entity_id, {}).get("entity_type") != kind:
                raise ValueError(f"正式镜头引用的 {kind} 不是现有 canonical entity：{entity_id}")

        prop_holders = _story_prop_holders(self._load(project_id), formal, prop_ids, char_ids)
        source = {
            "visual_plan_contract_version": 13,
            "shot_id": shot_id,
            "representative_state": _clean(formal.get("representative_state")),
            "summary": _clean(formal.get("summary")),
            "composition": _clean(formal.get("composition")),
            "shot_size": _clean(formal.get("shot_size")),
            "camera": _clean(formal.get("camera")),
            "environment": _clean(formal.get("environment")),
            "location_entity_id": location_id,
            "character_entity_ids": char_ids,
            "prop_entity_ids": prop_ids,
            "prop_holder_entity_ids": prop_holders,
            "identities": [identities[x] for x in [location_id, *char_ids, *prop_ids]],
            "reference_semantics": reference_semantics,
            "scene_plate": {key: scene_plate[key] for key in ("asset_id", "sha256", "support_regions")}
            if scene_plate else None,
            "source_prompt_asset_id": source_prompt_asset_id,
        }
        fingerprint = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        logical_key = f"studio:shot:{shot_id}:visual-plan"
        existing = [row for row in self.production.list_assets(project_id, active_only=True)
                    if _clean(row.get("logical_key")) == logical_key
                    and _clean(row.get("status")).lower() == "ready"
                    and _clean(row.get("dependency_state")).lower() != "stale"
                    and _clean((row.get("metadata") or {}).get("source_fingerprint")) == fingerprint]
        if existing:
            existing.sort(key=lambda row: int(row.get("version") or 0))
            return {"asset_id": _clean(existing[-1].get("asset_id")),
                    "plan": json.loads(self.production.read_text_asset(project_id, existing[-1]["asset_id"]))}

        system_prompt = (
            "你是分镜静态画面摄影师。输入是已经冻结的正式镜头和 canonical 资产；"
            "只能把 representative_state 编写为一张此刻可拍的静态图，不能改剧情、增删镜头、"
            "创造角色地点道具或改任何 entity_id。道具持有人只能取 prop_holder_entity_ids 的 canonical 映射，"
            "不得把不同道具的 entity_id 或持有人调换。明确屏幕位置、姿势视线、道具可见状态、"
            "构图；standing_surface 只能描述正式场景已有的地面材质、天气和可站立形态，"
            "不能为满足站位而编造广场或改变悬崖、石阶等地点结构；"
            "如有 scene_plate.support_regions，每个人物框的脚部中心和左右脚都必须落在其多边形内；"
            "每个人必须给出归一化的 screen_box=[左,上,右,下]，坐标在 0 到 1 之间，"
            "人物不能互相重叠，大小和前后景必须符合构图。任何 screen_box 坐标不得为负数或超过 1。"
            "背向镜头的人物 body_view 必须为 back，不能写成侧面；每人标注 body_view："
            "front、left_profile、right_profile 或 back，必须符合正式机位和人物视线。"
            "每件道具标注 attachment：hand、back、waist 或 world；hand 必须注明解剖学上的 hand_side=left/right；贴身道具给出真实附着点 attachment_anchor，"
            "点必须在道具框内且位于人物对应身体部位。动作过程和之后的结果不进入这一帧。只返回严格 JSON。"
            "顶层必须且只能有 shot_id、location_entity_id、frame_description、composition、"
            "standing_surface、subject_positions、prop_placements、visual_exclusions 八个键。"
            "不得返回 representative_state 或其他旧分镜结构。"
        )
        contract = (
            '{"shot_id":"","location_entity_id":"","frame_description":"",'
            '"composition":"","standing_surface":"","subject_positions":[{"entity_id":"",'
            '"screen_position":"","pose_and_gaze":"","body_view":null,"screen_box":null}],'
            '"prop_placements":[{"entity_id":"","holder_entity_id":"",'
            '"screen_position":"","visible_state":"","screen_box":null,'
            '"layer":null,"attachment":null,"attachment_anchor":null,"hand_side":null}],'
            '"visual_exclusions":[]}'
        )
        output_template = {
            "shot_id": shot_id,
            "location_entity_id": location_id,
            "frame_description": "填写这一帧可见的静态画面，不写动作过程",
            "composition": "填写景别、镜头角度、前中后景和主体位置",
            "standing_surface": "只填写正式场景中人物脚下可站立的地面材质与形态，不写人物道具",
            "subject_positions": [
                {"entity_id": entity_id, "screen_position": "填写画面位置",
                 "pose_and_gaze": "填写姿势和视线", "body_view": None,
                 "screen_box": None}
                for entity_id in char_ids
            ],
            "prop_placements": [
                {"entity_id": entity_id, "holder_entity_id": prop_holders.get(entity_id, ""),
                 "screen_position": "填写画面位置", "visible_state": "填写可见状态",
                 "screen_box": None, "layer": None,
                 "attachment": None, "attachment_anchor": None, "hand_side": None}
                for entity_id in prop_ids
            ],
            "visual_exclusions": [],
        }
        await self.legacy.gpu.ensure_ready(GPUOwner.gemma)
        user_prompt = (
            "FROZEN_SHOT=\n" + json.dumps(source, ensure_ascii=False) +
            "\n\nREQUIRED_OUTPUT_TEMPLATE=\n" + json.dumps(output_template, ensure_ascii=False) +
            "\n\n只填写模板中的描述字段。所有 ID 原样复制。subject_positions 和 prop_placements "
            "必须保留模板数组的长度、顺序、entity_id 和已有 holder_entity_id。按镜头构图调整 screen_box；"
            "standing_surface 必须与 FROZEN_SHOT.environment 一致，不得发明新地形；"
            "如有 scene_plate.support_regions，人物 screen_box 底边的脚点必须落在其中，"
            "不能将人放到多边形外的山壁、悬崖或空中；"
            "坐标表示最终画面的外接矩形，人物必须框全身，道具要框实际可见范围。"
            "道具的 attachment_anchor 是与人物身体接触的坐标，必须落在道具 screen_box 内。"
            "手持道具的 hand_side 来自正式动作描述；attachment_anchor 在实际握持部位，不能放在胸前衣襟。"
            "背负道具的框顶端和附着点必须在人物上背部，不能放到腰侧。"
            "例如人物框 [0.12,0.12,0.42,0.92]，背负道具框顶端 y 不得大于 0.36，"
            "锚点 y 不得大于 0.456；细长背负物的可见框要覆盖肩至腰，不能只画腰侧小框。"
            "道具的 screen_box 必须贴近其持有人手部或背部；layer 只可为 behind_subject、"
            "front_of_subject、world。背对镜头的背负物取 front_of_subject，正面或侧面背负物取 behind_subject。"
            "背负长物要显出肩至腰的完整长度，不能缩成被头发和衣服遮住的小把手；手持取 front_of_subject，"
            "没有持有人取 world 且 holder_entity_id 为空字符串。"
            "只输出填写后的 JSON 对象。"
        )
        for attempt in range(3):
            _, raw_plan, _ = await self.director._structured_json_call(
                phase=f"studio_stage04_visual_plan_qwen32b_{attempt + 1}",
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=1500,
                contract=contract,
            )
            try:
                raw_plan, binding_corrections = _reconcile_prop_holders(raw_plan, prop_holders)
                raw_plan, placement_corrections = _normalize_back_prop_geometry(raw_plan)
                plan = ShotVisualPlan.model_validate(raw_plan)
                if plan.shot_id != shot_id or plan.location_entity_id != location_id:
                    raise ValueError("视觉编写改动了正式镜头或 canonical 地点")
                subject_ids = [row.entity_id for row in plan.subject_positions]
                prop_plan_ids = [row.entity_id for row in plan.prop_placements]
                if len(subject_ids) != len(set(subject_ids)) or set(subject_ids) != set(char_ids):
                    raise ValueError("视觉编写的可见角色与正式分镜 canonical ID 不一致")
                if len(prop_plan_ids) != len(set(prop_plan_ids)) or set(prop_plan_ids) != set(prop_ids):
                    raise ValueError("视觉编写的道具与正式分镜 canonical ID 不一致")
                if any(row.holder_entity_id and row.holder_entity_id not in char_ids for row in plan.prop_placements):
                    raise ValueError("视觉编写把道具交给了镜头外角色")
                ground_corrections = []
                if scene_plate:
                    regions = [ShotSupportRegion.model_validate(item)
                               for item in scene_plate["support_regions"]]
                    plan, ground_corrections = snap_shot_support(plan, regions)
                    validate_shot_support(plan, regions)
                subject_boxes = {row.entity_id: row.screen_box for row in plan.subject_positions}
                for row in plan.prop_placements:
                    if row.holder_entity_id:
                        owner = subject_boxes[row.holder_entity_id]
                        prop = row.screen_box
                        margin = 0.08
                        if (prop[2] < owner[0] - margin or prop[0] > owner[2] + margin
                                or prop[3] < owner[1] - margin or prop[1] > owner[3] + margin):
                            raise ValueError("道具 screen_box 与 canonical 持有人空间分离")
                        if row.attachment_anchor is None:
                            raise ValueError("贴身道具缺少附着点")
                        anchor_x, anchor_y = row.attachment_anchor
                        relative_x = (anchor_x - owner[0]) / (owner[2] - owner[0])
                        relative_y = (anchor_y - owner[1]) / (owner[3] - owner[1])
                        regions = {"back": (0.1, 0.9, 0.08, 0.53),
                                   "hand": (-0.2, 1.2, 0.3, 0.85),
                                   "waist": (0.05, 0.95, 0.4, 0.72)}
                        xmin, xmax, ymin, ymax = regions[row.attachment]
                        if row.attachment != "hand" and not (xmin <= relative_x <= xmax and ymin <= relative_y <= ymax):
                            raise ValueError(f"道具 {row.attachment} 附着点不在持有人对应身体区域")
                boxes = [row.screen_box for row in plan.subject_positions]
                for left_index, left_box in enumerate(boxes):
                    for right_box in boxes[left_index + 1:]:
                        overlap = max(0, min(left_box[2], right_box[2]) - max(left_box[0], right_box[0])) * max(
                            0, min(left_box[3], right_box[3]) - max(left_box[1], right_box[1]))
                        if overlap > 0.35 * min(
                            (left_box[2] - left_box[0]) * (left_box[3] - left_box[1]),
                            (right_box[2] - right_box[0]) * (right_box[3] - right_box[1]),
                        ):
                            raise ValueError("视觉编写的人物位置重叠，无法独立约束身份")
                break
            except ValueError as error:
                if attempt == 2:
                    raise ValueError(
                        "视觉编写连续三次违反画面合同：" + str(error) +
                        "；最后一次模型输出：" + json.dumps(raw_plan, ensure_ascii=False)
                    ) from error
                user_prompt += (
                    "\n\n上一次输出不符合 REQUIRED_OUTPUT_TEMPLATE：" + str(error) +
                    "\n上一次输出：" + json.dumps(raw_plan, ensure_ascii=False) +
                    "\n请从 FROZEN_SHOT 重新填写模板，不能添加旧字段或新身份。"
                )
        asset = self.production.create_text_asset(
            project_id,
            stage="04",
            skill="xiaoduan-storyboard-director",
            logical_key=logical_key,
            asset_role="shot_visual_plan",
            name=f"镜头 {shot_id} · 静态画面合同",
            content=plan.model_dump_json(indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "formal_shot_visual_authoring", "shot_id": shot_id},
            parent_asset_ids=[source_prompt_asset_id],
            entity_ids=[location_id, *char_ids, *prop_ids],
            metadata={"shot_id": shot_id, "source_fingerprint": fingerprint,
                      "reference_ids": [row["reference_id"] for row in reference_semantics],
                      "placement_normalizations": binding_corrections + placement_corrections + ground_corrections},
        )
        return {"asset_id": _clean(asset.get("asset_id")), "plan": plan.model_dump(mode="json")}

    def render_visual_plan(self, project_id: str, plan: dict[str, Any],
                           *, formal: dict[str, Any] | None = None) -> str:
        names = {_clean(row.get("entity_id")): _clean(row.get("name"))
                 for row in self.production.list_entities(project_id)}
        subjects = [f"{names[row['entity_id']]}：{row['screen_position']}，{row['pose_and_gaze']}"
                    for row in plan["subject_positions"]]
        props = [f"{names[row['entity_id']]}：{row['screen_position']}，{row['visible_state']}，"
                 f"持有人：{names.get(row['holder_entity_id'], '无')}" for row in plan["prop_placements"]]
        formal = formal or {}
        return "\n".join([
            f"单帧画面：{plan['frame_description']}",
            *([f"正式景别：{_clean(formal.get('shot_size'))}"] if _clean(formal.get("shot_size")) else []),
            *([f"正式机位：{_clean(formal.get('camera'))}"] if _clean(formal.get("camera")) else []),
            f"构图：{plan['composition']}",
            *([f"站立地面：{plan['standing_surface']}"] if plan.get("standing_surface") else []),
            f"地点：{names[plan['location_entity_id']]}",
            "人物位置：" + "；".join(subjects or ["无人"]),
            "道具位置：" + "；".join(props or ["无"]),
            "禁止出现：" + "；".join(plan["visual_exclusions"]),
        ])

    def _entity_ids(self, state: dict[str, Any], shot: dict[str, Any]) -> list[str]:
        result = []
        result.extend(_clean(item) for item in shot.get("character_entity_ids") or [] if _clean(item))
        result.extend(_clean(item) for item in shot.get("prop_entity_ids") or [] if _clean(item))
        scene = self._scene(state, _clean(shot.get("scene_id")))
        location_id = _clean(scene.get("location_entity_id"))
        if location_id:
            result.append(location_id)
        shot_entity_id = _clean(shot.get("entity_id"))
        if shot_entity_id:
            result.append(shot_entity_id)
        return list(dict.fromkeys(result))

    def _contract_payload(self, shot: dict[str, Any], revision_id: str) -> dict[str, Any]:
        return {
            "schema_version": "xiaoduan_manual_shot_contract_v1",
            "revision_id": revision_id,
            "shot_id": _clean(shot.get("shot_id")),
            **{field: _copy(shot.get(field), shot.get(field)) for field in _EDITABLE_FIELDS},
            "scene_id": _clean(shot.get("scene_id")),
            "character_entity_ids": list(shot.get("character_entity_ids") or []),
            "prop_entity_ids": list(shot.get("prop_entity_ids") or []),
            "source_provenance": _copy(shot.get("source_provenance") or {}, {}),
        }

    def _invalidate_shot_media(
        self,
        project_id: str,
        shot: dict[str, Any],
        revision_id: str,
    ) -> list[str]:
        graph = self.production.get_graph(project_id)
        assets = graph.get("assets") or {}
        shot_id = _clean(shot.get("shot_id"))
        shot_entity_id = _clean(shot.get("entity_id"))
        stale_ids: set[str] = set()

        for asset_id, asset in assets.items():
            if not isinstance(asset, dict) or asset.get("active") is False:
                continue
            role = _clean(asset.get("asset_role"))
            if role not in _SHOT_MEDIA_ROLES:
                continue
            metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
            source = asset.get("source") if isinstance(asset.get("source"), dict) else {}
            bound_shot = _clean(metadata.get("shot_id") or source.get("shot_id"))
            entity_ids = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
            if bound_shot != shot_id and (not shot_entity_id or shot_entity_id not in entity_ids):
                continue
            asset["dependency_state"] = "stale"
            asset.setdefault("metadata", {})["stale_reason"] = "该镜头的分镜合同已修改"
            asset["metadata"]["shot_revision_id"] = revision_id
            asset["updated_at"] = _now()
            stale_ids.add(str(asset_id))

        changed = True
        while changed:
            changed = False
            for asset_id, asset in assets.items():
                if not isinstance(asset, dict) or asset.get("active") is False or str(asset_id) in stale_ids:
                    continue
                parents = {_clean(item) for item in asset.get("parent_asset_ids") or [] if _clean(item)}
                hits = parents & stale_ids
                if not hits:
                    continue
                asset["dependency_state"] = "stale"
                stale_parents = asset.setdefault("stale_parent_asset_ids", [])
                for parent_id in sorted(hits):
                    if parent_id not in stale_parents:
                        stale_parents.append(parent_id)
                metadata = asset.setdefault("metadata", {})
                metadata["stale_reason"] = "引用的镜头版本已修改"
                metadata["shot_revision_id"] = revision_id
                asset["updated_at"] = _now()
                stale_ids.add(str(asset_id))
                changed = True

        self.production._save(graph)
        return sorted(stale_ids)

    def get(self, project_id: str, shot_id: str) -> dict[str, Any]:
        state = self._load(project_id)
        shot = self._shot(state, shot_id)
        contract = self.active_contract(project_id, shot_id)
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "fields": {field: _copy(shot.get(field), shot.get(field)) for field in _EDITABLE_FIELDS},
            "contract_asset_id": _clean((contract or {}).get("asset_id")),
            "contract_version": int((contract or {}).get("version") or 0),
            "editable_fields": list(_EDITABLE_FIELDS),
        }

    def approve_scene_plate(self, project_id: str, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Version an inspected camera plate and its measured walkable regions."""
        state = self._load(project_id)
        shot = self._shot(state, shot_id)
        scene = self._scene(state, _clean(shot.get("scene_id")))
        location_id = _clean(scene.get("location_entity_id"))
        source_id = _clean(payload.get("source_location_asset_id"))
        source = self.production.get_asset(project_id, source_id)
        if (_clean(source.get("asset_role")) not in {"location_reference", "scene_reference"}
                or _clean(source.get("status")).lower() != "ready"
                or _clean(source.get("dependency_state")).lower() == "stale"
                or location_id not in source.get("entity_ids", [])):
            raise ValueError("场景机位底图必须继承当前地点已采用的 canonical 参考图")
        url = _clean(payload.get("image_url"))
        if not url.startswith("/files/"):
            raise ValueError("场景机位底图必须是平台内的图片文件")
        root = Path(self.settings.data_dir).resolve()
        path = (root / url.removeprefix("/files/")).resolve()
        if root not in path.parents or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("场景机位底图不存在或越出平台数据目录")
        with Image.open(path) as opened:
            if opened.width < 512 or opened.height < 512 or opened.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError("场景机位底图格式或分辨率不符合生产要求")
        regions = [ShotSupportRegion.model_validate(item).model_dump(mode="json")
                   for item in payload.get("support_regions") or []]
        if not regions:
            raise ValueError("场景机位底图必须标注至少一块可站立区域")
        contract = self.active_contract(project_id, shot_id)
        contract_id = _clean((contract or {}).get("asset_id"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        asset = self.production.register_existing_file(
            project_id, stage="04", skill="xiaoduan-storyboard-director",
            logical_key=f"studio:shot:{shot_id}:scene-plate",
            asset_type="IMAGE", asset_role="shot_scene_plate",
            name=f"镜头 {shot_id} · 已确认场景机位底图", url=url,
            source={"type": "approved_shot_scene_plate",
                    "comfy_prompt_id": _clean(payload.get("comfy_prompt_id"))},
            parent_asset_ids=[source_id, *([contract_id] if contract_id else [])],
            entity_ids=[location_id],
            metadata={"shot_id": shot_id, "location_entity_id": location_id,
                      "shot_contract_asset_id": contract_id,
                      "storyboard_source_sha256": _clean(shot.get("storyboard_source_sha256")),
                      "manual_revision_id": _clean((shot.get("manual_revision") or {}).get("revision_id")),
                      "sha256": digest, "support_regions": regions},
        )
        return {"asset_id": asset["asset_id"], "version": asset["version"],
                "image_url": url, "sha256": digest, "support_regions": regions}

    def update(self, project_id: str, shot_id: str, patch: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
        state = self._load(project_id)
        shot = self._shot(state, shot_id)
        applied: dict[str, Any] = {}
        for field in _EDITABLE_FIELDS:
            if field not in patch:
                continue
            if field == "duration_seconds":
                value = float(patch.get(field) or 0)
                if value <= 0 or value > 120:
                    raise ValueError("镜头时长必须大于0秒且不超过120秒")
                shot[field] = value
            elif field in {"character_entity_ids", "prop_entity_ids"}:
                supplied = patch.get(field)
                if not isinstance(supplied, list) or any(not isinstance(item, str) for item in supplied):
                    raise ValueError(f"{field} 必须是 canonical entity ID 列表")
                ids = [_clean(item) for item in supplied]
                if any(not item for item in ids) or len(ids) != len(set(ids)):
                    raise ValueError(f"{field} 含有空值或重复 ID")
                expected_type = "character" if field == "character_entity_ids" else "prop"
                known = {
                    _clean(item.get("entity_id")): _clean(item.get("entity_type")).lower()
                    for item in self.production.list_entities(project_id)
                }
                invalid = [item for item in ids if known.get(item) != expected_type]
                if invalid:
                    raise ValueError(f"{field} 只能引用现有 {expected_type} entity：{invalid}")
                shot[field] = ids
            else:
                shot[field] = _clean(patch.get(field))
            applied[field] = shot[field]
        if not applied:
            raise ValueError("没有提交可修改的镜头字段")
        if not _clean(shot.get("representative_state")):
            raise ValueError("镜头代表状态不能为空")
        if not _clean(shot.get("image_prompt")):
            raise ValueError("分镜画面生成要求不能为空")
        if not _clean(shot.get("video_prompt")):
            raise ValueError("视频生成要求不能为空")

        revision_id = f"shot-rev-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        shot["manual_revision"] = {
            "revision_id": revision_id,
            "reason": _clean(reason),
            "fields": sorted(applied),
            "updated_at": _now(),
        }
        # Preserve storyboard_source_sha256. It identifies the confirmed Stage04
        # source asset; keeping it stable means an ordinary continuity refresh
        # does not overwrite a local shot revision. A true Stage04 rebuild changes
        # the source hash and correctly replaces the local edit.
        self._save(project_id, state)

        entity_ids = self._entity_ids(state, shot)
        profile_parents = [
            self._profile_parent(project_id, entity_id)
            for entity_id in entity_ids
        ]
        profile_parents = [item for item in profile_parents if item]
        contract = self.production.create_text_asset(
            project_id,
            stage="04",
            skill="xiaoduan-shot-authoring",
            logical_key=self.contract_key(shot_id),
            asset_role="shot_contract",
            name=f"镜头 {shot_id} · 人工修改合同",
            content=json.dumps(self._contract_payload(shot, revision_id), ensure_ascii=False, indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "manual_shot_revision", "shot_id": shot_id},
            parent_asset_ids=profile_parents,
            entity_ids=entity_ids,
            metadata={
                "manual_revision": True,
                "shot_id": shot_id,
                "revision_id": revision_id,
                "reason": _clean(reason),
                "strict_generation_gate_preserved": True,
            },
        )
        stale = self._invalidate_shot_media(project_id, shot, revision_id)
        return {
            "updated": True,
            "project_id": project_id,
            "shot_id": shot_id,
            "revision_id": revision_id,
            "contract_asset_id": _clean(contract.get("asset_id")),
            "contract_version": int(contract.get("version") or 1),
            "changed_fields": sorted(applied),
            "stale_asset_count": len(stale),
            "stale_asset_ids": stale,
            "local_invalidation": True,
            "message": "镜头已保存为新版本；重新生成该镜头画面/视频时仍会执行现有严格合同校验。",
        }

    def bind_active_contract_to_target(self, project_id: str, target: dict[str, Any]) -> dict[str, Any]:
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        source = target.get("source") if isinstance(target.get("source"), dict) else {}
        shot_id = _clean(metadata.get("shot_id") or source.get("shot_id"))
        if not shot_id:
            return target
        contract = self.active_contract(project_id, shot_id)
        if not contract:
            return target
        return self.production.set_asset_dependencies(
            project_id,
            _clean(target.get("asset_id")),
            [_clean(contract.get("asset_id"))],
            merge=True,
        )


def create_shot_authoring_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ShotAuthoringService(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/shots/{shot_id}/authoring")
    async def get_shot_authoring(project_id: str, shot_id: str) -> dict[str, Any]:
        try:
            return service.get(project_id, shot_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/v3/studio/projects/{project_id}/shots/{shot_id}/authoring")
    async def update_shot_authoring(project_id: str, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
            return service.update(project_id, shot_id, fields, reason=_clean(payload.get("reason")))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/shots/{shot_id}/scene-plate/approve")
    async def approve_shot_scene_plate(project_id: str, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return service.approve_scene_plate(project_id, shot_id, payload)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


def install_shot_video_keyframe_route(app: Any, legacy_runtime: Any) -> None:
    """Make the adopted formal shot image the H3 first frame.

    The pinned workbench route required an independently generated video-start
    image. That broke the requested shot image -> adoption -> video lineage and
    allowed two different pictures to claim the same opening state.
    """
    path = "/api/studio/projects/{project_id}/shots/{shot_id}/generate-video"
    old = [route for route in app.router.routes if getattr(route, "path", "") == path]
    if not old:
        raise RuntimeError("formal shot video route is unavailable")
    app.router.routes[:] = [route for route in app.router.routes if route not in old]
    authoring = ShotAuthoringService(legacy_runtime.settings, legacy_runtime)

    async def generate_video(project_id: str, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            legacy_runtime.director.get_project(project_id)
            shot = legacy_runtime._studio_formal_shot(project_id, shot_id)
            legacy_runtime._studio_v2371_require_strict_shot(shot)
            fingerprint = legacy_runtime._studio_shot_contract_fingerprint(shot)
            first_frame = legacy_runtime._studio_current_role_asset(project_id, shot_id, "shot_keyframe")
            if not legacy_runtime._studio_rep_keyframe_valid(first_frame):
                raise ValueError("当前镜头还没有已采用的正式分镜图片")
            if _clean((first_frame.get("metadata") or {}).get("shot_contract_fingerprint")) != fingerprint:
                raise ValueError("已采用分镜图片属于旧镜头合同，请重新生成并采用")

            profile = _clean(payload.get("video_profile") or "standard").lower()
            if profile not in {"standard", "turbo"}:
                raise ValueError("video_profile 只能是 standard 或 turbo")
            aspect = _clean(payload.get("aspect_ratio") or "16:9")
            width, height = legacy_runtime._studio_shot_video_dimensions(aspect)
            intended, length, effective = legacy_runtime._studio_h3_length(shot.get("duration_seconds"))
            contract = authoring.active_contract(project_id, shot_id)
            parents = [first_frame["asset_id"]]
            if contract:
                parents.append(contract["asset_id"])
            prompt_asset = legacy_runtime.director.production.create_text_asset(
                project_id,
                stage="make",
                skill="xiaoduan-shot-video-from-keyframe",
                logical_key=f"studio:shot:{shot_id}:adopted-keyframe-video-prompt",
                asset_role="shot_video_prompt",
                name=f"镜头 {int(shot.get('global_order') or 0):03d} · 视频运动 Prompt",
                content=_clean(shot.get("video_prompt")),
                asset_type="TEXT",
                extension=".txt",
                source={"type": "formal_shot_video_prompt", "shot_id": shot_id},
                parent_asset_ids=parents,
                entity_ids=authoring._entity_ids(authoring._load(project_id), shot),
                metadata={
                    "shot_id": shot_id,
                    "shot_contract_fingerprint": fingerprint,
                    "first_frame_asset_id": first_frame["asset_id"],
                    "prompt_source": "stage04.video_prompt",
                },
            )
            target = legacy_runtime._studio_shot_target(
                project_id,
                shot,
                asset_type="VIDEO",
                asset_role="shot_clip",
                name=f"镜头 {int(shot.get('global_order') or 0):03d} · H3 视频",
                parent_asset_ids=[prompt_asset["asset_id"], first_frame["asset_id"]],
                extra_metadata={
                    "video_contract_version": "h3-adopted-keyframe-lineage-v1",
                    "stage04_contract_version": "strict-shot-v2",
                    "shot_contract_fingerprint": fingerprint,
                    "first_frame_asset_id": first_frame["asset_id"],
                    "first_frame_role": "shot_keyframe",
                    "video_motion_prompt_asset_id": prompt_asset["asset_id"],
                    "aspect_ratio": aspect,
                    "intended_duration_seconds": intended,
                    "h3_length": length,
                    "effective_duration_seconds": effective,
                    "fps": 24,
                    "video_profile": profile,
                },
            )
            return await legacy_runtime.director_workbench_execute_candidate(
                project_id,
                {
                    "target_asset_id": target["asset_id"],
                    "capability": "video",
                    "mode": "fl2va",
                    "prompt_asset_id": prompt_asset["asset_id"],
                    "first_frame_asset_id": first_frame["asset_id"],
                    "last_frame_asset_id": "",
                    "params": {
                        "mode": "fl2va",
                        "prompt": "",
                        "width": width,
                        "height": height,
                        "length": length,
                        "steps": 4 if profile == "turbo" else int(payload.get("steps") or 20),
                        "seed": -1,
                        "ref_image_size": "match",
                        "video_profile": profile,
                    },
                },
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    app.add_api_route(path, generate_video, methods=["POST"])
    app.openapi_schema = None


__all__ = ["ShotAuthoringService", "create_shot_authoring_router", "install_shot_video_keyframe_route"]
