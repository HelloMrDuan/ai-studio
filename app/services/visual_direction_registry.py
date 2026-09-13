from __future__ import annotations

from typing import Any

from app.services.project_visual_context import create_visual_direction_context


class VisualDirectionRegistry:
    """Bridge for project-level visual direction.

    Keeps visual rules independent from character names or asset names.
    Story bible analysis can populate the returned context later.
    """

    def __init__(self, production=None):
        self.production = production

    def get(self, project_id: str) -> dict[str, Any]:
        if self.production is None:
            raise ValueError("VisualDirectionRegistry requires ProductionAssetService")
        return self.production.get_visual_direction(project_id)

    def ensure(self, project_id: str) -> dict[str, Any]:
        return self.get(project_id)

    def merge(self, project_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        value = self.merge_analysis(self.get(project_id), analysis)
        self.production.set_visual_direction(project_id, value)
        return value

    def create_pending(self, project_id: str) -> dict[str, Any]:
        context = create_visual_direction_context()
        context["project_id"] = project_id
        return context

    def merge_analysis(
        self,
        context: dict[str, Any],
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(context)
        for key in (
            "world_style",
            "culture",
            "era",
            "art_style",
            "character_rules",
            "environment_rules",
            "prop_rules",
            "negative_constraints",
        ):
            if key in analysis:
                result[key] = analysis[key]
        result["status"] = "ready"
        result["source"] = "story_bible_analysis"
        return result
