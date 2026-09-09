from __future__ import annotations

from typing import Any

from .stage_asset_materialization import (
    StageOutputAssetMaterializer,
    _RETIRABLE_PROFILE_ROLES,
    _clean,
    _name_key,
    _norm_name,
)


def install_stage_asset_materialization_guard() -> None:
    """Prevent explicit asset headings from being parsed a second time.

    Older compatibility parsing accepted a bare Markdown heading as an entity
    when its body looked character-like. A heading such as ``角色资产：沈川``
    is already handled by the explicit parser and therefore must never enter the
    bare-heading path again. This guard normalizes any such duplicate row back
    to its inner canonical name and retires wrapper/field entities persisted by
    older builds. It performs no model calls.
    """
    cls = StageOutputAssetMaterializer
    if getattr(cls, "_xiaoduan_explicit_heading_guard_installed", False):
        return

    original_extract = cls._extract
    original_materialize = cls.materialize

    def extract(self: StageOutputAssetMaterializer, project_id: str, stage: str, text: str):
        rows = original_extract(self, project_id, stage, text)
        normalized: dict[tuple[str, str], dict[str, str]] = {}
        for raw in rows:
            row = dict(raw)
            kind = _clean(row.get("kind"))
            name = _norm_name(row.get("name"))
            explicit = self._explicit_kind_name(name)
            if explicit and explicit[0] == kind:
                name = _norm_name(explicit[1])
                row["name"] = name
            if not name or self._looks_like_field_heading(name):
                continue
            key = (kind, _name_key(name))
            current = normalized.get(key)
            if current is None:
                normalized[key] = row
                continue
            # Prefer the richer block while keeping one canonical entity row.
            if len(_clean(row.get("design"))) > len(_clean(current.get("design"))):
                normalized[key] = row
        return list(normalized.values())

    def materialize(self: StageOutputAssetMaterializer, project_id: str) -> dict[str, Any]:
        result = original_materialize(self, project_id)
        project = self.director.get_project(project_id)
        graph = self.production.get_graph(project_id)
        entities = graph.get("entities") or {}
        retired_ids: set[str] = set(result.get("retired_invalid_entity_ids") or [])

        for stage in ("02", "03"):
            if not self._stage_ready(project, stage):
                continue
            text = self._stage_text(project, stage)
            if not text:
                continue
            valid_rows = extract(self, project_id, stage, text)
            valid = {(row["kind"], _name_key(row["name"])) for row in valid_rows}
            for entity_id, entity in entities.items():
                if not isinstance(entity, dict):
                    continue
                kind = _clean(entity.get("entity_type")).lower()
                metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
                authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
                if not bool(authoring.get("materialized_from_stage_output")):
                    continue
                if _clean(authoring.get("source_stage")) != stage:
                    continue
                name = _norm_name(entity.get("name"))
                explicit = self._explicit_kind_name(name)
                wrapper = bool(explicit and explicit[0] == kind and _name_key(explicit[1]) != _name_key(name))
                invalid = self._looks_like_field_heading(name) or wrapper
                if not invalid or (kind, _name_key(name)) in valid:
                    continue
                entity["entity_type"] = "retired_fragment"
                metadata["retired_materialized_fragment"] = True
                metadata["retired_reason"] = "旧版标题兼容解析误识别为可复用实体"
                entity["metadata"] = metadata
                retired_ids.add(str(entity_id))

        if retired_ids:
            for asset in (graph.get("assets") or {}).values():
                if not isinstance(asset, dict) or asset.get("active") is False:
                    continue
                entity_ids = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
                if not (entity_ids & retired_ids):
                    continue
                if _clean(asset.get("asset_role")) not in _RETIRABLE_PROFILE_ROLES:
                    continue
                asset["active"] = False
                asset["status"] = "archived"
                asset["dependency_state"] = "stale"
                metadata = asset.setdefault("metadata", {})
                if isinstance(metadata, dict):
                    metadata["retired_reason"] = "上游假实体已被严格资产解析器清理"
            self.production._save(graph)

        result["retired_invalid_entity_ids"] = sorted(retired_ids)
        result["strict_explicit_heading_guard"] = True
        return result

    cls._extract = extract
    cls.materialize = materialize
    cls._xiaoduan_explicit_heading_guard_installed = True


__all__ = ["install_stage_asset_materialization_guard"]
