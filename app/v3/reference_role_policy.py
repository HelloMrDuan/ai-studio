from __future__ import annotations

from typing import Any

import app.v3.generation_executor as generation_executor
from app.v3.generation_executor import ReferenceAsset


_FACE_IDENTITY_ROLES = {
    "character_face_anchor",
    "character_identity",
    "character_identity_reference",
    "face_anchor",
}
_CHARACTER_STRUCTURE_ROLES = {
    "character_costume_reference",
    "character_reference",
    "character_turnaround",
    "character_consistency",
    "character_fullbody_reference",
}


def reference_channel(asset: ReferenceAsset | dict[str, Any]) -> str:
    """Return the semantic conditioning channel for one reference asset.

    FaceID is deliberately reserved for face-identity evidence. Whole-body,
    costume and turnaround images condition structure/appearance through the
    generic image adapter instead of being mistaken for face embeddings.
    """
    if isinstance(asset, dict):
        role = str(asset.get("role") or asset.get("asset_role") or "").strip().lower()
        entity_type = str(asset.get("entity_type") or "").strip().lower()
    else:
        role = str(asset.role or "").strip().lower()
        entity_type = str(asset.entity_type or "").strip().lower()

    if role in _FACE_IDENTITY_ROLES:
        return "face_identity"
    if role in _CHARACTER_STRUCTURE_ROLES:
        return "character_structure"
    if entity_type == "location" or role in {"location_reference", "scene_reference"}:
        return "location_structure"
    if entity_type == "prop" or role in {"prop_reference", "item_reference"}:
        return "prop_structure"
    if entity_type == "character":
        # Unknown character images are never promoted to FaceID implicitly.
        # Face identity requires an explicit role produced by the package flow.
        return "character_structure"
    return "generic_structure"


def install_reference_role_policy() -> dict[str, Any]:
    current = generation_executor._is_character_reference
    if not getattr(current, "_xiaoduan_semantic_reference_roles", False):
        def is_face_identity_reference(asset: ReferenceAsset) -> bool:
            return reference_channel(asset) == "face_identity"

        setattr(is_face_identity_reference, "_xiaoduan_semantic_reference_roles", True)
        generation_executor._is_character_reference = is_face_identity_reference
    return {
        "installed": True,
        "faceid_roles": sorted(_FACE_IDENTITY_ROLES),
        "character_structure_roles": sorted(_CHARACTER_STRUCTURE_ROLES),
        "unknown_character_policy": "structure_not_faceid",
    }


__all__ = ["install_reference_role_policy", "reference_channel"]
