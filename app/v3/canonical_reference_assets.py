from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.media_generation_pipeline import MediaGenerationPipeline
from .reference_assets import (
    ReferenceAssetBootstrap,
    _ACTIVE,
    _PENDING,
    _clean,
    _flatten_metadata,
)


_PRIORITY = {"character": 0, "location": 1, "prop": 2}
_PROFILE_ROLES = {
    "character": "character_profile",
    "location": "location_profile",
    "prop": "prop_profile",
}
_KIND_BY_PROFILE_ROLE = {role: kind for kind, role in _PROFILE_ROLES.items()}


class CanonicalReferenceAssetBootstrap(ReferenceAssetBootstrap):
    """Reference workflow driven only by formal stable authoring profiles.

    Character references are intentionally two-stage:
    1. create/adopt a large face identity anchor;
    2. use that adopted anchor as a real image reference for the turnaround sheet.

    Location and prop references keep the existing direct generation path. The
    whole flow still uses the existing ProductionAssetService/candidate/adoption
    model; no parallel asset system is introduced.
    """

    def _profile_asset(self, project_id: str, entity_id: str, kind: str) -> dict[str, Any] | None:
        key = f"studio:authoring:{entity_id}:profile"
        role = _PROFILE_ROLES.get(kind, "")
        rows = [
            item
            for item in self.director.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == key
            and _clean(item.get("asset_role")) == role
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    @staticmethod
    def _name_from_profile(profile: dict[str, Any]) -> str:
        value = _clean(profile.get("name"))
        match = re.search(r"[「『](.+?)[」』]", value)
        return _clean(match.group(1)) if match else value.removesuffix("稳定设定").strip()

    def _formal_entities(self, project_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        project = self.director.get_project(project_id)
        visual = project.get("visual_direction") or (project.get("metadata") or {}).get("visual_direction")
        if isinstance(visual, dict) and visual:
            self.director.production.set_visual_direction(project_id, visual)
        profiles = self._stable_profiles(project_id)
        rows = self._build_reference_candidates(project_id, profiles)
        self._validate_reference_candidates(profiles, rows)
        return rows

    def _stable_profiles(self, project_id: str) -> list[dict[str, Any]]:
        return [
            asset
            for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("asset_role")) in _KIND_BY_PROFILE_ROLE
            and _clean(asset.get("status")).lower() == "ready"
            and _clean(asset.get("dependency_state")).lower() != "stale"
        ]

    def _build_reference_candidates(
        self,
        project_id: str,
        profiles: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for profile in profiles:
            kind = _KIND_BY_PROFILE_ROLE[_clean(profile.get("asset_role"))]
            raw = self.director.production.read_text_asset(project_id, _clean(profile.get("asset_id")))
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            metadata = profile.get("metadata") or {}
            key = re.fullmatch(r"studio:authoring:(.+):profile", _clean(profile.get("logical_key")))
            owners = list(dict.fromkeys(_clean(x) for x in profile.get("entity_ids") or [] if _clean(x)))
            entity_id = (
                _clean(payload.get("entity_id"))
                or _clean(metadata.get("canonical_entity_id"))
                or (key.group(1) if key else "")
                or (owners[0] if len(owners) == 1 else "")
            )
            name = _clean(payload.get("name")) or self._name_from_profile(profile)
            if not entity_id or not name:
                raise ValueError(
                    f"缺失参考资产：{name or profile.get('asset_id')}，正式稳定设定缺少名称或唯一归属 ID"
                )

            entity = {
                "entity_id": entity_id,
                "entity_type": kind,
                "name": name,
                "metadata": {
                    "stable_profile": payload.get("stable_profile") or {},
                    "default_state": payload.get("default_state") or {},
                    "stable_design": payload.get("stable_design") or raw,
                },
                "evidence": [{"source_asset_id": profile["asset_id"]}],
            }
            rows.append((entity, profile))

        rows.sort(
            key=lambda pair: (
                _PRIORITY.get(_clean(pair[0].get("entity_type")), 9),
                _clean(pair[0].get("name")),
            )
        )
        return rows

    @staticmethod
    def _validate_reference_candidates(
        profiles: list[dict[str, Any]],
        rows: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        expected = {_clean(profile.get("asset_id")) for profile in profiles}
        actual = [_clean(profile.get("asset_id")) for _, profile in rows]
        if len(profiles) != len(rows) or expected != set(actual) or len(set(actual)) != len(actual):
            missing = [
                str(profile.get("name") or profile.get("asset_id"))
                for profile in profiles
                if _clean(profile.get("asset_id")) not in actual
            ]
            raise ValueError(
                f"缺失参考资产：{', '.join(missing) or '候选重复或包含非正式资产'}；"
                f"stable_profile_count={len(profiles)}, reference_candidate_count={len(rows)}"
            )

        owners: dict[str, str] = {}
        for entity, _ in rows:
            owner = entity["entity_id"]
            if owner in owners:
                raise ValueError(
                    f"缺失参考资产：{owners[owner]}、{entity['name']} 的稳定设定归属 ID 冲突：{owner}"
                )
            owners[owner] = entity["name"]

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for entity, _profile in self._formal_entities(project_id):
            if _clean(entity.get("entity_id")) == _clean(entity_id):
                return entity
        raise ValueError("当前元素还没有经过②角色/③视觉形成稳定设定，暂不能生成一致性参考图")

    def _appearance_entity(self, project_id: str, entity: dict[str, Any], version: str) -> dict[str, Any]:
        if entity["entity_type"] != "character":
            raise ValueError("只有角色支持形象版本")
        for asset in self.director.production.list_assets(project_id, active_only=True):
            context = (asset.get("metadata") or {}).get("visual_context") or {}
            if (
                asset.get("asset_role") == "character_appearance"
                and asset.get("status") == "ready"
                and asset.get("dependency_state") != "stale"
                and entity["entity_id"] in (asset.get("entity_ids") or [])
                and context.get("appearance_version") == version
            ):
                payload = json.loads(
                    self.director.production.read_text_asset(project_id, asset["asset_id"])
                )
                return {
                    **entity,
                    "appearance_version": version,
                    "metadata": {"stable_design": payload.get("stable_design", "")},
                    "evidence": [{"source_asset_id": asset["asset_id"]}],
                }
        raise ValueError(f"角色 {entity['entity_id']} 缺少正式形象版本 {version}")

    @staticmethod
    def _face_anchor_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:face-anchor:{entity_id}:image{suffix}"

    @staticmethod
    def _face_prompt_key(entity_id: str, version: str = "v1") -> str:
        suffix = f":appearance:{version}" if version not in {"", "v1", "default"} else ""
        return f"studio:face-anchor:{entity_id}:prompt{suffix}"

    def _face_target(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        key = self._face_anchor_key(
            _clean(entity.get("entity_id")),
            _clean(entity.get("appearance_version")) or "v1",
        )
        rows = [
            asset
            for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("logical_key")) == key
            and _clean(asset.get("asset_type")).upper() == "IMAGE"
            and _clean(asset.get("asset_role")) == "character_face_anchor"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _ready_face_anchor(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        target = self._face_target(project_id, entity)
        if target is None:
            return None
        if _clean(target.get("status")).lower() != "ready":
            return None
        if _clean(target.get("dependency_state")).lower() == "stale":
            return None
        return target

    def _face_prompt(self, entity: dict[str, Any]) -> str:
        name = _clean(entity.get("name")) or "角色"
        rows = _flatten_metadata(entity.get("metadata") or {})
        facts = (
            "\n".join(f"- {_clean(row)}" for row in rows[-24:] if _clean(row))
            or "- 只遵循已经确认的稳定角色设定。"
        )
        return (
            f"角色「{name}」身份锁脸锚点。\n"
            "最高优先级：忠实保持项目世界观、文化、时代、年龄、脸型、五官、发型、肤色和角色气质。\n"
            f"项目已确认设定：\n{facts}\n\n"
            "只生成一个角色、一个正面胸像/头肩肖像，脸部必须足够大并占据画面主要区域。"
            "自然中性表情，双眼清楚且对称，正常人类面部解剖，鼻口下颌比例自然，皮肤纹理自然。"
            "使用干净中性背景，不生成全身，不生成多人，不生成三视图，不生成剧情动作、文字或水印。"
            "这张图只用于锁定同一个角色的脸、年龄和发型，后续三视图必须引用它。"
        )

    def _ensure_face_target_and_prompt(
        self,
        project_id: str,
        entity: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        entity_id = _clean(entity.get("entity_id"))
        name = _clean(entity.get("name")) or "角色"
        version = _clean(entity.get("appearance_version")) or "v1"
        evidence = [
            _clean(row.get("source_asset_id"))
            for row in entity.get("evidence") or []
            if isinstance(row, dict) and _clean(row.get("source_asset_id"))
        ]

        target = self._face_target(project_id, entity)
        if target is None or _clean(target.get("status")).lower() in {"archived", "superseded"}:
            target = self.director.production.declare_asset(
                project_id,
                stage="03",
                skill="xiaoduan-character-face-anchor",
                logical_key=self._face_anchor_key(entity_id, version),
                asset_type="IMAGE",
                asset_role="character_face_anchor",
                name=f"{name} · 锁脸锚点",
                status="planned",
                source={"type": "auto_character_face_anchor", "entity_id": entity_id},
                parent_asset_ids=evidence[-16:],
                entity_ids=[entity_id],
                metadata={
                    "reference_asset": True,
                    "reference_kind": "character",
                    "reference_phase": "face_anchor",
                    "manual_adoption_required": True,
                    "design_source": "formal_stable_profile",
                    "visual_context": {"appearance_version": version},
                },
            )

        prompt = self.director.production.create_text_asset(
            project_id,
            stage="03",
            skill="xiaoduan-character-face-anchor",
            logical_key=self._face_prompt_key(entity_id, version),
            asset_role="character_face_anchor_prompt",
            name=f"{name} · 锁脸锚点生成要求",
            content=self._face_prompt(entity),
            asset_type="TEXT",
            extension=".txt",
            source={"type": "auto_character_face_anchor_prompt", "entity_id": entity_id},
            parent_asset_ids=evidence[-16:],
            entity_ids=[entity_id],
            metadata={
                "reference_asset": True,
                "reference_kind": "character",
                "reference_phase": "face_anchor",
            },
        )
        return target, prompt

    def _pending_candidate(
        self,
        project_id: str,
        target: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if target is None:
            return None
        return next(
            (
                row
                for row in self._candidate_rows(project_id, _clean(target.get("asset_id")))
                if not _clean(row.get("confirmed_asset_id"))
                and _clean(row.get("status")).lower() not in {"rejected", "failed"}
            ),
            None,
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

            if ready is not None:
                phase = "ready"
                active_target = final_target
            elif kind == "character" and face_ready is None:
                phase = "face_anchor"
                active_target = face_target
            else:
                phase = "turnaround" if kind == "character" else "reference"
                active_target = final_target

            candidate = self._pending_candidate(project_id, active_target)
            prompt_text = self._reference_prompt(entity)
            if prompt_asset is not None and self._prompt_asset_is_user_edited(prompt_asset):
                stored = self._read_prompt_asset(project_id, prompt_asset)
                if stored:
                    prompt_text = stored

            preview_asset = ready or face_ready
            items.append(
                {
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
                }
            )

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
            "generation_backend": "face_anchor_then_reference_first_comfy",
            "asset_policy": "character_face_anchor_then_turnaround; location_prop_direct_reference",
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
        final_ready = self._ready_reference(project_id, entity)
        if final_ready is not None and not force:
            return {"already_ready": True, "asset": final_ready, "status": self.status(project_id)}

        if kind == "character":
            face_ready = self._ready_face_anchor(project_id, entity)
            if face_ready is None:
                target, prompt = self._ensure_face_target_and_prompt(project_id, entity)
                for row in self._candidate_rows(project_id, _clean(target.get("asset_id"))):
                    state = _clean(row.get("status")).lower()
                    if not _clean(row.get("confirmed_asset_id")) and state in _ACTIVE and force:
                        raise ValueError("锁脸锚点仍在生成，请完成后再重新生成")
                    if not _clean(row.get("confirmed_asset_id")) and state in _PENDING and not force:
                        return {
                            "already_pending": True,
                            "candidate": row,
                            "generation_phase": "face_anchor",
                            "status": self.status(project_id),
                        }

                payload = MediaGenerationPipeline().prepare_candidate(
                    self.director.production,
                    project_id,
                    {
                        "target_asset_id": _clean(target.get("asset_id")),
                        "capability": "image",
                        "mode": "txt2img",
                        "prompt_asset_id": _clean(prompt.get("asset_id")),
                        "params": {
                            "aspect_ratio": "4:5",
                            "width": 1024,
                            "height": 1280,
                            "model_key": "smart",
                            "steps": 36,
                            "cfg": 6.0,
                            "seed": -1,
                            "sampler": "dpmpp_2m",
                            "scheduler": "karras",
                            "count": 1,
                            "semantic_compile": "auto",
                            "reference_phase": "face_anchor",
                        },
                    },
                )
                response = await self.submit_candidate(project_id, payload)
                return {
                    "submitted": True,
                    "generation_phase": "face_anchor",
                    **dict(response or {}),
                    "status": self.status(project_id),
                }

            target, prompt = self._ensure_target_and_prompt(
                project_id,
                entity,
                prompt_override=prompt_override,
            )
            for row in self._candidate_rows(project_id, _clean(target.get("asset_id"))):
                state = _clean(row.get("status")).lower()
                if not _clean(row.get("confirmed_asset_id")) and state in _ACTIVE and force:
                    raise ValueError("三视图参考图仍在生成，请完成后再重新生成")
                if not _clean(row.get("confirmed_asset_id")) and state in _PENDING and not force:
                    return {
                        "already_pending": True,
                        "candidate": row,
                        "generation_phase": "turnaround",
                        "status": self.status(project_id),
                    }

            payload = MediaGenerationPipeline().prepare_candidate(
                self.director.production,
                project_id,
                {
                    "target_asset_id": _clean(target.get("asset_id")),
                    "capability": "image",
                    "mode": "reference_img2img",
                    "prompt_asset_id": _clean(prompt.get("asset_id")),
                    "params": {
                        "aspect_ratio": "4:3",
                        "width": 1536,
                        "height": 1152,
                        "model_key": "smart",
                        "steps": 36,
                        "cfg": 6.0,
                        "seed": -1,
                        "sampler": "dpmpp_2m",
                        "scheduler": "karras",
                        "count": 1,
                        "semantic_compile": "auto",
                        "reference_phase": "turnaround",
                        "reference_asset_ids": [_clean(face_ready.get("asset_id"))],
                    },
                },
            )
            response = await self.submit_candidate(project_id, payload)
            return {
                "submitted": True,
                "generation_phase": "turnaround",
                **dict(response or {}),
                "status": self.status(project_id),
            }

        return await super().generate_candidate(
            project_id,
            entity_id,
            force=force,
            prompt_override=prompt_override,
            appearance_version=appearance_version,
        )

    async def generate_first_missing_for_entities(
        self,
        project_id: str,
        entity_ids: list[str],
    ) -> dict[str, Any] | None:
        wanted = {_clean(value) for value in entity_ids if _clean(value)}
        for entity, _profile in self._formal_entities(project_id):
            if _clean(entity.get("entity_id")) not in wanted:
                continue
            if self._ready_reference(project_id, entity) is None:
                return await self.generate_candidate(project_id, _clean(entity.get("entity_id")))
        return None


def create_canonical_reference_asset_router(legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = CanonicalReferenceAssetBootstrap(legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/references")
    async def reference_status(project_id: str) -> dict[str, Any]:
        try:
            return service.status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/references/{entity_id}/generate")
    async def generate_reference(
        project_id: str,
        entity_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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


__all__ = ["CanonicalReferenceAssetBootstrap", "create_canonical_reference_asset_router"]
