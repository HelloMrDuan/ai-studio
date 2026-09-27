from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from . import professional_output_runtime as runtime
from .canonical_reference_assets import CanonicalReferenceAssetBootstrap


logger = logging.getLogger(__name__)


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


def _contract_from_reference_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Recover the typed contract already persisted in a formal profile."""
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    direct = metadata.get("typed_character_contract")
    if isinstance(direct, dict) and direct:
        return deepcopy(direct)
    stable_profile = metadata.get("stable_profile") if isinstance(metadata.get("stable_profile"), dict) else {}
    for key in ("专业角色合同", "typed_character_contract", "character_contract"):
        value = stable_profile.get(key)
        if isinstance(value, dict) and value:
            return deepcopy(value)
    return {}


def resolve_reference_contract(
    production: Any,
    project_id: str,
    entity: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    name = _norm(entity.get("name"))
    live = _typed_contracts(production, project_id).get(name) or {}
    persisted = _contract_from_reference_entity(entity)
    if live:
        merged = deepcopy(persisted)
        merged.update(deepcopy(live))
        return merged, "stage02_professional_output"
    if persisted:
        return persisted, "canonical_profile_projection"
    return {}, "missing"


def enrich_reference_entity(
    entity: dict[str, Any],
    contract: dict[str, Any],
    *,
    authority_source: str = "stage02_professional_output",
) -> dict[str, Any]:
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
    metadata["typed_character_contract_authority"] = authority_source
    enriched["metadata"] = metadata
    return enriched


def _enrich_for_project(
    production: Any,
    project_id: str,
    entity: dict[str, Any],
) -> dict[str, Any]:
    if _clean(entity.get("entity_type")).lower() != "character":
        return entity
    contract, source = resolve_reference_contract(production, project_id, entity)
    enriched = enrich_reference_entity(entity, contract, authority_source=source)
    # Warning level is intentional: production previously ran with INFO filtered,
    # hiding the one diagnostic needed to prove which Stage02 contract reached
    # the reference boundary.
    logger.warning(
        "REFERENCE_TYPED_CONTRACT project_id=%s entity_id=%s name=%s source=%s keys=%s clothing=%s hair=%s",
        project_id,
        _clean(entity.get("entity_id")),
        _clean(entity.get("name")),
        source,
        sorted(contract.keys()),
        _clean(contract.get("服装")),
        _clean(contract.get("发型")),
    )
    return enriched


def install_typed_reference_profile_authority() -> dict[str, Any]:
    current = CanonicalReferenceAssetBootstrap._build_reference_candidates
    if getattr(current, "_xiaoduan_typed_reference_profile_authority_v2", False):
        return {
            "installed": True,
            "status": "already_installed",
            "authority": "stage02_professional_character_assets_or_profile_projection",
        }

    original_build = current
    original_entity = CanonicalReferenceAssetBootstrap._entity
    original_appearance = CanonicalReferenceAssetBootstrap._appearance_entity

    def build_reference_candidates(
        self: CanonicalReferenceAssetBootstrap,
        project_id: str,
        profiles: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        rows = original_build(self, project_id, profiles)
        return [
            (_enrich_for_project(self.director.production, project_id, entity), profile)
            for entity, profile in rows
        ]

    def entity_for_generation(
        self: CanonicalReferenceAssetBootstrap,
        project_id: str,
        entity_id: str,
    ) -> dict[str, Any]:
        entity = original_entity(self, project_id, entity_id)
        contract, source = resolve_reference_contract(self.director.production, project_id, entity)
        if _clean(entity.get("entity_type")).lower() == "character" and not contract:
            raise ValueError(
                f"角色「{_clean(entity.get('name')) or entity_id}」缺少 Stage02 严格角色合同；"
                "为避免继续生成错误锁脸图，本次请求已拒绝。请先修复角色资产事实链。"
            )
        return _enrich_for_project(self.director.production, project_id, entity)

    def appearance_entity(
        self: CanonicalReferenceAssetBootstrap,
        project_id: str,
        entity: dict[str, Any],
        version: str,
    ) -> dict[str, Any]:
        base = _enrich_for_project(self.director.production, project_id, entity)
        result = original_appearance(self, project_id, base, version)
        base_meta = deepcopy(base.get("metadata") if isinstance(base.get("metadata"), dict) else {})
        result_meta = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        base_meta.update(deepcopy(result_meta))
        result["metadata"] = base_meta
        return _enrich_for_project(self.director.production, project_id, result)

    setattr(build_reference_candidates, "_xiaoduan_typed_reference_profile_authority_v2", True)
    setattr(entity_for_generation, "_xiaoduan_typed_reference_profile_authority_v2", True)
    setattr(appearance_entity, "_xiaoduan_typed_reference_profile_authority_v2", True)
    CanonicalReferenceAssetBootstrap._build_reference_candidates = build_reference_candidates
    CanonicalReferenceAssetBootstrap._entity = entity_for_generation
    CanonicalReferenceAssetBootstrap._appearance_entity = appearance_entity
    return {
        "installed": True,
        "status": "installed",
        "authority": "stage02_professional_character_assets_or_profile_projection",
        "final_entity_read_boundary_enforced": True,
        "missing_character_contract_fails_closed": True,
        "formal_profile_is_not_allowed_to_drop_typed_identity": True,
        "appearance_version_preserves_typed_identity": True,
    }


__all__ = [
    "enrich_reference_entity",
    "resolve_reference_contract",
    "install_typed_reference_profile_authority",
]
