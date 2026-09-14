from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _has(value: Any, *tokens: str) -> bool:
    text = _norm(value)
    return any(token.lower() in text for token in tokens)


@dataclass(frozen=True)
class VisualDirection:
    """Project level visual rules consumed by media generation.

    Rules are derived only from project visual-direction fields. Character names
    and asset names never participate in culture/style inference.
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
        world = self.world_style or "未指定"
        culture = self.culture or "未指定"
        era = self.era or "未指定"
        art = self.art_style or "未指定，遵循已确认视觉锚点"

        # Keep bilingual semantic anchors because local image text encoders may
        # respond more strongly to either Chinese or English vocabulary.
        if _has(self.world_style, "xianxia", "仙侠"):
            world = "东方仙侠 xianxia"
        if _has(self.culture, "chinese", "china", "中国", "中华"):
            culture = "中国东方文化 chinese"
        elif _has(self.culture, "东亚", "east asian"):
            culture = "东亚文化 East Asian"
        if _has(self.era, "ancient", "古代", "古风"):
            era = "古代 ancient"

        parts = [
            f"世界观: {world}",
            f"文化背景: {culture}",
            f"时代: {era}",
            f"美术风格: {art}",
        ]

        # Semantic vocabulary is additive and follows explicit project fields.
        if _has(self.world_style, "xianxia", "仙侠"):
            parts.append("东方仙侠 xianxia cultivation aesthetic, eastern fantasy visual language")
        if _has(self.culture, "chinese", "china", "中国", "中华", "东亚", "east asian"):
            parts.append("East Asian facial identity, Chinese visual identity")
        if (
            _has(self.era, "ancient", "古代", "古风")
            and _has(self.culture, "chinese", "china", "中国", "中华", "东亚", "east asian")
        ):
            parts.append("ancient Chinese costume language, traditional Chinese robe structure, traditional Chinese hairstyle")

        rules: list[str] = []
        for group in (self.character_rules, self.environment_rules, self.prop_rules):
            rules.extend(str(value) for value in group.values() if value)
        return ", ".join(dict.fromkeys(item for item in [*parts, *rules] if item))

    def compile_negative_prompt(self) -> str:
        constraints = list(self.negative_constraints)
        culture_is_east_asian = _has(
            self.culture,
            "chinese", "china", "中国", "中华", "东亚", "east asian",
        )
        ancient = _has(self.era, "ancient", "古代", "古风")
        xianxia = _has(self.world_style, "xianxia", "仙侠")

        if culture_is_east_asian:
            constraints.extend(["western face", "european features", "european facial features"])
        if ancient:
            constraints.extend([
                "modern hairstyle",
                "modern clothing",
                "contemporary fashion",
                "tank top",
                "t-shirt",
                "shorts",
                "sneakers",
                "modern high heels",
            ])
        if xianxia:
            constraints.extend([
                "western fantasy knight armor",
                "european medieval costume",
                "western fantasy character design",
            ])
        return ", ".join(dict.fromkeys(item for item in constraints if item))


class VisualDirectionCompiler:
    """Single entry used before image/video providers."""

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
