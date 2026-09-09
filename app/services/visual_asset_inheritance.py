from __future__ import annotations

from typing import Any
from dataclasses import asdict

from app.services.generation_contract import GenerationContract
from app.services.prompt_compiler import PromptCompiler
from app.services.visual_direction import VisualDirection


class VisualAssetInheritance:
    """Bridge project visual rules into existing production asset graph.

    ProductionAssetService owns persistence; this adapter uses its metadata.
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
            prompt="",
            visual_direction=asdict(direction),
            visual_context=dict((asset.get("metadata") or {}).get("visual_context") or {}),
            entity_ids=tuple(asset.get("entity_ids") or []),
        )

    def compile_asset_prompt(
        self,
        *,
        asset: dict[str, Any],
        description: str,
        direction: VisualDirection,
    ) -> dict[str, str]:
        contract = self.build_contract(asset=asset, direction=direction)
        kind = contract.visual_context.get("asset_identity_type") or str(asset.get("asset_role") or "").split("_")[0]
        return asdict(PromptCompiler().compile(
            asset_kind=kind,
            asset_description=description,
            visual_direction=direction,
            contract=contract,
        ))
