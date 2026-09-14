from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v3.project_management import ProjectDeletionService


PROJECT_ID = "0123456789abcdef01234567"


class _FakeDirector:
    def __init__(self) -> None:
        self._locks = {PROJECT_ID: object()}

    def get_project(self, project_id: str) -> dict:
        if project_id != PROJECT_ID:
            raise FileNotFoundError("作品不存在")
        return {"project_id": project_id, "title": "测试作品"}


class ProjectDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_project_removes_project_state_but_preserves_shared_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data_dir = root / "data"
            projects_dir = root / "director-projects"
            projects_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (projects_dir / f"{PROJECT_ID}.json").write_text("{}\n", encoding="utf-8")

            project_paths = [
                data_dir / "director_production" / PROJECT_ID,
                data_dir / "v3" / "projects" / PROJECT_ID,
                data_dir / "v3" / "workflow-payloads" / PROJECT_ID,
                data_dir / "v3" / "workflow-idempotency" / PROJECT_ID,
                data_dir / "v3" / "original-workbench-postproduction" / PROJECT_ID,
            ]
            for path in project_paths:
                path.mkdir(parents=True, exist_ok=True)
                (path / "state.txt").write_text("owned", encoding="utf-8")

            continuity = data_dir / "story_continuity" / f"{PROJECT_ID}.json"
            continuity.parent.mkdir(parents=True)
            continuity.write_text("{}\n", encoding="utf-8")
            candidates = data_dir / "director_workbench_candidates" / f"{PROJECT_ID}.json"
            candidates.parent.mkdir(parents=True)
            candidates.write_text("[]\n", encoding="utf-8")

            job_root = data_dir / "studio_jobs"
            job_root.mkdir(parents=True)
            job_id = "stjob_delete_me"
            (job_root / f"{job_id}.json").write_text(
                json.dumps({"project_id": PROJECT_ID, "job_id": job_id}),
                encoding="utf-8",
            )

            shared = data_dir / "tasks" / "shared-task" / "output.mp4"
            shared.parent.mkdir(parents=True)
            shared.write_bytes(b"shared-output")

            studio_task = asyncio.create_task(asyncio.sleep(3600))
            continuity_task = asyncio.create_task(asyncio.sleep(3600))
            legacy = SimpleNamespace(
                director=_FakeDirector(),
                _STUDIO_TASKS={job_id: studio_task},
                _STUDIO_CONTINUITY_TASKS={PROJECT_ID: continuity_task},
            )
            settings = SimpleNamespace(
                data_dir=data_dir,
                director_projects_dir=projects_dir,
            )

            result = await ProjectDeletionService(settings, legacy).delete_project(PROJECT_ID)

            self.assertTrue(result["deleted"])
            self.assertTrue(result["shared_task_outputs_preserved"])
            self.assertFalse((projects_dir / f"{PROJECT_ID}.json").exists())
            self.assertFalse(continuity.exists())
            self.assertFalse(candidates.exists())
            self.assertFalse((job_root / f"{job_id}.json").exists())
            for path in project_paths:
                self.assertFalse(path.exists())
            self.assertTrue(shared.is_file())
            self.assertNotIn(PROJECT_ID, legacy.director._locks)
            self.assertTrue(studio_task.done())
            self.assertTrue(continuity_task.done())

    async def test_invalid_project_id_is_rejected_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = SimpleNamespace(
                data_dir=root / "data",
                director_projects_dir=root / "projects",
            )
            legacy = SimpleNamespace(director=_FakeDirector())
            with self.assertRaises(ValueError):
                await ProjectDeletionService(settings, legacy).delete_project("../bad")

    def test_original_workbench_exposes_delete_control(self) -> None:
        root = Path(__file__).resolve().parents[1]
        overlay = (root / "app" / "v3" / "original_workbench_overlay.py").read_text(encoding="utf-8")
        frontend = (root / "app" / "v3" / "static" / "project-delete-overlay.js").read_text(encoding="utf-8")
        entry = (root / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("project-delete-overlay.js", overlay)
        self.assertIn("method: 'DELETE'", frontend)
        self.assertIn("共享素材库和其他作品不会被删除", frontend)
        self.assertIn("create_project_management_router", entry)


if __name__ == "__main__":
    unittest.main()
