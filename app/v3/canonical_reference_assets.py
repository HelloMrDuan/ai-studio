from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from .reference_assets import ReferenceAssetBootstrap, _clean


_CANONICAL_TYPES = {"character", "location", "prop"}
_PRIORITY = {"character": 0, "location": 1, "prop": 2}


class CanonicalReferenceAssetBootstrap(ReferenceAssetBootstrap):
    """Reference workflow for canonical reusable asset kinds only.

    Narrative scene records are deliberately excluded: a scene points to its
    stable location asset. This avoids duplicate scene/location reference images
    and mirrors the character/location/prop split studied in the OSS asset hub.
    """

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.director.production.list_entities(project_id):
            if _clean(item.get("entity_id")) != _clean(entity_id):
                continue
            kind = _clean(item.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                raise ValueError("当前故事元素不是可复用角色、地点或道具参考资产")
            return item
        raise FileNotFoundError(f"故事元素不存在：{entity_id}")

    def status(self, project_id: str) -> dict[str, Any]:
        raw = super().status(project_id)
        items = [item for item in raw.get("items") or [] if _clean(item.get("entity_type")).lower() in _CANONICAL_TYPES]
        return {
            **raw,
            "items": items,
            "required_count": len(items),
            "ready_count": sum(1 for item in items if item.get("ready")),
            "canonical_asset_kinds": ["character", "location", "prop"],
        }

    async def generate_first_missing_for_entities(self, project_id: str, entity_ids: list[str]) -> dict[str, Any] | None:
        wanted = {_clean(value) for value in entity_ids if _clean(value)}
        entities = [
            item for item in self.director.production.list_entities(project_id)
            if _clean(item.get("entity_id")) in wanted
            and _clean(item.get("entity_type")).lower() in _CANONICAL_TYPES
        ]
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
