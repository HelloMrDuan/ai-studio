from __future__ import annotations

import json
import hashlib
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from PIL import Image
from starlette.responses import JSONResponse

from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap
from app.v3.reference_assets import _ACTIVE, _PENDING, _clean


logger = logging.getLogger(__name__)


_FACE_TOKENS = (
    "年龄", "岁", "少年", "少女", "脸", "面部", "脸型", "五官", "眉", "眼", "鼻", "嘴", "轮廓",
    "发型", "发色", "黑发", "肤色", "气质", "冷峻", "清秀", "age", "face", "facial", "hair", "skin", "teen",
)
_COSTUME_TOKENS = (
    "服装", "上衣", "下装", "衣", "袍", "鞋", "靴", "配饰", "配色", "颜色", "纹样", "刺绣", "材质",
    "剑", "剑鞘", "体型", "身高", "costume", "clothing", "outfit", "robe", "shoe", "boot", "accessory", "color", "body", "build",
)


def _atomic_facts(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_atomic_facts(item, next_prefix, depth + 1))
        return rows
    if isinstance(value, list):
        for item in value[:24]:
            rows.extend(_atomic_facts(item, prefix, depth + 1))
        return rows
    text = str(value or "").strip()
    if not text:
        return rows
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"^[\s\-*#>]+", "", line).strip()
        if not line:
            continue
        rows.append(f"{prefix}：{line}" if prefix and len(text.splitlines()) <= 1 else line)
    return rows


def _select_facts(metadata: dict[str, Any], tokens: tuple[str, ...], limit: int = 18) -> list[str]:
    selected: list[str] = []
    for row in _atomic_facts(metadata):
        lowered = row.lower()
        if any(token in lowered for token in tokens):
            if row not in selected:
                selected.append(row)
    return selected[-limit:]


class CharacterReferencePackageBootstrap(CanonicalReferenceAssetBootstrap):
    """Production character package derived from one adopted identity master.

    Z-Image first renders one canonical front costume view.  The production
    worker then derives side/back views from that exact image with i2L identity
    conditioning and fixed pose controls, and composes the three panels in the
    workflow.  Adoption produces deterministic face/costume/turnaround assets
    without introducing a second asset system.
    """

    _MASTER_LAYOUT = "character_identity_master_v2_controlled_three_view"
    _DERIVATION_VERSION = "character_master_controlled_v3"

    @staticmethod
    def _master_view_regions(image: Image.Image) -> list[tuple[int, int]]:
        """Locate the three head/upper-body regions without a vision dependency.

        Controlled masters use a neutral studio background and fixed panel
        composition.  This remains a final corruption guard; panel creation is
        enforced by the generation graph itself.
        """
        sample = image.convert("RGB").resize((256, 128))
        pixels = list(sample.getdata())
        background: list[int] = []
        for channel in range(3):
            values = sorted(pixel[channel] for pixel in pixels)
            background.append(values[len(values) // 2])

        projection: list[float] = []
        scan_height = 48
        for x in range(sample.width):
            foreground = 0
            for y in range(scan_height):
                pixel = sample.getpixel((x, y))
                distance = sum(abs(pixel[index] - background[index]) for index in range(3))
                chroma = max(pixel) - min(pixel)
                if distance > 60 or chroma > 28:
                    foreground += 1
            projection.append(foreground / scan_height)

        smoothed = [
            sum(projection[max(0, x - 2):min(sample.width, x + 3)])
            / len(projection[max(0, x - 2):min(sample.width, x + 3)])
            for x in range(sample.width)
        ]
        groups: list[tuple[int, int]] = []
        start: int | None = None
        for x, value in enumerate([*smoothed, 0.0]):
            if value >= 0.12 and start is None:
                start = x
            elif value < 0.12 and start is not None:
                if x - start >= 10:
                    groups.append((start, x))
                start = None
        scale = image.width / sample.width
        return [(round(left * scale), round(right * scale)) for left, right in groups]

    @staticmethod
    def _master_has_stacked_portrait(image: Image.Image) -> bool:
        """Reject a second large face rendered below the first-panel portrait."""
        rgb = image.convert("RGB")
        lower_portrait = rgb.crop((0, rgb.height // 2, rgb.width // 4, rgb.height * 3 // 4)).resize((128, 64))
        skin_pixels = 0
        for red, green, blue in lower_portrait.getdata():
            if (
                red > 80
                and green > 40
                and blue > 20
                and max(red, green, blue) - min(red, green, blue) > 15
                and abs(red - green) > 5
                and red > green
                and red > blue
            ):
                skin_pixels += 1
        return skin_pixels / (128 * 64) > 0.10

    @staticmethod
    def _turnaround_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:turnaround-derived:{entity_id}:image{suffix}"

    @staticmethod
    def _costume_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:costume-anchor:{entity_id}:image{suffix}"

    @staticmethod
    def _costume_prompt_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:costume-anchor:{entity_id}:prompt{suffix}"

    @staticmethod
    def _package_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:character-reference-package:{entity_id}{suffix}"

    def _costume_target(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        key = self._costume_key(
            _clean(entity.get("entity_id")),
            _clean(entity.get("appearance_version")) or "v1",
        )
        rows = [
            asset for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("logical_key")) == key
            and _clean(asset.get("asset_type")).upper() == "IMAGE"
            and _clean(asset.get("asset_role")) == "character_costume_reference"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _ready_costume(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        target = self._costume_target(project_id, entity)
        if target is None:
            return None
        if _clean(target.get("status")).lower() != "ready":
            return None
        if _clean(target.get("dependency_state")).lower() == "stale":
            return None
        return target

    def _master_prompt(self, entity: dict[str, Any]) -> str:
        """Compile the sole free-generation step: one canonical front view."""
        rejected = ("未明确", "待角色设计", "未指定", "待确认", "authority", "参考图要求")
        facts = []
        for row in _atomic_facts(entity.get("metadata") or {}):
            lowered = row.lower()
            if any(marker.lower() in lowered for marker in rejected):
                continue
            if not any(token in lowered for token in (*_FACE_TOKENS, *_COSTUME_TOKENS)):
                continue
            value = row.split("：", 1)[-1].strip()
            if value and value not in facts:
                facts.append(value)
        fact_text = "；".join(facts[-16:]) or "保持已确认的年龄、脸部、发型、体型和服装事实。"
        return (
            "专业角色身份母版的正面源图。只生成同一角色的一张严格正面全身定装图："
            "中性站姿，双眼可见，双肩正对镜头且左右对称，从头到脚完整可见。"
            "脸部、眼睛、年龄、妆容、发型和发饰必须清晰，服装的领口、袖型、腰带、"
            "下摆、鞋履、材质和颜色必须完整可辨。\n"
            f"角色视觉事实：{fact_text}\n"
            "使用中性浅灰摄影棚背景。全图只能有一个人物和一个身体；禁止拼图、分栏、三视图、"
            "侧面、背面、近景插图、双层肖像、重复人物；禁止任何汉字、字母、数字、角色名、"
            "标题、标签、字段、标注、网格或水印；禁止裁掉头部或脚。"
        )

    def _ensure_master_target_and_prompt(
        self,
        project_id: str,
        entity: dict[str, Any],
        *,
        prompt_override: str = "",
        force_new: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_id = _clean(entity.get("entity_id"))
        name = _clean(entity.get("name")) or "角色"
        version = _clean(entity.get("appearance_version")) or "v1"
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        evidence = [
            _clean(row.get("source_asset_id")) for row in entity.get("evidence") or []
            if isinstance(row, dict) and _clean(row.get("source_asset_id"))
        ]
        target = self._target(project_id, entity)
        metadata = target.get("metadata") if isinstance((target or {}).get("metadata"), dict) else {}
        if (
            target is None
            or force_new
            or _clean((target or {}).get("status")).lower() in {"archived", "superseded"}
            or _clean((target or {}).get("dependency_state")).lower() == "stale"
            or _clean(metadata.get("reference_layout")) != self._MASTER_LAYOUT
        ):
            target = self.director.production.declare_asset(
                project_id,
                stage="03",
                skill="xiaoduan-character-identity-master",
                logical_key=self._logical_key(entity_id) + suffix,
                asset_type="IMAGE",
                asset_role="character_reference",
                name=f"{name} · 角色身份母版",
                status="planned",
                source={"type": "auto_character_identity_master", "entity_id": entity_id},
                parent_asset_ids=evidence[-16:],
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": "character",
                    "reference_phase": "character_master",
                    "reference_layout": self._MASTER_LAYOUT,
                    "manual_adoption_required": True,
                    "design_source": "formal_stable_profile",
                    "visual_context": {"appearance_version": version},
                },
            )

        requested = _clean(prompt_override)
        content = requested or self._master_prompt(entity)
        prompt = self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-character-identity-master",
            logical_key=self._prompt_key(entity_id) + suffix,
            asset_role="character_reference_prompt",
            name=f"{name} · 角色身份母版生成要求",
            content=content,
            asset_type="TEXT",
            extension=".txt",
            source={
                "type": "manual_reference_prompt" if requested else "auto_character_identity_master_prompt",
                "entity_id": entity_id,
            },
            parent_asset_ids=evidence[-16:],
            entity_ids=[entity_id],
            metadata={
                "reference_asset": True,
                "reference_kind": "character",
                "reference_phase": "character_master",
                "reference_layout": self._MASTER_LAYOUT,
                "user_edited": bool(requested),
            },
        )
        return target, prompt

    def _ready_master(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        target = self._ready_reference(project_id, entity)
        metadata = target.get("metadata") if isinstance((target or {}).get("metadata"), dict) else {}
        return target if _clean(metadata.get("reference_layout")) == self._MASTER_LAYOUT else None

    def _master_target(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        target = self._target(project_id, entity)
        metadata = target.get("metadata") if isinstance((target or {}).get("metadata"), dict) else {}
        return target if _clean(metadata.get("reference_layout")) == self._MASTER_LAYOUT else None

    def _ready_master_derivative(
        self,
        project_id: str,
        entity: dict[str, Any],
        *,
        role: str,
        master_asset_id: str,
    ) -> dict[str, Any] | None:
        rows = [
            asset for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("asset_role")) == role
            and _clean(asset.get("status")).lower() == "ready"
            and _clean(asset.get("dependency_state")).lower() != "stale"
            and master_asset_id in {_clean(value) for value in asset.get("parent_asset_ids") or []}
            and _clean(entity.get("entity_id")) in {_clean(value) for value in asset.get("entity_ids") or []}
            and bool((asset.get("metadata") or {}).get("derived_from_adopted_master"))
            and _clean((asset.get("metadata") or {}).get("derivation_version")) == self._DERIVATION_VERSION
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _master_candidate_path(self, row: dict[str, Any], output_index: int = 0) -> Path:
        files = [_clean(value) for value in row.get("output_files") or [] if _clean(value)]
        if not files:
            raise ValueError("角色身份母版候选没有生成文件")
        index = max(0, min(int(output_index), len(files) - 1))
        path = self.legacy.assets.resolve_data_url(files[index])
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError("角色身份母版候选文件不存在")
        return path

    def validate_master_adoption(self, project_id: str, row: dict[str, Any], output_index: int = 0) -> None:
        target_id = _clean(row.get("target_asset_id"))
        if not target_id:
            return
        target = self.director.production.get_asset(project_id, target_id)
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        if _clean(metadata.get("reference_layout")) != self._MASTER_LAYOUT:
            return
        path = self._master_candidate_path(row, output_index)
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if rgb.width < 2100 or rgb.height < 900 or not 2.1 <= rgb.width / rgb.height <= 2.4:
                raise ValueError("角色身份母版必须是工作流合成的正/侧/背三栏高清图，当前候选不能采用")
            regions = self._master_view_regions(rgb)
            if len(regions) != 3:
                raise ValueError(
                    f"角色身份母版必须包含独立的正面、侧面、背面三幅视图；"
                    f"当前只识别到 {len(regions)} 幅，不能采用"
                )

    def materialize_master_derivatives(
        self,
        project_id: str,
        row: dict[str, Any],
        output_index: int = 0,
    ) -> dict[str, dict[str, Any]]:
        target_id = _clean(row.get("target_asset_id"))
        if not target_id:
            return {}
        master = self.director.production.get_asset(project_id, target_id)
        metadata = master.get("metadata") if isinstance(master.get("metadata"), dict) else {}
        if _clean(metadata.get("reference_layout")) != self._MASTER_LAYOUT:
            return {}
        if _clean(master.get("status")).lower() != "ready":
            raise ValueError("角色身份母版尚未正式采用，不能派生参考资产")

        entity_id = next((_clean(value) for value in master.get("entity_ids") or [] if _clean(value)), "")
        entity = self._entity(project_id, entity_id)
        version = _clean((metadata.get("visual_context") or {}).get("appearance_version")) or "v1"
        existing = {
            role: self._ready_master_derivative(
                project_id, entity, role=role, master_asset_id=target_id,
            )
            for role in ("character_face_anchor", "character_costume_reference", "character_turnaround")
        }
        if all(existing.values()):
            return {role: asset for role, asset in existing.items() if asset is not None}

        path = self._master_candidate_path(row, output_index)
        with Image.open(path) as source:
            rgb = source.convert("RGB")
            regions = self._master_view_regions(rgb)
            if len(regions) != 3:
                raise ValueError("角色身份母版结构校验失败，不能派生参考资产")
            panel_width = rgb.width // 3
            front = rgb.crop((0, 0, panel_width, rgb.height))
            face = front.crop((
                round(panel_width * 0.18),
                0,
                round(panel_width * 0.82),
                min(rgb.height, round(panel_width * 0.64)),
            )).resize(
                (512, 512),
                Image.Resampling.LANCZOS,
            )
            crops = {
                "character_face_anchor": face,
                "character_costume_reference": front,
                "character_turnaround": rgb.copy(),
            }

        digest = hashlib.sha256(f"{project_id}:{target_id}".encode("utf-8")).hexdigest()[:20]
        rel_root = Path("v3") / "reference-assets" / "character-master" / digest
        roles = {
            "character_face_anchor": (self._face_anchor_key(entity_id, version), "锁脸裁片", "face"),
            "character_costume_reference": (self._costume_key(entity_id, version), "定装裁片", "costume"),
            "character_turnaround": (self._turnaround_key(entity_id, version), "三视图裁片", "turnaround"),
        }
        result: dict[str, dict[str, Any]] = {}
        for role, image in crops.items():
            current = existing.get(role)
            if current is not None:
                result[role] = current
                continue
            logical_key, label, crop_kind = roles[role]
            rel = rel_root / f"{crop_kind}.png"
            destination = self.director.production.data_dir / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="PNG", compress_level=4)
            result[role] = self.director.production.register_existing_file(
                project_id,
                stage="03",
                skill="xiaoduan-character-master-derivation",
                logical_key=logical_key,
                asset_type="IMAGE",
                asset_role=role,
                name=f"{_clean(entity.get('name')) or '角色'} · {label}",
                url="/files/" + rel.as_posix(),
                source={
                    "type": "adopted_character_master_crop",
                    "master_asset_id": target_id,
                    "crop_kind": crop_kind,
                },
                parent_asset_ids=[target_id],
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": "character",
                    "reference_phase": crop_kind,
                    "derived_from_adopted_master": True,
                    "master_asset_id": target_id,
                    "manual_adoption_required": False,
                    "derivation_version": self._DERIVATION_VERSION,
                    "visual_context": {"appearance_version": version},
                },
            )
        return result

    def _face_prompt(self, entity: dict[str, Any]) -> str:
        name = _clean(entity.get("name")) or "角色"
        facts = _select_facts(entity.get("metadata") or {}, _FACE_TOKENS)
        fact_text = "\n".join(f"- {row}" for row in facts) or "- 只遵循已经确认的年龄、脸部、发型和肤色设定。"
        return (
            f"角色「{name}」身份锁脸锚点。\n"
            "最高优先级：只锁定同一个角色的年龄、脸型、五官、发型、发色、肤色和稳定气质。\n"
            f"已确认脸部事实：\n{fact_text}\n\n"
            "只生成一个角色，一个正面头肩/胸像身份肖像，脸部占画面主要区域，正视镜头，中性自然表情。"
            "必须是正常人类面部解剖：双眼大小与位置自然对称，鼻口下颌比例正常，皮肤纹理自然，脸部细节清晰。"
            "使用干净中性浅灰或米白背景，均匀柔和光线。"
            "不要全身，不要持剑或其他道具，不要雪山、建筑、剧情场景、动作姿势、三视图、文字、标签或水印。"
            "这张图只作为后续服装与三视图的脸部身份锚点。"
        )

    def _costume_prompt(self, entity: dict[str, Any]) -> str:
        name = _clean(entity.get("name")) or "角色"
        facts = _select_facts(entity.get("metadata") or {}, _COSTUME_TOKENS)
        fact_text = "\n".join(f"- {row}" for row in facts) or "- 只遵循已经确认的服装、配色、鞋履和固定配饰。"
        return (
            f"角色「{name}」服装定装参考。\n"
            "最高优先级：脸和发型必须严格继承已采用的锁脸锚点；本阶段只锁定服装、鞋履、固定配饰、主辅配色、材质与体型比例。\n"
            f"已确认服装事实：\n{fact_text}\n\n"
            "只生成同一个角色的单人全身正面中性站姿，从头到脚完整可见，服装层次、鞋履、腰带、固定配饰和纹样清楚。"
            "使用干净中性浅灰或米白背景，不生成山景、建筑、剧情环境，不做挥剑或战斗动作，不生成多人、文字、标签或水印。"
            "人物脸部不得重新设计；这张图只作为最终三视图的服装与体型锚点。"
        )

    def _ensure_costume_target_and_prompt(
        self,
        project_id: str,
        entity: dict[str, Any],
        face_ready: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_id = _clean(entity.get("entity_id"))
        name = _clean(entity.get("name")) or "角色"
        version = _clean(entity.get("appearance_version")) or "v1"
        evidence = [
            _clean(row.get("source_asset_id")) for row in entity.get("evidence") or []
            if isinstance(row, dict) and _clean(row.get("source_asset_id"))
        ]
        parents = list(dict.fromkeys([*evidence[-12:], _clean(face_ready.get("asset_id"))]))
        target = self._costume_target(project_id, entity)
        if target is None or _clean(target.get("status")).lower() in {"archived", "superseded"}:
            target = self.director.production.declare_asset(
                project_id,
                stage="03",
                skill="xiaoduan-character-costume-anchor",
                logical_key=self._costume_key(entity_id, version),
                asset_type="IMAGE",
                asset_role="character_costume_reference",
                name=f"{name} · 服装定装参考",
                status="planned",
                source={"type": "auto_character_costume_anchor", "entity_id": entity_id},
                parent_asset_ids=parents,
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": "character",
                    "reference_phase": "costume",
                    "manual_adoption_required": True,
                    "design_source": "formal_stable_profile",
                    "visual_context": {"appearance_version": version},
                },
            )
        prompt = self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-character-costume-anchor",
            logical_key=self._costume_prompt_key(entity_id, version),
            asset_role="character_costume_reference_prompt",
            name=f"{name} · 服装定装生成要求",
            content=self._costume_prompt(entity),
            asset_type="TEXT",
            extension=".txt",
            source={"type": "auto_character_costume_prompt", "entity_id": entity_id},
            parent_asset_ids=parents,
            entity_ids=[entity_id],
            metadata={
                "reference_asset": True,
                "reference_kind": "character",
                "reference_phase": "costume",
            },
        )
        return target, prompt

    def _reference_package(
        self,
        project_id: str,
        entity: dict[str, Any],
        *,
        face: dict[str, Any],
        costume: dict[str, Any],
        turnaround: dict[str, Any],
    ) -> dict[str, Any]:
        entity_id = _clean(entity.get("entity_id"))
        version = _clean(entity.get("appearance_version")) or "v1"
        key = self._package_key(entity_id, version)
        master_asset_id = next(
            (_clean(value) for value in face.get("parent_asset_ids") or [] if _clean(value)),
            "",
        )
        components = {
            "master_asset_id": master_asset_id,
            "face_anchor_asset_id": _clean(face.get("asset_id")),
            "costume_asset_id": _clean(costume.get("asset_id")),
            "turnaround_asset_id": _clean(turnaround.get("asset_id")),
        }
        rows = [
            asset for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("logical_key")) == key
            and _clean(asset.get("asset_role")) == "character_reference_package"
            and _clean(asset.get("status")).lower() == "ready"
        ]
        for asset in reversed(rows):
            if (asset.get("metadata") or {}).get("components") == components:
                return asset
        payload = {
            "schema_version": "character_reference_package_v3",
            "character_id": entity_id,
            "appearance_version": version,
            "components": components,
            "policy": {
                "identity_source": "controlled_front_side_back_character_master",
                "derivation": "front_face_crop_plus_front_costume_plus_full_turnaround",
                "manual_adoption_required_once": True,
                "redraw_after_adoption": False,
            },
        }
        return self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-character-reference-package",
            logical_key=key,
            asset_role="character_reference_package",
            name=f"{_clean(entity.get('name')) or '角色'} · 角色参考资产包",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "assembled_character_reference_package", "entity_id": entity_id},
            parent_asset_ids=list(components.values()),
            entity_ids=[entity_id],
            metadata={
                "components": components,
                "visual_context": {"appearance_version": version},
                "manual_adoption_required_once": True,
            },
        )

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        items: list[dict[str, Any]] = []
        for entity, profile in self._formal_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            entity_id = _clean(entity.get("entity_id"))
            name = _clean(entity.get("name"))
            ready = self._ready_reference(project_id, entity) if kind != "character" else None
            final_target = self._master_target(project_id, entity) if kind == "character" else self._target(project_id, entity)
            prompt_asset = self._prompt_asset(project_id, entity_id)
            master_ready = self._ready_master(project_id, entity) if kind == "character" else None
            face_ready = None
            costume_ready = None
            turnaround_ready = None
            package = None

            if kind == "character":
                if master_ready is not None:
                    master_id = _clean(master_ready.get("asset_id"))
                    face_ready = self._ready_master_derivative(
                        project_id, entity, role="character_face_anchor", master_asset_id=master_id,
                    )
                    costume_ready = self._ready_master_derivative(
                        project_id, entity, role="character_costume_reference", master_asset_id=master_id,
                    )
                    turnaround_ready = self._ready_master_derivative(
                        project_id, entity, role="character_turnaround", master_asset_id=master_id,
                    )
                ready = master_ready if face_ready and costume_ready and turnaround_ready else None
                phase = "ready" if ready is not None else "character_master"
                active_target = final_target
                if ready is not None:
                    package = self._reference_package(
                        project_id,
                        entity,
                        face=face_ready,
                        costume=costume_ready,
                        turnaround=turnaround_ready,
                    )
            else:
                phase = "ready" if ready is not None else "reference"
                active_target = final_target

            candidate = self._pending_candidate(project_id, active_target)
            prompt_text = self._master_prompt(entity) if kind == "character" else self._reference_prompt(entity)
            if prompt_asset is not None and self._prompt_asset_is_user_edited(prompt_asset):
                stored = self._read_prompt_asset(project_id, prompt_asset)
                if stored:
                    prompt_text = stored
            preview_asset = master_ready or ready
            items.append({
                "entity_id": entity_id,
                "entity_type": kind,
                "label": {"character": "角色", "location": "场景", "prop": "道具"}[kind],
                "name": name,
                "ready": ready is not None,
                "reference_asset_id": _clean((ready or {}).get("asset_id")),
                "reference_url": _clean(((preview_asset or {}).get("storage") or {}).get("url")),
                "target_asset_id": _clean((active_target or {}).get("asset_id")),
                "prompt_asset_id": _clean((prompt_asset or {}).get("asset_id")),
                "prompt_text": prompt_text,
                "candidate": candidate,
                "profile_asset_id": _clean(profile.get("asset_id")),
                "profile_version": int(profile.get("version") or 0),
                "generation_phase": phase,
                "face_anchor_ready": face_ready is not None,
                "face_anchor_asset_id": _clean((face_ready or {}).get("asset_id")),
                "face_anchor_url": _clean(((face_ready or {}).get("storage") or {}).get("url")),
                "costume_ready": costume_ready is not None,
                "costume_asset_id": _clean((costume_ready or {}).get("asset_id")),
                "costume_url": _clean(((costume_ready or {}).get("storage") or {}).get("url")),
                "turnaround_asset_id": _clean((turnaround_ready or {}).get("asset_id")),
                "turnaround_url": _clean(((turnaround_ready or {}).get("storage") or {}).get("url")),
                "character_master_ready": master_ready is not None,
                "character_master_asset_id": _clean((master_ready or {}).get("asset_id")),
                "character_master_url": _clean(((master_ready or {}).get("storage") or {}).get("url")),
                "reference_package_asset_id": _clean((package or {}).get("asset_id")),
                "package_components_ready": bool(ready),
            })

        completed = {_clean(value) for value in project.get("completed_stages") or []}
        current_stage = _clean(project.get("current_stage"))
        return {
            "project_id": project_id,
            "items": items,
            "required_count": len(items),
            "stable_profile_count": len(items),
            "reference_candidate_count": len(items),
            "ready_count": sum(1 for item in items if item.get("ready")),
            "manual_adoption_required": True,
            "upload_required": False,
            "generation_backend": "zimage_front_then_dual_controlnet_three_view",
            "asset_policy": "one_adopted_character_master; location_prop_direct_reference",
            "canonical_asset_kinds": ["character", "location", "prop"],
            "stable_profile_required": True,
            "stage01_story_entities_exposed": False,
            "profile_roles_are_authoritative": True,
            "waiting_for_stable_assets": not items and current_stage in {"01", "02", "03"},
            "gate_message": (
                "先完成②角色、③视觉的稳定资产；故事解析阶段的粗实体不会提前生成参考图。"
                if not items and not ({"02", "03"} & completed)
                else ""
            ),
        }

    async def generate_candidate(
        self,
        project_id: str,
        entity_id: str,
        *,
        force: bool = False,
        prompt_override: str = "",
        appearance_version: str = "v1",
    ) -> dict[str, Any]:
        self.director.get_project(project_id)
        entity = self._entity(project_id, entity_id)
        if appearance_version not in {"", "v1", "default"}:
            entity = self._appearance_entity(project_id, entity, appearance_version)
        kind = _clean(entity.get("entity_type")).lower()
        if kind != "character":
            return await super().generate_candidate(
                project_id,
                entity_id,
                force=force,
                prompt_override=prompt_override,
                appearance_version=appearance_version,
            )

        final_ready = self._ready_master(project_id, entity)
        if final_ready is not None and not force:
            return {"already_ready": True, "asset": final_ready, "status": self.status(project_id)}

        current_target = self._master_target(project_id, entity)
        pending = self._pending_candidate(project_id, current_target)
        if pending and _clean(pending.get("status")).lower() in _ACTIVE and force:
            raise ValueError("角色身份母版仍在生成，请完成后再重新生成")
        if pending and _clean(pending.get("status")).lower() in _PENDING and not force:
            return {"already_pending": True, "candidate": pending, "generation_phase": "character_master", "status": self.status(project_id)}

        target, prompt = self._ensure_master_target_and_prompt(
            project_id,
            entity,
            prompt_override=prompt_override,
            force_new=bool(force),
        )
        payload = MediaGenerationPipeline().prepare_candidate(
            self.director.production,
            project_id,
            {
                "target_asset_id": _clean(target.get("asset_id")),
                "capability": "image",
                "mode": "txt2img",
                "prompt_asset_id": _clean(prompt.get("asset_id")),
                "params": {
                    "aspect_ratio": "3:4", "width": 768, "height": 1024,
                    "model_key": "smart", "steps": 36, "cfg": 6.0, "seed": -1,
                    "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1,
                    "semantic_compile": "auto", "reference_phase": "character_master",
                },
            },
        )
        response = await self.submit_candidate(project_id, payload)
        return {"submitted": True, "generation_phase": "character_master", **dict(response or {}), "status": self.status(project_id)}


def create_character_reference_package_router(legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = CharacterReferencePackageBootstrap(legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/references")
    async def reference_status(project_id: str) -> dict[str, Any]:
        try:
            return service.status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/references/{entity_id}/generate")
    async def generate_reference(project_id: str, entity_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            body = payload or {}
            return await service.generate_candidate(
                project_id,
                entity_id,
                force=bool(body.get("force")),
                prompt_override=_clean(body.get("prompt") or body.get("prompt_text")),
                appearance_version=_clean(body.get("appearance_version")) or "v1",
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"生成一致性参考图失败：{exc}") from exc

    @router.post("/api/v3/studio/projects/{project_id}/references/generate-missing")
    async def generate_missing_references(project_id: str) -> dict[str, Any]:
        try:
            return await service.generate_missing(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=500, detail=f"生成一致性参考图失败：{exc}") from exc

    return router


def install_character_master_confirmation_guard(
    app: Any,
    legacy_runtime: Any,
    service: CharacterReferencePackageBootstrap,
) -> None:
    """Guard the real generic candidate-confirm route used by reference assets.

    The archived workbench calls its shot-publish hook only for shot canonical
    assets. Reusable reference images take the generic bind_task branch, so the
    adoption invariant has to live at this shared HTTP mutation boundary.
    """
    marker = "_xiaoduan_character_master_confirmation_guard"
    if getattr(app.state, marker, False):
        return
    pattern = re.compile(
        r"^/api/director/workbench/projects/([^/]+)/candidates/([^/]+)/confirm$"
    )

    @app.middleware("http")
    async def character_master_confirmation_guard(request: Request, call_next: Any) -> Any:
        match = pattern.fullmatch(request.url.path) if request.method.upper() == "POST" else None
        if match is None:
            return await call_next(request)
        project_id, candidate_id = match.groups()
        try:
            _rows, row = legacy_runtime._wb_find_candidate(project_id, candidate_id)
            service.validate_master_adoption(project_id, row, 0)
        except FileNotFoundError as exc:
            return JSONResponse(status_code=404, content={"detail": str(exc)})
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"detail": str(exc)})

        response = await call_next(request)
        if response.status_code >= 300:
            return response
        try:
            _rows, confirmed = legacy_runtime._wb_find_candidate(project_id, candidate_id)
            service.materialize_master_derivatives(project_id, confirmed, 0)
        except Exception as exc:
            logger.exception(
                "CHARACTER_MASTER_DERIVATION_FAILED project_id=%s candidate_id=%s",
                project_id,
                candidate_id,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": f"角色母版已采用，但参考资产包派生失败：{exc}"},
            )
        return response

    setattr(app.state, marker, True)


__all__ = [
    "CharacterReferencePackageBootstrap",
    "create_character_reference_package_router",
    "install_character_master_confirmation_guard",
    "_select_facts",
]
