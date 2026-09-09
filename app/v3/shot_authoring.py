from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException


_EDITABLE_FIELDS = (
    "title",
    "summary",
    "duration_seconds",
    "composition",
    "shot_size",
    "camera",
    "camera_move",
    "action",
    "performance",
    "environment",
    "dialogue",
    "narration",
    "sound",
    "music",
    "representative_state",
    "video_start_state",
    "video_end_state",
    "image_prompt",
    "video_prompt",
)
_SHOT_MEDIA_ROLES = {
    "shot_keyframe",
    "shot_video_start_frame",
    "shot_clip",
    "shot_image_processed",
    "shot_video_processed",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


class ShotAuthoringService:
    """Edit one formal shot without reopening the whole storyboard stage.

    A manual shot edit is persisted as a new versioned shot-contract asset.
    Only media bound to that shot and their real descendants are invalidated.
    The existing Stage04 generation routes remain the production gate: after an
    edit, image/video generation still has to satisfy the strict shot contract.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self.root = Path(settings.data_dir) / "story_continuity"

    def _path(self, project_id: str) -> Path:
        self.director.get_project(project_id)
        return self.root / f"{project_id}.json"

    def _load(self, project_id: str) -> dict[str, Any]:
        path = self._path(project_id)
        if not path.is_file():
            raise FileNotFoundError("当前作品还没有正式分镜数据")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("分镜数据格式无效")
        if not isinstance(value.get("shots"), list):
            value["shots"] = []
        return value

    def _save(self, project_id: str, state: dict[str, Any]) -> None:
        path = self._path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = _now()
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    @staticmethod
    def _shot(state: dict[str, Any], shot_id: str) -> dict[str, Any]:
        wanted = _clean(shot_id)
        for item in state.get("shots") or []:
            if isinstance(item, dict) and _clean(item.get("shot_id")) == wanted:
                return item
        raise FileNotFoundError(f"正式镜头不存在：{shot_id}")

    @staticmethod
    def _scene(state: dict[str, Any], scene_id: str) -> dict[str, Any]:
        wanted = _clean(scene_id)
        for item in state.get("scenes") or []:
            if isinstance(item, dict) and _clean(item.get("scene_id")) == wanted:
                return item
        return {}

    @staticmethod
    def contract_key(shot_id: str) -> str:
        return f"studio:shot:{shot_id}:manual-contract"

    def active_contract(self, project_id: str, shot_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == self.contract_key(shot_id)
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def _profile_parent(self, project_id: str, entity_id: str) -> str:
        key = f"studio:authoring:{entity_id}:profile"
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("logical_key")) == key
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return _clean((rows[-1] if rows else {}).get("asset_id"))

    def _entity_ids(self, state: dict[str, Any], shot: dict[str, Any]) -> list[str]:
        result = []
        result.extend(_clean(item) for item in shot.get("character_entity_ids") or [] if _clean(item))
        result.extend(_clean(item) for item in shot.get("prop_entity_ids") or [] if _clean(item))
        scene = self._scene(state, _clean(shot.get("scene_id")))
        location_id = _clean(scene.get("location_entity_id"))
        if location_id:
            result.append(location_id)
        shot_entity_id = _clean(shot.get("entity_id"))
        if shot_entity_id:
            result.append(shot_entity_id)
        return list(dict.fromkeys(result))

    def _contract_payload(self, shot: dict[str, Any], revision_id: str) -> dict[str, Any]:
        return {
            "schema_version": "xiaoduan_manual_shot_contract_v1",
            "revision_id": revision_id,
            "shot_id": _clean(shot.get("shot_id")),
            **{field: _copy(shot.get(field), shot.get(field)) for field in _EDITABLE_FIELDS},
            "scene_id": _clean(shot.get("scene_id")),
            "character_entity_ids": list(shot.get("character_entity_ids") or []),
            "prop_entity_ids": list(shot.get("prop_entity_ids") or []),
            "source_provenance": _copy(shot.get("source_provenance") or {}, {}),
        }

    def _invalidate_shot_media(
        self,
        project_id: str,
        shot: dict[str, Any],
        revision_id: str,
    ) -> list[str]:
        graph = self.production.get_graph(project_id)
        assets = graph.get("assets") or {}
        shot_id = _clean(shot.get("shot_id"))
        shot_entity_id = _clean(shot.get("entity_id"))
        stale_ids: set[str] = set()

        for asset_id, asset in assets.items():
            if not isinstance(asset, dict) or asset.get("active") is False:
                continue
            role = _clean(asset.get("asset_role"))
            if role not in _SHOT_MEDIA_ROLES:
                continue
            metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
            source = asset.get("source") if isinstance(asset.get("source"), dict) else {}
            bound_shot = _clean(metadata.get("shot_id") or source.get("shot_id"))
            entity_ids = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
            if bound_shot != shot_id and (not shot_entity_id or shot_entity_id not in entity_ids):
                continue
            asset["dependency_state"] = "stale"
            asset.setdefault("metadata", {})["stale_reason"] = "该镜头的分镜合同已修改"
            asset["metadata"]["shot_revision_id"] = revision_id
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
                metadata["stale_reason"] = "引用的镜头版本已修改"
                metadata["shot_revision_id"] = revision_id
                asset["updated_at"] = _now()
                stale_ids.add(str(asset_id))
                changed = True

        self.production._save(graph)
        return sorted(stale_ids)

    def get(self, project_id: str, shot_id: str) -> dict[str, Any]:
        state = self._load(project_id)
        shot = self._shot(state, shot_id)
        contract = self.active_contract(project_id, shot_id)
        return {
            "project_id": project_id,
            "shot_id": shot_id,
            "fields": {field: _copy(shot.get(field), shot.get(field)) for field in _EDITABLE_FIELDS},
            "contract_asset_id": _clean((contract or {}).get("asset_id")),
            "contract_version": int((contract or {}).get("version") or 0),
            "editable_fields": list(_EDITABLE_FIELDS),
        }

    def update(self, project_id: str, shot_id: str, patch: dict[str, Any], *, reason: str = "") -> dict[str, Any]:
        state = self._load(project_id)
        shot = self._shot(state, shot_id)
        applied: dict[str, Any] = {}
        for field in _EDITABLE_FIELDS:
            if field not in patch:
                continue
            if field == "duration_seconds":
                value = float(patch.get(field) or 0)
                if value <= 0 or value > 120:
                    raise ValueError("镜头时长必须大于0秒且不超过120秒")
                shot[field] = value
            else:
                shot[field] = _clean(patch.get(field))
            applied[field] = shot[field]
        if not applied:
            raise ValueError("没有提交可修改的镜头字段")
        if not _clean(shot.get("representative_state")):
            raise ValueError("镜头代表状态不能为空")
        if not _clean(shot.get("image_prompt")):
            raise ValueError("分镜画面生成要求不能为空")
        if not _clean(shot.get("video_prompt")):
            raise ValueError("视频生成要求不能为空")

        revision_id = f"shot-rev-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        shot["manual_revision"] = {
            "revision_id": revision_id,
            "reason": _clean(reason),
            "fields": sorted(applied),
            "updated_at": _now(),
        }
        # Preserve storyboard_source_sha256. It identifies the confirmed Stage04
        # source asset; keeping it stable means an ordinary continuity refresh
        # does not overwrite a local shot revision. A true Stage04 rebuild changes
        # the source hash and correctly replaces the local edit.
        self._save(project_id, state)

        entity_ids = self._entity_ids(state, shot)
        profile_parents = [
            self._profile_parent(project_id, entity_id)
            for entity_id in entity_ids
        ]
        profile_parents = [item for item in profile_parents if item]
        contract = self.production.create_text_asset(
            project_id,
            stage="04",
            skill="xiaoduan-shot-authoring",
            logical_key=self.contract_key(shot_id),
            asset_role="shot_contract",
            name=f"镜头 {shot_id} · 人工修改合同",
            content=json.dumps(self._contract_payload(shot, revision_id), ensure_ascii=False, indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "manual_shot_revision", "shot_id": shot_id},
            parent_asset_ids=profile_parents,
            entity_ids=entity_ids,
            metadata={
                "manual_revision": True,
                "shot_id": shot_id,
                "revision_id": revision_id,
                "reason": _clean(reason),
                "strict_generation_gate_preserved": True,
            },
        )
        stale = self._invalidate_shot_media(project_id, shot, revision_id)
        return {
            "updated": True,
            "project_id": project_id,
            "shot_id": shot_id,
            "revision_id": revision_id,
            "contract_asset_id": _clean(contract.get("asset_id")),
            "contract_version": int(contract.get("version") or 1),
            "changed_fields": sorted(applied),
            "stale_asset_count": len(stale),
            "stale_asset_ids": stale,
            "local_invalidation": True,
            "message": "镜头已保存为新版本；重新生成该镜头画面/视频时仍会执行现有严格合同校验。",
        }

    def bind_active_contract_to_target(self, project_id: str, target: dict[str, Any]) -> dict[str, Any]:
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        source = target.get("source") if isinstance(target.get("source"), dict) else {}
        shot_id = _clean(metadata.get("shot_id") or source.get("shot_id"))
        if not shot_id:
            return target
        contract = self.active_contract(project_id, shot_id)
        if not contract:
            return target
        return self.production.set_asset_dependencies(
            project_id,
            _clean(target.get("asset_id")),
            [_clean(contract.get("asset_id"))],
            merge=True,
        )


def create_shot_authoring_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ShotAuthoringService(settings, legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/shots/{shot_id}/authoring")
    async def get_shot_authoring(project_id: str, shot_id: str) -> dict[str, Any]:
        try:
            return service.get(project_id, shot_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/api/v3/studio/projects/{project_id}/shots/{shot_id}/authoring")
    async def update_shot_authoring(project_id: str, shot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
            return service.update(project_id, shot_id, fields, reason=_clean(payload.get("reason")))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["ShotAuthoringService", "create_shot_authoring_router"]
