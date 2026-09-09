from __future__ import annotations

import re
from types import MethodType
from typing import Any

from app.services import director as director_module
from app.services.production_skills import (
    STAGE_PRODUCTION_SKILLS,
    WORKFLOW_PRODUCTION_SKILL,
    builtin_production_skill,
    is_builtin_production_skill,
)


class ProductionSkillRegistry:
    """Make Xiaoduan's native production Skills authoritative at runtime.

    The legacy workbench still owns routing/UI/project storage, but the content
    generator no longer reads the old monolithic workflow as its production
    source. Built-in Skills have no external reference files, so only their
    exact checked-in definitions participate in contract compilation.

    Native authoring stages are deliberately single-pass. A stage first runs
    its one real production step exactly once. After the produced content,
    entities and contract receipts have been materialized, the platform
    deterministically re-evaluates readiness. Only a genuinely ready stage is
    promoted locally to ``complete_stage``. No second content-generation turn
    and no legacy ``advance`` call is allowed.
    """

    def __init__(self, director: Any) -> None:
        self.director = director
        self._original_skill_md = getattr(director, "_skill_md")
        self._original_available_files = getattr(director, "_available_files")
        self._original_read_source_file = getattr(director, "_read_source_file")
        self._original_source_status = getattr(director, "source_status")
        self._original_ensure_native_plan = getattr(director, "_ensure_native_plan", None)
        self._original_native_target = getattr(director, "_native_target", None)
        self._original_message = getattr(director, "message", None)

    @staticmethod
    def _single_step(skill_md: str) -> str:
        match = re.search(
            r"##\s*唯一步骤\s*\n+\s*\*\*(.+?)\*\*",
            str(skill_md or ""),
            flags=re.S,
        )
        if not match:
            raise RuntimeError("小段生产 Skill 缺少可确定的“唯一步骤”")
        value = match.group(1).strip()
        if not value or value not in skill_md:
            raise RuntimeError("小段生产 Skill 的唯一步骤无法回溯到原文")
        return value

    @staticmethod
    def _install_single_pass_completion_promotion() -> None:
        """Promote a ready single-pass step to terminal locally.

        Director.message() intentionally materializes the generated turn,
        entities and contract receipts before calling apply_asset_completion.
        That is the first point where readiness can be known truthfully. The
        old implementation targeted complete_stage before this point and could
        therefore fail with ``complete_stage_must_be_ready`` even though the
        content itself had been produced successfully.
        """
        if getattr(
            director_module,
            "_xiaoduan_single_pass_completion_promotion_installed",
            False,
        ):
            return

        original_apply = director_module.apply_asset_completion

        def apply_asset_completion(
            *,
            contract: dict[str, Any],
            runtime_state: dict[str, Any],
            control_runtime: dict[str, Any] | None,
            native_target: dict[str, Any],
            native_plan: dict[str, Any],
            asset_readiness: dict[str, list[str]],
        ) -> dict[str, Any]:
            if (
                bool(native_plan.get("single_pass"))
                and str(native_target.get("kind") or "") == "step"
            ):
                steps = list(native_plan.get("steps") or [])
                terminal_target = {
                    "kind": "complete_stage",
                    "index": len(steps),
                    "name": "",
                    "single_pass": True,
                    "promoted_after_readiness": True,
                }
                terminal_state = original_apply(
                    contract=contract,
                    runtime_state=runtime_state,
                    control_runtime=control_runtime,
                    native_target=terminal_target,
                    native_plan=native_plan,
                    asset_readiness=asset_readiness,
                )
                completion = terminal_state.get("completion") or {}
                if completion.get("ready") is True:
                    # Mutate the original target because Director.message()
                    # later persists and mechanically validates this same dict.
                    native_target.clear()
                    native_target.update(terminal_target)
                    if steps:
                        native_plan["current_index"] = len(steps) - 1
                    native_plan["completed_locally"] = True
                    native_plan["completion_mode"] = (
                        "single_pass_after_materialization"
                    )
                    return terminal_state

                # The one production call did not satisfy its contract. Keep
                # the stage non-terminal and expose the actual missing items;
                # never trigger another hidden Qwen turn.
                return original_apply(
                    contract=contract,
                    runtime_state=terminal_state,
                    control_runtime=control_runtime,
                    native_target=native_target,
                    native_plan=native_plan,
                    asset_readiness=asset_readiness,
                )

            return original_apply(
                contract=contract,
                runtime_state=runtime_state,
                control_runtime=control_runtime,
                native_target=native_target,
                native_plan=native_plan,
                asset_readiness=asset_readiness,
            )

        director_module.apply_asset_completion = apply_asset_completion
        director_module._xiaoduan_single_pass_completion_promotion_installed = True

    def install(self) -> None:
        if getattr(self.director, "_xiaoduan_native_production_skills_installed", False):
            return

        # DirectorService resolves these globals at call time, so replacing the
        # module-level registry switches the real stage runtime without forking
        # the legacy project/session implementation.
        director_module.STAGE_SKILLS = dict(STAGE_PRODUCTION_SKILLS)
        director_module.WORKFLOW_SKILL = WORKFLOW_PRODUCTION_SKILL
        self._install_single_pass_completion_promotion()

        original_skill_md = self._original_skill_md
        original_available_files = self._original_available_files
        original_read_source_file = self._original_read_source_file
        original_source_status = self._original_source_status
        original_ensure_native_plan = self._original_ensure_native_plan
        original_native_target = self._original_native_target
        original_message = self._original_message

        def skill_md(instance: Any, skill_name: str) -> str:
            if is_builtin_production_skill(skill_name):
                return builtin_production_skill(skill_name)
            return original_skill_md(skill_name)

        def available_files(instance: Any, skill_name: str) -> list[str]:
            if is_builtin_production_skill(skill_name):
                return []
            return original_available_files(skill_name)

        def read_source_file(instance: Any, skill_name: str, relative: str) -> str:
            if is_builtin_production_skill(skill_name):
                raise FileNotFoundError("内置生产技能没有外部引用文件")
            return original_read_source_file(skill_name, relative)

        def source_status(instance: Any) -> dict[str, Any]:
            try:
                value = original_source_status()
            except Exception:
                value = {}
            checks = {
                WORKFLOW_PRODUCTION_SKILL: True,
                **{name: True for name in STAGE_PRODUCTION_SKILLS.values()},
            }
            runtime = dict(value.get("skill_runtime") or {}) if isinstance(value, dict) else {}
            runtime.update({
                "native_production_skills": True,
                "story_bible": True,
                "typed_stage_boundaries": True,
                "single_pass_authoring": True,
                "local_readiness_completion": True,
                "legacy_auto_advance": False,
                "external_workflow_required": False,
            })
            return {
                "ready": True,
                "manifest": {
                    "schema_version": "xiaoduan-production-skills-v3-single-pass-readiness",
                    "workflow_skill": WORKFLOW_PRODUCTION_SKILL,
                    "stage_order": [
                        {"stage": stage, "skill": skill}
                        for stage, skill in STAGE_PRODUCTION_SKILLS.items()
                    ],
                },
                "checks": checks,
                "skill_runtime": runtime,
            }

        self.director._skill_md = MethodType(skill_md, self.director)
        self.director._available_files = MethodType(available_files, self.director)
        self.director._read_source_file = MethodType(read_source_file, self.director)
        self.director.source_status = MethodType(source_status, self.director)

        if callable(original_ensure_native_plan):
            async def ensure_native_plan(
                instance: Any,
                *,
                skill_name: str,
                skill_md: str,
                project: dict[str, Any],
                stage: str,
                stage_state: dict[str, Any],
                user_text: str,
            ) -> dict[str, Any]:
                if is_builtin_production_skill(skill_name):
                    step = ProductionSkillRegistry._single_step(skill_md)
                    source_sha = instance._skill_source_sha256(skill_md)
                    plan = {
                        "schema_version": getattr(
                            director_module,
                            "NATIVE_PLAN_SCHEMA_VERSION",
                            "native_plan_v2_single_step",
                        ),
                        "mode": "sequential",
                        "steps": [step],
                        "current_index": -1,
                        "reason": (
                            "小段①-④专业 Skill 单次生成完整阶段产物；"
                            "产物落库后由本地 readiness 决定是否完成阶段"
                        ),
                        "source_sha256": source_sha,
                        "single_pass": True,
                        "legacy_auto_advance": False,
                        "audit": {
                            "valid": True,
                            "reason": "唯一步骤由内置 Skill 原文确定性提取，无需额外 LLM 规划",
                        },
                    }
                    stage_state["native_plan"] = plan
                    return plan
                return await original_ensure_native_plan(
                    skill_name=skill_name,
                    skill_md=skill_md,
                    project=project,
                    stage=stage,
                    stage_state=stage_state,
                    user_text=user_text,
                )

            self.director._ensure_native_plan = MethodType(ensure_native_plan, self.director)

        if callable(original_native_target):
            def native_target(
                instance: Any,
                *,
                plan: dict[str, Any],
                previous_step: str,
                control_event: dict[str, Any],
            ) -> dict[str, Any]:
                if bool(plan.get("single_pass")):
                    steps = list(plan.get("steps") or [])
                    if not steps:
                        raise RuntimeError("小段单次生产计划缺少唯一步骤")
                    # First run the sole real production step. It may only be
                    # promoted to complete_stage after assets/receipts exist.
                    return {
                        "kind": "step",
                        "index": 0,
                        "name": steps[0],
                        "single_pass": True,
                    }
                return original_native_target(
                    plan=plan,
                    previous_step=previous_step,
                    control_event=control_event,
                )

            self.director._native_target = MethodType(native_target, self.director)

        if callable(original_message):
            async def message(
                instance: Any,
                project_id: str,
                user_text: str,
                *,
                native_control_action: str = "",
            ) -> dict[str, Any]:
                action = str(native_control_action or "").strip().lower()
                if action:
                    project = instance.get_project(project_id)
                    stage = str(project.get("current_stage") or "").strip()
                    skill_name = director_module.STAGE_SKILLS.get(stage, "")
                    if is_builtin_production_skill(skill_name):
                        raise RuntimeError(
                            "小段①-④已使用单次生产模式，旧后台自动推进已禁用；"
                            "本阶段不会再次调用模型。"
                        )
                return await original_message(
                    project_id,
                    user_text,
                    native_control_action=native_control_action,
                )

            self.director.message = MethodType(message, self.director)

        self.director._xiaoduan_native_production_skills_installed = True


__all__ = ["ProductionSkillRegistry"]
