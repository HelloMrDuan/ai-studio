from __future__ import annotations

from typing import Any

from app.services.visual_direction_registry import VisualDirectionRegistry


class DirectorVisualIntegration:
    """Bridge Director projects and project-level visual direction.

    Keeps Director workflow unchanged while providing a single place for
    future story-bible analysis to populate visual rules.
    """

    def __init__(self, registry: VisualDirectionRegistry):
        self.registry = registry

    def ensure_project_visual_context(self, project_id: str) -> dict[str, Any]:
        return self.registry.ensure(project_id)

    def merge_story_analysis(
        self,
        project_id: str,
        analysis: dict[str, Any],
    ) -> dict[str, Any]:
        return self.registry.merge(
            project_id,
            {
                "source": "story_bible_analysis",
                **analysis,
            },
        )

    def build_generation_context(
        self,
        project_id: str,
        asset: dict[str, Any],
    ) -> dict[str, Any]:
        visual = self.registry.get(project_id)
        return {
            "visual_direction": visual,
            "asset_id": asset.get("asset_id", ""),
            "asset_version": asset.get("version", ""),
            "asset_role": asset.get("asset_role", ""),
        }
