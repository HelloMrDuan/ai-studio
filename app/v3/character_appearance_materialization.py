from __future__ import annotations

import json
from typing import Any

from .front_half_quality_gate import parse_character_appearance_versions
from .stage_asset_materialization import StageOutputAssetMaterializer, _clean, _name_key


class CharacterAppearanceMaterializer:
    """Turn Stage02's exact appearance machine block into real versioned assets.

    The existing ProductionAsset graph remains the only asset store. This layer
    does not generate images and does not invent variants; it only materializes
    the versions Qwen already committed in the validated Stage02 output.
    """

    def __init__(self, legacy_runtime: Any) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self.stage_materializer = StageOutputAssetMaterializer(legacy_runtime)

    def _character(self, project_id: str, name: str) -> dict[str, Any] | None:
        wanted = _name_key(name)
        rows = []
        for entity in self.production.list_entities(project_id):
            if _clean(entity.get("entity_type")).lower() != "character":
                continue
            if _name_key(entity.get("name")) != wanted:
                continue
            metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
            rows.append((
                0 if metadata.get("hidden_from_normal_lists") else 1,
                0 if metadata.get("merged_duplicate") else 1,
                _clean(entity.get("entity_id")),
                entity,
            ))
        rows.sort(reverse=True, key=lambda item: item[:3])
        return rows[0][3] if rows else None

    def _profile(self, project_id: str, entity_id: str) -> dict[str, Any] | None:
        rows = [
            asset for asset in self.production.list_assets(project_id, active_only=True)
            if _clean(asset.get("asset_role")) == "character_profile"
            and entity_id in {_clean(value) for value in asset.get("entity_ids") or []}
            and _clean(asset.get("status")).lower() == "ready"
            and _clean(asset.get("dependency_state")).lower() != "stale"
        ]
        rows.sort(key=lambda item: (int(item.get("version") or 0), _clean(item.get("updated_at"))))
        return rows[-1] if rows else None

    def materialize(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        if not self.stage_materializer._stage_ready(project, "02"):
            return {"project_id": project_id, "materialized": False, "asset_ids": [], "reason": "stage02_not_ready"}
        text = self.stage_materializer._stage_text(project, "02")
        if not text:
            return {"project_id": project_id, "materialized": False, "asset_ids": [], "reason": "stage02_output_missing"}
        try:
            packages = parse_character_appearance_versions(text)
        except ValueError as exc:
            # Compatibility only for projects whose Stage02 was confirmed before
            # the machine contract existed. New output cannot reach READY without
            # passing front_half_quality_gate first.
            return {
                "project_id": project_id,
                "materialized": False,
                "asset_ids": [],
                "legacy_unstructured_stage02": True,
                "reason": _clean(exc),
            }

        created: list[str] = []
        by_character: dict[str, list[str]] = {}
        for character_name, versions in packages.items():
            character = self._character(project_id, character_name)
            if character is None:
                raise ValueError(f"Stage02 形象版本找不到正式角色实体：{character_name}")
            entity_id = _clean(character.get("entity_id"))
            profile = self._profile(project_id, entity_id)
            if profile is None:
                raise ValueError(f"Stage02 形象版本找不到角色稳定资产：{character_name}")

            for row in versions:
                appearance_id = _clean(row.get("appearance_id"))
                appearance_version = "v1" if appearance_id == "default" else appearance_id
                payload = {
                    "schema_version": "xiaoduan_character_appearance_v1",
                    "appearance_id": appearance_id,
                    "character_entity_id": entity_id,
                    "character_id": entity_id,
                    "appearance_version": appearance_version,
                    "character_name": character_name,
                    "name": _clean(row.get("name")),
                    "stable_design": _clean(row.get("stable_design")),
                    "change_reason": _clean(row.get("change_reason")),
                    "effective_story_node_ids": list(row.get("effective_story_node_ids") or []),
                    "inherits_identity": True,
                }
                asset = self.production.create_text_asset(
                    project_id,
                    stage="02",
                    skill="xiaoduan-character-appearances",
                    logical_key=f"studio:character:{entity_id}:appearance:{appearance_id}",
                    asset_role="character_appearance",
                    name=f"{character_name} · {payload['name']}",
                    content=json.dumps(payload, ensure_ascii=False, indent=2),
                    asset_type="STRUCTURED_DATA",
                    extension=".json",
                    source={"type": "stage02_appearance_version", "character_name": character_name},
                    parent_asset_ids=[_clean(profile.get("asset_id"))],
                    entity_ids=[entity_id],
                    metadata={
                        "appearance_id": appearance_id,
                        "character_name": character_name,
                        "change_reason": payload["change_reason"],
                        "effective_story_node_ids": payload["effective_story_node_ids"],
                        "inherits_identity": True,
                        "materialized_from_stage02": True,
                    },
                )
                aid = _clean(asset.get("asset_id"))
                created.append(aid)
                by_character.setdefault(entity_id, []).append(aid)

        return {
            "project_id": project_id,
            "materialized": bool(created),
            "asset_ids": list(dict.fromkeys(created)),
            "character_asset_ids": by_character,
            "source": "validated_stage02_appearance_versions",
            "model_calls": 0,
        }


__all__ = ["CharacterAppearanceMaterializer"]
