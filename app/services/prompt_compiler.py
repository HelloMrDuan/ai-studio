from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection, VisualDirectionCompiler


@dataclass(frozen=True)
class CompiledPrompt:
    positive_prompt: str
    negative_prompt: str

    def __getitem__(self, key: str) -> str:
        return getattr(self, key)


class PromptCompiler:
    """Unified media prompt compilation entry.

    All media providers should receive compiled prompts instead of raw asset
    descriptions. Project visual direction and asset identity constraints are
    merged before provider execution.
    """

    def __init__(self) -> None:
        self.visual_compiler = VisualDirectionCompiler()

    def compile(
        self,
        *,
        asset_kind: str,
        asset_description: str,
        visual_direction: VisualDirection,
        contract_context: str = "",
        provider_negative: str = "",
        contract: Any = None,
        reference: bool = True,
    ) -> CompiledPrompt:
        template = get_reference_template(asset_kind) if reference else None
        if contract is not None:
            contract.validate()
            contract_context = "\n".join(filter(None, [
                contract_context, f"Asset {contract.asset_id} version {contract.asset_version}",
                f"身份引用: {contract.entity_ids}; 形象版本: {contract.character_appearances}",
                "固定视觉锚点: " + (contract.identity_anchors or "遵循已确认设定，不添加未确认外观"),
            ]))

        compiled = self.visual_compiler.compile(
            asset_description=asset_description,
            direction=visual_direction,
            contract_context=contract_context,
        )

        positive = ", ".join(
            part
            for part in (
                template.positive if template else "shot production, preserve established identities and visual anchors",
                compiled["positive_prompt"],
            )
            if part
        )

        negative = ", ".join(
            part
            for part in (
                template.negative if template else "identity change, inconsistent visual anchors",
                compiled["negative_prompt"],
                provider_negative,
            )
            if part
        )

        return CompiledPrompt(
            positive_prompt=positive,
            negative_prompt=negative,
        )
