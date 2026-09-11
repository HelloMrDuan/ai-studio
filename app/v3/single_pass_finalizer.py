from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.services.production_skills import STAGE_PRODUCTION_SKILLS
from app.services.skill_runtime import empty_runtime_state
from app.v3.production_skill_registry import (
    ProductionSkillRegistry,
    builtin_single_pass_contract,
)
from app.v3.story_source_coverage import reconcile_stage01_story_characters


def _clean(value: Any) -> str:
    return str(value or "").strip()


class SinglePassStageFinalizer:
    """Finish a persisted ①-④ result without another model generation turn."""

    def __init__(self, legacy_runtime: Any, progress_tracker: Any | None = None) -> None:
        self.legacy = legacy_runtime
        self.director = legacy_runtime.director
        self.progress_tracker = progress_tracker

    async def finalize(self, project_id: str) -> dict[str, Any]:
        project = self.director.get_project(project_id)
        stage = _clean(project.get("current_stage"))
        skill_name = STAGE_PRODUCTION_SKILLS.get(stage, "")
        if not skill_name:
            raise ValueError("当前阶段不属于①-④专业创作阶段")

        state = ((project.get("stage_state") or {}).get(stage) or {})
        content = self.director._latest_stage_output(project, stage)
        if not _clean(content):
            return {
                "project_id": project_id,
                "stage": stage,
                "finalized": False,
                "needs_regeneration": True,
                "reason": "当前阶段没有可复用的已生成正文",
            }

        skill_md = self.director._skill_md(skill_name)
        step = ProductionSkillRegistry._single_step(skill_md)
        contract = builtin_single_pass_contract(skill_name, skill_md)
        plan = {
            "schema_version": getattr(
                __import__("app.services.director", fromlist=["NATIVE_PLAN_SCHEMA_VERSION"]),
                "NATIVE_PLAN_SCHEMA_VERSION",
                "native_plan_v2_single_step",
            ),
            "mode": "sequential",
            "steps": [step],
            "current_index": 0,
            "reason": "复用已生成正文并执行本地单次阶段收口",
            "source_sha256": contract["source_sha256"],
            "single_pass": True,
            "legacy_auto_advance": False,
            "completed_locally": True,
            "completion_mode": "single_pass_reconcile_existing_result",
            "audit": {
                "valid": True,
                "reason": "已生成正文不重新调用模型，仅本地重建阶段完成状态",
            },
        }
        terminal_target = {
            "kind": "complete_stage",
            "index": 1,
            "name": "",
            "single_pass": True,
            "reconciled_existing_result": True,
        }

        state["skill_contract"] = contract
        state["native_plan"] = plan
        state["internal_step"] = step
        state["last_native_target"] = terminal_target
        state["last_skill_runtime_control"] = {}
        state["skill_runtime"] = empty_runtime_state()
        state["stage_ready"] = False
        state["handoff"] = ""
        state["next_expected_action"] = "正在执行本地完成校验"
        project.setdefault("stage_state", {})[stage] = state
        self.director._save_project(project)

        refreshed = self.director.refresh_production_completion(project_id)
        completion = (refreshed.get("skill_runtime") or {}).get("completion") or {}
        if refreshed.get("stage_ready") is not True:
            reason = _clean(completion.get("reason")) or "本地完成校验未通过"
            if self.progress_tracker is not None and hasattr(self.progress_tracker, "needs_regeneration"):
                self.progress_tracker.needs_regeneration(project_id, stage, reason)
            return {
                "project_id": project_id,
                "stage": stage,
                "finalized": False,
                "needs_regeneration": True,
                "reason": reason,
                "completion": completion,
            }

        project = self.director.get_project(project_id)
        state = ((project.get("stage_state") or {}).get(stage) or {})
        consumer_stage, consumer_skill = self.director._handoff_consumer_skill(stage)
        consumer_skill_md = self.director._skill_md(consumer_skill)
        handoff, audit = await self.director._compile_and_audit_stage_handoff(
            project=project,
            stage=stage,
            source_skill=skill_name,
            source_skill_md=skill_md,
            consumer_skill=consumer_skill,
            consumer_skill_md=consumer_skill_md,
            final_content=content,
            draft_handoff="",
        )
        audit["consumer_stage"] = consumer_stage
        state["handoff"] = handoff
        state["last_handoff_audit"] = audit
        state["stage_ready"] = True
        state["next_expected_action"] = "确认本阶段并进入下一阶段"
        state["last_native_target"] = terminal_target
        state["native_plan"] = plan
        project["stage_state"][stage] = state
        self.director._save_project(project)

        # The story-elements panel reads ProductionAsset entities immediately
        # when Stage① reaches 100%, before manual stage confirmation. Reconcile
        # validated Story Bible roles here so the UI and downstream Stage② read
        # the same canonical character set rather than an older continuity pass.
        stage01_source_coverage: dict[str, Any] = {}
        if stage == "01":
            stage01_source_coverage = reconcile_stage01_story_characters(
                self.director,
                project_id,
                require_ready=True,
            )

        if self.progress_tracker is not None:
            self.progress_tracker.complete(project_id, stage)

        return {
            "project_id": project_id,
            "stage": stage,
            "finalized": True,
            "reused_existing_output": True,
            "model_calls": 0,
            "stage_ready": True,
            "handoff_ready": True,
            "completion": (state.get("skill_runtime") or {}).get("completion") or {},
            "stage01_source_coverage": stage01_source_coverage,
        }


def create_single_pass_finalizer_router(
    legacy_runtime: Any,
    progress_tracker: Any | None = None,
) -> APIRouter:
    router = APIRouter()
    service = SinglePassStageFinalizer(legacy_runtime, progress_tracker)

    @router.post("/api/v3/studio/projects/{project_id}/finalize-authoring-stage")
    async def finalize_authoring_stage(project_id: str) -> dict[str, Any]:
        try:
            return await service.finalize(project_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"本地完成阶段失败：{type(exc).__name__}: {exc}",
            ) from exc

    return router


__all__ = [
    "SinglePassStageFinalizer",
    "create_single_pass_finalizer_router",
]
