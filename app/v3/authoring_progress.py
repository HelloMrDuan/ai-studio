from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.v3.stage_progress import StageProgressTracker, create_stage_progress_router


def create_authoring_progress_tracker(settings: Any, director: Any) -> Any:
    tracker = StageProgressTracker(settings, director)
    tracker.install()
    return tracker


def create_authoring_progress_router(tracker: Any) -> APIRouter:
    return create_stage_progress_router(tracker)


__all__ = ["create_authoring_progress_tracker", "create_authoring_progress_router"]
