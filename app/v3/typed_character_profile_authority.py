from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import professional_output_runtime as runtime


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _norm(value: Any) -> str:
    return "".join(_clean(value).split()).casefold()


def _stable_contract(package: dict[str, Any]) -> dict[str, Any]:
    """Project the typed Stage02 package into reusable identity fields only.

    The professional CharacterAsset schema already carries the facts required by
    face/costume reference generation. Older materialization kept only
    ``stable_description`` and silently discarded hair/clothing/body fields,
    which allowed modern short hair / T-shirts to appear despite the typed
    package containing period-correct facts.
    """
    return {
        "性别呈现": _clean(package.get("gender_presentation")),
        "年龄": _clean(package.get("age")),
        "脸部": _clean(package.get("face")),
        "发型": _clean(package.get("hair_style")),
        "发色": _clean(package.get("hair_color")),
        "肤色": _clean(package.get("skin_tone")),
        "体型": _clean(package.get("body_type")),
        "身高感": _clean(package.get("height_impression")),
        "服装": _clean(package.get("clothing")),
        "鞋履": _clean(package.get("footwear")),
        "固定身份锚点": [
            _clean(x) for x in package.get("fixed_identity_anchors") or [] if _clean(x)
        ],
        "允许变化项": [
            _clean(x) for x in package.get("allowed_variations") or [] if _clean(x)
        ],
        "参考图要求": _clean(package.get("reference_requirements")),
    }


def reconcile_typed_character_profiles(production: Any, project_id: str) -> dict[str, Any]:
    """Merge the full typed Stage02 identity contract into canonical entities.

    This is deterministic and adds zero model calls. It intentionally does not
    overwrite a user's manually edited ``authoring.stable_design``; it only
    restores machine-typed facts that should always have survived
    materialization.
    """
    payload, asset = runtime._latest_professional_output(production, project_id, "02")
    if not isinstance(payload, dict) or _clean(payload.get("output_kind")) != "character_assets":
        return {
            "project_id": project_id,
            "reconciled": False,
            "reason": "typed_character_assets_unavailable",
            "updated_entity_ids": [],
            "model_calls": 0,
        }

    by_name = {
        _norm(row.get("name")): row
        for row in payload.get("characters") or []
        if isinstance(row, dict) and _clean(row.get("name"))
    }
    updated: list[str] = []

    for entity in production.list_entities(project_id, "character"):
        if not isinstance(entity, dict):
            continue
        package = by_name.get(_norm(entity.get("name")))
        if package is None:
            continue

        metadata = deepcopy(entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {})
        authoring = deepcopy(metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {})
        continuity = deepcopy(metadata.get("continuity") if isinstance(metadata.get("continuity"), dict) else {})
        core_profile = deepcopy(
            continuity.get("core_profile") if isinstance(continuity.get("core_profile"), dict) else {}
        )

        contract = _stable_contract(package)
        contract = {
            key: value
            for key, value in contract.items()
            if value not in ("", [], {}, None)
        }

        before_contract = authoring.get("typed_character_contract")
        before_profile = core_profile.get("专业角色合同")
        if before_contract == contract and before_profile == contract:
            continue

        authoring["typed_character_contract"] = deepcopy(contract)
        authoring["typed_character_output_asset_id"] = _clean((asset or {}).get("asset_id"))
        authoring["typed_character_contract_version"] = "v1"
        # Keep manual stable_design untouched. The formal typed facts live in a
        # separate contract projection and are consumed by reference prompts.
        metadata["authoring"] = authoring

        core_profile["专业角色合同"] = deepcopy(contract)
        continuity["core_profile"] = core_profile
        metadata["continuity"] = continuity

        entity_id = _clean(entity.get("entity_id"))
        production.update_entity(project_id, entity_id, {"metadata": metadata})
        updated.append(entity_id)

    return {
        "project_id": project_id,
        "reconciled": True,
        "typed_character_output_asset_id": _clean((asset or {}).get("asset_id")),
        "updated_entity_ids": updated,
        "updated_count": len(updated),
        "model_calls": 0,
    }


__all__ = ["reconcile_typed_character_profiles"]
