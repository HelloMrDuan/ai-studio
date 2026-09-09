from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VisualDirection:
    """Project level visual rules consumed by media generation.

    This is intentionally project driven. It does not infer rules from
    character names or asset names. Story bible analysis can populate this
    object and all downstream generators consume the same contract.
    """

    world_style: str = ""
    culture: str = ""
    era: str = ""
    art_style: str = ""
    character_rules: dict[str, Any] = field(default_factory=dict)
    environment_rules: dict[str, Any] = field(default_factory=dict)
    prop_rules: dict[str, Any] = field(default_factory=dict)
    negative_constraints: list[str] = field(default_factory=list)

    def compile_context(self) -> str:
        parts = [
            self.world_style,
            self.culture,
            self.era,
            self.art_style,
        ]
        rules = []
        for group in (
            self.character_rules,
            self.environment_rules,
            self.prop_rules,
        ):
            rules.extend(str(value) for value in group.values() if value)
        return ", ".join(item for item in [*parts, *rules] if item)

    def compile_negative_prompt(self) -> str:
        return ", ".join(item for item in self.negative_constraints if item)


class VisualDirectionCompiler:
    """Single entry used before image/video providers.

    Asset text must not bypass this layer.
    """

    def compile(
        self,
        asset_description: str,
        direction: VisualDirection,
        contract_context: str = "",
    ) -> dict[str, str]:
        positive = ", ".join(
            part
            for part in (
                direction.compile_context(),
                asset_description,
                contract_context,
            )
            if part
        )
        return {
            "positive_prompt": positive,
            "negative_prompt": direction.compile_negative_prompt(),
        }
