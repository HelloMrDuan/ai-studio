from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GenerationContract:
    asset_id: str
    asset_version: str
    prompt: str
    visual_direction: dict[str, object] = field(default_factory=dict)
    visual_context: dict[str, str] = field(default_factory=dict)
    entity_ids: tuple[str, ...] = ()
    character_appearances: tuple[dict[str, str], ...] = ()
    identity_anchors: str = ""

    def validate(self) -> None:
        if not self.asset_id:
            raise ValueError("asset_id is required")
        if not self.asset_version:
            raise ValueError("asset_version is required")
        if not self.visual_direction:
            raise ValueError("visual_direction is required")

    def context(self) -> str:
        self.validate()
        parts = [
            str(self.visual_direction.get("world_style", "")),
            str(self.visual_direction.get("culture", "")),
            str(self.visual_direction.get("era", "")),
            str(self.visual_direction.get("art_style", "")),
            self.prompt,
        ]
        return ", ".join(x for x in parts if x)

    def negative_prompt(self) -> str:
        self.validate()
        values = self.visual_direction.get("negative_constraints", [])
        if not isinstance(values, list):
            return ""
        return ", ".join(str(x) for x in values if x)
