from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException


_REUSABLE_TYPES = {"character", "location", "scene", "prop"}
_STAGE_BY_TYPE = {
    "character": "02",
    "location": "03",
    "scene": "03",
    "prop": "03",
}
_ROLE_BY_TYPE = {
    "character": "character_profile",
    "location": "location_profile",
    "scene": "location_profile",
    "prop": "prop_profile",
}
_LABEL_BY_TYPE = {
    "character": "角色",
    "location": "场景",
    "scene": "场景",
    "prop": "道具",
}
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
_ACTIVE_TASK_STATES = {"starting", "warming", "queued", "switching_gpu", "running", "generating", "persisting"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


def _flatten(value: Any, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 3:
        return []
    rows: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            rows.extend(_flatten(item, name, depth + 1))
    elif isinstance(value, list):
        values = [_clean(item) for item in value if not isinstance(item, (dict, list)) and _clean(item)]
        if prefix and values:
            rows.append(f"{prefix}：{'；'.join(values[:24])}")
    elif prefix and value is not None and _clean(value):
        rows.append(f"{prefix}：{_clean(value)}")
    return rows


class AuthoringAssetService:
    """Front-half reusable asset/version layer on top of the existing workbench.

    The shape follows the useful asset discipline studied in waoowaoo:
    character/location/prop are durable reusable assets, their descriptions are
    editable versions, rendered references are candidates, and downstream shots
    consume selected/adopted versions. This implementation is independent and
    keeps Xiaoduan's existing ProductionAssetService/candidate lifecycle.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self._original_confirm: Callable[[str], Awaitable[dict[str, Any]]] | None = None

    @staticmethod
    def profile_key(entity_id: str) -> str:
        return f"studio:authoring:{entity_id}:profile"

    @staticmethod
    def story_key() -> str:
        return "studio:authoring:story-bible"

    def _entity(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for item in self.production.list_entities(project_id):
            if _clean(item.get("entity_id")) == _clean(entity_id):
                kind = _clean(item.get("entity_type")).lower()
                if kind not in _REUSABLE_TYPES:
                    raise ValueError("当前故事元素不是可复用角色、场景或道具资产")
                return item
        raise FileNotFoundError(f"故事资产不存在：{entity_id}")

    def _active_profile(self, project_id: str, entity_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == self.profile_key(entity_id)
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _active_story(self, project_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == self.story_key()
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _story_payload(self, project_id: str) -> dict[str, Any] | None:
        project = self.director.get_project(project_id)
        confirmed = (project.get("confirmed_outputs") or {}).get("01") or {}
        handoff = _clean(confirmed.get("handoff"))
        if not handoff:
            return None
        return {
            "schema_version": "xiaoduan_story_bible_v1",
            "source": "confirmed_stage_01",
            "handoff": handoff,
            "confirmed_at": _clean(confirmed.get("confirmed_at") or confirmed.get("refreshed_at")),
            "production_asset_ids": list(confirmed.get("production_asset_ids") or []),
        }

    def _sync_story(self, project_id: str) -> dict[str, Any] | None:
        payload = self._story_payload(project_id)
        if payload is None:
            return None
        parents = [
            _clean(item) for item in payload.get("production_asset_ids") or []
            if _clean(item)
        ]
        return self.production.create_text_asset(
            project_id,
            stage="01",
            skill="xiaoduan-authoring-assets",
            logical_key=self.story_key(),
            asset_role="story_bible",
            name="已确认故事结构",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "confirmed_stage_01"},
            parent_asset_ids=parents,
            metadata={
                "asset_model": "reusable_authoring_asset_v1",
                "editable": False,
                "source_of_truth": "stage_01_confirmed_output",
            },
        )

    @staticmethod
    def _continuity(entity: dict[str, Any]) -> dict[str, Any]:
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        continuity = metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {}
        return continuity

    def _manual_design(self, entity: dict[str, Any]) -> str:
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
        return _clean(authoring.get("stable_design"))

    def _default_profile_text(self, entity: dict[str, Any]) -> str:
        manual = self._manual_design(entity)
        if manual:
            return manual
        continuity = self._continuity(entity)
        rows = []
        for section_name, section in (
            ("稳定身份", continuity.get("core_profile") or {}),
            ("默认状态", continuity.get("default_state") or {}),
        ):
            values = _flatten(section)
            if values:
                rows.append(section_name + "：\n" + "\n".join(f"- {item}" for item in values))
        if not rows:
            metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
            values = [
                item for item in _flatten(metadata)
                if not item.startswith("authoring.")
            ]
            if values:
                rows.append("项目已确认设定：\n" + "\n".join(f"- {item}" for item in values[:32]))
        return "\n\n".join(rows).strip() or "当前仅确认了名称；请补充稳定、可跨镜头复用的视觉设定。"

    def _profile_payload(self, entity: dict[str, Any]) -> dict[str, Any]:
        kind = _clean(entity.get("entity_type")).lower()
        continuity = self._continuity(entity)
        return {
            "schema_version": "xiaoduan_reusable_asset_profile_v1",
            "entity_id": _clean(entity.get("entity_id")),
            "kind": kind,
            "name": _clean(entity.get("name")),
            "aliases": list(continuity.get("aliases") or []),
            "stable_profile": _copy(continuity.get("core_profile") or {}, {}),
            "default_state": _copy(continuity.get("default_state") or {}, {}),
            "stable_design": self._default_profile_text(entity),
        }

    def _profile_parents(self, project_id: str, entity: dict[str, Any]) -> list[str]:
        parents: list[str] = []
        story = self._active_story(project_id)
        if story:
            parents.append(_clean(story.get("asset_id")))
        for evidence in entity.get("evidence") or []:
            if isinstance(evidence, dict) and _clean(evidence.get("source_asset_id")):
                parents.append(_clean(evidence.get("source_asset_id")))
        return list(dict.fromkeys(item for item in parents if item))

    def _sync_entity(self, project_id: str, entity: dict[str, Any]) -> dict[str, Any] | None:
        kind = _clean(entity.get("entity_type")).lower()
        if kind not in _REUSABLE_TYPES:
            return None
        payload = self._profile_payload(entity)
        entity_id = payload["entity_id"]
        return self.production.create_text_asset(
            project_id,
            stage=_STAGE_BY_TYPE[kind],
            skill="xiaoduan-authoring-assets",
            logical_key=self.profile_key(entity_id),
            asset_role=_ROLE_BY_TYPE[kind],
            name=f"{_LABEL_BY_TYPE[kind]}「{payload['name']}」稳定设定",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
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
            },
        )

    def sync(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        completed = {_clean(item) for item in project.get("completed_stages") or []}
        story = self._sync_story(project_id) if "01" in completed or _clean(project.get("current_stage")) != "01" else None
        profiles = []
        if completed & {"02", "03", "04"} or _clean(project.get("current_stage")) in {"03", "04"}:
            for entity in self.production.list_entities(project_id):
                kind = _clean(entity.get("entity_type")).lower()
                if kind not in _REUSABLE_TYPES:
                    continue
                # Character profile belongs to stage 02; location/prop belong to stage 03.
                required_stage = _STAGE_BY_TYPE[kind]
                if required_stage in completed or _STAGE_ORDER_INDEX.get(_clean(project.get("current_stage")), 0) > _STAGE_ORDER_INDEX[required_stage]:
                    profile = self._sync_entity(project_id, entity)
                    if profile:
                        profiles.append(profile)
        self.link_existing_dependents(project_id)
        return {
            "project_id": project_id,
            "story_asset_id": _clean((story or {}).get("asset_id")),
            "profile_asset_ids": [_clean(item.get("asset_id")) for item in profiles],
        }

    def _profile_map(self, project_id: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for entity in self.production.list_entities(project_id):
            entity_id = _clean(entity.get("entity_id"))
            profile = self._active_profile(project_id, entity_id)
            if profile:
                result[entity_id] = profile
        return result

    def link_existing_dependents(self, project_id: str) -> int:
        """Attach exact reusable-profile parents to existing shot/media assets.

        This makes future profile version changes invalidate only assets that
        actually reference the edited entity, rather than an entire stage.
        """
        profiles = self._profile_map(project_id)
        if not profiles:
            return 0
        changed = 0
        for asset in self.production.list_assets(project_id, active_only=True):
            stage = _clean(asset.get("stage"))
            role = _clean(asset.get("asset_role"))
            if stage not in _DOWNSTREAM_STAGES and role not in _REFERENCE_ROLES:
                continue
            entity_ids = [_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)]
            parents = [
                _clean(profiles[item].get("asset_id"))
                for item in entity_ids
                if item in profiles
            ]
            if not parents:
                continue
            before = set(asset.get("parent_asset_ids") or [])
            self.production.set_asset_dependencies(project_id, _clean(asset.get("asset_id")), parents, merge=True)
            if not set(parents).issubset(before):
                changed += 1
        return changed

    def _assert_idle(self, project_id: str) -> None:
        loader = getattr(self.legacy, "_wb_load_candidates", None)
        if callable(loader):
            for row in loader(project_id) or []:
                if _clean(row.get("status")).lower() in _ACTIVE_TASK_STATES and not _clean(row.get("confirmed_asset_id")):
                    raise RuntimeError("当前作品仍有生成任务运行中，请等待任务结束后再修改资产设定。")

    def _invalidate_entity_dependents(
        self,
        project_id: str,
        entity_id: str,
        *,
        keep_asset_id: str,
        revision_id: str,
    ) -> list[str]:
        graph = self.production.get_graph(project_id)
        assets = graph.get("assets") or {}
        stale_ids: set[str] = set()

        # First invalidate only resources explicitly bound to this entity.
        for asset_id, asset in assets.items():
            if not isinstance(asset, dict) or asset.get("active") is False or str(asset_id) == keep_asset_id:
                continue
            entities = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
            stage = _clean(asset.get("stage"))
            role = _clean(asset.get("asset_role"))
            if entity_id not in entities:
                continue
            if stage not in _DOWNSTREAM_STAGES and role not in _REFERENCE_ROLES:
                continue
            asset["dependency_state"] = "stale"
            metadata = asset.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["stale_reason"] = "可复用资产设定已更新"
                metadata["entity_revision_id"] = revision_id
                metadata["changed_entity_id"] = entity_id
            asset["updated_at"] = _now()
            stale_ids.add(str(asset_id))

        # Then propagate only through real parent-child lineage.
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
                    metadata["changed_entity_id"] = entity_id
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
        entity = self._entity(project_id, entity_id)
        text = _clean(stable_design)
        if len(text) < 8:
            raise ValueError("稳定设定过短，请至少写清能够跨镜头保持一致的外观或结构特征")
        metadata = _copy(entity.get("metadata") or {}, {})
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
        self.production.update_entity(project_id, entity_id, {"metadata": metadata})

        updated_entity = self._entity(project_id, entity_id)
        previous = self._active_profile(project_id, entity_id)
        profile = self._sync_entity(project_id, updated_entity)
        if profile is None:
            raise RuntimeError("无法创建新的资产设定版本")
        revision_id = f"asset-rev-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        stale = self._invalidate_entity_dependents(
            project_id,
            entity_id,
            keep_asset_id=_clean(profile.get("asset_id")),
            revision_id=revision_id,
        )
        return {
            "updated": True,
            "project_id": project_id,
            "entity_id": entity_id,
            "revision_id": revision_id,
            "previous_profile_asset_id": _clean((previous or {}).get("asset_id")),
            "profile_asset_id": _clean(profile.get("asset_id")),
            "profile_version": int(profile.get("version") or 1),
            "stale_asset_count": len(stale),
            "stale_asset_ids": stale,
            "local_invalidation": True,
        }

    def status(self, project_id: str) -> dict[str, Any]:
        self.sync(project_id)
        items = []
        for entity in self.production.list_entities(project_id):
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in _REUSABLE_TYPES:
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
            "model": "asset_driven_front_half_v1",
            "local_invalidation": True,
        }

    def install_confirmation_hook(self) -> None:
        if getattr(self.director, "_xiaoduan_authoring_asset_hook_installed", False):
            return
        original = getattr(self.director, "confirm_stage", None)
        if not callable(original):
            raise RuntimeError("原工作台缺少阶段确认入口，无法安装资产驱动同步")
        self._original_confirm = original

        async def wrapped(project_id: str) -> dict[str, Any]:
            before = self.director.get_project(project_id)
            stage = _clean(before.get("current_stage"))
            result = await original(project_id)
            if stage in {"01", "02", "03", "04"}:
                self.sync(project_id)
            return result

        self.director.confirm_stage = wrapped
        self.director._xiaoduan_authoring_asset_hook_installed = True


_STAGE_ORDER_INDEX = {"01": 1, "02": 2, "03": 3, "04": 4, "": 0}


def create_authoring_asset_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = AuthoringAssetService(settings, legacy_runtime)

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


__all__ = ["AuthoringAssetService", "create_authoring_asset_router"]
