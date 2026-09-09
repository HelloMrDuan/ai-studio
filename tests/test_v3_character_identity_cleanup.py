from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.character_appearances import CharacterAppearanceService
from app.v3.character_identity_cleanup import sanitize_character_stable_design
from app.v3.production_authoring_assets import ProductionAuthoringAssetService


class _Director:
    def __init__(self, root: Path, project_id: str, output: str) -> None:
        self.production = ProductionAssetService(root)
        self.output = output
        self.project = {
            "project_id": project_id,
            "title": "身份清洗测试",
            "status": "active",
            "current_stage": "02",
            "completed_stages": ["01"],
            "confirmed_outputs": {"01": {"handoff": "沈川登上苍梧山顶。", "production_asset_ids": []}},
            "stage_state": {"02": {"stage_ready": True, "skill_runtime": {"completion": {"ready": True}}}},
        }
        self.production.ensure_project(project_id, self.project["title"])

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    def list_projects(self):
        return [self.get_project(self.project["project_id"])]

    def _latest_stage_output(self, project, stage: str):
        return self.output if stage == "02" else ""

    async def confirm_stage(self, project_id: str):
        return {"ok": True}


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []

    def _wb_sync_candidates(self, project_id: str):
        return []


class CharacterIdentityCleanupTests(unittest.TestCase):
    def test_sanitizer_removes_reference_background_but_keeps_identity(self) -> None:
        source = """沈川
- 稳定身份：17岁男性，深蓝色束袖长袍，左眉旧伤
- 发型：黑色短发
- 角色参考图生成要求：深蓝色束袖长袍，背景为苍梧山顶风雪场景
- 背景：苍梧山顶
- 固定身份锚点：左眉旧伤
"""
        result = sanitize_character_stable_design(source)
        self.assertIn("深蓝色束袖长袍", result)
        self.assertIn("左眉旧伤", result)
        self.assertNotIn("参考图生成要求", result)
        self.assertNotIn("苍梧山顶", result)

    def test_ready_stage_rewrites_profile_and_default_appearance_without_scene_background(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "d" * 24
            output = """
## 角色资产：沈川
- 稳定身份：17岁男性，深蓝色束袖长袍，左眉旧伤
- 发型：黑色短发
- 固定身份锚点：左眉旧伤、深蓝长袍
- 角色参考图生成要求：深蓝色束袖长袍，背景为苍梧山顶风雪场景
"""
            director = _Director(root, project_id, output)
            legacy = _Legacy(director)
            service = ProductionAuthoringAssetService(SimpleNamespace(data_dir=root), legacy)

            state = service.status(project_id)
            row = next(item for item in state["items"] if item["name"] == "沈川")
            self.assertNotIn("参考图生成要求", row["stable_design"])
            self.assertNotIn("苍梧山顶", row["stable_design"])

            appearances = CharacterAppearanceService(legacy).list(project_id)["appearances"]
            self.assertEqual(len(appearances), 1)
            self.assertEqual(appearances[0]["character_name"], "沈川")
            self.assertEqual(appearances[0]["name"], "默认造型")
            self.assertNotIn("苍梧山顶", appearances[0]["stable_design"])


if __name__ == "__main__":
    unittest.main()
