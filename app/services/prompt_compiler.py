from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection, VisualDirectionCompiler


@dataclass(frozen=True)
class CompiledPrompt:
    positive_prompt: str
    negative_prompt: str


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
    ) -> CompiledPrompt:
        template = get_reference_template(asset_kind)

        compiled = self.visual_compiler.compile(
            asset_description=asset_description,
            direction=visual_direction,
            contract_context=contract_context,
        )

        positive = ", ".join(
            part
            for part in (
                template.positive,
                compiled["positive_prompt"],
            )
            if part
        )

        negative = ", ".join(
            part
            for part in (
                template.negative,
                compiled["negative_prompt"],
                provider_negative,
            )
            if part
        )

        return CompiledPrompt(
            positive_prompt=positive,
            negative_prompt=negative,
        )
