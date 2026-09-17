from __future__ import annotations

import re
from typing import Any

from .front_half_quality_gate import parse_visual_direction_block
from .stable_asset_projection import project_stable_design
from .stage_asset_materialization import (
    StageOutputAssetMaterializer,
    _RETIRABLE_PROFILE_ROLES,
    _clean,
    _name_key,
    _norm_name,
)


def _source_anchor_text(self: StageOutputAssetMaterializer, project_id: str) -> str:
    """Return only authoritative Stage01/user text for reusable identity names."""
    project = self.director.get_project(project_id)
    confirmed = ((project.get("confirmed_outputs") or {}).get("01") or {})
    rows = [_clean(confirmed.get("handoff"))]
    for item in project.get("history") or []:
        if not isinstance(item, dict):
            continue
        if _clean(item.get("stage")) == "01" and _clean(item.get("role")) == "user":
            rows.append(_clean(item.get("content")))
    for entity in self.production.list_entities(project_id):
        if _clean(entity.get("stage")) == "01":
            rows.append(_clean(entity.get("name")))
    return "\n".join(row for row in rows if row)


def _source_anchored(self: StageOutputAssetMaterializer, project_id: str, stage: str, name: str) -> bool:
    """Stage02/03 may design an existing identity but may not invent a new one."""
    if stage not in {"02", "03"}:
        return True
    wanted = _name_key(name)
    if not wanted:
        return False
    source = _source_anchor_text(self, project_id)
    compact = re.sub(r"\s+", "", source).casefold()
    return wanted in compact


def _project_entity_identity(entity: dict[str, Any]) -> bool:
    """Persist a stable visible projection while retaining the latest raw block.

    StageOutputAssetMaterializer refreshes ``stable_design`` from the newest
    ready Stage02/03 text on every materialization. If that raw block changed,
    it must replace the previous ``source_design`` before projection; otherwise
    reopening a stage would silently snap the asset back to its old design.
    """
    kind = _clean(entity.get("entity_type")).lower()
    if kind not in {"character", "location", "prop"}:
        return False
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
    current = _clean(authoring.get("stable_design"))
    previous_source = _clean(authoring.get("source_design"))
    previous_projection = (
        project_stable_design(kind, previous_source) if previous_source else ""
    )

    # If a projection already existed and the materializer just supplied text
    # different from that projection, ``current`` is the fresh authoritative
    # Stage output. Promote it to source_design. Repeated syncs with unchanged
    # source are harmless because the same raw source is promoted again.
    if (
        previous_source
        and _clean(authoring.get("stable_projection")) == "visible_reusable_identity_v1"
        and current
        and current != previous_projection
    ):
        source_design = current
    else:
        source_design = previous_source or current
    if not source_design:
        return False

    projected = project_stable_design(kind, source_design)
    changed = (
        current != projected
        or previous_source != source_design
        or _clean(authoring.get("stable_projection")) != "visible_reusable_identity_v1"
    )
    authoring["source_design"] = source_design
    authoring["stable_design"] = projected
    authoring["stable_projection"] = "visible_reusable_identity_v1"
    metadata["authoring"] = authoring

    continuity = metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {}
    core = continuity.get("core_profile") if isinstance(continuity.get("core_profile"), dict) else {}
    if _clean(core.get("阶段正式设定")) != projected:
        changed = True
    core["阶段正式设定"] = projected
    continuity["core_profile"] = core
    metadata["continuity"] = continuity
    entity["metadata"] = metadata
    return changed


def install_stage_asset_materialization_guard() -> None:
    """Strict reusable-asset boundary for ready Stage02/03 output.

    The guard enforces three production invariants before authoring profiles are
    built: identities must exist in the real upstream story source; durable
    stable_design contains only reusable visible facts; Stage03 machine Visual
    Direction becomes the versioned project direction in the existing graph.
    """
    cls = StageOutputAssetMaterializer
    if getattr(cls, "_xiaoduan_explicit_heading_guard_installed", False):
        return

    original_extract = cls._extract
    original_materialize = cls.materialize

    def extract(self: StageOutputAssetMaterializer, project_id: str, stage: str, text: str):
        rows = original_extract(self, project_id, stage, text)
        normalized: dict[tuple[str, str], dict[str, str]] = {}
        rejected: list[dict[str, str]] = []
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
            if not _source_anchored(self, project_id, stage, name):
                rejected.append({"stage": stage, "kind": kind, "name": name})
                continue
            key = (kind, _name_key(name))
            current = normalized.get(key)
            if current is None or len(_clean(row.get("design"))) > len(_clean(current.get("design"))):
                normalized[key] = row
        self._xiaoduan_last_rejected_unanchored = rejected
        return list(normalized.values())

    def materialize(self: StageOutputAssetMaterializer, project_id: str) -> dict[str, Any]:
        result = original_materialize(self, project_id)
        project = self.director.get_project(project_id)
        graph = self.production.get_graph(project_id)
        entities = graph.get("entities") or {}
        retired_ids: set[str] = set(result.get("retired_invalid_entity_ids") or [])
        rejected_unanchored: list[dict[str, str]] = []
        projected_ids: list[str] = []
        pending_visual_direction: dict[str, Any] | None = None
        visual_direction_error = ""
        graph_changed = False

        for stage in ("02", "03"):
            if not self._stage_ready(project, stage):
                continue
            text = self._stage_text(project, stage)
            if not text:
                continue
            valid_rows = extract(self, project_id, stage, text)
            rejected_unanchored.extend(list(getattr(self, "_xiaoduan_last_rejected_unanchored", []) or []))
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
                unanchored = not _source_anchored(self, project_id, stage, name)
                invalid = self._looks_like_field_heading(name) or wrapper or unanchored
                if invalid and (kind, _name_key(name)) not in valid:
                    entity["entity_type"] = "retired_fragment"
                    metadata["retired_materialized_fragment"] = True
                    metadata["retired_reason"] = (
                        "Stage02/03 资产名称不在已确认故事事实源中"
                        if unanchored
                        else "旧版标题兼容解析误识别为可复用实体"
                    )
                    entity["metadata"] = metadata
                    retired_ids.add(str(entity_id))
                    graph_changed = True
                    continue

                if kind in {"character", "location", "prop"} and _project_entity_identity(entity):
                    projected_ids.append(str(entity_id))
                    graph_changed = True

            if stage == "03":
                try:
                    pending_visual_direction = parse_visual_direction_block(text)
                except ValueError as exc:
                    # New outputs are rejected before write by front_half_quality_gate.
                    # Old confirmed projects remain readable; no prose inference is made.
                    visual_direction_error = _clean(exc)

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
                graph_changed = True

        if graph_changed:
            self.production._save(graph)

        visual_direction_asset_id = ""
        if pending_visual_direction is not None:
            asset = self.production.set_visual_direction(project_id, pending_visual_direction)
            visual_direction_asset_id = _clean(asset.get("asset_id"))

        result["retired_invalid_entity_ids"] = sorted(retired_ids)
        result["rejected_unanchored_assets"] = rejected_unanchored
        result["stable_identity_projected_entity_ids"] = list(dict.fromkeys(projected_ids))
        result["stable_identity_projection"] = "visible_reusable_identity_v1"
        result["strict_story_identity_anchor"] = True
        result["visual_direction_asset_id"] = visual_direction_asset_id
        if visual_direction_error:
            result["visual_direction_materialization_error"] = visual_direction_error
        result["strict_explicit_heading_guard"] = True
        return result

    cls._extract = extract
    cls.materialize = materialize
    cls._xiaoduan_explicit_heading_guard_installed = True


__all__ = ["install_stage_asset_materialization_guard"]
