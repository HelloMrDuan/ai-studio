from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException

from .authoring_assets import AuthoringAssetService, _clean


_CANONICAL_TYPES = {"character", "location", "prop"}
_STAGE_BY_TYPE = {"character": "02", "location": "03", "prop": "03"}
_ROLE_BY_TYPE = {"character": "character_profile", "location": "location_profile", "prop": "prop_profile"}
_LABEL_BY_TYPE = {"character": "角色", "location": "场景", "prop": "道具"}
_STAGE_ORDER_INDEX = {"01": 1, "02": 2, "03": 3, "04": 4, "": 0}


class RefinedAuthoringAssetService(AuthoringAssetService):
    """Canonical and idempotent reusable-asset layer.

    Reusable asset kinds intentionally match the mature asset-hub model studied
    upstream: character/location/prop. Narrative scenes remain story structure
    and point to a location asset instead of becoming duplicate visual assets.
    GET/status sync is content-addressed: an unchanged page refresh never creates
    a new profile version.
    """

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.production.list_entities(project_id):
            if _clean(item.get("entity_id")) != _clean(entity_id):
                continue
            kind = _clean(item.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                raise ValueError("当前故事元素不是可复用角色、地点或道具资产")
            return item
        raise FileNotFoundError(f"故事资产不存在：{entity_id}")

    def _same_text(self, project_id: str, asset: dict[str, Any] | None, expected: str) -> bool:
        if not asset:
            return False
        try:
            actual = self.production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=max(30000, len(expected) + 100))
        except Exception:
            return False
        return actual.strip() == expected.strip()

    def _sync_story(self, project_id: str) -> dict[str, Any] | None:
        payload = self._story_payload(project_id)
        if payload is None:
            return None
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        current = self._active_story(project_id)
        if self._same_text(project_id, current, content):
            return current
        parents = [_clean(item) for item in payload.get("production_asset_ids") or [] if _clean(item)]
        return self.production.create_text_asset(
            project_id,
            stage="01",
            skill="xiaoduan-authoring-assets",
            logical_key=self.story_key(),
            asset_role="story_bible",
            name="已确认故事结构",
            content=content,
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "confirmed_stage_01"},
            parent_asset_ids=parents,
            metadata={
                "asset_model": "reusable_authoring_asset_v1",
                "editable": False,
                "source_of_truth": "stage_01_confirmed_output",
                "content_idempotent": True,
            },
        )

    def _sync_entity(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        kind = _clean(entity.get("entity_type")).lower()
        if kind not in _CANONICAL_TYPES:
            return None
        payload = self._profile_payload(entity)
        entity_id = payload["entity_id"]
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        current = self._active_profile(project_id, entity_id)
        if self._same_text(project_id, current, content):
            return current
        return self.production.create_text_asset(
            project_id,
            stage=_STAGE_BY_TYPE[kind],
            skill="xiaoduan-authoring-assets",
            logical_key=self.profile_key(entity_id),
            asset_role=_ROLE_BY_TYPE[kind],
            name=f"{_LABEL_BY_TYPE[kind]}「{payload['name']}」稳定设定",
            content=content,
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "canonical_reusable_asset_profile", "entity_id": entity_id},
            parent_asset_ids=self._profile_parents(project_id, entity),
            entity_ids=[entity_id],
            metadata={
                "asset_model": "reusable_authoring_asset_v1",
                "editable": True,
                "manual_design": bool(self._manual_design(entity)),
                "kind": kind,
                "content_idempotent": True,
            },
        )

    def sync(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        completed = {_clean(item) for item in project.get("completed_stages") or []}
        current_stage = _clean(project.get("current_stage"))
        story = self._sync_story(project_id) if "01" in completed or current_stage != "01" else None
        profiles = []
        if completed & {"02", "03", "04"} or current_stage in {"03", "04"}:
            for entity in self.production.list_entities(project_id):
                kind = _clean(entity.get("entity_type")).lower()
                if kind not in _CANONICAL_TYPES:
                    continue
                required_stage = _STAGE_BY_TYPE[kind]
                if required_stage in completed or _STAGE_ORDER_INDEX.get(current_stage, 0) > _STAGE_ORDER_INDEX[required_stage]:
                    profile = self._sync_entity(project_id, entity)
                    if profile:
                        profiles.append(profile)
        self.link_existing_dependents(project_id)
        return {
            "project_id": project_id,
            "story_asset_id": _clean((story or {}).get("asset_id")),
            "profile_asset_ids": [_clean(item.get("asset_id")) for item in profiles],
            "canonical_asset_kinds": ["character", "location", "prop"],
        }

    def status(self, project_id: str) -> dict[str, Any]:
        self.sync(project_id)
        items = []
        for entity in self.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                continue
            profile = self._active_profile(project_id, _clean(entity.get("entity_id")))
            items.append({
                "entity_id": _clean(entity.get("entity_id")),
                "entity_type": kind,
                "label": _LABEL_BY_TYPE[kind],
                "name": _clean(entity.get("name")),
                "stable_design": self._default_profile_text(entity),
                "profile_asset_id": _clean((profile or {}).get("asset_id")),
                "profile_version": int((profile or {}).get("version") or 0),
                "editable": True,
            })
        return {
            "project_id": project_id,
            "items": items,
            "story_asset_id": _clean((self._active_story(project_id) or {}).get("asset_id")),
            "model": "asset_driven_front_half_v2",
            "canonical_asset_kinds": ["character", "location", "prop"],
            "local_invalidation": True,
            "content_idempotent": True,
        }


def create_refined_authoring_asset_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = RefinedAuthoringAssetService(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/authoring-assets")
    async def authoring_assets(project_id: str) -> dict[str, Any]:
        try:
            return service.status(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/v3/studio/projects/{project_id}/authoring-assets/{entity_id}")
    async def update_authoring_asset(project_id: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return service.update_profile(
                project_id,
                entity_id,
                stable_design=_clean(payload.get("stable_design")),
                change_reason=_clean(payload.get("change_reason")),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/authoring-assets/sync")
    async def sync_authoring_assets(project_id: str) -> dict[str, Any]:
        try:
            return service.sync(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["RefinedAuthoringAssetService", "create_refined_authoring_asset_router"]
