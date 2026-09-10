from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap
from app.v3.reference_assets import _ACTIVE, _PENDING, _clean


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
    """Production character package: identity -> face -> costume -> turnaround.

    The existing ProductionAsset graph remains authoritative. This class adds
    staged image assets and one structured package artifact; it does not create a
    second asset system.
    """

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
        components = {
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
            "schema_version": "character_reference_package_v1",
            "character_id": entity_id,
            "appearance_version": version,
            "components": components,
            "policy": {
                "face_identity_source": "face_anchor",
                "costume_identity_source": "costume_reference",
                "render_identity_source": "turnaround",
                "manual_adoption_required_each_stage": True,
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
                "manual_adoption_required_each_stage": True,
            },
        )

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        items: list[dict[str, Any]] = []
        for entity, profile in self._formal_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            entity_id = _clean(entity.get("entity_id"))
            name = _clean(entity.get("name"))
            ready = self._ready_reference(project_id, entity)
            final_target = self._target(project_id, entity)
            prompt_asset = self._prompt_asset(project_id, entity_id)
            face_ready = self._ready_face_anchor(project_id, entity) if kind == "character" else None
            face_target = self._face_target(project_id, entity) if kind == "character" else None
            costume_ready = self._ready_costume(project_id, entity) if kind == "character" else None
            costume_target = self._costume_target(project_id, entity) if kind == "character" else None
            package = None

            if ready is not None:
                phase = "ready"
                active_target = final_target
                if kind == "character" and face_ready and costume_ready and final_target:
                    package = self._reference_package(
                        project_id,
                        entity,
                        face=face_ready,
                        costume=costume_ready,
                        turnaround=final_target,
                    )
            elif kind == "character" and face_ready is None:
                phase = "face_anchor"
                active_target = face_target
            elif kind == "character" and costume_ready is None:
                phase = "costume"
                active_target = costume_target
            else:
                phase = "turnaround" if kind == "character" else "reference"
                active_target = final_target

            candidate = self._pending_candidate(project_id, active_target)
            prompt_text = self._reference_prompt(entity)
            if prompt_asset is not None and self._prompt_asset_is_user_edited(prompt_asset):
                stored = self._read_prompt_asset(project_id, prompt_asset)
                if stored:
                    prompt_text = stored
            preview_asset = ready or costume_ready or face_ready
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
                "reference_package_asset_id": _clean((package or {}).get("asset_id")),
                "package_components_ready": bool(ready and (kind != "character" or (face_ready and costume_ready))),
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
            "generation_backend": "face_then_costume_then_multi_reference_turnaround",
            "asset_policy": "character_identity_package; location_prop_direct_reference",
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

        final_ready = self._ready_reference(project_id, entity)
        if final_ready is not None and not force:
            return {"already_ready": True, "asset": final_ready, "status": self.status(project_id)}

        face_ready = self._ready_face_anchor(project_id, entity)
        if face_ready is None:
            target, prompt = self._ensure_face_target_and_prompt(project_id, entity)
            pending = self._pending_candidate(project_id, target)
            if pending and _clean(pending.get("status")).lower() in _ACTIVE and force:
                raise ValueError("锁脸锚点仍在生成，请完成后再重新生成")
            if pending and _clean(pending.get("status")).lower() in _PENDING and not force:
                return {"already_pending": True, "candidate": pending, "generation_phase": "face_anchor", "status": self.status(project_id)}
            payload = MediaGenerationPipeline().prepare_candidate(
                self.director.production,
                project_id,
                {
                    "target_asset_id": _clean(target.get("asset_id")),
                    "capability": "image",
                    "mode": "txt2img",
                    "prompt_asset_id": _clean(prompt.get("asset_id")),
                    "params": {
                        "aspect_ratio": "1:1", "width": 1024, "height": 1024,
                        "model_key": "smart", "steps": 36, "cfg": 6.0, "seed": -1,
                        "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1,
                        "semantic_compile": "auto", "reference_phase": "face_anchor",
                    },
                },
            )
            response = await self.submit_candidate(project_id, payload)
            return {"submitted": True, "generation_phase": "face_anchor", **dict(response or {}), "status": self.status(project_id)}

        costume_ready = self._ready_costume(project_id, entity)
        if costume_ready is None:
            target, prompt = self._ensure_costume_target_and_prompt(project_id, entity, face_ready)
            pending = self._pending_candidate(project_id, target)
            if pending and _clean(pending.get("status")).lower() in _ACTIVE and force:
                raise ValueError("服装定装参考仍在生成，请完成后再重新生成")
            if pending and _clean(pending.get("status")).lower() in _PENDING and not force:
                return {"already_pending": True, "candidate": pending, "generation_phase": "costume", "status": self.status(project_id)}
            payload = MediaGenerationPipeline().prepare_candidate(
                self.director.production,
                project_id,
                {
                    "target_asset_id": _clean(target.get("asset_id")),
                    "capability": "image",
                    "mode": "reference_img2img",
                    "prompt_asset_id": _clean(prompt.get("asset_id")),
                    "params": {
                        "aspect_ratio": "3:4", "width": 768, "height": 1024,
                        "model_key": "smart", "steps": 36, "cfg": 6.0, "seed": -1,
                        "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1,
                        "semantic_compile": "auto", "reference_phase": "costume",
                        "reference_asset_ids": [_clean(face_ready.get("asset_id"))],
                    },
                },
            )
            response = await self.submit_candidate(project_id, payload)
            return {"submitted": True, "generation_phase": "costume", **dict(response or {}), "status": self.status(project_id)}

        target, prompt = self._ensure_target_and_prompt(
            project_id,
            entity,
            prompt_override=prompt_override,
        )
        pending = self._pending_candidate(project_id, target)
        if pending and _clean(pending.get("status")).lower() in _ACTIVE and force:
            raise ValueError("三视图参考图仍在生成，请完成后再重新生成")
        if pending and _clean(pending.get("status")).lower() in _PENDING and not force:
            return {"already_pending": True, "candidate": pending, "generation_phase": "turnaround", "status": self.status(project_id)}

        self._reference_package(
            project_id,
            entity,
            face=face_ready,
            costume=costume_ready,
            turnaround=target,
        )
        payload = MediaGenerationPipeline().prepare_candidate(
            self.director.production,
            project_id,
            {
                "target_asset_id": _clean(target.get("asset_id")),
                "capability": "image",
                "mode": "reference_img2img",
                "prompt_asset_id": _clean(prompt.get("asset_id")),
                "params": {
                    "aspect_ratio": "4:3", "width": 1536, "height": 1152,
                    "model_key": "smart", "steps": 36, "cfg": 6.0, "seed": -1,
                    "sampler": "dpmpp_2m", "scheduler": "karras", "count": 1,
                    "semantic_compile": "auto", "reference_phase": "turnaround",
                    "reference_asset_ids": [
                        _clean(face_ready.get("asset_id")),
                        _clean(costume_ready.get("asset_id")),
                    ],
                },
            },
        )
        response = await self.submit_candidate(project_id, payload)
        return {"submitted": True, "generation_phase": "turnaround", **dict(response or {}), "status": self.status(project_id)}


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


__all__ = ["CharacterReferencePackageBootstrap", "create_character_reference_package_router", "_select_facts"]
