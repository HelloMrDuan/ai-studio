from __future__ import annotations

from typing import Any


class AssetVisualBinding:
    """Attach project visual rules to production asset metadata.

    This adapter intentionally does not replace ProductionAssetService. It only
    provides the missing inheritance boundary between project visual direction
    and generated assets.
    """

    def bind(
        self,
        asset: dict[str, Any],
        *,
        visual_direction_id: str = "",
        visual_direction: dict[str, Any] | None = None,
        appearance_version: str = "",
        generation_contract_id: str = "",
        asset_identity_type: str = "",
    ) -> dict[str, Any]:
        metadata = asset.setdefault("metadata", {})
        metadata["visual_context"] = {
            "visual_direction_id": visual_direction_id,
            "appearance_version": appearance_version,
            "asset_identity_type": asset_identity_type,
        }
        if generation_contract_id:
            asset["contract_artifact_id"] = generation_contract_id
        return asset

    def build_generation_context(self, asset: dict[str, Any]) -> dict[str, Any]:
        metadata = asset.get("metadata") or {}
        binding = metadata.get("visual_context") or metadata.get("visual_binding") or {}
        return {
            "asset_id": asset.get("asset_id", ""),
            "asset_version": asset.get("version", ""),
            "asset_role": asset.get("asset_role", ""),
            "visual_direction_id": binding.get("visual_direction_id", ""),
            "visual_direction": binding.get("visual_direction", {}),
            "generation_contract_id": asset.get("contract_artifact_id") or binding.get("generation_contract_id", ""),
            "appearance_version": binding.get("appearance_version", ""),
            "asset_identity_type": binding.get("asset_identity_type", ""),
        }
