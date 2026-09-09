from __future__ import annotations

import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _safe_id(value: str) -> str:
    raw = _clean(value)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", raw):
        raise ValueError("形象版本编号格式不正确")
    return raw


class CharacterAppearanceService:
    """Durable appearance variants for one canonical character entity."""

    def __init__(self, legacy_runtime: Any) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production

    def _character(self, project_id: str, entity_id: str) -> dict[str, Any]:
        for entity in self.production.list_entities(project_id):
            if _clean(entity.get("entity_id")) != _clean(entity_id):
                continue
            if _clean(entity.get("entity_type")).lower() != "character":
                raise ValueError("只有角色资产支持形象版本")
            return entity
        raise FileNotFoundError(f"角色不存在：{entity_id}")

    @staticmethod
    def _logical_key(entity_id: str, appearance_id: str) -> str:
        return f"studio:character:{entity_id}:appearance:{appearance_id}"

    def _character_profile(self, project_id: str, entity_id: str) -> dict[str, Any] | None:
        rows = [
            item for item in self.production.list_assets(project_id, active_only=True)
            if _clean(item.get("asset_role")) == "character_profile"
            and entity_id in {_clean(x) for x in item.get("entity_ids") or []}
            and _clean(item.get("status")).lower() == "ready"
            and _clean(item.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda x: (int(x.get("version") or 0), _clean(x.get("updated_at"))))
        return rows[-1] if rows else None

    def _profile_text(self, project_id: str, entity_id: str) -> str:
        profile = self._character_profile(project_id, entity_id)
        if not profile:
            return ""
        try:
            raw = self.production.read_text_asset(project_id, _clean(profile.get("asset_id")), max_chars=20000)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return _clean(parsed.get("stable_design")) or raw
        except Exception:
            try:
                return self.production.read_text_asset(project_id, _clean(profile.get("asset_id")), max_chars=20000)
            except Exception:
                return ""
        return ""

    def ensure_default(self, project_id: str, entity_id: str) -> dict[str, Any] | None:
        character = self._character(project_id, entity_id)
        text = self._profile_text(project_id, entity_id)
        if not text:
            return None
        profile = self._character_profile(project_id, entity_id)
        payload = {
            "schema_version": "xiaoduan_character_appearance_v1",
            "appearance_id": "default",
            "character_entity_id": entity_id,
            "name": "默认造型",
            "stable_design": text,
            "change_reason": "角色基础造型",
            "effective_story_node_ids": [],
            "inherits_identity": True,
        }
        return self.production.create_text_asset(
            project_id,
            stage="02",
            skill="xiaoduan-character-appearances",
            logical_key=self._logical_key(entity_id, "default"),
            asset_role="character_appearance",
            name=f"{_clean(character.get('name'))} · 默认造型",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "character_profile_default_appearance"},
            parent_asset_ids=[_clean(profile.get("asset_id"))] if profile else [],
            entity_ids=[entity_id],
            metadata={
                "appearance_id": "default",
                "change_reason": "角色基础造型",
                "inherits_identity": True,
            },
        )

    def list(self, project_id: str) -> dict[str, Any]:
        self.director.get_project(project_id)
        for entity in self.production.list_entities(project_id, entity_type="character"):
            self.ensure_default(project_id, _clean(entity.get("entity_id")))
        rows = []
        for asset in self.production.list_assets(project_id, active_only=True):
            if _clean(asset.get("asset_role")) != "character_appearance":
                continue
            entity_ids = [_clean(x) for x in asset.get("entity_ids") or [] if _clean(x)]
            if not entity_ids:
                continue
            content: dict[str, Any] = {}
            try:
                raw = self.production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=20000)
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    content = parsed
            except Exception:
                pass
            rows.append({
                "asset_id": _clean(asset.get("asset_id")),
                "asset_version": int(asset.get("version") or 1),
                "character_entity_id": entity_ids[0],
                "appearance_id": _clean(content.get("appearance_id") or (asset.get("metadata") or {}).get("appearance_id")),
                "name": _clean(content.get("name") or asset.get("name")),
                "stable_design": _clean(content.get("stable_design")),
                "change_reason": _clean(content.get("change_reason")),
                "effective_story_node_ids": list(content.get("effective_story_node_ids") or []),
                "dependency_state": _clean(asset.get("dependency_state") or "current"),
            })
        rows.sort(key=lambda x: (x["character_entity_id"], x["appearance_id"], x["asset_version"]))
        return {
            "project_id": project_id,
            "appearances": rows,
            "model": "character_appearance_variants_v1",
        }

    def save(
        self,
        project_id: str,
        entity_id: str,
        *,
        appearance_id: str,
        name: str,
        stable_design: str,
        change_reason: str,
        effective_story_node_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        character = self._character(project_id, entity_id)
        aid = _safe_id(appearance_id or ("look_" + secrets.token_hex(4)))
        design = _clean(stable_design)
        if len(design) < 8:
            raise ValueError("形象版本设定过短")
        reason = _clean(change_reason)
        if aid != "default" and not reason:
            raise ValueError("新增剧情形象版本必须填写变化原因")
        profile = self._character_profile(project_id, entity_id)
        previous = [
            row for row in self.production.list_assets(project_id, active_only=True)
            if _clean(row.get("logical_key")) == self._logical_key(entity_id, aid)
        ]
        previous_asset_id = _clean(previous[-1].get("asset_id")) if previous else ""
        payload = {
            "schema_version": "xiaoduan_character_appearance_v1",
            "appearance_id": aid,
            "character_entity_id": entity_id,
            "name": _clean(name) or ("默认造型" if aid == "default" else aid),
            "stable_design": design,
            "change_reason": reason or "角色基础造型",
            "effective_story_node_ids": [
                _clean(x) for x in (effective_story_node_ids or []) if _clean(x)
            ],
            "inherits_identity": True,
        }
        asset = self.production.create_text_asset(
            project_id,
            stage="02",
            skill="xiaoduan-character-appearances",
            logical_key=self._logical_key(entity_id, aid),
            asset_role="character_appearance",
            name=f"{_clean(character.get('name'))} · {payload['name']}",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
            asset_type="STRUCTURED_DATA",
            extension=".json",
            source={"type": "manual_character_appearance"},
            parent_asset_ids=[_clean(profile.get("asset_id"))] if profile else [],
            entity_ids=[entity_id],
            metadata={
                "appearance_id": aid,
                "change_reason": payload["change_reason"],
                "inherits_identity": True,
            },
        )
        return {
            "saved": True,
            "project_id": project_id,
            "character_entity_id": entity_id,
            "appearance_id": aid,
            "previous_asset_id": previous_asset_id,
            "asset_id": _clean(asset.get("asset_id")),
            "asset_version": int(asset.get("version") or 1),
        }


def create_character_appearance_router(legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = CharacterAppearanceService(legacy_runtime)

    @router.get("/api/v3/studio/projects/{project_id}/character-appearances")
    async def list_appearances(project_id: str) -> dict[str, Any]:
        try:
            return service.list(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/v3/studio/projects/{project_id}/character-appearances/{entity_id}")
    async def save_appearance(project_id: str, entity_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return service.save(
                project_id,
                entity_id,
                appearance_id=_clean(payload.get("appearance_id")),
                name=_clean(payload.get("name")),
                stable_design=_clean(payload.get("stable_design")),
                change_reason=_clean(payload.get("change_reason")),
                effective_story_node_ids=list(payload.get("effective_story_node_ids") or []),
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router


__all__ = ["CharacterAppearanceService", "create_character_appearance_router"]
