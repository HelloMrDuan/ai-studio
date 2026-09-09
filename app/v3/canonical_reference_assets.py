from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException

from .reference_assets import ReferenceAssetBootstrap, _clean


_PRIORITY = {"character": 0, "location": 1, "prop": 2}
_PROFILE_ROLES = {
    "character": "character_profile",
    "location": "location_profile",
    "prop": "prop_profile",
}
_KIND_BY_PROFILE_ROLE = {role: kind for kind, role in _PROFILE_ROLES.items()}


class CanonicalReferenceAssetBootstrap(ReferenceAssetBootstrap):
    """Reference workflow driven by formal stable authoring profiles.

    Story discovery entities are deliberately not authoritative here. A reusable
    reference exists only when Stage②/③ has produced a READY character/location/
    prop profile. This also makes the reference UI resilient to legacy entity
    type names such as ``scene`` or ``artifact``: the formal profile role is the
    source of truth for the reusable kind.
    """

    def _profile_asset(self, project_id: str, entity_id: str, kind: str) -> dict[str, Any] | None:
        key = f"studio:authoring:{entity_id}:profile"
        role = _PROFILE_ROLES.get(kind, "")
        rows = [
            item for item in self.director.production.list_assets(project_id, active_only=True)
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
            asset for asset in self.director.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("asset_role")) in _KIND_BY_PROFILE_ROLE
            and _clean(asset.get("status")).lower() == "ready"
            and _clean(asset.get("dependency_state")).lower() != "stale"
        ]

    def _build_reference_candidates(
        self, project_id: str, profiles: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        # One candidate per formal profile, never per story entity or alias.
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
                raise ValueError(f"缺失参考资产：{name or profile.get('asset_id')}，正式稳定设定缺少名称或唯一归属 ID")
            entity = {
                "entity_id": entity_id, "entity_type": kind, "name": name,
                "metadata": {
                    "stable_profile": payload.get("stable_profile") or {},
                    "default_state": payload.get("default_state") or {},
                    "stable_design": payload.get("stable_design") or raw,
                },
                "evidence": [{"source_asset_id": profile["asset_id"]}],
            }
            rows.append((entity, profile))
        rows.sort(key=lambda pair: (_PRIORITY.get(_clean(pair[0].get("entity_type")), 9), _clean(pair[0].get("name"))))
        return rows

    @staticmethod
    def _validate_reference_candidates(
        profiles: list[dict[str, Any]],
        rows: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        expected = {_clean(profile.get("asset_id")) for profile in profiles}
        actual = [_clean(profile.get("asset_id")) for _, profile in rows]
        if len(profiles) != len(rows) or expected != set(actual) or len(set(actual)) != len(actual):
            missing = [str(profile.get("name") or profile.get("asset_id")) for profile in profiles
                       if _clean(profile.get("asset_id")) not in actual]
            raise ValueError(
                f"缺失参考资产：{', '.join(missing) or '候选重复或包含非正式资产'}；"
                f"stable_profile_count={len(profiles)}, reference_candidate_count={len(rows)}"
            )
        owners: dict[str, str] = {}
        for entity, _ in rows:
            owner = entity["entity_id"]
            if owner in owners:
                raise ValueError(f"缺失参考资产：{owners[owner]}、{entity['name']} 的稳定设定归属 ID 冲突：{owner}")
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
            if (asset.get("asset_role") == "character_appearance" and asset.get("status") == "ready"
                and asset.get("dependency_state") != "stale"
                and entity["entity_id"] in (asset.get("entity_ids") or [])
                and context.get("appearance_version") == version):
                payload = json.loads(self.director.production.read_text_asset(project_id, asset["asset_id"]))
                return {**entity, "appearance_version": version,
                        "metadata": {"stable_design": payload.get("stable_design", "")},
                        "evidence": [{"source_asset_id": asset["asset_id"]}]}
        raise ValueError(f"角色 {entity['entity_id']} 缺少正式形象版本 {version}")

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        items: list[dict[str, Any]] = []

        for entity, profile in self._formal_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            entity_id = _clean(entity.get("entity_id"))
            name = _clean(entity.get("name"))

            ready = self._ready_reference(project_id, entity)
            target = self._target(project_id, entity)
            prompt_asset = self._prompt_asset(project_id, entity_id)
            candidate = None
            if target is not None:
                candidate = next(
                    (
                        row for row in self._candidate_rows(project_id, _clean(target.get("asset_id")))
                        if not _clean(row.get("confirmed_asset_id"))
                        and _clean(row.get("status")).lower() not in {"rejected", "failed"}
                    ),
                    None,
                )

            prompt_text = self._reference_prompt(entity)
            if prompt_asset is not None:
                try:
                    prompt_text = self.director.production.read_text_asset(project_id, _clean(prompt_asset.get("asset_id")))
                except Exception:
                    pass

            items.append({
                "entity_id": entity_id,
                "entity_type": kind,
                "label": {"character": "角色", "location": "场景", "prop": "道具"}[kind],
                "name": name,
                "ready": ready is not None,
                "reference_asset_id": _clean((ready or {}).get("asset_id")),
                "reference_url": _clean(((ready or {}).get("storage") or {}).get("url")),
                "target_asset_id": _clean((target or {}).get("asset_id")),
                "prompt_asset_id": _clean((prompt_asset or {}).get("asset_id")),
                "prompt_text": prompt_text,
                "candidate": candidate,
                "profile_asset_id": _clean(profile.get("asset_id")),
                "profile_version": int(profile.get("version") or 0),
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
            "generation_backend": "existing_workbench_txt2img",
            "asset_policy": "formal_profile_then_reference_candidate_then_manual_adoption",
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

    async def generate_first_missing_for_entities(self, project_id: str, entity_ids: list[str]) -> dict[str, Any] | None:
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
    async def generate_reference(project_id: str, entity_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await service.generate_candidate(
                project_id,
                entity_id,
                force=bool((payload or {}).get("force")),
                prompt_override=_clean((payload or {}).get("prompt")),
                appearance_version=_clean((payload or {}).get("appearance_version")) or "v1",
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
