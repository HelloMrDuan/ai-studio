from __future__ import annotations

import unittest

from app.services.production_skills import (
    STAGE_PRODUCTION_SKILLS,
    WORKFLOW_PRODUCTION_SKILL,
    builtin_production_skill,
)
from app.v3.production_skill_registry import ProductionSkillRegistry


class _FakeDirector:
    def __init__(self) -> None:
        self._skill_calls = []

    def _skill_md(self, name: str) -> str:
        self._skill_calls.append(name)
        return f"legacy:{name}"

    def _available_files(self, name: str):
        return ["legacy.md"]

    def _read_source_file(self, name: str, relative: str):
        return "legacy-source"

    def source_status(self):
        return {"ready": False, "skill_runtime": {"legacy": True}}


class NativeProductionSkillTests(unittest.TestCase):
    def test_stage_registry_no_longer_uses_legacy_captain_skill_names(self):
        self.assertEqual(STAGE_PRODUCTION_SKILLS["01"], "xiaoduan-story-bible")
        self.assertEqual(STAGE_PRODUCTION_SKILLS["04"], "xiaoduan-storyboard-director")
        for name in STAGE_PRODUCTION_SKILLS.values():
            self.assertNotIn("chuanzhang", name.lower())
        self.assertNotIn("chuanzhang", WORKFLOW_PRODUCTION_SKILL.lower())

    def test_story_skill_is_story_bible_not_one_line_summary(self):
        text = builtin_production_skill("xiaoduan-story-bible")
        self.assertIn("故事生产圣经", text)
        self.assertIn("剧情节点", text)
        self.assertIn("不可篡改事实", text)
        self.assertIn("禁止用“一句话故事概括”", text)
        self.assertIn("不提前写最终图片 Prompt", text)

    def test_character_skill_has_appearance_versions(self):
        text = builtin_production_skill("xiaoduan-character-assets")
        self.assertIn("形象版本", text)
        self.assertIn("change_reason", text)
        self.assertIn("固定身份锚点", text)

    def test_storyboard_skill_requires_continuity_links_and_quality_tiers(self):
        text = builtin_production_skill("xiaoduan-storyboard-director")
        self.assertIn("continuity_link", text)
        self.assertIn("representative_state", text)
        self.assertIn("video_start_state", text)
        self.assertIn("质量等级", text)

    def test_registry_serves_builtin_skills_without_external_files(self):
        director = _FakeDirector()
        registry = ProductionSkillRegistry(director)
        registry.install()
        text = director._skill_md("xiaoduan-character-assets")
        self.assertIn("角色资产设计", text)
        self.assertEqual(director._available_files("xiaoduan-character-assets"), [])
        status = director.source_status()
        self.assertTrue(status["ready"])
        self.assertTrue(status["skill_runtime"]["native_production_skills"])
        self.assertFalse(status["skill_runtime"]["external_workflow_required"])


if __name__ == "__main__":
    unittest.main()
