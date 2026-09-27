from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.services.production_assets import ProductionAssetService
from app.v3.canonical_reference_assets import CanonicalReferenceAssetBootstrap
from app.v3.production_authoring_assets import ProductionAuthoringAssetService


class _Director:
    def __init__(self, root: Path, project_id: str, output: str) -> None:
        self.production = ProductionAssetService(root)
        self.output = output
        self.project = {
            "project_id": project_id,
            "title": "视觉恢复测试",
            "status": "active",
            "current_stage": "03",
            "completed_stages": ["01", "02"],
            "confirmed_outputs": {"01": {"handoff": "测试故事", "production_asset_ids": []}},
            "stage_state": {
                "03": {"stage_ready": True, "skill_runtime": {"completion": {"ready": True}}}
            },
        }
        self.production.ensure_project(project_id, "视觉恢复测试")

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return dict(self.project)

    def list_projects(self):
        return [self.get_project(self.project["project_id"])]

    def _latest_stage_output(self, project, stage: str):
        return self.output if stage == "03" else ""


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director

    def _wb_load_candidates(self, project_id: str):
        return []

    def _wb_sync_candidates(self, project_id: str):
        return []

    async def director_workbench_execute_candidate(self, project_id: str, payload: dict):
        return {"ok": True}


def _profile(production: ProductionAssetService, project_id: str, entity_id: str, name: str) -> None:
    payload = {
        "schema_version": "xiaoduan_reusable_asset_profile_v1",
        "entity_id": entity_id,
        "kind": "character",
        "name": name,
        "stable_design": f"{name}稳定身份",
    }
    production.create_text_asset(
        project_id,
        stage="02",
        skill="test",
        logical_key=f"studio:authoring:{entity_id}:profile",
        asset_role="character_profile",
        name=f"角色「{name}」稳定设定",
        content=json.dumps(payload, ensure_ascii=False),
        asset_type="STRUCTURED_DATA",
        extension=".json",
        entity_ids=[entity_id],
        metadata={"kind": "character"},
    )


class VisualAssetRecoveryTests(unittest.TestCase):
    def test_stage03_aliases_become_formal_profiles_and_references_follow_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            project_id = "9" * 24
            output = """
画面设计生成稿

苍梧山顶：
空间边界为积雪断崖和废弃山门；布局以石阶、山门、中央石台为轴线；主要材质为覆雪青灰岩；固定视觉锚点包含断崖、山门、石台；参考图只展示稳定空间结构。

古剑：
完整轮廓为长直剑身和旧式剑格；结构包含剑柄、剑格、剑身；材质为暗色旧钢；颜色与纹样为黑布缠柄和云纹；剧情功能为关键线索；参考图完整展示道具结构。

青玉坠：
完整轮廓为小型玉坠；结构为玉坠和挂绳；材质为青玉；颜色为青绿色；固定纹样清晰；剧情功能为角色固定配饰；参考图完整展示道具结构。
"""
            director = _Director(root, project_id, output)
            legacy = _Legacy(director)

            shen = director.production.create_entity(
                project_id, entity_type="character", name="沈川", logical_key="story:character:shen", stage="01"
            )
            su = director.production.create_entity(
                project_id, entity_type="character", name="苏瑶", logical_key="story:character:su", stage="01"
            )
            _profile(director.production, project_id, shen["entity_id"], "沈川")
            _profile(director.production, project_id, su["entity_id"], "苏瑶")

            director.production.create_entity(
                project_id, entity_type="scene", name="苍梧山顶", logical_key="story:scene:mountain", stage="01"
            )
            director.production.create_entity(
                project_id, entity_type="scene", name="苍梧山古剑现世", logical_key="story:scene:event", stage="01"
            )
            director.production.create_entity(
                project_id, entity_type="artifact", name="古剑", logical_key="story:artifact:sword", stage="01"
            )
            director.production.create_entity(
                project_id, entity_type="item", name="青玉坠", logical_key="story:item:jade", stage="01"
            )

            authoring = ProductionAuthoringAssetService(SimpleNamespace(data_dir=root), legacy)
            state = authoring.status(project_id)
            visible = {(row["entity_type"], row["name"]) for row in state["items"]}
            self.assertIn(("character", "沈川"), visible)
            self.assertIn(("character", "苏瑶"), visible)
            self.assertIn(("location", "苍梧山顶"), visible)
            self.assertIn(("prop", "古剑"), visible)
            self.assertIn(("prop", "青玉坠"), visible)
            self.assertNotIn(("location", "苍梧山古剑现世"), visible)
            self.assertEqual(state["model_calls_added"], 0)

            references = CanonicalReferenceAssetBootstrap(legacy).status(project_id)
            names = {(row["entity_type"], row["name"]) for row in references["items"]}
            self.assertEqual(
                names,
                {
                    ("character", "沈川"),
                    ("character", "苏瑶"),
                    ("location", "苍梧山顶"),
                    ("prop", "古剑"),
                    ("prop", "青玉坠"),
                },
            )
            self.assertEqual(references["required_count"], 5)
            self.assertTrue(references["profile_roles_are_authoritative"])


if __name__ == "__main__":
    unittest.main()
