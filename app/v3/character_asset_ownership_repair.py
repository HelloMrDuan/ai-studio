from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from .stage_asset_materialization import StageOutputAssetMaterializer, _clean


_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}


def _name_key(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold()
    return "".join(ch for ch in text if ch not in _ZERO_WIDTH and not ch.isspace())


def _profile_name(asset: dict[str, Any], payload: dict[str, Any]) -> str:
    name = _clean(payload.get("name"))
    if name:
        return name
    match = re.search(r"[「『](.+?)[」』]", _clean(asset.get("name")))
    return _clean(match.group(1)) if match else ""


def _appearance_name(asset: dict[str, Any], payload: dict[str, Any]) -> str:
    name = _clean(payload.get("character_name") or (asset.get("metadata") or {}).get("character_name"))
    if name:
        return name
    value = _clean(asset.get("name"))
    if "·" in value:
        return _clean(value.split("·", 1)[0])
    return ""


class CharacterAssetOwnershipRepair:
    """Repair old Stage② character ownership without another model call.

    Earlier compatibility passes could leave a valid Stage② character hidden as
    an alias/retired fragment, or leave a profile/appearance asset bound to the
    wrong entity id while its textual payload still contained the correct name.
    Stage②'s explicit character blocks are authoritative, so we restore exactly
    those characters and re-home derived assets by their embedded character name.
    """

    def __init__(self, legacy_runtime: Any) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.production = self.director.production
        self.materializer = StageOutputAssetMaterializer(legacy_runtime)

    def _stage02_names(self, project_id: str) -> list[str]:
        project = self.director.get_project(project_id)
        if not self.materializer._stage_ready(project, "02"):
            return []
        text = self.materializer._stage_text(project, "02")
        if not text:
            return []
        rows = self.materializer._extract(project_id, "02", text)
        names: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if _clean(row.get("kind")) != "character":
                continue
            name = _clean(row.get("name"))
            key = _name_key(name)
            if name and key and key not in seen:
                seen.add(key)
                names.append(name)
        return names

    def _ensure_entities(self, project_id: str, names: list[str]) -> tuple[dict[str, str], bool]:
        graph = self.production.get_graph(project_id)
        entities = graph.get("entities") or {}
        changed = False
        result: dict[str, str] = {}

        for name in names:
            wanted = _name_key(name)
            candidates: list[tuple[str, dict[str, Any]]] = []
            for entity_id, entity in entities.items():
                if not isinstance(entity, dict) or _name_key(entity.get("name")) != wanted:
                    continue
                kind = _clean(entity.get("entity_type")).lower()
                metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
                authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
                stage02_owned = (
                    kind == "character"
                    or _clean(authoring.get("source_stage")) == "02"
                    or _clean(entity.get("stage")) == "02"
                    or _clean(entity.get("logical_key")).startswith("xiaoduan:character:")
                )
                if stage02_owned:
                    candidates.append((str(entity_id), entity))

            if candidates:
                # Prefer a currently visible character; otherwise revive the
                # richest Stage② row instead of inventing another identity.
                candidates.sort(
                    key=lambda pair: (
                        1 if _clean(pair[1].get("entity_type")).lower() == "character" else 0,
                        0 if bool((pair[1].get("metadata") or {}).get("merged_duplicate")) else 1,
                        len(json.dumps(pair[1].get("metadata") or {}, ensure_ascii=False, default=str)),
                        pair[0],
                    ),
                    reverse=True,
                )
                canonical_id, canonical = candidates[0]
            else:
                created = self.production.create_entity(
                    project_id,
                    entity_type="character",
                    name=name,
                    logical_key=f"xiaoduan:character:recovered:{wanted}",
                    stage="02",
                    skill="xiaoduan-character-assets",
                    metadata={"ownership_recovered_from_stage02": True},
                    evidence={"source_stage": "02", "type": "stage02_character_recovery"},
                )
                graph = self.production.get_graph(project_id)
                entities = graph.get("entities") or {}
                canonical_id = _clean(created.get("entity_id"))
                canonical = entities.get(canonical_id) or created
                changed = True

            metadata = canonical.get("metadata") if isinstance(canonical.get("metadata"), dict) else {}
            target_id = _clean(metadata.get("canonical_entity_id"))
            target = entities.get(target_id) if target_id else None
            # Keep a valid same-name canonical target. Cross-name aliases are
            # corruption and must never collapse two different characters.
            if isinstance(target, dict) and _name_key(target.get("name")) == wanted and _clean(target.get("entity_type")).lower() == "character":
                canonical_id, canonical = target_id, target
                metadata = canonical.get("metadata") if isinstance(canonical.get("metadata"), dict) else {}
            else:
                if _clean(canonical.get("entity_type")).lower() != "character":
                    canonical["entity_type"] = "character"
                    changed = True
                for key in (
                    "canonical_entity_id", "merged_duplicate", "hidden_from_normal_lists",
                    "retired_materialized_fragment", "retired_reason",
                ):
                    if key in metadata:
                        metadata.pop(key, None)
                        changed = True
                canonical["metadata"] = metadata
                canonical["stage"] = "02"

            result[wanted] = canonical_id

            # Any other same-name character row becomes a normal historical alias
            # of this exact same character. Different names are never touched.
            for other_id, other in candidates:
                if other_id == canonical_id:
                    continue
                ometa = other.get("metadata") if isinstance(other.get("metadata"), dict) else {}
                if _clean(ometa.get("canonical_entity_id")) != canonical_id or not bool(ometa.get("merged_duplicate")):
                    ometa["canonical_entity_id"] = canonical_id
                    ometa["merged_duplicate"] = True
                    ometa["hidden_from_normal_lists"] = True
                    ometa["merge_reason"] = "same_stage02_character_name"
                    other["metadata"] = ometa
                    changed = True

        if changed:
            self.production._save(graph)
        return result, changed

    def _read_payload(self, project_id: str, asset: dict[str, Any]) -> dict[str, Any]:
        try:
            raw = self.production.read_text_asset(project_id, _clean(asset.get("asset_id")), max_chars=30000)
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _archive_asset(self, graph: dict[str, Any], asset: dict[str, Any], reason: str) -> None:
        if not asset.get("active"):
            return
        asset["active"] = False
        asset["status"] = "archived"
        asset["dependency_state"] = "stale"
        metadata = asset.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["ownership_repair_reason"] = reason
        logical = (graph.get("logical_assets") or {}).get(_clean(asset.get("logical_key"))) or {}
        if _clean(logical.get("active_asset_id")) == _clean(asset.get("asset_id")):
            logical["active_asset_id"] = ""

    def _repair_profiles(self, project_id: str, name_to_id: dict[str, str]) -> int:
        assets = list(self.production.list_assets(project_id, active_only=True))
        repaired = 0
        for asset in assets:
            if _clean(asset.get("asset_role")) != "character_profile":
                continue
            payload = self._read_payload(project_id, asset)
            name = _profile_name(asset, payload)
            entity_id = name_to_id.get(_name_key(name), "")
            if not entity_id:
                continue
            expected_key = f"studio:authoring:{entity_id}:profile"
            current_ids = [_clean(x) for x in asset.get("entity_ids") or [] if _clean(x)]
            payload["entity_id"] = entity_id
            payload["kind"] = "character"
            payload["name"] = name
            payload["source_entity_ids"] = [entity_id]
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            if _clean(asset.get("logical_key")) == expected_key and current_ids == [entity_id]:
                continue

            self.production.create_text_asset(
                project_id,
                stage="02",
                skill=_clean(asset.get("skill")) or "xiaoduan-authoring-assets",
                logical_key=expected_key,
                asset_role="character_profile",
                name=f"角色「{name}」稳定设定",
                content=content,
                asset_type="STRUCTURED_DATA",
                extension=".json",
                source={"type": "character_profile_ownership_repair", "source_asset_id": _clean(asset.get("asset_id"))},
                parent_asset_ids=list(asset.get("parent_asset_ids") or []),
                entity_ids=[entity_id],
                metadata={**(asset.get("metadata") or {}), "canonical_entity_id": entity_id, "source_entity_ids": [entity_id], "ownership_repaired": True},
            )
            if _clean(asset.get("logical_key")) != expected_key:
                graph = self.production.get_graph(project_id)
                old = (graph.get("assets") or {}).get(_clean(asset.get("asset_id")))
                if isinstance(old, dict):
                    self._archive_asset(graph, old, "角色稳定资产曾绑定错误角色 ID")
                    self.production._save(graph)
            repaired += 1
        return repaired

    def _repair_appearances(self, project_id: str, name_to_id: dict[str, str]) -> int:
        assets = list(self.production.list_assets(project_id, active_only=True))
        repaired = 0
        for asset in assets:
            if _clean(asset.get("asset_role")) != "character_appearance":
                continue
            payload = self._read_payload(project_id, asset)
            name = _appearance_name(asset, payload)
            entity_id = name_to_id.get(_name_key(name), "")
            if not entity_id:
                continue
            appearance_id = _clean(payload.get("appearance_id") or (asset.get("metadata") or {}).get("appearance_id")) or "default"
            expected_key = f"studio:character:{entity_id}:appearance:{appearance_id}"
            current_ids = [_clean(x) for x in asset.get("entity_ids") or [] if _clean(x)]
            payload["character_entity_id"] = entity_id
            payload["character_name"] = name
            payload["appearance_id"] = appearance_id
            content = json.dumps(payload, ensure_ascii=False, indent=2)
            if _clean(asset.get("logical_key")) == expected_key and current_ids == [entity_id]:
                continue

            self.production.create_text_asset(
                project_id,
                stage="02",
                skill=_clean(asset.get("skill")) or "xiaoduan-character-appearances",
                logical_key=expected_key,
                asset_role="character_appearance",
                name=f"{name} · {_clean(payload.get('name')) or '默认造型'}",
                content=content,
                asset_type="STRUCTURED_DATA",
                extension=".json",
                source={"type": "character_appearance_ownership_repair", "source_asset_id": _clean(asset.get("asset_id"))},
                parent_asset_ids=list(asset.get("parent_asset_ids") or []),
                entity_ids=[entity_id],
                metadata={**(asset.get("metadata") or {}), "appearance_id": appearance_id, "character_name": name, "ownership_repaired": True},
            )
            if _clean(asset.get("logical_key")) != expected_key:
                graph = self.production.get_graph(project_id)
                old = (graph.get("assets") or {}).get(_clean(asset.get("asset_id")))
                if isinstance(old, dict):
                    self._archive_asset(graph, old, "角色形象版本曾绑定错误角色 ID")
                    self.production._save(graph)
            repaired += 1
        return repaired

    def reconcile(self, project_id: str) -> dict[str, Any]:
        names = self._stage02_names(project_id)
        if not names:
            return {"project_id": project_id, "changed": False, "character_names": [], "model_calls": 0}
        name_to_id, entity_changed = self._ensure_entities(project_id, names)
        profile_count = self._repair_profiles(project_id, name_to_id)
        appearance_count = self._repair_appearances(project_id, name_to_id)
        return {
            "project_id": project_id,
            "changed": bool(entity_changed or profile_count or appearance_count),
            "character_names": names,
            "character_entity_ids": [name_to_id[_name_key(name)] for name in names if _name_key(name) in name_to_id],
            "profiles_repaired": profile_count,
            "appearances_repaired": appearance_count,
            "model_calls": 0,
        }


__all__ = ["CharacterAssetOwnershipRepair"]
