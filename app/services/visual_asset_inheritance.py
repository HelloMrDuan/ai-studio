from __future__ import annotations

from typing import Any

from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.reference_templates import get_reference_template
from app.services.visual_direction import VisualDirection


class VisualAssetInheritance:
    """Bridge project visual rules into existing production asset graph.

    This adapter keeps ProductionAssetService unchanged while making every
    generated asset inherit project-level visual constraints.
    """

    def build_contract(
        self,
        *,
        asset: dict[str, Any],
        direction: VisualDirection,
    ) -> GenerationContract:
        return GenerationContract(
            asset_id=str(asset.get("asset_id") or ""),
            asset_version=str(asset.get("version") or ""),
            visual_direction=direction,
        )

    def compile_asset_prompt(
        self,
        *,
        asset: dict[str, Any],
        description: str,
        direction: VisualDirection,
    ) -> dict[str, str]:
        template = get_reference_template(
            str(asset.get("asset_role") or "generic")
        )
        contract = self.build_contract(asset=asset, direction=direction)
        return PromptCompiler().compile(
            asset_description=description,
            template=template,
            contract=contract,
        )
