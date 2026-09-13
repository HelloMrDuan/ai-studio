from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.services.production_assets import ProductionAssetService
from app.v3.professional_output_registry import (
    PROFESSIONAL_OUTPUT_REGISTRY,
    parse_professional_output,
    professional_output_json_schema,
    validate_professional_output,
)
from app.v3.professional_output_runtime import (
    ProfessionalOutputStore,
    _deterministic_control,
    _latest_professional_output,
)


_SOURCE = """
大雪初停，北方古城的城门已经落锁。
沈璃独自站在城外石桥上。她提着一盏六角青铜灯笼。
陆沉勒马停在桥头，乌木剑鞘斜背在身后。
沈璃看了他一眼。陆沉没有回答。沈璃提灯跟了上去。
""".strip()


def story_payload() -> dict:
    return {
        "schema_version": 1,
        "output_kind": "story_bible",
        "document": (
            "# 故事生产圣经\n"
            "## 故事定位\n雪夜古城危机短篇。\n"
            "## 不可篡改事实\n沈璃与陆沉在古城门外会合。\n"
            "## 世界观与时间线\n古代北方城镇；大雪初停后的傍晚。\n"
            "## 剧情节点\nN01 石桥会合；N02 前往城门。\n"
            "## 角色实体表\n沈璃；陆沉。\n"
            "## 地点实体表\n城外石桥；古城城门。\n"
            "## 道具实体表\n六角青铜灯笼；乌木剑鞘。\n"
            "## 对白与旁白事实\n对白：无固定对白；旁白：保留雪夜与同行事实。\n"
            "## 连续性事件\n沈璃持灯；陆沉背剑。\n"
            "## 创作计划\n先会合，再以城门危机升级悬念。\n"
            + "事实边界与后续制作说明。" * 24
        ),
        "characters": [
            {"name": "沈璃", "source_evidence": "沈璃独自站在城外石桥上"},
            {"name": "陆沉", "source_evidence": "陆沉勒马停在桥头"},
        ],
        "locations": [
            {"name": "城外石桥", "source_evidence": "沈璃独自站在城外石桥上"},
        ],
        "props": [
            {"name": "六角青铜灯笼", "source_evidence": "一盏六角青铜灯笼"},
            {"name": "乌木剑鞘", "source_evidence": "乌木剑鞘斜背在身后"},
        ],
        "assumptions": [],
        "warnings": [],
    }


def character_payload() -> dict:
    return {
        "schema_version": 1,
        "output_kind": "character_assets",
        "document": (
            "# 角色资产\n## 沈璃\n稳定少女角色设计。\n"
            "## 陆沉\n稳定少年角色设计。\n" + "角色设计说明。" * 40
        ),
        "characters": [
            {
                "name": "沈璃",
                "stable_description": "沈璃，年轻女性，低髻黑发，银色梅花簪，深红交领长裙，灰白短斗篷，黑色布靴。",
                "gender_presentation": "女性",
                "age": "少女年龄感",
                "face": "清秀东亚面孔",
                "hair_style": "黑色长发盘成低髻",
                "hair_color": "黑色",
                "skin_tone": "自然偏白",
                "body_type": "纤细自然",
                "height_impression": "中等",
                "clothing": "深红交领长裙，灰白短斗篷",
                "footwear": "黑色布靴",
                "fixed_identity_anchors": ["低髻", "银色梅花簪"],
                "allowed_variations": ["表情", "轻微衣物风雪痕迹"],
                "appearances": [
                    {
                        "appearance_id": "default",
                        "name": "默认造型",
                        "stable_design": "低髻黑发，银色梅花簪，深红交领长裙，灰白短斗篷，黑色布靴",
                        "change_reason": "基础造型",
                        "effective_story_node_ids": [],
                    }
                ],
                "reference_requirements": "稳定身份参考，完整辨识角色",
                "source_evidence": "沈璃独自站在城外石桥上",
                "design_basis": "原文明确发簪和服装，其余未指定部分采用保守设计补全",
            },
            {
                "name": "陆沉",
                "stable_description": "陆沉，年轻男性，黑发高束，深青古代长袍，沉稳清晰的少年轮廓。",
                "gender_presentation": "男性",
                "age": "青年年龄感",
                "face": "清晰东亚青年面孔",
                "hair_style": "黑发高束",
                "hair_color": "黑色",
                "skin_tone": "自然肤色",
                "body_type": "修长",
                "height_impression": "偏高",
                "clothing": "深青古代长袍",
                "footwear": "深色古代靴",
                "fixed_identity_anchors": ["黑发高束", "深青长袍"],
                "allowed_variations": ["表情"],
                "appearances": [
                    {
                        "appearance_id": "default",
                        "name": "默认造型",
                        "stable_design": "黑发高束，深青古代长袍，深色古代靴",
                        "change_reason": "基础造型",
                        "effective_story_node_ids": [],
                    }
                ],
                "reference_requirements": "稳定身份参考，完整辨识角色",
                "source_evidence": "陆沉勒马停在桥头",
                "design_basis": "原文明确长袍，其余未指定部分采用保守设计补全",
            },
        ],
        "assumptions": [],
        "warnings": [],
    }


class ProfessionalOutputRuntimeTests(unittest.TestCase):
    def test_registry_is_single_authority_for_front_half_skills(self) -> None:
        self.assertEqual(
            set(PROFESSIONAL_OUTPUT_REGISTRY),
            {"xiaoduan-story-bible", "xiaoduan-character-assets", "xiaoduan-visual-assets"},
        )
        self.assertEqual(
            {row["output_kind"] for row in PROFESSIONAL_OUTPUT_REGISTRY.values()},
            {"story_bible", "character_assets", "visual_assets"},
        )

    def test_story_schema_rejects_unknown_fields(self) -> None:
        payload = story_payload()
        payload["invented_runtime_field"] = True
        with self.assertRaises(ValueError):
            parse_professional_output(payload, expected_output_kind="story_bible")

    def test_human_document_has_no_arbitrary_300_500_character_floor(self) -> None:
        for output_kind in ("story_bible", "character_assets", "visual_assets"):
            schema = professional_output_json_schema(output_kind)
            self.assertEqual(schema["properties"]["document"]["minLength"], 1)

        payload = story_payload()
        payload["document"] = (
            "# 故事生产圣经\n"
            "沈璃与陆沉在城外石桥会合；六角青铜灯笼与乌木剑鞘保持连续。"
        )
        parsed = parse_professional_output(payload, expected_output_kind="story_bible")
        self.assertEqual(parsed["document"], payload["document"])
        self.assertLess(len(parsed["document"]), 500)

    def test_story_source_lineage_and_entity_completeness_are_deterministic(self) -> None:
        payload = parse_professional_output(story_payload(), expected_output_kind="story_bible")
        issues = validate_professional_output(
            payload,
            source_text=_SOURCE,
            required_names={"characters": ["沈璃", "陆沉"]},
        )
        self.assertEqual(issues, [])

        broken = json.loads(json.dumps(payload, ensure_ascii=False))
        broken["characters"] = [broken["characters"][0]]
        issues = validate_professional_output(
            broken,
            source_text=_SOURCE,
            required_names={"characters": ["沈璃", "陆沉"]},
        )
        self.assertTrue(any("陆沉" in item and "遗漏" in item for item in issues), issues)

    def test_stable_description_cannot_contain_generation_state(self) -> None:
        payload = parse_professional_output(character_payload(), expected_output_kind="character_assets")
        payload["characters"][0]["stable_description"] += "，白色背景参考图"
        issues = validate_professional_output(
            payload,
            source_text=_SOURCE,
            required_names={"characters": ["沈璃", "陆沉"]},
        )
        self.assertTrue(any("stable_description" in item and "背景" in item for item in issues), issues)

    def test_deterministic_control_never_invents_markdown_field_labels_as_entities(self) -> None:
        payload = parse_professional_output(story_payload(), expected_output_kind="story_bible")
        control = _deterministic_control(payload, payload["document"])
        names = {row["name"] for row in control["production_entities"]}
        self.assertEqual(
            names,
            {"沈璃", "陆沉", "城外石桥", "六角青铜灯笼", "乌木剑鞘"},
        )
        self.assertFalse({"人物关系", "涉及", "身份", "原文证据"} & names)

    def test_store_and_real_production_asset_keep_exact_structured_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = parse_professional_output(story_payload(), expected_output_kind="story_bible")
            store = ProfessionalOutputStore(root)
            store.put(payload)
            self.assertEqual(store.get_by_document(payload["document"]), payload)

            production = ProductionAssetService(root)
            project_id = "a" * 24
            production.ensure_project(project_id, "雪夜归城")
            production.create_text_asset(
                project_id,
                stage="01",
                skill="xiaoduan-story-bible",
                logical_key="studio:professional-output:story_bible",
                asset_role="professional_story_bible",
                name="故事生产圣经 · 专业结构化结果",
                content=json.dumps(payload, ensure_ascii=False, indent=2),
                asset_type="STRUCTURED_DATA",
                extension=".json",
                metadata={"source_of_truth": True, "single_writer": True},
            )
            loaded, asset = _latest_professional_output(production, project_id, "01")
            self.assertEqual(loaded, payload)
            self.assertEqual(asset["version"], 1)
            self.assertTrue(asset["metadata"]["source_of_truth"])


if __name__ == "__main__":
    unittest.main()
