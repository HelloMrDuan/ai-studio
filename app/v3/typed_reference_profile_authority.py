from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import professional_output_runtime as runtime
from .canonical_reference_assets import CanonicalReferenceAssetBootstrap


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return "".join(_clean(value).split()).casefold()


def _typed_contract(row: dict[str, Any]) -> dict[str, Any]:
    """Convert the strict Stage02 CharacterAsset row into reusable visual facts."""
    contract = {
        "性别呈现": _clean(row.get("gender_presentation")),
        "年龄": _clean(row.get("age")),
        "脸部": _clean(row.get("face")),
        "发型": _clean(row.get("hair_style")),
        "发色": _clean(row.get("hair_color")),
        "肤色": _clean(row.get("skin_tone")),
        "体型": _clean(row.get("body_type")),
        "身高感": _clean(row.get("height_impression")),
        "服装": _clean(row.get("clothing")),
        "鞋履": _clean(row.get("footwear")),
        "固定身份锚点": [
            _clean(item) for item in row.get("fixed_identity_anchors") or [] if _clean(item)
        ],
        "允许变化项": [
            _clean(item) for item in row.get("allowed_variations") or [] if _clean(item)
        ],
        "参考图要求": _clean(row.get("reference_requirements")),
    }
    return {key: value for key, value in contract.items() if value not in ("", [], {}, None)}


def _typed_contracts(production: Any, project_id: str) -> dict[str, dict[str, Any]]:
    payload, _asset = runtime._latest_professional_output(production, project_id, "02")
    if not isinstance(payload, dict) or _clean(payload.get("output_kind")) != "character_assets":
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in payload.get("characters") or []:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("name"))
        if not name:
            continue
        contract = _typed_contract(row)
        if contract:
            result[_norm(name)] = contract
    return result


def enrich_reference_entity(entity: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Make the typed Stage02 contract available to the reference compiler.

    Formal profile assets stay useful/versioned presentation resources, but the
    strict Stage02 professional object is the semantic authority for stable
    character identity. This prevents a stale/generic profile payload from
    dropping hair or period clothing before Face Anchor compilation.
    """
    if not contract:
        return entity
    enriched = deepcopy(entity)
    metadata = deepcopy(enriched.get("metadata") if isinstance(enriched.get("metadata"), dict) else {})
    stable_profile = deepcopy(
        metadata.get("stable_profile") if isinstance(metadata.get("stable_profile"), dict) else {}
    )
    stable_profile["专业角色合同"] = deepcopy(contract)
    metadata["stable_profile"] = stable_profile
    metadata["typed_character_contract"] = deepcopy(contract)
    metadata["typed_character_contract_authority"] = "stage02_professional_output"
    enriched["metadata"] = metadata
    return enriched


def install_typed_reference_profile_authority() -> dict[str, Any]:
    """Install one semantic authority boundary before reference prompt creation."""
    current = CanonicalReferenceAssetBootstrap._build_reference_candidates
    if getattr(current, "_xiaoduan_typed_reference_profile_authority", False):
        return {
            "installed": True,
            "status": "already_installed",
            "authority": "stage02_professional_character_assets",
        }

    original_build = current
    original_appearance = CanonicalReferenceAssetBootstrap._appearance_entity

    def build_reference_candidates(
        self: CanonicalReferenceAssetBootstrap,
        project_id: str,
        profiles: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        rows = original_build(self, project_id, profiles)
        contracts = _typed_contracts(self.director.production, project_id)
        if not contracts:
            return rows
        enriched_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for entity, profile in rows:
            if _clean(entity.get("entity_type")).lower() == "character":
                contract = contracts.get(_norm(entity.get("name"))) or {}
                entity = enrich_reference_entity(entity, contract)
            enriched_rows.append((entity, profile))
        return enriched_rows

    def appearance_entity(
        self: CanonicalReferenceAssetBootstrap,
        project_id: str,
        entity: dict[str, Any],
        version: str,
    ) -> dict[str, Any]:
        result = original_appearance(self, project_id, entity, version)
        # Older implementation replaced metadata with appearance stable_design,
        # accidentally discarding the typed identity contract. Appearance may
        # override style/clothing version facts but never erase base identity.
        base_meta = deepcopy(entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {})
        result_meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        base_meta.update(deepcopy(result_meta))
        result["metadata"] = base_meta
        return result

    setattr(build_reference_candidates, "_xiaoduan_typed_reference_profile_authority", True)
    setattr(appearance_entity, "_xiaoduan_typed_reference_profile_authority", True)
    CanonicalReferenceAssetBootstrap._build_reference_candidates = build_reference_candidates
    CanonicalReferenceAssetBootstrap._appearance_entity = appearance_entity
    return {
        "installed": True,
        "status": "installed",
        "authority": "stage02_professional_character_assets",
        "formal_profile_is_not_allowed_to_drop_typed_identity": True,
        "appearance_version_preserves_typed_identity": True,
    }


__all__ = [
    "enrich_reference_entity",
    "install_typed_reference_profile_authority",
]
