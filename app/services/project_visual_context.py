from __future__ import annotations

from typing import Any


DEFAULT_VISUAL_DIRECTION = {
    "status": "pending",
    "source": "story_bible_analysis",
    "world_style": "",
    "culture": "",
    "era": "",
    "art_style": "",
    "character_rules": {},
    "environment_rules": {},
    "prop_rules": {},
    "negative_constraints": [],
}


def create_visual_direction_context() -> dict[str, Any]:
    """Create a project-scoped visual rule container.

    The values are intentionally empty. They must be filled from story bible
    analysis instead of hardcoded genre checks.
    """
    return {
        key: value.copy() if isinstance(value, dict) else list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_VISUAL_DIRECTION.items()
    }


def merge_visual_direction(
    base: dict[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    result = create_visual_direction_context()
    result.update(base or {})
    result.update(patch or {})
    return result
