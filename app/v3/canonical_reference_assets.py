from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, HTTPException

from .reference_assets import ReferenceAssetBootstrap, _clean


_CANONICAL_TYPES = {"character", "location", "prop"}
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
        return _clean(match.group(1)) if match else ""

    def _formal_entities(self, project_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        entities = {
            _clean(item.get("entity_id")): dict(item)
            for item in self.director.production.list_entities(project_id)
            if _clean(item.get("entity_id"))
        }
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for asset in self.director.production.list_assets(project_id, active_only=True):
            role = _clean(asset.get("asset_role"))
            kind = _KIND_BY_PROFILE_ROLE.get(role, "")
            if not kind:
                continue
            if _clean(asset.get("status")).lower() != "ready":
                continue
            if _clean(asset.get("dependency_state")).lower() == "stale":
                continue
            for entity_id in [_clean(x) for x in asset.get("entity_ids") or [] if _clean(x)]:
                key = (kind, entity_id)
                current = latest.get(key)
                if current is None or (
                    int(asset.get("version") or 0), _clean(asset.get("updated_at"))
                ) > (
                    int(current.get("version") or 0), _clean(current.get("updated_at"))
                ):
                    latest[key] = asset

        rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for (kind, entity_id), profile in latest.items():
            entity = dict(entities.get(entity_id) or {"entity_id": entity_id})
            entity["entity_id"] = entity_id
            # Formal profile role is authoritative. Do not drop a valid profile
            # merely because a legacy story entity still carries scene/artifact.
            entity["entity_type"] = kind
            if not _clean(entity.get("name")):
                entity["name"] = self._name_from_profile(profile) or f"未命名{kind}"
            rows.append((entity, profile))
        rows.sort(key=lambda pair: (_PRIORITY.get(_clean(pair[0].get("entity_type")), 9), _clean(pair[0].get("name"))))
        return rows

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for entity, _profile in self._formal_entities(project_id):
            if _clean(entity.get("entity_id")) == _clean(entity_id):
                return entity
        raise ValueError("当前元素还没有经过②角色/③视觉形成稳定设定，暂不能生成一致性参考图")

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for entity, profile in self._formal_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            entity_id = _clean(entity.get("entity_id"))
            name = _clean(entity.get("name"))
            identity = (kind, name.casefold())
            if identity in seen:
                continue
            seen.add(identity)

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
