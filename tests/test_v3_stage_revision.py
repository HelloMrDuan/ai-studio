from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.stage_revision import StageRevisionService


class _FakeDirector:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project = {
            "project_id": project_id,
            "status": "completed",
            "current_stage": "04",
            "completed_stages": ["01", "02", "03", "04"],
            "confirmed_outputs": {stage: {"stage": stage} for stage in ("01", "02", "03", "04")},
            "stage_state": {stage: {"stage_ready": True, "handoff": f"handoff-{stage}"} for stage in ("01", "02", "03", "04")},
            "history": [{"stage": "01", "role": "user", "content": "原始故事"}],
        }

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    def _save_project(self, project):
        self.project = copy.deepcopy(project)


class _FakeLegacy:
    def __init__(self, director: _FakeDirector, candidates=None) -> None:
        self.director = director
        self._candidates = candidates or []

    def _wb_load_candidates(self, project_id: str):
        return copy.deepcopy(self._candidates)


class StageRevisionTests(unittest.TestCase):
    def _asset(self, production, project_id, *, stage, role, key, parents=None):
        return production.create_text_asset(
            project_id,
            stage=stage,
            skill="test",
            logical_key=key,
            asset_role=role,
            name=key,
            content=key,
            parent_asset_ids=parents or [],
        )

    def test_reopen_stage_preserves_history_and_stales_downstream_versions(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director = _FakeDirector(root, project_id)
            p = director.production
            source = self._asset(p, project_id, stage="01", role="source_brief", key="source")
            script = self._asset(p, project_id, stage="01", role="screenplay", key="script")
            character = self._asset(p, project_id, stage="02", role="character_bible", key="character", parents=[script["asset_id"]])
            visual = self._asset(p, project_id, stage="03", role="visual_design", key="visual", parents=[character["asset_id"]])
            storyboard = self._asset(p, project_id, stage="04", role="storyboard_master", key="storyboard", parents=[visual["asset_id"]])
            image = self._asset(p, project_id, stage="make", role="shot_keyframe", key="image", parents=[storyboard["asset_id"]])
            final = self._asset(p, project_id, stage="final", role="final_cut", key="final", parents=[image["asset_id"]])

            service = StageRevisionService(SimpleNamespace(data_dir=root), _FakeLegacy(director))
            result = service.reopen(project_id, "02", reason="修改角色服装")

            self.assertTrue(result["history_preserved"])
            self.assertTrue(result["old_asset_versions_preserved"])
            self.assertEqual(director.project["current_stage"], "02")
            self.assertEqual(director.project["status"], "active")
            self.assertEqual(director.project["completed_stages"], ["01"])
            self.assertEqual(set(director.project["confirmed_outputs"]), {"01"})
            self.assertEqual(director.project["history"][0]["content"], "原始故事")

            self.assertEqual(p.get_asset(project_id, source["asset_id"])["dependency_state"], "current")
            self.assertEqual(p.get_asset(project_id, script["asset_id"])["dependency_state"], "current")
            for asset in (character, visual, storyboard, image, final):
                current = p.get_asset(project_id, asset["asset_id"])
                self.assertEqual(current["dependency_state"], "stale")
                self.assertTrue(current["active"])

    def test_reopen_stage_is_blocked_while_candidate_is_running(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director = _FakeDirector(root, project_id)
            legacy = _FakeLegacy(
                director,
                candidates=[{"candidate_id": "cand-1", "status": "running", "confirmed_asset_id": ""}],
            )
            service = StageRevisionService(SimpleNamespace(data_dir=root), legacy)
            with self.assertRaisesRegex(RuntimeError, "生成任务运行中"):
                service.reopen(project_id, "03")

    def test_reopen_stage_rejects_unfinished_earlier_stage(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director = _FakeDirector(root, project_id)
            director.project["completed_stages"] = ["01"]
            director.project["current_stage"] = "02"
            director.project["status"] = "active"
            service = StageRevisionService(SimpleNamespace(data_dir=root), _FakeLegacy(director))
            with self.assertRaisesRegex(ValueError, "尚未完成"):
                service.reopen(project_id, "03")


if __name__ == "__main__":
    unittest.main()
