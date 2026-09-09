from __future__ import annotations

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


class CanonicalReferenceAssetBootstrap(ReferenceAssetBootstrap):
    """Reference workflow for confirmed reusable character/location/prop assets.

    Stage① story entities are discovery facts, not stable visual identities. A
    reference candidate is therefore exposed only after the corresponding
    authoring profile exists and is READY: character profiles come from ②,
    location/prop profiles from ③. Narrative scene records remain excluded.
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

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.director.production.list_entities(project_id):
            if _clean(item.get("entity_id")) != _clean(entity_id):
                continue
            kind = _clean(item.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                raise ValueError("当前故事元素不是可复用角色、地点或道具参考资产")
            if self._profile_asset(project_id, entity_id, kind) is None:
                raise ValueError("当前元素还没有经过②角色/③视觉形成稳定设定，暂不能生成一致性参考图")
            return item
        raise FileNotFoundError(f"故事元素不存在：{entity_id}")

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        raw = super().status(project_id)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in raw.get("items") or []:
            kind = _clean(item.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                continue
            entity_id = _clean(item.get("entity_id"))
            profile = self._profile_asset(project_id, entity_id, kind)
            if profile is None:
                continue
            # Older story extraction may contain repeated logical entities.
            # Once stable profiles exist, expose one canonical card per kind+name
            # instead of asking the user to generate duplicate references.
            identity = (kind, _clean(item.get("name")).casefold())
            if identity in seen:
                continue
            seen.add(identity)
            row = dict(item)
            row["profile_asset_id"] = _clean(profile.get("asset_id"))
            row["profile_version"] = int(profile.get("version") or 0)
            items.append(row)

        completed = {_clean(value) for value in project.get("completed_stages") or []}
        current_stage = _clean(project.get("current_stage"))
        return {
            **raw,
            "items": items,
            "required_count": len(items),
            "ready_count": sum(1 for item in items if item.get("ready")),
            "canonical_asset_kinds": ["character", "location", "prop"],
            "stable_profile_required": True,
            "stage01_story_entities_exposed": False,
            "waiting_for_stable_assets": not items and current_stage in {"01", "02", "03"},
            "gate_message": (
                "先完成并确认②角色、③视觉的稳定资产；故事解析阶段的粗实体不会提前生成参考图。"
                if not items and not ({"02", "03"} & completed)
                else ""
            ),
        }

    async def generate_first_missing_for_entities(self, project_id: str, entity_ids: list[str]) -> dict[str, Any] | None:
        wanted = {_clean(value) for value in entity_ids if _clean(value)}
        entities = []
        for item in self.director.production.list_entities(project_id):
            entity_id = _clean(item.get("entity_id"))
            kind = _clean(item.get("entity_type")).lower()
            if entity_id not in wanted or kind not in _CANONICAL_TYPES:
                continue
            if self._profile_asset(project_id, entity_id, kind) is None:
                continue
            entities.append(item)
        entities.sort(key=lambda item: (_PRIORITY.get(_clean(item.get("entity_type")).lower(), 9), _clean(item.get("name"))))
        for entity in entities:
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
