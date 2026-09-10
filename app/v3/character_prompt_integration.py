from __future__ import annotations

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
            return build_face_anchor_prompt(entity)

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

            constraints = compile_character_constraints(
                asset_description=str(kwargs.get("asset_description") or ""),
                identity_anchors=anchor,
                contract_context=str(kwargs.get("contract_context") or ""),
                visual_direction=direction,
                reference_phase=phase,
            )
            return CompiledPrompt(
                positive_prompt=_prepend(result.positive_prompt, constraints.positive),
                negative_prompt=_prepend(result.negative_prompt, constraints.negative),
            )

        setattr(compile_once, "_xiaoduan_unified_character_prompt", True)
        PromptCompiler.compile = compile_once

    return {
        "installed": True,
        "compiler_boundary": "single_idempotent_character_prompt_contract",
        "identity_projection": "phase_scoped",
        "face_prompt": "affirmative_identity_only",
        "costume_prompt": "body_clothing_wearables_only_no_story_props",
        "provider_prompt": "frozen_positive_negative_contract",
        "adult_bias_removed": True,
    }


__all__ = ["install_character_prompt_integration"]
