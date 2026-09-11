from __future__ import annotations

from types import MethodType
from typing import Any

from . import professional_output_runtime as runtime
from .project_source_snapshot import _snapshot_payload
from .typed_front_half_authority import normalize_story_bible_characters


_CANONICAL_TYPES = ("character", "location", "prop")
_RETIRABLE_ASSET_ROLES = {
    "character_profile",
    "character_appearance",
    "character_reference",
    "character_turnaround",
    "character_consistency",
    "location_profile",
    "location_reference",
    "scene_reference",
    "prop_profile",
    "prop_reference",
    "item_reference",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return "".join(_clean(value).split()).casefold()


class TypedEntityGraphAuthority:
    """Keep the reusable Entity graph exactly aligned with typed Story Bible.

    Wao's asset-development contract has one machine authority for reusable
    character/location/prop identities. Once this project has a typed
    ``professional_story_bible`` resource, legacy provenance no longer grants
    an entity the right to stay visible. This is deliberately stricter than the
    old cleanup code: an untagged legacy fragment such as ``手中`` is retired
    even if it was created before the typed runtime existed.

    Markdown/continuity/control extraction remains readable historical data, but
    it is not a second writer of the reusable entity registry.
    """

    def __init__(self, settings: Any, director: Any) -> None:
        self.settings = settings
        self.director = director
        self.production = director.production
        self._original_list_entities = self.production.list_entities

    def _authority(self, project_id: str) -> tuple[dict[str, set[str]], dict[str, Any]] | None:
        payload, asset = runtime._latest_professional_output(
            self.production,
            project_id,
            "01",
        )
        if not isinstance(payload, dict) or _clean(payload.get("output_kind")) != "story_bible":
            return None

        snapshot = _snapshot_payload(self.production, project_id)
        source_text = _clean((snapshot or {}).get("text"))
        normalized, normalization = normalize_story_bible_characters(payload, source_text)
        valid = {
            "character": {
                _norm(row.get("name"))
                for row in normalized.get("characters") or []
                if isinstance(row, dict) and _clean(row.get("name"))
            },
            "location": {
                _norm(row.get("name"))
                for row in normalized.get("locations") or []
                if isinstance(row, dict) and _clean(row.get("name"))
            },
            "prop": {
                _norm(row.get("name"))
                for row in normalized.get("props") or []
                if isinstance(row, dict) and _clean(row.get("name"))
            },
        }
        return valid, {
            "professional_asset_id": _clean((asset or {}).get("asset_id")),
            "normalization": normalization,
        }

    def reconcile(self, project_id: str) -> dict[str, Any]:
        authority = self._authority(project_id)
        if authority is None:
            return {
                "project_id": project_id,
                "reconciled": False,
                "reason": "typed_story_output_unavailable",
                "retired_entity_ids": [],
            }

        valid, meta = authority
        graph = self.production.ensure_project(project_id)
        retired: list[str] = []

        # The typed Story Bible is the sole registry authority. Do not preserve
        # an unsupported reusable identity merely because an older writer forgot
        # to tag it with stage/typed metadata. That exact loophole allowed the
        # production ghost character ``手中`` to survive the previous fix.
        for entity_id, entity in (graph.get("entities") or {}).items():
            if not isinstance(entity, dict):
                continue
            kind = _clean(entity.get("entity_type")).lower()
            if kind not in valid:
                continue
            name = _clean(entity.get("name"))
            if _norm(name) in valid[kind]:
                continue

            metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
            entity["entity_type"] = "retired_fragment"
            metadata["hidden_from_normal_lists"] = True
            metadata["retired_story_entity"] = True
            metadata["retired_reason"] = "typed Story Bible 不包含该可复用身份"
            metadata["retired_by_policy"] = "typed_story_graph_single_authority_v1"
            entity["metadata"] = metadata
            retired.append(_clean(entity_id))

        retired_set = {item for item in retired if item}
        if retired_set:
            for asset in (graph.get("assets") or {}).values():
                if not isinstance(asset, dict) or asset.get("active") is False:
                    continue
                bound = {_clean(item) for item in asset.get("entity_ids") or [] if _clean(item)}
                if not (bound & retired_set):
                    continue
                if _clean(asset.get("asset_role")) not in _RETIRABLE_ASSET_ROLES:
                    continue
                asset["active"] = False
                asset["status"] = "archived"
                asset["dependency_state"] = "stale"
                asset.setdefault("metadata", {})["retired_reason"] = (
                    "typed Story Bible 已退休对应的非规范实体"
                )
            self.production._save(graph)

        visible = {
            kind: sorted(
                _clean(entity.get("name"))
                for entity in (graph.get("entities") or {}).values()
                if isinstance(entity, dict)
                and _clean(entity.get("entity_type")).lower() == kind
                and _clean(entity.get("name"))
            )
            for kind in _CANONICAL_TYPES
        }
        return {
            "project_id": project_id,
            "reconciled": True,
            "policy": "typed_story_graph_single_authority_v1",
            "retired_entity_ids": retired,
            "character_names": visible["character"],
            "location_names": visible["location"],
            "prop_names": visible["prop"],
            **meta,
            "model_calls": 0,
        }

    def reconcile_existing_projects(self) -> dict[str, int]:
        scanned = 0
        changed = 0
        for project in self.director.list_projects():
            project_id = _clean(project.get("project_id"))
            if not project_id:
                continue
            scanned += 1
            try:
                result = self.reconcile(project_id)
            except Exception:
                continue
            if result.get("retired_entity_ids"):
                changed += 1
        return {"scanned": scanned, "changed": changed}

    def install(self) -> dict[str, Any]:
        if getattr(self.production, "_xiaoduan_typed_entity_graph_authority_installed", False):
            return {
                "status": "already_installed",
                "policy": "typed_story_graph_single_authority_v1",
            }

        service = self
        original_list = self._original_list_entities

        def list_entities(instance: Any, project_id: str, entity_type: str = "") -> list[dict[str, Any]]:
            service.reconcile(project_id)
            return original_list(project_id, entity_type)

        self.production.list_entities = MethodType(list_entities, self.production)
        self.production._xiaoduan_typed_entity_graph_authority_installed = True
        startup = self.reconcile_existing_projects()
        return {
            "status": "installed",
            "policy": "typed_story_graph_single_authority_v1",
            "typed_story_is_sole_reusable_entity_authority": True,
            "legacy_entity_provenance_does_not_bypass_authority": True,
            "startup_reconciliation": startup,
        }


__all__ = ["TypedEntityGraphAuthority"]
