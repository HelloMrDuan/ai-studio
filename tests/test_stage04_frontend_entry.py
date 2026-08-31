from __future__ import annotations

import hashlib
import importlib
import inspect
from pathlib import Path
from unittest import TestCase, mock


ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / "app/static/studio.html"
INDEX = ROOT / "app/static/index.html"
RUNTIME = ROOT / "app/stage04_v238_runtime.py"
RUNTIME_SHA256 = "46177fdd2947d9478ab3bedf31811fd6239c5fe13a11fe23ff6aad3be813188a"


class Stage04FrontendEntryTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")
        cls.html = STUDIO.read_text(encoding="utf-8")

    def test_stage04_generate_button_uses_rebuild_route_not_run_stage(self) -> None:
        stage04_branch = self.html.split(
            "if(p.status==='active'&&stage==='04'){", 1,
        )[1].split("if(p.status==='active'){", 1)[0]
        self.assertIn("btn.textContent=progress?.status==='failed'?'重试生成详细分镜':'生成详细分镜'", stage04_branch)
        self.assertIn("primaryActionMode='stage04_rebuild'", stage04_branch)
        self.assertNotIn("primaryActionMode='run'", stage04_branch)

        primary = self.html.split(
            "async function performPrimaryAction(){", 1,
        )[1].split("function renderNextAction(){", 1)[0]
        self.assertIn(
            "if(primaryActionMode==='stage04_rebuild'){await rebuildStage04Production();return}",
            primary,
        )
        rebuild = self.html.split(
            "async function rebuildStage04Production", 1,
        )[1].split("async function rebuildStage04Contract", 1)[0]
        self.assertIn(
            "`/api/studio/projects/${current}/stage04/rebuild-production`",
            rebuild,
        )
        self.assertNotIn("/run-stage", rebuild)

    def test_rebuild_creation_refreshes_existing_stage_progress_adapter(self) -> None:
        rebuild = self.html.split(
            "async function rebuildStage04Production", 1,
        )[1].split("async function rebuildStage04Contract", 1)[0]
        post_index = rebuild.index("stage04/rebuild-production")
        refresh_index = rebuild.index("await refreshAll()")
        self.assertLess(post_index, refresh_index)
        self.assertIn("stageProgressRow('04')", self.html)
        self.assertIn("progress?.source==='stage04_rebuild_task'", self.html)
        self.assertIn("progress.status==='running'", self.html)

        task = {
            "status": "queued",
            "scene_done": 0,
            "scene_total": 6,
            "message": "④严格分镜重建已排队",
            "created_at": "2026-08-31T00:00:00+00:00",
        }
        project = {
            "project_id": "a" * 24,
            "status": "active",
            "current_stage": "04",
            "completed_stages": ["01", "02", "03"],
            "stage_state": {"04": {
                "stage_ready": False,
                "skill_runtime": {"completion": {"ready": False}},
            }},
        }
        with mock.patch.object(
            self.main, "_studio_v23963_current_stage04_task", return_value=task,
        ):
            progress = self.main._studio_stage04_progress(project, None)
        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["source"], "stage04_rebuild_task")
        self.assertGreater(progress["percent"], 0)
        self.assertEqual(progress["total_steps"], 6)

    def test_ready_stage04_button_keeps_deterministic_finalize_action(self) -> None:
        stage04_branch = self.html.split(
            "if(p.status==='active'&&stage==='04'){", 1,
        )[1].split("if(p.status==='active'){", 1)[0]
        self.assertIn("btn.textContent='确认并进入制作'", stage04_branch)
        self.assertIn("primaryActionMode='approve'", stage04_branch)
        self.assertIn(
            "stage==='04'&&state.studio_stage04_pipeline?.ready===true&&state.studio_stage04_pipeline?.coverage_ok===true",
            self.html,
        )

        primary = self.html.split(
            "async function performPrimaryAction(){", 1,
        )[1].split("function renderNextAction(){", 1)[0]
        self.assertIn(
            "if(primaryActionMode==='approve'){const el=$('phaseInput');if(el)el.value='通过';await runPhase();return}",
            primary,
        )
        worker = inspect.getsource(self.main._studio_run_stage_job)
        self.assertIn("await _studio_stage04_finalize(project_id, job)", worker)
        finalize = inspect.getsource(self.main._studio_stage04_finalize)
        for forbidden in (
            "_studio_v2371_rebuild_stage04",
            "_studio_stage04_scene_shots",
            "_studio_stage04_replace_formal_shots",
            "_studio_stage04_generate_detailed",
            "director.message",
        ):
            self.assertNotIn(forbidden, finalize)

    def test_frontend_copies_and_current_stage04_runtime_are_consistent(self) -> None:
        self.assertEqual(INDEX.read_bytes(), STUDIO.read_bytes())
        self.assertEqual(hashlib.sha256(RUNTIME.read_bytes()).hexdigest(), RUNTIME_SHA256)


if __name__ == "__main__":
    import unittest

    unittest.main()
