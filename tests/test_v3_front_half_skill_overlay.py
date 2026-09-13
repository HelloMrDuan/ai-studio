from __future__ import annotations

import unittest

from app.v3.front_half_skill_overlay import FrontHalfSkillOverlay


class _Director:
    def __init__(self) -> None:
        self.calls = []

    def _skill_md(self, skill_name: str) -> str:
        self.calls.append(skill_name)
        return f"# {skill_name}\n\n原有成熟 Skill 内容。\n"


class FrontHalfSkillOverlayTests(unittest.TestCase):
    def test_character_skill_keeps_original_and_adds_stable_identity_rules(self):
        director = _Director()
        overlay = FrontHalfSkillOverlay(director)
        overlay.install()

        value = director._skill_md("ai-studio-character-design")

        self.assertIn("原有成熟 Skill 内容", value)
        self.assertIn("稳定身份", value)
        self.assertIn("脸部结构", value)
        self.assertIn("不要混入某个镜头的表情、姿势、动作", value)
        self.assertIn("不新增交付物/完成条件", value)

    def test_visual_skill_separates_reusable_assets_from_momentary_shot_state(self):
        director = _Director()
        FrontHalfSkillOverlay(director).install()
        value = director._skill_md("ai-studio-visual-design")
        self.assertIn("可复用视觉资产", value)
        self.assertIn("一次性动作或纯镜头效果不要升级为资产身份", value)
        self.assertIn("4:3参考图候选", value)

    def test_story_skill_preserves_source_fidelity_and_does_not_prewrite_image_prompt(self):
        director = _Director()
        FrontHalfSkillOverlay(director).install()
        value = director._skill_md("chuanzhang-chuangzuo-v1")
        self.assertIn("严格保持用户原故事的事实与因果", value)
        self.assertIn("不是提前写图片提示词", value)

    def test_unrelated_skill_is_unchanged(self):
        director = _Director()
        FrontHalfSkillOverlay(director).install()
        value = director._skill_md("other-skill")
        self.assertEqual(value, "# other-skill\n\n原有成熟 Skill 内容。\n")

    def test_install_is_idempotent(self):
        director = _Director()
        first = FrontHalfSkillOverlay(director)
        first.install()
        second = FrontHalfSkillOverlay(director)
        second.install()
        value = director._skill_md("ai-studio-character-design")
        self.assertEqual(value.count("## 小段映画角色资产约束"), 1)


if __name__ == "__main__":
    unittest.main()
