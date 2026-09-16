from __future__ import annotations

import unittest
from pathlib import Path

from app.services.media_generation_pipeline import _phase_anchor_text
from app.v3.character_reference_package import _FACE_TOKENS, _COSTUME_TOKENS, _select_facts


class CharacterReferencePackageTests(unittest.TestCase):
    def test_face_fact_selector_excludes_costume_and_scene_noise(self):
        metadata = {
            "stable_profile": {
                "年龄感": "17岁",
                "脸部结构": "眉目清秀，深邃眼眸，鼻梁挺直",
                "发型": "黑发束起",
                "肤色": "偏白",
                "上衣": "深蓝色古式长袍",
                "固定配饰": "乌木剑鞘",
            },
            "stable_design": "背景为青云山断崖，落雪环境\n古剑出鞘时亮起纹路",
        }
        rows = _select_facts(metadata, _FACE_TOKENS)
        text = "\n".join(rows)
        self.assertIn("17岁", text)
        self.assertIn("眉目清秀", text)
        self.assertIn("黑发束起", text)
        self.assertNotIn("深蓝色古式长袍", text)
        self.assertNotIn("乌木剑鞘", text)
        self.assertNotIn("青云山断崖", text)

    def test_costume_fact_selector_keeps_clothing_and_accessories(self):
        metadata = {
            "stable_profile": {
                "年龄感": "17岁",
                "脸部结构": "眉目清秀",
                "上衣": "深蓝色古式长袍，云纹刺绣",
                "鞋履": "黑色厚底靴",
                "固定配饰": "乌木剑鞘，暗银云纹",
                "主配色": "深蓝",
            }
        }
        rows = _select_facts(metadata, _COSTUME_TOKENS)
        text = "\n".join(rows)
        self.assertIn("深蓝色古式长袍", text)
        self.assertIn("黑色厚底靴", text)
        self.assertIn("乌木剑鞘", text)
        self.assertNotIn("眉目清秀", text)

    def test_media_pipeline_filters_identity_anchors_by_phase(self):
        raw = (
            "年龄感: 17岁; 脸部结构: 眉目清秀; 发型: 黑发束起; "
            "上衣: 深蓝色古式长袍; 固定配饰: 乌木剑鞘; 背景: 青云山断崖"
        )
        face = _phase_anchor_text(raw, "face_anchor")
        costume = _phase_anchor_text(raw, "costume")
        turnaround = _phase_anchor_text(raw, "turnaround")
        self.assertIn("17岁", face)
        self.assertIn("眉目清秀", face)
        self.assertNotIn("深蓝色古式长袍", face)
        self.assertIn("深蓝色古式长袍", costume)
        self.assertIn("乌木剑鞘", costume)
        self.assertNotIn("青云山断崖", costume)
        self.assertEqual(turnaround, raw)

    def test_character_master_anchor_removes_internal_schema_and_unknown_values(self):
        raw = (
            "name: 测试角色; stable_profile.年龄: 17岁; stable_profile.发型: 黑发高髻; "
            "stable_profile.服装: 浅青色古式长裙; stable_profile.脸部: 未明确描述，待角色设计; "
            "change_reason: 角色基础造型; effective_story_node_ids: N001"
        )
        master = _phase_anchor_text(raw, "character_master")
        self.assertIn("17岁", master)
        self.assertIn("黑发高髻", master)
        self.assertIn("浅青色古式长裙", master)
        self.assertNotIn("stable_profile", master)
        self.assertNotIn("name", master)
        self.assertNotIn("未明确", master)
        self.assertNotIn("change_reason", master)
        self.assertNotIn("effective_story", master)

    def test_package_pipeline_uses_one_adopted_master_and_deterministic_crops(self):
        root = Path(__file__).resolve().parents[1]
        backend = (root / "app" / "v3" / "character_reference_package.py").read_text(encoding="utf-8")
        main = (root / "app" / "main.py").read_text(encoding="utf-8")
        state_owner = (root / "app" / "v3" / "static" / "reference-generation-ux-overlay.js").read_text(encoding="utf-8")
        self.assertIn('"generation_phase": "character_master"', backend)
        self.assertIn('"mode": "txt2img"', backend)
        self.assertIn('"width": 768, "height": 1024', backend)
        self.assertNotIn('"mode": "reference_img2img"', backend)
        self.assertIn("character_reference_package_v3", backend)
        self.assertIn("front_face_crop_plus_front_costume_plus_full_turnaround", backend)
        self.assertIn("zimage_front_layout_then_i2l_identity_master", backend)
        self.assertIn("derived_from_adopted_master", backend)
        self.assertIn("采用母版并完成角色资产包", state_owner)
        self.assertIn("角色母版只需采用一次", state_owner)
        self.assertIn("install_character_master_confirmation_guard", main)
        self.assertIn("service.validate_master_adoption(project_id, row, 0)", backend)
        self.assertIn("service.materialize_master_derivatives(project_id, confirmed, 0)", backend)


if __name__ == "__main__":
    unittest.main()
