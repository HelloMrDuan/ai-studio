from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3 import story_source_coverage as coverage
from app.v3.story_entity_sanitizer import (
    extract_story_table_names_strict,
    install_story_entity_sanitizer,
)


_SOURCE = """
雪夜，沈璃提着灯笼站在石桥上。陆沉勒马停在桥头。
沈璃看向陆沉。陆沉按住剑柄，随后两人一起朝城门走去。
"""

_STAGE01_DETAIL_BLOCK = """
# 故事生产圣经
## 故事定位
雪夜古城危机短篇。
## 不可篡改事实
沈璃与陆沉共同前往城门。
## 世界观与时间线
古代北方城镇；雪夜连续发生。
## 剧情节点
N01 石桥会合；N02 前往城门。
## 角色实体表
### 沈璃
- 身份：提灯女子
- 人物关系：与陆沉同行
- 涉及：N01、N02
- 原文证据：沈璃提灯并看向陆沉
### 陆沉
- 身份：持剑男子
- 人物关系：与沈璃同行
- 涉及剧情节点：N01、N02
- 原文证据：陆沉勒马并按住剑柄
## 地点实体表
石桥；城门。
## 道具实体表
灯笼；长剑。
## 对白与旁白事实
无固定对白；旁白保留雪夜和同行事实。
## 连续性事件
沈璃持灯；陆沉持剑。
## 创作计划
先会合，再前往城门。
"""


class _Director:
    def __init__(self, root: Path, project_id: str) -> None:
        self.production = ProductionAssetService(root)
        self.project = {
            "project_id": project_id,
            "title": "字段标签清理测试",
            "current_stage": "02",
            "completed_stages": ["01"],
            "confirmed_outputs": {"01": {"handoff": _STAGE01_DETAIL_BLOCK}},
            "history": [
                {"role": "user", "stage": "01", "content": _SOURCE},
                {"role": "assistant", "stage": "01", "content": _STAGE01_DETAIL_BLOCK},
            ],
            "stage_state": {
                "01": {"stage_ready": True, "skill_runtime": {"completion": {"ready": True}}},
                "02": {"stage_ready": False, "skill_runtime": {"completion": {"ready": False}}},
            },
        }
        self.production.ensure_project(project_id, self.project["title"])

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    def _latest_stage_output(self, project, stage: str):
        return _STAGE01_DETAIL_BLOCK if stage == "01" else ""


class StoryEntitySanitizerTests(unittest.TestCase):
    def test_detail_fields_are_not_character_names(self) -> None:
        names = extract_story_table_names_strict(_STAGE01_DETAIL_BLOCK, "角色实体表")
        self.assertEqual(names, ["沈璃", "陆沉"])
        for bad in ("人物关系", "涉及", "涉及剧情节点", "身份", "原文证据"):
            self.assertNotIn(bad, names)

    def test_reconcile_retires_previously_materialized_schema_labels(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            director = _Director(Path(raw), "e" * 24)
            project_id = director.project["project_id"]
            for name in ("人物关系", "涉及"):
                director.production.create_entity(
                    project_id,
                    entity_type="character",
                    name=name,
                    logical_key=f"stage01:character:bad:{name}",
                    stage="01",
                    skill="xiaoduan-story-bible",
                    metadata={
                        "story_entity_contract": "source_coverage_v2",
                        "materialized_from_validated_story_bible": True,
                    },
                )

            install_story_entity_sanitizer(director)
            result = coverage.reconcile_stage01_story_characters(
                director,
                project_id,
                require_ready=True,
            )

            active = director.production.list_entities(project_id, "character")
            self.assertEqual({item["name"] for item in active}, {"沈璃", "陆沉"})
            self.assertEqual(set(result["character_names"]), {"沈璃", "陆沉"})

            graph = director.production.get_graph(project_id)
            retired = {
                item.get("name")
                for item in (graph.get("entities") or {}).values()
                if item.get("entity_type") == "retired_fragment"
                and (item.get("metadata") or {}).get("retired_story_entity")
            }
            self.assertEqual(retired, {"人物关系", "涉及"})


if __name__ == "__main__":
    unittest.main()
