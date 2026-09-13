from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from types import MethodType
from typing import Any


_CANONICAL_TYPES = {"character", "location", "prop"}
_TYPE_FAMILY = {
    "character": "character",
    "location": "location",
    "prop": "prop",
    # Continuity extraction historically used these generic labels for physical
    # story objects. If a formal prop with the same normalized name exists they
    # are aliases, not two visible story elements.
    "object": "prop",
    "item": "prop",
    "weapon": "prop",
}
_STAGE_RANK = {"": 0, "01": 1, "02": 2, "03": 3, "04": 4, "make": 5, "final": 6}
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normal_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", _clean(value)).casefold()
    return "".join(ch for ch in text if ch not in _ZERO_WIDTH and not ch.isspace())


def _copy(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except Exception:
        return fallback


def _merge_value(primary: Any, secondary: Any) -> Any:
    if isinstance(primary, dict) and isinstance(secondary, dict):
        out = _copy(primary, {})
        for key, value in secondary.items():
            if key not in out:
                out[key] = _copy(value, value)
            else:
                out[key] = _merge_value(out[key], value)
        return out
    if isinstance(primary, list) and isinstance(secondary, list):
        out = list(primary)
        seen = {json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) for item in out}
        for item in secondary:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                out.append(_copy(item, item))
        return out
    if primary in (None, "", [], {}):
        return _copy(secondary, secondary)
    return primary


def _replace_ids(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_ids(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, aliases) for item in value]
    if isinstance(value, str):
        return aliases.get(value, value)
    return value


class CanonicalEntityReconciler:
    """Collapse duplicate reusable entities without deleting historical IDs.

    Same-name character/location/prop duplicates are merged. Historical generic
    physical-object types (object/item/weapon) are also merged into an existing
    formal ``prop`` of the same normalized name. Narrative ``scene`` entities
    remain separate from reusable ``location`` identities.
    """

    def __init__(self, settings: Any, director: Any) -> None:
        self.settings = settings
        self.director = director
        self.production = director.production
        self._original_list_entities = self.production.list_entities
        self._original_create_entity = self.production.create_entity

    def _profile_count(self, graph: dict[str, Any], entity_id: str) -> int:
        count = 0
        for asset in (graph.get("assets") or {}).values():
            if entity_id not in set(asset.get("entity_ids") or []):
                continue
            if _clean(asset.get("asset_role")) in {"character_profile", "location_profile", "prop_profile"}:
                count += 1
        return count

    def _score(self, graph: dict[str, Any], entity: dict[str, Any], family: str) -> tuple[int, int, int, int, int, str]:
        entity_id = _clean(entity.get("entity_id"))
        kind = _clean(entity.get("entity_type")).lower()
        try:
            metadata_size = len(json.dumps(entity.get("metadata") or {}, ensure_ascii=False, sort_keys=True))
        except Exception:
            metadata_size = 0
        return (
            1 if kind == family else 0,
            self._profile_count(graph, entity_id),
            _STAGE_RANK.get(_clean(entity.get("stage")), 0),
            len(entity.get("asset_ids") or []),
            metadata_size + len(entity.get("evidence") or []) * 32,
            entity_id,
        )

    def reconcile(self, project_id: str) -> dict[str, Any]:
        graph = self.production.ensure_project(project_id)
        entities = graph.get("entities") or {}
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entity in entities.values():
            kind = _clean(entity.get("entity_type")).lower()
            family = _TYPE_FAMILY.get(kind)
            if not family:
                continue
            metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
            if _clean(metadata.get("canonical_entity_id")) and bool(metadata.get("merged_duplicate")):
                continue
            key = (family, _normal_name(entity.get("name")))
            if not key[1]:
                continue
            groups.setdefault(key, []).append(entity)

        aliases: dict[str, str] = {}
        merged_groups = 0
        for (family, _name), rows in groups.items():
            if len(rows) < 2:
                continue
            # Cross-type physical-object aliases are merged only when a formal
            # prop exists. A lone legacy object/item/weapon keeps its type until
            # the formal Story/Visual asset pipeline creates the prop identity.
            if family == "prop" and not any(_clean(row.get("entity_type")).lower() == "prop" for row in rows):
                continue
            rows.sort(key=lambda item: self._score(graph, item, family), reverse=True)
            canonical = rows[0]
            canonical_id = _clean(canonical.get("entity_id"))
            source_ids = [canonical_id]
            source_types = {_clean(canonical.get("entity_type")).lower()}
            for duplicate in rows[1:]:
                duplicate_id = _clean(duplicate.get("entity_id"))
                if not duplicate_id or duplicate_id == canonical_id:
                    continue
                source_types.add(_clean(duplicate.get("entity_type")).lower())
                aliases[duplicate_id] = canonical_id
                source_ids.append(duplicate_id)
                canonical["metadata"] = _merge_value(
                    canonical.get("metadata") or {}, duplicate.get("metadata") or {}
                )
                canonical["evidence"] = _merge_value(
                    canonical.get("evidence") or [], duplicate.get("evidence") or []
                )
                canonical["asset_ids"] = list(dict.fromkeys(
                    list(canonical.get("asset_ids") or []) + list(duplicate.get("asset_ids") or [])
                ))
                duplicate.setdefault("metadata", {})["canonical_entity_id"] = canonical_id
                duplicate["metadata"]["merged_duplicate"] = True
                duplicate["metadata"]["hidden_from_normal_lists"] = True
                duplicate["metadata"]["merge_reason"] = (
                    "same_prop_identity_across_legacy_object_types"
                    if len(source_types) > 1 and family == "prop"
                    else "same_type_same_normalized_name"
                )
            canonical.setdefault("metadata", {})["canonical_source_entity_ids"] = sorted(set(source_ids))
            canonical["metadata"]["canonical_source_entity_types"] = sorted(source_types)
            canonical["metadata"]["canonicalized"] = True
            merged_groups += 1

        if not aliases:
            return {"project_id": project_id, "merged_groups": 0, "aliases": {}}

        for asset in (graph.get("assets") or {}).values():
            ids = [aliases.get(_clean(item), _clean(item)) for item in asset.get("entity_ids") or [] if _clean(item)]
            asset["entity_ids"] = list(dict.fromkeys(ids))
            source = asset.get("source") if isinstance(asset.get("source"), dict) else {}
            metadata = asset.get("metadata") if isinstance(asset.get("metadata"), dict) else {}
            asset["source"] = _replace_ids(source, aliases)
            asset["metadata"] = _replace_ids(metadata, aliases)

        graph["relations"] = _replace_ids(graph.get("relations") or [], aliases)
        graph.setdefault("entity_aliases", {}).update(aliases)
        self.production._save(graph)
        return {"project_id": project_id, "merged_groups": merged_groups, "aliases": aliases}

    def reconcile_existing_projects(self) -> dict[str, int]:
        root = Path(self.settings.data_dir) / "director_production"
        projects = 0
        groups = 0
        if root.is_dir():
            for path in root.iterdir():
                if not path.is_dir() or len(path.name) != 24:
                    continue
                try:
                    result = self.reconcile(path.name)
                except Exception:
                    continue
                projects += 1
                groups += int(result.get("merged_groups") or 0)
        return {"projects": projects, "merged_groups": groups}

    def install(self) -> None:
        if getattr(self.production, "_xiaoduan_canonical_entity_reconciler_installed", False):
            return
        service = self
        original_list = self._original_list_entities
        original_create = self._original_create_entity

        def list_entities(instance: Any, project_id: str, entity_type: str = "") -> list[dict[str, Any]]:
            service.reconcile(project_id)
            rows = original_list(project_id, entity_type)
            result = []
            for row in rows:
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                if bool(metadata.get("merged_duplicate")) and _clean(metadata.get("canonical_entity_id")):
                    continue
                result.append(row)
            return result

        def create_entity(instance: Any, project_id: str, **kwargs: Any) -> dict[str, Any]:
            created = original_create(project_id, **kwargs)
            service.reconcile(project_id)
            graph = service.production.ensure_project(project_id)
            item = (graph.get("entities") or {}).get(_clean(created.get("entity_id"))) or created
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            canonical_id = _clean(metadata.get("canonical_entity_id"))
            if canonical_id:
                return (graph.get("entities") or {}).get(canonical_id) or item
            return item

        self.production.list_entities = MethodType(list_entities, self.production)
        self.production.create_entity = MethodType(create_entity, self.production)
        self.production._xiaoduan_canonical_entity_reconciler_installed = True
        self.reconcile_existing_projects()


__all__ = ["CanonicalEntityReconciler"]
