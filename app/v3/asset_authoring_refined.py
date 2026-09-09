from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from .authoring_assets import AuthoringAssetService, _clean, _copy, _now


_CANONICAL_TYPES = {"character", "location", "prop"}
_STAGE_BY_TYPE = {"character": "02", "location": "03", "prop": "03"}
_ROLE_BY_TYPE = {"character": "character_profile", "location": "location_profile", "prop": "prop_profile"}
_LABEL_BY_TYPE = {"character": "角色", "location": "场景", "prop": "道具"}
_STAGE_ORDER_INDEX = {"01": 1, "02": 2, "03": 3, "04": 4, "": 0}
_REFERENCE_ROLES = {
    "character_reference",
    "character_turnaround",
    "character_consistency",
    "scene_reference",
    "location_reference",
    "prop_reference",
    "item_reference",
}
_DOWNSTREAM_STAGES = {"04", "make", "edit", "final"}


class RefinedAuthoringAssetService(AuthoringAssetService):
    """Canonical, stage-gated and de-duplicated reusable authoring assets.

    Same-type entities with the same visible name can exist in older projects
    because different extraction passes used different logical keys. They are
    treated as aliases of one canonical reusable asset. The UI exposes one card,
    every alias entity id points at the same profile asset, and edits invalidate
    dependants bound to any alias rather than only the selected id.
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
            actual = self.production.read_text_asset(
                project_id,
                _clean(asset.get("asset_id")),
                max_chars=max(30000, len(expected) + 100),
            )
        except Exception:
            return False
        return actual.strip() == expected.strip()

    @staticmethod
    def _group_key(entity: dict[str, Any]) -> tuple[str, str]:
        kind = _clean(entity.get("entity_type")).lower()
        name = _clean(entity.get("name"))
        identity = name.casefold() if name else _clean(entity.get("entity_id")).casefold()
        return kind, identity

    def _entity_score(self, project_id: str, entity: dict[str, Any]) -> tuple[int, int, int, int, str]:
        profile = self._active_profile(project_id, _clean(entity.get("entity_id")))
        try:
            metadata_size = len(json.dumps(entity.get("metadata") or {}, ensure_ascii=False, sort_keys=True))
        except Exception:
            metadata_size = 0
        return (
            1 if profile else 0,
            int((profile or {}).get("version") or 0),
            1 if self._manual_design(entity) else 0,
            metadata_size,
            _clean(entity.get("entity_id")),
        )

    def _canonical_groups(self, project_id: str) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entity in self.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in _CANONICAL_TYPES:
                continue
            grouped.setdefault(self._group_key(entity), []).append(entity)

        result: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for rows in grouped.values():
            rows = sorted(rows, key=lambda item: self._entity_score(project_id, item), reverse=True)
            result.append((rows[0], rows))
        result.sort(key=lambda pair: (_clean(pair[0].get("entity_type")), _clean(pair[0].get("name"))))
        return result

    def _group_for_entity(
        self,
        project_id: str,
        entity_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        wanted = _clean(entity_id)
        for canonical, rows in self._canonical_groups(project_id):
            if wanted in {_clean(item.get("entity_id")) for item in rows}:
                return canonical, rows
        raise FileNotFoundError(f"故事资产不存在：{entity_id}")

    def _merged_design(self, canonical: dict[str, Any], rows: list[dict[str, Any]]) -> str:
        manual = self._manual_design(canonical)
        if manual:
            return manual
        values: list[str] = []
        seen: set[str] = set()
        for entity in rows:
            text = _clean(self._default_profile_text(entity))
            if not text or text in seen:
                continue
            seen.add(text)
            values.append(text)
        return "\n\n".join(values).strip() or self._default_profile_text(canonical)

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

    def _sync_entity_group(
        self,
        project_id: str,
        canonical: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        kind = _clean(canonical.get("entity_type")).lower()
        if kind not in _CANONICAL_TYPES:
            return None
        payload = self._profile_payload(canonical)
        payload["stable_design"] = self._merged_design(canonical, rows)
        entity_id = _clean(canonical.get("entity_id"))
        source_entity_ids = sorted({_clean(item.get("entity_id")) for item in rows if _clean(item.get("entity_id"))})
        payload["source_entity_ids"] = source_entity_ids
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        current = self._active_profile(project_id, entity_id)
        if self._same_text(project_id, current, content):
            return current

        parents: list[str] = []
        for entity in rows:
            parents.extend(self._profile_parents(project_id, entity))
        parents = list(dict.fromkeys(item for item in parents if item))

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
            source={
                "type": "canonical_reusable_asset_profile",
                "entity_id": entity_id,
                "merged_source_entity_ids": source_entity_ids,
            },
            parent_asset_ids=parents,
            entity_ids=source_entity_ids or [entity_id],
            metadata={
                "asset_model": "reusable_authoring_asset_v1",
                "editable": True,
                "manual_design": bool(self._manual_design(canonical)),
                "kind": kind,
                "content_idempotent": True,
                "canonical_entity_id": entity_id,
                "source_entity_ids": source_entity_ids,
                "duplicate_sources_merged": len(source_entity_ids) > 1,
            },
        )

    @staticmethod
    def _stage_available(project: dict[str, Any], kind: str) -> bool:
        required = _STAGE_BY_TYPE[kind]
        completed = {_clean(item) for item in project.get("completed_stages") or []}
        current = _clean(project.get("current_stage"))
        return required in completed or _STAGE_ORDER_INDEX.get(current, 0) > _STAGE_ORDER_INDEX[required]

    def _profile_map(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Resolve every duplicate/legacy entity id to the canonical profile."""
        result: dict[str, dict[str, Any]] = {}
        for canonical, rows in self._canonical_groups(project_id):
            profile = self._active_profile(project_id, _clean(canonical.get("entity_id")))
            if not profile:
                continue
            for entity in rows:
                entity_id = _clean(entity.get("entity_id"))
                if entity_id:
                    result[entity_id] = profile
        return result

    def sync(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        completed = {_clean(item) for item in project.get("completed_stages") or []}
        current_stage = _clean(project.get("current_stage"))
        story = self._sync_story(project_id) if "01" in completed or current_stage != "01" else None
        profiles: list[dict[str, Any]] = []
        duplicate_groups = 0

        for canonical, rows in self._canonical_groups(project_id):
            kind = _clean(canonical.get("entity_type")).lower()
            if not self._stage_available(project, kind):
                continue
            if len(rows) > 1:
                duplicate_groups += 1
            profile = self._sync_entity_group(project_id, canonical, rows)
            if profile:
                profiles.append(profile)

        self.link_existing_dependents(project_id)
        return {
            "project_id": project_id,
            "story_asset_id": _clean((story or {}).get("asset_id")),
            "profile_asset_ids": [_clean(item.get("asset_id")) for item in profiles],
            "canonical_asset_kinds": ["character", "location", "prop"],
            "duplicate_groups_merged": duplicate_groups,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        sync_result = self.sync(project_id)
        items: list[dict[str, Any]] = []

        for canonical, rows in self._canonical_groups(project_id):
            kind = _clean(canonical.get("entity_type")).lower()
            if not self._stage_available(project, kind):
                continue
            profile = self._active_profile(project_id, _clean(canonical.get("entity_id")))
            if profile is None:
                continue
            stable_design = self._merged_design(canonical, rows)
            try:
                payload = json.loads(
                    self.production.read_text_asset(project_id, _clean(profile.get("asset_id")), max_chars=30000)
                )
                stable_design = _clean(payload.get("stable_design")) or stable_design
            except Exception:
                pass
            source_entity_ids = sorted({_clean(item.get("entity_id")) for item in rows if _clean(item.get("entity_id"))})
            items.append({
                "entity_id": _clean(canonical.get("entity_id")),
                "entity_type": kind,
                "label": _LABEL_BY_TYPE[kind],
                "name": _clean(canonical.get("name")),
                "stable_design": stable_design,
                "profile_asset_id": _clean(profile.get("asset_id")),
                "profile_version": int(profile.get("version") or 0),
                "editable": True,
                "deduplicated": True,
                "duplicate_source_count": len(source_entity_ids),
                "source_entity_ids": source_entity_ids,
            })

        return {
            "project_id": project_id,
            "items": items,
            "story_asset_id": _clean((self._active_story(project_id) or {}).get("asset_id")),
            "model": "asset_driven_front_half_v3_deduplicated",
            "canonical_asset_kinds": ["character", "location", "prop"],
            "local_invalidation": True,
            "content_idempotent": True,
            "stage01_story_entities_exposed": False,
            "duplicate_groups_merged": int(sync_result.get("duplicate_groups_merged") or 0),
        }

    def _invalidate_alias_dependents(
        self,
        project_id: str,
        source_entity_ids: set[str],
        *,
        keep_asset_id: str,
        revision_id: str,
        canonical_entity_id: str,
    ) -> list[str]:
        graph = self.production.get_graph(project_id)
        assets = graph.get("assets") or {}
        stale_ids: set[str] = set()

        for asset_id, asset in assets.items():
            if not isinstance(asset, dict) or asset.get("active") is False or str(asset_id) == keep_asset_id:
                continue
            entities = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
            stage = _clean(asset.get("stage"))
            role = _clean(asset.get("asset_role"))
            if not (entities & source_entity_ids):
                continue
            if stage not in _DOWNSTREAM_STAGES and role not in _REFERENCE_ROLES:
                continue
            asset["dependency_state"] = "stale"
            metadata = asset.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["stale_reason"] = "可复用资产设定已更新"
                metadata["entity_revision_id"] = revision_id
                metadata["changed_entity_id"] = canonical_entity_id
                metadata["changed_entity_alias_ids"] = sorted(source_entity_ids)
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
                if isinstance(metadata, dict):
                    metadata["stale_reason"] = "引用的上游资产版本已更新"
                    metadata["entity_revision_id"] = revision_id
                    metadata["changed_entity_id"] = canonical_entity_id
                asset["updated_at"] = _now()
                stale_ids.add(str(asset_id))
                changed = True

        self.production._save(graph)
        return sorted(stale_ids)

    def update_profile(
        self,
        project_id: str,
        entity_id: str,
        *,
        stable_design: str,
        change_reason: str = "",
    ) -> dict[str, Any]:
        self._assert_idle(project_id)
        canonical, rows = self._group_for_entity(project_id, entity_id)
        text = _clean(stable_design)
        if len(text) < 8:
            raise ValueError("稳定设定过短，请至少写清能够跨镜头保持一致的外观或结构特征")

        canonical_id = _clean(canonical.get("entity_id"))
        metadata = _copy(canonical.get("metadata") or {}, {})
        authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
        authoring.update({
            "stable_design": text,
            "change_reason": _clean(change_reason),
            "updated_at": _now(),
        })
        metadata["authoring"] = authoring
        continuity = metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {}
        core_profile = continuity.get("core_profile") if isinstance(continuity.get("core_profile"), dict) else {}
        core_profile["已确认稳定设定"] = text
        continuity["core_profile"] = core_profile
        metadata["continuity"] = continuity
        self.production.update_entity(project_id, canonical_id, {"metadata": metadata})

        previous = self._active_profile(project_id, canonical_id)
        canonical, rows = self._group_for_entity(project_id, canonical_id)
        profile = self._sync_entity_group(project_id, canonical, rows)
        if profile is None:
            raise RuntimeError("无法创建新的资产设定版本")

        source_entity_ids = {_clean(item.get("entity_id")) for item in rows if _clean(item.get("entity_id"))}
        revision_id = f"asset-rev-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        stale = self._invalidate_alias_dependents(
            project_id,
            source_entity_ids,
            keep_asset_id=_clean(profile.get("asset_id")),
            revision_id=revision_id,
            canonical_entity_id=canonical_id,
        )
        self.link_existing_dependents(project_id)
        return {
            "updated": True,
            "project_id": project_id,
            "entity_id": canonical_id,
            "source_entity_ids": sorted(source_entity_ids),
            "revision_id": revision_id,
            "previous_profile_asset_id": _clean((previous or {}).get("asset_id")),
            "profile_asset_id": _clean(profile.get("asset_id")),
            "profile_version": int(profile.get("version") or 1),
            "stale_asset_count": len(stale),
            "stale_asset_ids": stale,
            "duplicate_aliases_updated": max(0, len(source_entity_ids) - 1),
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
