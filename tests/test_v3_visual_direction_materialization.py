from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.stage_asset_materialization import StageOutputAssetMaterializer
from app.v3.stage_asset_materialization_guard import install_stage_asset_materialization_guard


install_stage_asset_materialization_guard()


_STAGE03 = """
## 项目视觉圣经
- 媒介与技法：电影级写实摄影。
- 项目色板：主色 #203A5F，辅色 #DDE6EA，强调色 #6F8A63。
- 边缘与线条：人物轮廓自然，背景柔和。
- 完成度与细节密度：人物与核心道具高细节，远景适度降细节。
- 禁止漂移：人物身份、地点结构、道具结构不得变化。
```visual-direction-json
{
  "world_style": "东方仙侠",
  "culture": "古代中国文化语境",
  "era": "古代",
  "art_style": "电影级写实摄影",
  "character_rules": {"identity": "东亚人物身份与正式形象版本稳定"},
  "environment_rules": {"space": "古代中式建筑与自然山地结构"},
  "prop_rules": {"design": "材质轮廓纹样跨镜头稳定"},
  "negative_constraints": ["western fantasy identity drift", "modern clothing"]
}
```

## 地点资产：青云山道观
- 空间边界：山腰平台与院墙
- 布局：山门、前院、正殿
- 材质：青砖灰瓦旧木
- 前景：石阶
- 中景：山门
- 后景：正殿
- 固定视觉锚点：至少三个；石阶、飞檐、石灯
- 可变化状态：天气与时段
- 参考图生成要求：稳定空间身份

## 道具资产：青玉坠
- 完整轮廓：椭圆
- 比例：掌心大小
- 结构：玉坠与系绳
- 材质：青玉
- 颜色：青绿色
- 尺度关系：掌心大小
- 剧情功能：角色身份物件
- change_reason：默认无变化
- 参考图生成要求：白底完整展示
"""


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project_id = project_id
        self.project = {
            "project_id": project_id,
            "title": "视觉方向物化测试",
            "current_stage": "03",
            "completed_stages": ["01", "02"],
            "confirmed_outputs": {
                "01": {
                    "handoff": "故事地点为青云山道观，关键道具为青玉坠。",
                    "production_asset_ids": [],
                },
                "02": {"handoff": "角色阶段完成", "production_asset_ids": []},
            },
            "stage_state": {
                "03": {
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
        return _STAGE03 if stage == "03" else ""


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director


class VisualDirectionMaterializationTests(unittest.TestCase):
    def test_stage03_json_becomes_versioned_production_visual_direction(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            project_id = "d" * 24
            director = _Director(Path(raw), project_id)
            service = StageOutputAssetMaterializer(_Legacy(director))
            result = service.materialize(project_id)

            self.assertTrue(result["visual_direction_asset_id"])
            self.assertEqual(result["rejected_unanchored_assets"], [])
            direction = director.production.get_visual_direction(project_id)
            self.assertEqual(direction["world_style"], "东方仙侠")
            self.assertEqual(direction["culture"], "古代中国文化语境")
            self.assertEqual(direction["era"], "古代")
            self.assertEqual(direction["art_style"], "电影级写实摄影")
            self.assertIn("western fantasy identity drift", direction["negative_constraints"])

            active = [
                item for item in director.production.list_assets(project_id, active_only=True)
                if item.get("asset_role") == "project_visual_direction"
            ]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["asset_id"], result["visual_direction_asset_id"])


if __name__ == "__main__":
    unittest.main()
