from __future__ import annotations

import asyncio
import copy
import unittest

from app.services.production_skills import builtin_production_skill
from app.v3.single_pass_finalizer import SinglePassStageFinalizer


class _Progress:
    def __init__(self) -> None:
        self.completed = []

    def complete(self, project_id: str, stage: str) -> None:
        self.completed.append((project_id, stage))


class _Director:
    def __init__(self) -> None:
        self.project = {
            "project_id": "demo-project",
            "current_stage": "01",
            "completed_stages": [],
            "stage_state": {"01": {}},
            "history": [
                {"stage": "01", "role": "assistant", "content": "完整故事生产圣经正文"},
            ],
        }
        self.model_calls = 0

    def get_project(self, project_id: str):
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return copy.deepcopy(self.project)

    def _save_project(self, project):
        self.project = copy.deepcopy(project)

    def _latest_stage_output(self, project, stage):
        for row in reversed(project.get("history") or []):
            if row.get("stage") == stage and row.get("role") == "assistant":
                return row.get("content") or ""
        return ""

    def _skill_md(self, skill_name: str):
        return builtin_production_skill(skill_name)

    def refresh_production_completion(self, project_id: str):
        state = self.project["stage_state"]["01"]
        state["stage_ready"] = True
        state["skill_runtime"] = {
            "completion": {
                "ready": True,
                "mode": "native_only",
                "missing_artifact_ids": [],
                "missing_requirement_ids": [],
            }
        }
        return {
            "stage_ready": True,
            "skill_runtime": copy.deepcopy(state["skill_runtime"]),
        }

    def _handoff_consumer_skill(self, stage: str):
        return ("02", "xiaoduan-character-assets")

    async def _compile_and_audit_stage_handoff(self, **kwargs):
        return (
            "【跨阶段原始证据包】\n完整故事生产圣经正文",
            {
                "valid": True,
                "provenance_verified": True,
                "contract_version": "verbatim_evidence_v1",
                "missing": [],
            },
        )


class _Legacy:
    def __init__(self, director: _Director) -> None:
        self.director = director


class SinglePassFinalizerTests(unittest.TestCase):
    def test_existing_94_percent_result_finishes_without_model_call(self):
        director = _Director()
        progress = _Progress()
        service = SinglePassStageFinalizer(_Legacy(director), progress)

        result = asyncio.run(service.finalize("demo-project"))

        self.assertTrue(result["finalized"])
        self.assertTrue(result["reused_existing_output"])
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(director.model_calls, 0)
        state = director.project["stage_state"]["01"]
        self.assertTrue(state["stage_ready"])
        self.assertEqual(state["last_native_target"]["kind"], "complete_stage")
        self.assertTrue(state["handoff"])
        self.assertEqual(progress.completed, [("demo-project", "01")])


if __name__ == "__main__":
    unittest.main()
