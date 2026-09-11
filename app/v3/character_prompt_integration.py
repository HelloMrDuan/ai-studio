from __future__ import annotations

import json
import re
from typing import Any

import app.services.media_generation_pipeline as media_pipeline_module
from app.services.comfyui import STYLE_PRESETS
from app.services.prompt_compiler import CompiledPrompt, PromptCompiler, naturalize_visual_anchor
from app.services.reference_templates import REFERENCE_TEMPLATES, ReferenceTemplate
from app.services.visual_direction import VisualDirection
from app.v3.character_identity_contract import (
    build_costume_reference_prompt,
    build_face_anchor_prompt,
    compile_character_constraints,
    project_character_anchor,
)
from app.v3.character_reference_package import CharacterReferencePackageBootstrap


_PERIOD_HINT = re.compile(
    r"古代|古风|古式|古装|汉服|长袍|交领|襦裙|武侠|仙侠|"
    r"ancient|historical|period|traditional\s+(?:robe|garment)|\brobe\b",
    re.IGNORECASE,
)
_EAST_ASIAN_HINT = re.compile(
    r"中国|中华|中式|东亚|武侠|仙侠|chinese|east\s*asian",
    re.IGNORECASE,
)
_MODERN_UPPER_NEGATIVE = (
    "modern T-shirt, crew-neck T-shirt, white T-shirt, hoodie, sweatshirt, "
    "modern casual shirt, western suit, contemporary sportswear, modern fashion collar"
)
_FACE_PERIOD_POSITIVE = (
    "FACE ANCHOR VISIBLE GARMENT CONTRACT: the visible neckline and shoulders must use the confirmed "
    "historical period garment construction and period-appropriate fabric; keep the crop head-and-shoulders "
    "and do not redesign the full costume"
)


def _prepend(value: str, additions: tuple[str, ...]) -> str:
    parts = [str(item or "").strip().strip(",") for item in additions]
    tail = str(value or "").strip().strip(",")
    if tail:
        parts.append(tail)
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return ", ".join(seen)


def _entity_contract_text(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    try:
        return json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(metadata or "")


def _typed_character_contract(entity: dict[str, Any]) -> dict[str, Any]:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    stable_profile = metadata.get("stable_profile") if isinstance(metadata.get("stable_profile"), dict) else {}
    for key in ("专业角色合同", "typed_character_contract", "character_contract"):
        value = stable_profile.get(key)
        if isinstance(value, dict):
            return value
    authoring = metadata.get("authoring") if isinstance(metadata.get("authoring"), dict) else {}
    value = authoring.get("typed_character_contract")
    return value if isinstance(value, dict) else {}


def _confirmed_visible_garment(entity: dict[str, Any]) -> str:
    contract = _typed_character_contract(entity)
    for key in ("服装", "clothing", "costume", "outfit"):
        value = str(contract.get(key) or "").strip()
        if value and value not in {"未指定", "未知", "待设计"}:
            return value
    return ""


def _face_period_cue(entity: dict[str, Any]) -> str:
    """Carry only the visible period neckline into Face Anchor rendering.

    This is a render-policy cue, not a reusable identity fact. Z-Image-Turbo is
    run at CFG=1.0, so provider negatives cannot be the primary mechanism for
    excluding a modern T-shirt. The positive prompt must explicitly describe the
    small amount of period clothing that remains visible in a head-and-shoulders
    identity crop.
    """
    source = _entity_contract_text(entity)
    if not _PERIOD_HINT.search(source):
        return ""

    garment = _confirmed_visible_garment(entity)
    if garment:
        return (
            f"锁脸构图可见服装边界：领口与肩部必须来自已确认服装「{garment}」，"
            "保持其已确认颜色和古代服饰结构；画面仍只展示头肩，不展开完整服装设计。"
        )
    if _EAST_ASIAN_HINT.search(source):
        return (
            "锁脸构图可见服装边界：领口与肩部采用古代东亚传统袍服结构和历史织物质感；"
            "画面仍只展示头肩，不展开完整服装设计。"
        )
    return (
        "锁脸构图可见服装边界：领口与肩部采用与已确认历史时代一致的传统服装结构和织物；"
        "画面仍只展示头肩，不展开完整服装设计。"
    )


def _remove_legacy_adult_bias() -> None:
    portrait = STYLE_PRESETS.get("portrait_photo")
    if not isinstance(portrait, dict):
        return
    positive = str(portrait.get("positive") or "")
    portrait["positive"] = positive.replace(
        "single clearly visible adult subject when a person is requested",
        "single clearly visible person when a person is requested",
    )


def _strengthen_turnaround_template() -> None:
    current = REFERENCE_TEMPLATES.get("character")
    if current is None or "same confirmed identity in every panel" in current.positive:
        return
    REFERENCE_TEMPLATES["character"] = ReferenceTemplate(
        asset_type=current.asset_type,
        role=current.role,
        positive=(
            "same confirmed identity in every panel, same confirmed gender identity, same face, same hairstyle, same body proportions, same adopted costume, "
            + current.positive
        ),
        negative=(
            "identity drift, gender drift, face drift between panels, hairstyle drift, mixed outfits, "
            + current.negative
        ),
    )


def install_character_prompt_integration() -> dict[str, Any]:
    """Install one idempotent character prompt boundary.

    Stable identity projection, phase-specific prompt rules and provider-ready
    positive/negative prompts are applied exactly once here. Face and costume
    authoring prompts are both generated from the same stable-fact contract.
    """
    _remove_legacy_adult_bias()
    _strengthen_turnaround_template()

    if not getattr(CharacterReferencePackageBootstrap._face_prompt, "_xiaoduan_unified_character_prompt", False):
        def face_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
            prompt = build_face_anchor_prompt(entity)
            cue = _face_period_cue(entity)
            return f"{prompt}\n{cue}" if cue else prompt

        setattr(face_prompt, "_xiaoduan_unified_character_prompt", True)
        CharacterReferencePackageBootstrap._face_prompt = face_prompt

    if not getattr(CharacterReferencePackageBootstrap._costume_prompt, "_xiaoduan_unified_character_prompt", False):
        def costume_prompt(self: CharacterReferencePackageBootstrap, entity: dict[str, Any]) -> str:
            return build_costume_reference_prompt(entity)

        setattr(costume_prompt, "_xiaoduan_unified_character_prompt", True)
        CharacterReferencePackageBootstrap._costume_prompt = costume_prompt

    if not getattr(media_pipeline_module._phase_anchor_text, "_xiaoduan_unified_character_prompt", False):
        original_phase = media_pipeline_module._phase_anchor_text

        def phase_anchor(text: str, phase: str) -> str:
            current = str(phase or "").strip().lower()
            if current in {"face_anchor", "costume"}:
                return project_character_anchor(text, current)
            return original_phase(text, phase)

        setattr(phase_anchor, "_xiaoduan_unified_character_prompt", True)
        media_pipeline_module._phase_anchor_text = phase_anchor

    if not getattr(PromptCompiler.compile, "_xiaoduan_unified_character_prompt", False):
        original_compile = PromptCompiler.compile

        def compile_once(self: PromptCompiler, *args: Any, **kwargs: Any) -> CompiledPrompt:
            result = original_compile(self, *args, **kwargs)
            if str(kwargs.get("asset_kind") or "").strip().lower() != "character":
                return result

            contract = kwargs.get("contract")
            anchor = naturalize_visual_anchor(getattr(contract, "identity_anchors", "")) if contract is not None else ""
            visual_context = getattr(contract, "visual_context", {}) if contract is not None else {}
            phase = str(visual_context.get("reference_phase") if isinstance(visual_context, dict) else "").strip().lower()
            direction = kwargs.get("visual_direction")
            if not isinstance(direction, VisualDirection):
                direction = VisualDirection()

            asset_description = str(kwargs.get("asset_description") or "")
            contract_context = str(kwargs.get("contract_context") or "")
            constraints = compile_character_constraints(
                asset_description=asset_description,
                identity_anchors=anchor,
                contract_context=contract_context,
                visual_direction=direction,
                reference_phase=phase,
            )
            positive = constraints.positive
            negative = constraints.negative

            # Z-Image-Turbo runs with CFG=1.0. A negative prompt is therefore not
            # a reliable way to keep a modern T-shirt out of Face Anchor output.
            # Make the visible period neckline an affirmative provider-ready rule;
            # retain negatives as secondary metadata/compatibility for other image
            # providers that do use classifier-free negative guidance.
            if phase == "face_anchor":
                period_probe = "\n".join((asset_description, anchor, contract_context))
                if _PERIOD_HINT.search(period_probe):
                    positive = tuple(dict.fromkeys((*positive, _FACE_PERIOD_POSITIVE)))
                    negative = tuple(dict.fromkeys((*negative, _MODERN_UPPER_NEGATIVE)))

            return CompiledPrompt(
                positive_prompt=_prepend(result.positive_prompt, positive),
                negative_prompt=_prepend(result.negative_prompt, negative),
            )

        setattr(compile_once, "_xiaoduan_unified_character_prompt", True)
        PromptCompiler.compile = compile_once

    return {
        "installed": True,
        "compiler_boundary": "single_idempotent_character_prompt_contract",
        "identity_projection": "phase_scoped",
        "face_prompt": "affirmative_identity_plus_visible_period_garment_boundary",
        "costume_prompt": "body_clothing_wearables_only_no_story_props",
        "provider_prompt": "frozen_positive_negative_contract",
        "adult_bias_removed": True,
        "zimage_cfg1_period_clothing_enforced_in_positive": True,
    }


__all__ = ["install_character_prompt_integration"]
