from __future__ import annotations

import importlib
from pathlib import Path
from unittest import TestCase, mock


ROOT = Path(__file__).resolve().parents[1]
STAGE_IDS = ["01", "02", "03", "04", "05", "06"]
REQUIRED_FIELDS = {
    "stage_id", "stage_name", "status", "current_step", "total_steps",
    "percent", "completed_items", "current_item", "eta_seconds", "source",
}


class StageProgressDisplayTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")

    @staticmethod
    def project(stage: str = "02") -> dict:
        stage_state = {
            stage_id: {
                "stage_ready": False,
                "skill_runtime": {
                    "completion": {
                        "ready": False,
                        "required_artifact_ids": [],
                        "missing_artifact_ids": [],
                        "active_requirement_ids": [],
                        "missing_requirement_ids": [],
                    }
                },
            }
            for stage_id in ("01", "02", "03", "04")
        }
        return {
            "project_id": "a" * 24,
            "status": "active",
            "current_stage": stage,
            "completed_stages": [x for x in ("01", "02", "03") if x < stage],
            "confirmed_outputs": {},
            "stage_state": stage_state,
        }

    def test_snapshot_has_uniform_stage_progress_for_all_six_stages(self) -> None:
        job = {
            "stage": "02", "status": "running", "turn_count": 1,
            "message": "正在生成角色", "created_at": "2026-08-24T00:00:00+00:00",
        }
        with mock.patch.object(
            self.main.story_continuity, "load", return_value={"shots": []},
        ), mock.patch.object(
            self.main, "_studio_v23963_current_stage04_task", return_value={},
        ):
            result = self.main._studio_stage_progress_snapshot(
                self.project("02"), job, [], [],
            )
        self.assertEqual(result["schema_version"], "stage-progress-v1")
        self.assertEqual(result["current_stage"], "02")
        self.assertEqual([x["stage_id"] for x in result["stages"]], STAGE_IDS)
        self.assertTrue(all(set(x) == REQUIRED_FIELDS for x in result["stages"]))
        self.assertEqual(result["stages"][1]["current_item"], "正在生成角色")

    def test_stage04_completed_rebuild_is_ready_until_stage_confirmation(self) -> None:
        task = {
            "status": "completed", "scene_done": 4, "scene_total": 4,
            "message": "严格分镜重建完成", "created_at": "2026-08-24T00:00:00+00:00",
        }
        with mock.patch.object(
            self.main.story_continuity, "load", return_value={"shots": []},
        ), mock.patch.object(
            self.main, "_studio_v23963_current_stage04_task", return_value=task,
        ):
            result = self.main._studio_stage_progress_snapshot(
                self.project("04"), None, [], [],
            )
        stage04 = result["stages"][3]
        self.assertEqual(stage04["status"], "ready")
        self.assertEqual(stage04["percent"], 100)
        self.assertIn("等待确认", stage04["current_item"])

    def test_stage05_and_stage06_map_assets_without_changing_project_state(self) -> None:
        shots = [{"shot_id": "s1", "global_order": 1}]
        assets = [
            {
                "asset_id": "v1", "asset_role": "shot_clip", "status": "ready",
                "active": True, "dependency_state": "current", "metadata": {"shot_id": "s1"},
            },
            {
                "asset_id": "f1", "asset_role": "final_cut", "status": "ready",
                "active": True, "dependency_state": "current", "name": "最终成片",
            },
        ]
        project = self.project("04")
        project["status"] = "completed"
        project["completed_stages"] = ["01", "02", "03", "04"]
        before = repr(project)
        with mock.patch.object(
            self.main.story_continuity, "load", return_value={"shots": shots},
        ), mock.patch.object(
            self.main, "_studio_v23963_current_stage04_task", return_value={},
        ):
            result = self.main._studio_stage_progress_snapshot(project, None, assets, [])
        self.assertEqual(result["stages"][4]["percent"], 100)
        self.assertEqual(result["stages"][5]["percent"], 100)
        self.assertEqual(result["current_stage"], "06")
        self.assertEqual(repr(project), before)

    def test_frontend_renders_required_current_stage_fields(self) -> None:
        index = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
        studio = (ROOT / "app/static/studio.html").read_text(encoding="utf-8")
        self.assertEqual(index, studio)
        for label in (
            "当前步骤", "完成百分比", "已完成列表", "当前执行项", "预计剩余时间",
        ):
            self.assertIn(label, index)
        self.assertIn("formatStageEta", index)
        self.assertIn("return'处理中'", index)

    def test_fallback_keeps_six_stage_display_available(self) -> None:
        result = self.main._studio_stage_progress_fallback(self.project("03"))
        self.assertEqual(result["current_stage"], "03")
        self.assertEqual([x["stage_id"] for x in result["stages"]], STAGE_IDS)
        self.assertEqual(result["stages"][2]["current_item"], "处理中")
