from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.stage_asset_materialization import StageOutputAssetMaterializer
from app.v3.stage_asset_materialization_guard import install_stage_asset_materialization_guard


install_stage_asset_materialization_guard()


_STAGE02 = """
# 角色资产包

## 沈川
- 稳定身份：17岁男性，深蓝色束袖长袍，左眉旧伤

### 发型、发色、肤色、体型、身高感
- 发型：高马尾
- 发色：黑色
- 肤色：偏白
- 体型：匀称
- 身高感：中等

### 常态服装分层、鞋履、固定配饰、主辅配色
- 服装：深蓝色束袖长袍
- 鞋履：黑色长靴
- 固定配饰：旧剑匣
- 主辅配色：深蓝、黑

## 苏瑶
- 稳定身份：16岁女性，红衣，高马尾，腰间青玉坠
- 发型：高马尾
- 发色：黑色
- 肤色：偏白
- 体型：纤细
- 服装：红衣
- 鞋履：黑靴
- 配色：红、青
- 参考图生成要求：稳定身份参考
"""


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.project = {
            "project_id": project_id,
            "title": "测试",
            "current_stage": "02",
            "completed_stages": ["01"],
            "confirmed_outputs": {"01": {"handoff": "故事已确认：沈川、苏瑶。", "production_asset_ids": []}},
            "stage_state": {
                "02": {
                    "stage_ready": True,
                    "skill_runtime": {"completion": {"ready": True}},
                }
            },
            "history": [],
        }
        self.production.ensure_project(project_id, "测试")

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return self.project

    def _latest_stage_output(self, project, stage: str) -> str:
        return _STAGE02 if stage == "02" else ""


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director


class StrictStageAssetMaterializationTests(unittest.TestCase):
    def test_field_headings_never_become_characters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "a" * 24
            director = _Director(root, project_id)
            director.production.create_entity(
                project_id,
                entity_type="character",
                name="沈川",
                logical_key="story:character:shen-chuan",
                stage="01",
            )
            director.production.create_entity(
                project_id,
                entity_type="character",
                name="苏瑶",
                logical_key="story:character:su-yao",
                stage="01",
            )
            service = StageOutputAssetMaterializer(_Legacy(director))
            rows = service._extract(project_id, "02", _STAGE02)
            names = {row["name"] for row in rows}
            self.assertEqual(names, {"沈川", "苏瑶"})
            self.assertNotIn("发型、发色、肤色、体型、身高感", names)
            self.assertNotIn("常态服装分层、鞋履、固定配饰、主辅配色", names)

    def test_plausible_new_character_not_in_story_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "c" * 24
            director = _Director(root, project_id)
            director.production.create_entity(
                project_id,
                entity_type="character",
                name="沈川",
                logical_key="story:character:shen-chuan",
                stage="01",
            )
            text = _STAGE02 + """

## 角色资产：陆云
- 稳定身份：17岁男性
- 发型：黑发束起
- 服装：白袍
- 参考图生成要求：稳定身份参考
"""
            service = StageOutputAssetMaterializer(_Legacy(director))
            rows = service._extract(project_id, "02", text)
            names = {row["name"] for row in rows}
            self.assertNotIn("陆云", names)
            rejected = getattr(service, "_xiaoduan_last_rejected_unanchored", [])
            self.assertTrue(any(row.get("name") == "陆云" for row in rejected))

    def test_existing_fake_materialized_characters_and_derived_assets_are_retired(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "b" * 24
            director = _Director(root, project_id)
            p = director.production
            for name in ("沈川", "苏瑶"):
                p.create_entity(
                    project_id,
                    entity_type="character",
                    name=name,
                    logical_key=f"story:character:{name}",
                    stage="01",
                )
            fake = p.create_entity(
                project_id,
                entity_type="character",
                name="发型、发色、肤色、体型、身高感",
                logical_key="xiaoduan:character:fake-fields",
                stage="02",
                skill="xiaoduan-stage-asset-materializer",
                metadata={
                    "authoring": {
                        "stable_design": "错误字段片段",
                        "source_stage": "02",
                        "materialized_from_stage_output": True,
                    }
                },
            )
            profile = p.create_text_asset(
                project_id,
                stage="02",
                skill="xiaoduan-authoring-assets",
                logical_key=f"studio:authoring:{fake['entity_id']}:profile",
                asset_role="character_profile",
                name="错误角色稳定设定",
                content="错误",
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[fake["entity_id"]],
            )
            appearance = p.create_text_asset(
                project_id,
                stage="02",
                skill="xiaoduan-character-appearances",
                logical_key=f"studio:character:{fake['entity_id']}:appearance:default",
                asset_role="character_appearance",
                name="错误默认造型",
                content="错误",
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[fake["entity_id"]],
            )

            service = StageOutputAssetMaterializer(_Legacy(director))
            result = service.materialize(project_id)
            self.assertIn(fake["entity_id"], result["retired_invalid_entity_ids"])

            retired = p.get_graph(project_id)["entities"][fake["entity_id"]]
            self.assertEqual(retired["entity_type"], "retired_fragment")
            self.assertTrue(retired["metadata"]["retired_materialized_fragment"])
            self.assertFalse(p.get_asset(project_id, profile["asset_id"])["active"])
            self.assertFalse(p.get_asset(project_id, appearance["asset_id"])["active"])


if __name__ == "__main__":
    unittest.main()
