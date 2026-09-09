from __future__ import annotations

from typing import Any

from app.services.project_visual_context import create_visual_direction_context


class VisualDirectionRegistry:
    """Bridge for project-level visual direction.

    Keeps visual rules independent from character names or asset names.
    Story bible analysis can populate the returned context later.
    """

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
