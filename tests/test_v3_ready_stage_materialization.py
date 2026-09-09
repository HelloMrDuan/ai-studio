from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.character_appearances import CharacterAppearanceService
from app.v3.production_authoring_assets import ProductionAuthoringAssetService


class _Director:
    def __init__(self, root: Path, project_id: str, *, stage: str, output: str) -> None:
        self.production = ProductionAssetService(root)
        self.output = output
        completed = ["01"] if stage == "02" else ["01", "02"]
        self.project = {
            "project_id": project_id,
            "title": "物化测试",
            "status": "active",
            "current_stage": stage,
            "completed_stages": completed,
            "confirmed_outputs": {
                "01": {"handoff": "沈川与苏瑶在苍梧山顶面对古剑。", "production_asset_ids": []},
            },
            "stage_state": {
                stage: {
                    "stage_ready": True,
                    "skill_runtime": {"completion": {"ready": True}},
                }
            },
        }
        self.production.ensure_project(project_id, "物化测试")

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    def list_projects(self):
        return [self.get_project(self.project["project_id"])]

    def _latest_stage_output(self, project, stage: str):
        if stage == self.project["current_stage"]:
            return self.output
        return ""

    async def confirm_stage(self, project_id: str):
        return {"ok": True}


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []

    def _wb_sync_candidates(self, project_id: str):
        return []


class ReadyStageMaterializationTests(unittest.TestCase):
    def test_stage02_ready_materializes_two_characters_profiles_and_default_looks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            output = """
# 角色生成稿

## 角色资产：沈川
- 稳定身份：17岁少年，左眉角浅伤。
- 脸部：清瘦脸型，黑色眼睛。
- 发型：黑色短发，略凌乱。
- 服装：深蓝束袖长袍，黑色长靴。
- 固定身份锚点：左眉浅伤、深蓝长袍。
- 角色参考图生成要求：干净背景，稳定身份展示。

## 角色资产：苏瑶
- 稳定身份：16岁少女。
- 脸部：清秀鹅蛋脸。
- 发型：黑色高马尾。
- 服装：红衣，腰间固定青玉坠。
- 固定身份锚点：红衣、高马尾、青玉坠。
- 角色参考图生成要求：干净背景，稳定身份展示。
"""
            director = _Director(root, project_id, stage="02", output=output)
            # Simulate an older Stage① extraction that only found 苏瑶.
            director.production.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:suyao",
                stage="01",
                metadata={"age": 16},
            )
            legacy = _Legacy(director)
            service = ProductionAuthoringAssetService(SimpleNamespace(data_dir=root), legacy)

            state = service.status(project_id)
            names = {row["name"] for row in state["items"] if row["entity_type"] == "character"}
            self.assertEqual(names, {"沈川", "苏瑶"})
            self.assertEqual(len([row for row in state["items"] if row["entity_type"] == "character"]), 2)
            self.assertTrue(all(row["profile_version"] >= 1 for row in state["items"] if row["entity_type"] == "character"))

            appearances = CharacterAppearanceService(legacy).list(project_id)["appearances"]
            defaults = [row for row in appearances if row["appearance_id"] == "default"]
            self.assertEqual(len(defaults), 2)
            self.assertEqual(len(director.production.list_entities(project_id, entity_type="character")), 2)

    def test_stage03_ready_materializes_location_and_prop_before_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            output = """
# 视觉资产稿

## 地点资产：苍梧山顶
- 空间边界：积雪断崖与废弃山门围合山顶平台。
- 布局：石阶通向山门，石台位于中轴。
- 材质：覆雪青灰岩与风化石柱。
- 固定视觉锚点：断崖、废弃山门、中央石台。
- 参考图生成要求：无人、无瞬时动作的稳定空间身份图。

## 道具资产：古剑
- 完整轮廓：长直剑身，旧式剑格。
- 材质：暗色旧钢。
- 颜色与纹样：黑色旧布缠柄，剑格云纹。
- 固定磨损：剑格边缘轻微缺口。
- 参考图生成要求：完整展示古剑结构，不出现人物握持。
"""
            director = _Director(root, project_id, stage="03", output=output)
            legacy = _Legacy(director)
            service = ProductionAuthoringAssetService(SimpleNamespace(data_dir=root), legacy)

            state = service.status(project_id)
            by_kind = {(row["entity_type"], row["name"]) for row in state["items"]}
            self.assertIn(("location", "苍梧山顶"), by_kind)
            self.assertIn(("prop", "古剑"), by_kind)
            self.assertTrue(all(row["profile_version"] >= 1 for row in state["items"]))
            self.assertTrue(state["ready_stage_assets_visible_before_confirmation"])

    def test_startup_reconcile_reuses_ready_output_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director = _Director(
                root,
                project_id,
                stage="02",
                output="## 角色资产：沈川\n年龄17。黑发。清瘦脸型。深蓝长袍。黑色长靴。固定左眉浅伤。参考图使用干净背景。",
            )
            service = ProductionAuthoringAssetService(SimpleNamespace(data_dir=root), _Legacy(director))
            result = service.reconcile_existing_projects()
            self.assertEqual(result["scanned"], 1)
            self.assertEqual(result["reconciled"], 1)
            state = service.status(project_id)
            self.assertEqual({row["name"] for row in state["items"]}, {"沈川"})
            self.assertEqual(state["model_calls_added"], 0)


if __name__ == "__main__":
    unittest.main()
