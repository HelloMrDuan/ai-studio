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
    """Install a writer-boundary guard on StoryContinuityService._merge_chunk."""
    if getattr(StoryContinuityService, "_xiaoduan_typed_entity_authority_installed", False):
        return {
            "status": "already_installed",
            "policy": "continuity_references_typed_entities_v1",
        }

    original_merge_chunk = StoryContinuityService._merge_chunk

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

    StoryContinuityService._merge_chunk = guarded_merge_chunk
    StoryContinuityService._xiaoduan_typed_entity_authority_installed = True
    return {
        "status": "installed",
        "policy": "continuity_references_typed_entities_v1",
        "continuity_can_create_reusable_entities_after_typed_stage01": False,
    }


__all__ = [
    "install_continuity_entity_authority",
    "sanitize_continuity_chunk",
]
