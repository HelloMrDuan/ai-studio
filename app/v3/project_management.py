from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException


_SAFE_PROJECT_ID = re.compile(r"[a-f0-9]{24}")


class ProjectDeletionService:
    """Delete one workbench project without deleting shared media objects.

    Project-scoped state is removed, while task/library outputs that may be
    referenced outside the project are intentionally preserved. This follows
    the same ownership rule used by reference-first asset systems: deleting a
    project relation must not silently destroy a shared physical object.
    """

    def __init__(self, settings: Any, legacy_runtime: Any) -> None:
        self.settings = settings
        self.legacy = legacy_runtime
        self.data_dir = Path(settings.data_dir)
        self.projects_dir = Path(settings.director_projects_dir)

    @staticmethod
    def _project_id(value: str) -> str:
        project_id = str(value or "").strip()
        if not _SAFE_PROJECT_ID.fullmatch(project_id):
            raise ValueError("作品编号格式不正确")
        return project_id

    @staticmethod
    def _read_project_job_files(root: Path, project_id: str) -> tuple[list[Path], set[str]]:
        paths: list[Path] = []
        job_ids: set[str] = set()
        if not root.is_dir():
            return paths, job_ids
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if str(data.get("project_id") or "").strip() != project_id:
                continue
            paths.append(path)
            job_id = str(data.get("job_id") or "").strip()
            if job_id:
                job_ids.add(job_id)
        return paths, job_ids

    async def _cancel_project_tasks(self, project_id: str, job_ids: set[str]) -> list[str]:
        cancelled: list[str] = []
        pending: list[asyncio.Task[Any]] = []
        keys = {project_id, *job_ids}

        for name, value in vars(self.legacy).items():
            if not name.endswith("_TASKS") or not isinstance(value, dict):
                continue
            for key in list(keys):
                task = value.pop(key, None)
                if task is None:
                    continue
                if isinstance(task, asyncio.Task) and not task.done():
                    task.cancel()
                    pending.append(task)
                cancelled.append(f"{name}:{key}")

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        return cancelled

    @staticmethod
    def _remove_path(path: Path, removed: list[str]) -> None:
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(str(path))
        elif path.exists():
            path.unlink()
            removed.append(str(path))

    async def delete_project(self, project_id: str) -> dict[str, Any]:
        project_id = self._project_id(project_id)

        # Validate through the existing project owner before any side effect.
        project = self.legacy.director.get_project(project_id)

        studio_job_files, studio_job_ids = self._read_project_job_files(
            self.data_dir / "studio_jobs", project_id
        )
        edit_job_files, edit_job_ids = self._read_project_job_files(
            self.data_dir / "studio_video_edit_jobs", project_id
        )
        job_ids = studio_job_ids | edit_job_ids
        cancelled = await self._cancel_project_tasks(project_id, job_ids)

        removed: list[str] = []
        for path in [*studio_job_files, *edit_job_files]:
            self._remove_path(path, removed)

        # Remove only project-owned state and project-owned materializations.
        # Shared TaskStore outputs/material-library files are deliberately kept.
        project_scoped_paths = [
            self.data_dir / "director_production" / project_id,
            self.data_dir / "story_continuity" / f"{project_id}.json",
            self.data_dir / "director_workbench_candidates" / f"{project_id}.json",
            self.data_dir / "v3" / "projects" / project_id,
            self.data_dir / "v3" / "workflow-payloads" / project_id,
            self.data_dir / "v3" / "workflow-idempotency" / project_id,
            self.data_dir / "v3" / "original-workbench-postproduction" / project_id,
        ]
        for path in project_scoped_paths:
            self._remove_path(path, removed)

        # Clear in-memory project locks only after background work is cancelled.
        locks = getattr(self.legacy.director, "_locks", None)
        if isinstance(locks, dict):
            locks.pop(project_id, None)

        project_file = self.projects_dir / f"{project_id}.json"
        if not project_file.is_file():
            raise FileNotFoundError("作品主记录不存在，未执行删除")
        self._remove_path(project_file, removed)

        return {
            "deleted": True,
            "project_id": project_id,
            "title": str(project.get("title") or ""),
            "cancelled_project_tasks": cancelled,
            "removed_project_paths": removed,
            "shared_task_outputs_preserved": True,
        }


def create_project_management_router(settings: Any, legacy_runtime: Any) -> APIRouter:
    router = APIRouter()
    service = ProjectDeletionService(settings, legacy_runtime)

    @router.delete("/api/v3/studio/projects/{project_id}")
    async def delete_studio_project(project_id: str) -> dict[str, Any]:
        try:
            return await service.delete_project(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"删除作品失败：{type(exc).__name__}: {exc}",
            ) from exc

    return router


__all__ = ["ProjectDeletionService", "create_project_management_router"]
