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

    def test_package_pipeline_is_manual_between_all_three_stages(self):
        root = Path(__file__).resolve().parents[1]
        backend = (root / "app" / "v3" / "character_reference_package.py").read_text(encoding="utf-8")
        frontend = (root / "app" / "v3" / "static" / "character-reference-package-overlay.js").read_text(encoding="utf-8")
        self.assertIn('"generation_phase": "face_anchor"', backend)
        self.assertIn('"generation_phase": "costume"', backend)
        self.assertIn('"generation_phase": "turnaround"', backend)
        self.assertIn('"reference_asset_ids": [', backend)
        self.assertIn('_clean(face_ready.get("asset_id"))', backend)
        self.assertIn('_clean(costume_ready.get("asset_id"))', backend)
        self.assertIn("character_reference_package_v1", backend)
        self.assertIn("采用锁脸图", frontend)
        self.assertIn("生成服装定装图", frontend)
        self.assertNotIn("锁脸图已采用，正在用这张脸继续生成三视图", frontend)


if __name__ == "__main__":
    unittest.main()
