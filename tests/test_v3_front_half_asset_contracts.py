from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.character_appearance_materialization import CharacterAppearanceMaterializer
from app.v3.character_appearances import CharacterAppearanceService
from app.v3.stage_asset_materialization import StageOutputAssetMaterializer
from app.v3.stage_asset_materialization_guard import install_stage_asset_materialization_guard


install_stage_asset_materialization_guard()


_STAGE02 = """
## 角色资产：沈川
- 稳定身份：17岁男性，东亚少年面孔，身形清瘦挺拔
- 性别呈现：男性
- 年龄感：17岁少年
- 脸部结构与五官：少年窄脸，深色眼睛，左眉浅疤
- 发型：黑发束起
- 发色：黑色
- 肤色：自然偏白
- 体型：清瘦匀称
- 身高感：中等偏高
- 服装：深蓝色古代束袖长袍
- 鞋履：黑色布靴
- 固定身份锚点：17岁少年脸、左眉浅疤、黑发束起
- 允许变化项：表情、姿势、临时污损
- 形象版本：默认造型与受伤造型
- change_reason：默认造型用于开场，受伤造型由 N02 后持续伤势触发
- 参考图生成要求：中性设定稿背景，只表现稳定身份
- 原文证据：沈川17岁，深蓝长袍，携古剑
- 设计补全来源：脸部细节与鞋履由角色设计补全
```appearance-versions-json
{
  "versions": [
    {
      "appearance_id": "default",
      "name": "默认造型",
      "stable_design": "17岁男性，少年窄脸，左眉浅疤，黑发束起，深蓝色古代束袖长袍，黑色布靴，清瘦匀称",
      "change_reason": "故事开场默认状态",
      "effective_story_node_ids": ["N01"]
    },
    {
      "appearance_id": "injured_n02",
      "name": "N02后受伤造型",
      "stable_design": "17岁男性，少年窄脸，左眉浅疤，黑发束起，深蓝色古代束袖长袍左肩持续破损并带干涸血迹，黑色布靴",
      "change_reason": "N02 后肩部伤势持续到后续剧情节点",
      "effective_story_node_ids": ["N02", "N03"]
    }
  ]
}
```
"""


_STAGE03 = """
## 项目视觉圣经
- 媒介与技法：电影级写实摄影，真实布料与旧木石材质。
- 项目色板：主色 #203A5F，辅色 #DDE6EA，强调色 #6F8A63，背景 #ECE8DF。
- 边缘与线条：人物和核心建筑边缘清晰，远景自然柔化。
- 完成度与细节密度：人物和核心道具高细节，远景适度降细节。
- 禁止漂移：人物身份、地点结构、道具轮廓不得跨镜头改变。
```visual-direction-json
{
  "world_style": "东方仙侠",
  "culture": "古代中国文化语境",
  "era": "古代",
  "art_style": "电影级写实摄影",
  "character_rules": {"identity": "东亚人物身份与正式形象版本稳定"},
  "environment_rules": {"space": "古代中式建筑和山地结构稳定"},
  "prop_rules": {"design": "道具材质、轮廓和纹样稳定"},
  "negative_constraints": ["western fantasy identity drift", "modern clothing"]
}
```
## 地点资产：青云山道观
- 空间边界：山腰平台、院墙与山体形成明确边界
- 布局：石阶、山门、前院、正殿沿中轴展开
- 材质：青砖、灰瓦、旧木
- 前景：石阶
- 中景：飞檐山门和成对石灯
- 后景：正殿与山体
- 固定视觉锚点：至少三个；石阶、飞檐山门、成对石灯
- 可变化状态：天气、时段、人物位置
- 参考图生成要求：只表现稳定空间身份
## 道具资产：青玉坠
- 完整轮廓：椭圆玉坠
- 比例：掌心大小
- 结构：玉坠与深色系绳
- 材质：青玉
- 颜色：青绿色
- 尺度关系：掌心大小
- 剧情功能：角色身份物件
- change_reason：默认无版本变化
- 参考图生成要求：完整展示道具本体
"""


class _Director:
    def __init__(self, root: Path, project_id: str, stage: str, stage_text: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.stage_text = stage_text
        completed = ["01"] if stage == "02" else ["01", "02"]
        self.project = {
            "project_id": project_id,
            "title": "前半段资产合同测试",
            "current_stage": stage,
            "completed_stages": completed,
            "confirmed_outputs": {
                "01": {
                    "handoff": "故事事实：沈川；地点青云山道观；关键道具青玉坠。剧情节点 N01、N02、N03。",
                    "production_asset_ids": [],
                }
            },
            "stage_state": {
                stage: {
                    "stage_ready": True,
                    "skill_runtime": {"completion": {"ready": True}},
                }
            },
            "history": [],
        }
        self.production.ensure_project(project_id, self.project["title"])

    def get_project(self, project_id: str):
        assert project_id == self.project_id
        return self.project

    def _latest_stage_output(self, project, stage: str) -> str:
        return self.stage_text if stage == self.project["current_stage"] else ""


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director


class FrontHalfAssetContractTests(unittest.TestCase):
    def _stage02_entity(self, director: _Director, project_id: str):
        p = director.production
        story_entity = p.create_entity(
            project_id,
            entity_type="character",
            name="沈川",
            logical_key="story:character:shen-chuan",
            stage="01",
            evidence={"source_stage": "01", "type": "story_fact"},
        )
        StageOutputAssetMaterializer(_Legacy(director)).materialize(project_id)
        return next(
            item for item in p.list_entities(project_id, entity_type="character")
            if item["entity_id"] == story_entity["entity_id"]
        )

    def test_stage02_versions_materialize_once_and_list_does_not_replace_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project_id = "f" * 24
            director = _Director(Path(raw), project_id, "02", _STAGE02)
            p = director.production
            character = self._stage02_entity(director, project_id)
            authoring = character["metadata"]["authoring"]
            self.assertIn("参考图生成要求", authoring["source_design"])
            self.assertNotIn("参考图生成要求", authoring["stable_design"])
            self.assertNotIn("允许变化项", authoring["stable_design"])
            self.assertNotIn("appearance-versions-json", authoring["stable_design"])

            profile_payload = {
                "entity_id": character["entity_id"],
                "kind": "character",
                "name": "沈川",
                "stable_design": authoring["stable_design"],
                "source_entity_ids": [character["entity_id"]],
            }
            profile = p.create_text_asset(
                project_id,
                stage="02",
                skill="xiaoduan-authoring-assets",
                logical_key=f"studio:authoring:{character['entity_id']}:profile",
                asset_role="character_profile",
                name="角色「沈川」稳定设定",
                content=json.dumps(profile_payload, ensure_ascii=False, indent=2),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                entity_ids=[character["entity_id"]],
            )

            versions = CharacterAppearanceMaterializer(_Legacy(director))
            first = versions.materialize(project_id)
            second = versions.materialize(project_id)
            self.assertEqual(first["asset_ids"], second["asset_ids"])

            active = [
                item for item in p.list_assets(project_id, active_only=True)
                if item.get("asset_role") == "character_appearance"
                and character["entity_id"] in (item.get("entity_ids") or [])
            ]
            self.assertEqual(len(active), 2)
            self.assertEqual({item["metadata"]["appearance_id"] for item in active}, {"default", "injured_n02"})
            self.assertTrue(all(profile["asset_id"] in (item.get("parent_asset_ids") or []) for item in active))

            default_before = next(item for item in active if item["metadata"]["appearance_id"] == "default")
            before_id = default_before["asset_id"]
            before_content = json.loads(p.read_text_asset(project_id, before_id))
            self.assertIn("深蓝色古代束袖长袍", before_content["stable_design"])

            listed = CharacterAppearanceService(_Legacy(director)).list(project_id)
            defaults = [row for row in listed["appearances"] if row["appearance_id"] == "default"]
            self.assertEqual(len(defaults), 1)
            self.assertEqual(defaults[0]["asset_id"], before_id)
            default_after = p.get_asset(project_id, before_id)
            self.assertEqual(default_after["source"]["type"], "stage02_appearance_version")

    def test_stage02_revision_refreshes_raw_source_and_stable_projection(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project_id = "r" * 24
            director = _Director(Path(raw), project_id, "02", _STAGE02)
            character = self._stage02_entity(director, project_id)
            self.assertIn("深蓝色古代束袖长袍", character["metadata"]["authoring"]["stable_design"])

            director.stage_text = _STAGE02.replace(
                "深蓝色古代束袖长袍",
                "墨绿色古代束袖长袍",
            )
            StageOutputAssetMaterializer(_Legacy(director)).materialize(project_id)
            refreshed = next(
                item for item in director.production.list_entities(project_id, entity_type="character")
                if item["entity_id"] == character["entity_id"]
            )
            authoring = refreshed["metadata"]["authoring"]
            self.assertIn("墨绿色古代束袖长袍", authoring["source_design"])
            self.assertIn("墨绿色古代束袖长袍", authoring["stable_design"])
            self.assertNotIn("深蓝色古代束袖长袍", authoring["stable_design"])
            self.assertNotIn("参考图生成要求", authoring["stable_design"])

    def test_stage03_direction_survives_same_pass_cleanup_of_legacy_fake_entity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project_id = "e" * 24
            director = _Director(Path(raw), project_id, "03", _STAGE03)
            p = director.production
            p.create_entity(
                project_id,
                entity_type="location",
                name="青云山道观",
                logical_key="story:location:qys-temple",
                stage="01",
            )
            p.create_entity(
                project_id,
                entity_type="prop",
                name="青玉坠",
                logical_key="story:prop:jade-pendant",
                stage="01",
            )
            fake = p.create_entity(
                project_id,
                entity_type="location",
                name="空间边界、布局",
                logical_key="xiaoduan:location:legacy-fake",
                stage="03",
                skill="xiaoduan-stage-asset-materializer",
                metadata={
                    "authoring": {
                        "stable_design": "错误字段片段",
                        "source_stage": "03",
                        "materialized_from_stage_output": True,
                    }
                },
            )
            fake_profile = p.create_text_asset(
                project_id,
                stage="03",
                skill="xiaoduan-authoring-assets",
                logical_key=f"studio:authoring:{fake['entity_id']}:profile",
                asset_role="location_profile",
                name="错误地点稳定设定",
                content="错误字段片段",
                entity_ids=[fake["entity_id"]],
            )

            result = StageOutputAssetMaterializer(_Legacy(director)).materialize(project_id)
            self.assertTrue(result["visual_direction_asset_id"])
            direction = p.get_visual_direction(project_id)
            self.assertEqual(direction["world_style"], "东方仙侠")
            self.assertEqual(direction["culture"], "古代中国文化语境")
            self.assertIn("western fantasy identity drift", direction["negative_constraints"])

            retired = p.get_graph(project_id)["entities"][fake["entity_id"]]
            self.assertEqual(retired["entity_type"], "retired_fragment")
            self.assertFalse(p.get_asset(project_id, fake_profile["asset_id"])["active"])


if __name__ == "__main__":
    unittest.main()
