from __future__ import annotations

import asyncio
import hashlib
import unittest

from app.services import director as director_module
from app.services.production_skills import (
    STAGE_PRODUCTION_SKILLS,
    WORKFLOW_PRODUCTION_SKILL,
    builtin_production_skill,
)
from app.services.skill_runtime import empty_runtime_state
from app.v3.production_skill_registry import ProductionSkillRegistry


class _FakeDirector:
    def __init__(self) -> None:
        self._skill_calls = []
        self._contract_calls = 0
        self._plan_calls = 0
        self._message_calls = 0

    def _skill_md(self, name: str) -> str:
        self._skill_calls.append(name)
        return f"legacy:{name}"

    def _available_files(self, name: str):
        return ["legacy.md"]

    def _read_source_file(self, name: str, relative: str):
        return "legacy-source"

    def source_status(self):
        return {"ready": False, "skill_runtime": {"legacy": True}}

    def _skill_source_sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    async def _ensure_skill_contract(self, **kwargs):
        self._contract_calls += 1
        return {"completion_mode": "artifact_gate"}

    async def _ensure_native_plan(self, **kwargs):
        self._plan_calls += 1
        return {"mode": "dynamic", "steps": []}

    def _native_target(self, **kwargs):
        return {"kind": "legacy"}

    def get_project(self, project_id: str):
        return {"project_id": project_id, "current_stage": "01"}

    async def message(self, project_id: str, user_text: str, *, native_control_action: str = ""):
        self._message_calls += 1
        return {
            "project_id": project_id,
            "user_text": user_text,
            "native_control_action": native_control_action,
        }


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
        self.assertTrue(status["skill_runtime"]["single_pass_authoring"])
        self.assertTrue(status["skill_runtime"]["deterministic_authoring_contract"])
        self.assertTrue(status["skill_runtime"]["local_readiness_completion"])
        self.assertFalse(status["skill_runtime"]["legacy_auto_advance"])
        self.assertFalse(status["skill_runtime"]["external_workflow_required"])

    def test_builtin_contract_does_not_call_legacy_llm_contract_compiler(self):
        director = _FakeDirector()
        ProductionSkillRegistry(director).install()
        skill_name = "xiaoduan-story-bible"
        skill_md = director._skill_md(skill_name)
        stage_state = {}
        contract = asyncio.run(director._ensure_skill_contract(
            skill_name=skill_name,
            skill_md=skill_md,
            stage_state=stage_state,
        ))
        self.assertEqual(director._contract_calls, 0)
        self.assertEqual(contract["completion_mode"], "native_only")
        self.assertTrue(contract["single_pass"])
        self.assertFalse(contract["llm_contract_compiler_required"])
        self.assertEqual(stage_state["skill_contract"], contract)

    def test_native_authoring_runs_one_step_before_local_completion(self):
        director = _FakeDirector()
        ProductionSkillRegistry(director).install()
        skill_name = "xiaoduan-story-bible"
        skill_md = director._skill_md(skill_name)
        state = {}
        plan = asyncio.run(director._ensure_native_plan(
            skill_name=skill_name,
            skill_md=skill_md,
            project={"project_id": "demo", "current_stage": "01"},
            stage="01",
            stage_state=state,
            user_text="测试故事",
        ))
        self.assertEqual(director._plan_calls, 0)
        self.assertEqual(plan["mode"], "sequential")
        self.assertTrue(plan["single_pass"])
        self.assertEqual(plan["legacy_auto_advance"], False)
        self.assertEqual(plan["steps"], ["生成故事生产圣经与创作计划"])
        target = director._native_target(
            plan=plan,
            previous_step="",
            control_event={"action": "other"},
        )
        self.assertEqual(target["kind"], "step")
        self.assertEqual(target["index"], 0)
        self.assertEqual(target["name"], "生成故事生产圣经与创作计划")
        self.assertTrue(target["single_pass"])

    def test_ready_single_pass_step_is_promoted_without_second_model_turn(self):
        director = _FakeDirector()
        ProductionSkillRegistry(director).install()
        skill_name = "xiaoduan-story-bible"
        skill_md = director._skill_md(skill_name)
        stage_state = {}
        contract = asyncio.run(director._ensure_skill_contract(
            skill_name=skill_name,
            skill_md=skill_md,
            stage_state=stage_state,
        ))
        plan = asyncio.run(director._ensure_native_plan(
            skill_name=skill_name,
            skill_md=skill_md,
            project={"project_id": "demo", "current_stage": "01"},
            stage="01",
            stage_state=stage_state,
            user_text="测试故事",
        ))
        target = director._native_target(
            plan=plan,
            previous_step="",
            control_event={"action": "other"},
        )
        result = director_module.apply_asset_completion(
            contract=contract,
            runtime_state=empty_runtime_state(),
            control_runtime={"stage_complete_claim": False},
            native_target=target,
            native_plan=plan,
            asset_readiness={},
        )
        self.assertTrue(result["completion"]["ready"])
        self.assertEqual(target["kind"], "complete_stage")
        self.assertTrue(target["promoted_after_readiness"])
        self.assertTrue(plan["completed_locally"])
        self.assertEqual(plan["current_index"], 0)

    def test_legacy_internal_advance_is_rejected_before_second_model_turn(self):
        director = _FakeDirector()
        ProductionSkillRegistry(director).install()
        with self.assertRaisesRegex(RuntimeError, "旧后台自动推进已禁用"):
            asyncio.run(director.message(
                "demo-project",
                "",
                native_control_action="advance",
            ))
        self.assertEqual(director._message_calls, 0)

        result = asyncio.run(director.message("demo-project", "真实故事输入"))
        self.assertEqual(director._message_calls, 1)
        self.assertEqual(result["user_text"], "真实故事输入")


if __name__ == "__main__":
    unittest.main()
