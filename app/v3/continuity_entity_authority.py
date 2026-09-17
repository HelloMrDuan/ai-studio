from __future__ import annotations

import copy
from typing import Any

from app.services.story_continuity import StoryContinuityService

from . import professional_output_runtime as runtime
from .project_source_snapshot import _snapshot_payload
from .typed_front_half_authority import normalize_story_bible_characters


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return "".join(_clean(value).split()).casefold()


def _typed_authority(service: StoryContinuityService, project_id: str) -> dict[str, Any] | None:
    payload, _asset = runtime._latest_professional_output(
        service.production,
        project_id,
        "01",
    )
    if not isinstance(payload, dict) or _clean(payload.get("output_kind")) != "story_bible":
        return None

    snapshot = _snapshot_payload(service.production, project_id)
    source_text = _clean((snapshot or {}).get("text"))
    normalized, _meta = normalize_story_bible_characters(payload, source_text)

    by_name: dict[str, dict[str, str]] = {
        "character": {},
        "location": {},
        "prop": {},
    }
    for kind, field in (
        ("character", "characters"),
        ("location", "locations"),
        ("prop", "props"),
    ):
        for row in normalized.get(field) or []:
            if not isinstance(row, dict):
                continue
            name = _clean(row.get("name"))
            key = _norm(name)
            if key:
                by_name[kind][key] = name

    allowed_ids: dict[str, set[str]] = {kind: set() for kind in by_name}
    # list_entities is intentionally used here so the installed typed graph
    # authority has a chance to retire any already-persisted legacy ghost first.
    for entity in service.production.list_entities(project_id):
        if not isinstance(entity, dict):
            continue
        kind = _clean(entity.get("entity_type")).lower()
        if kind not in by_name:
            continue
        if _norm(entity.get("name")) not in by_name[kind]:
            continue
        entity_id = _clean(entity.get("entity_id"))
        if entity_id:
            allowed_ids[kind].add(entity_id)

    return {
        "by_name": by_name,
        "allowed_ids": allowed_ids,
    }


def _accept_candidate(
    data: dict[str, Any],
    *,
    kind: str,
    authority: dict[str, Any],
) -> bool:
    by_name: dict[str, str] = authority["by_name"][kind]
    allowed_ids: set[str] = authority["allowed_ids"][kind]

    resolved = _clean(data.get("_resolved_entity_id"))
    if resolved and resolved in allowed_ids:
        return True

    names = [_clean(data.get("name"))]
    aliases = data.get("aliases") if isinstance(data.get("aliases"), list) else []
    names.extend(_clean(x) for x in aliases if _clean(x))
    for candidate in names:
        canonical = by_name.get(_norm(candidate))
        if not canonical:
            continue
        # Normalize aliases/narrative mentions back onto the canonical reusable
        # identity before the continuity writer reaches _find_entity().
        data["name"] = canonical
        return True
    return False


def _canonical_aliases(
    service: StoryContinuityService,
    project_id: str,
) -> dict[str, str]:
    """Return persisted historical-id -> canonical-id mappings.

    CanonicalEntityReconciler intentionally keeps duplicate entities for audit
    history but hides them from normal ``list_entities`` results. Older
    continuity snapshots may still contain those hidden IDs. Stage04 then calls
    StoryContinuityService._entity_by_id() and used to fail before the first LLM
    call because the hidden duplicate could no longer be resolved.
    """
    try:
        graph = service.production.ensure_project(project_id)
    except Exception:
        return {}
    entities = graph.get("entities") if isinstance(graph.get("entities"), dict) else {}
    aliases: dict[str, str] = {}

    for source, target in (graph.get("entity_aliases") or {}).items():
        source_id = _clean(source)
        target_id = _clean(target)
        if source_id and target_id and source_id != target_id and target_id in entities:
            aliases[source_id] = target_id

    # Backfill projects canonicalized by builds that wrote duplicate metadata
    # before the top-level entity_aliases index existed.
    for source_id, entity in entities.items():
        if not isinstance(entity, dict):
            continue
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        target_id = _clean(metadata.get("canonical_entity_id"))
        source_id = _clean(source_id)
        if source_id and target_id and source_id != target_id and target_id in entities:
            aliases[source_id] = target_id

    def resolve(value: str) -> str:
        current = _clean(value)
        seen: set[str] = set()
        while current in aliases and current not in seen:
            seen.add(current)
            current = _clean(aliases.get(current))
        return current

    return {
        source: target
        for source in list(aliases)
        if (target := resolve(source)) and target != source and target in entities
    }


def _replace_alias_ids(value: Any, aliases: dict[str, str]) -> tuple[Any, int]:
    if isinstance(value, dict):
        changed = 0
        result: dict[str, Any] = {}
        for key, item in value.items():
            replacement, count = _replace_alias_ids(item, aliases)
            result[key] = replacement
            changed += count
        return result, changed
    if isinstance(value, list):
        changed = 0
        result = []
        for item in value:
            replacement, count = _replace_alias_ids(item, aliases)
            result.append(replacement)
            changed += count
        return result, changed
    if isinstance(value, str):
        target = aliases.get(value)
        if target and target != value:
            return target, 1
    return value, 0


def repair_continuity_entity_aliases(
    service: StoryContinuityService,
    project_id: str,
    state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair stale entity IDs in a persisted continuity snapshot without LLM calls."""
    aliases = _canonical_aliases(service, project_id)
    if not aliases:
        return state, {"repaired": False, "replacement_count": 0, "aliases": {}}

    repaired, count = _replace_alias_ids(state, aliases)
    if not isinstance(repaired, dict):
        repaired = state
        count = 0
    if count:
        analysis = repaired.setdefault("analysis", {})
        audit = analysis.setdefault("entity_alias_repair", {})
        audit["policy"] = "canonical_continuity_alias_repair_v1"
        audit["replacement_count"] = int(audit.get("replacement_count") or 0) + count
        audit["last_aliases"] = dict(aliases)
    return repaired, {
        "repaired": bool(count),
        "replacement_count": count,
        "aliases": aliases,
    }


def sanitize_continuity_chunk(
    service: StoryContinuityService,
    project_id: str,
    parsed: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Filter continuity candidates through the typed Story Bible authority.

    Continuity is allowed to add scene/beat/state facts. It is not allowed to
    become a second writer of reusable character/location/prop identities once
    Stage01 has a typed Story Bible. This mirrors wao's single-authority asset
    boundary: downstream analysis may reference canonical resources but cannot
    silently mint new reusable resources.
    """
    authority = _typed_authority(service, project_id)
    if authority is None:
        return parsed, {"typed_authority": False, "dropped": []}

    value = copy.deepcopy(parsed if isinstance(parsed, dict) else {})
    dropped: list[dict[str, str]] = []
    scenes = value.get("scenes")
    if not isinstance(scenes, list):
        return value, {"typed_authority": True, "dropped": dropped}

    for scene in scenes:
        if not isinstance(scene, dict):
            continue

        location = scene.get("location")
        if isinstance(location, dict) and _clean(location.get("name")):
            if not _accept_candidate(location, kind="location", authority=authority):
                dropped.append({"entity_type": "location", "name": _clean(location.get("name"))})
                scene["location"] = {}

        for kind, field in (("character", "characters"), ("prop", "props")):
            rows = scene.get(field)
            if not isinstance(rows, list):
                scene[field] = []
                continue
            kept: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict) or not _clean(row.get("name")):
                    continue
                if _accept_candidate(row, kind=kind, authority=authority):
                    kept.append(row)
                else:
                    dropped.append({"entity_type": kind, "name": _clean(row.get("name"))})
            scene[field] = kept

    return value, {
        "typed_authority": True,
        "dropped": dropped,
    }


def install_continuity_entity_authority() -> dict[str, Any]:
    """Install typed writer guard plus historical continuity-ID reconciliation."""
    if getattr(StoryContinuityService, "_xiaoduan_typed_entity_authority_installed", False):
        return {
            "status": "already_installed",
            "policy": "continuity_references_typed_entities_v2",
        }

    original_merge_chunk = StoryContinuityService._merge_chunk
    original_load = StoryContinuityService.load
    original_entity_by_id = StoryContinuityService._entity_by_id

    def guarded_load(self: StoryContinuityService, project_id: str) -> dict[str, Any]:
        state = original_load(self, project_id)
        repaired, audit = repair_continuity_entity_aliases(self, project_id, state)
        if audit.get("repaired"):
            # Persist once so subsequent Stage04 workspace builds no longer carry
            # historical IDs at all. ``save`` does not call ``load`` recursively.
            self.save(project_id, repaired)
        return repaired

    def guarded_entity_by_id(
        self: StoryContinuityService,
        project_id: str,
        entity_id: str,
    ) -> dict[str, Any]:
        try:
            return original_entity_by_id(self, project_id, entity_id)
        except FileNotFoundError:
            canonical_id = _canonical_aliases(self, project_id).get(_clean(entity_id), "")
            if canonical_id and canonical_id != _clean(entity_id):
                return original_entity_by_id(self, project_id, canonical_id)
            raise

    def guarded_merge_chunk(
        self: StoryContinuityService,
        project_id: str,
        state: dict[str, Any],
        parsed: dict[str, Any],
        *,
        chunk_start: int,
        chunk_end: int,
        chunk_index: int,
    ) -> None:
        sanitized, audit = sanitize_continuity_chunk(self, project_id, parsed)
        analysis = state.setdefault("analysis", {})
        guard = analysis.setdefault("typed_entity_authority", {
            "policy": "continuity_references_typed_entities_v1",
            "dropped_noncanonical": [],
        })
        for row in audit.get("dropped") or []:
            if row not in guard["dropped_noncanonical"]:
                guard["dropped_noncanonical"].append(row)
        return original_merge_chunk(
            self,
            project_id,
            state,
            sanitized,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            chunk_index=chunk_index,
        )

    StoryContinuityService.load = guarded_load
    StoryContinuityService._entity_by_id = guarded_entity_by_id
    StoryContinuityService._merge_chunk = guarded_merge_chunk
    StoryContinuityService._xiaoduan_typed_entity_authority_installed = True
    return {
        "status": "installed",
        "policy": "continuity_references_typed_entities_v2",
        "continuity_can_create_reusable_entities_after_typed_stage01": False,
        "historical_entity_aliases_repaired_before_stage04": True,
    }


__all__ = [
    "install_continuity_entity_authority",
    "repair_continuity_entity_aliases",
    "sanitize_continuity_chunk",
]
