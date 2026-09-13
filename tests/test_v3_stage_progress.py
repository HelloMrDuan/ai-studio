from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.v3.authoring_progress import AuthoringStageProgressTracker
from app.v3.stage_progress import StageProgressTracker


class _Director:
    def __init__(self) -> None:
        self.project = {
            "project_id": "a" * 24,
            "current_stage": "01",
            "completed_stages": [],
            "status": "active",
            "stage_state": {
                "01": {
                    "stage_ready": False,
                    "skill_runtime": {"completion": {"ready": False}},
                }
            },
        }

    def get_project(self, project_id: str) -> dict[str, Any]:
        if project_id != self.project["project_id"]:
            raise FileNotFoundError(project_id)
        return self.project

    async def _tracked_llm_chat(
        self,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        return {"content": "ok", "phase": phase}

    async def message(
        self,
        project_id: str,
        user_text: str,
        *,
        native_control_action: str = "",
    ) -> dict[str, Any]:
        await self._tracked_llm_chat(
            phase="director_orchestrator_content",
            messages=[{"role": "user", "content": user_text}],
            system_prompt="system",
            temperature=0.5,
            max_tokens=100,
        )
        await self._tracked_llm_chat(
            phase="director_orchestrator_control",
            messages=[{"role": "user", "content": "control"}],
            system_prompt="control",
            temperature=0.0,
            max_tokens=100,
        )
        self.project["completed_stages"] = ["01"]
        self.project["stage_state"]["01"]["stage_ready"] = True
        self.project["stage_state"]["01"]["skill_runtime"]["completion"]["ready"] = True
        return {"ok": True}


class StageProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_story_stage_exposes_requested_semantic_substeps_and_eta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            director = _Director()
            tracker = StageProgressTracker(SimpleNamespace(data_dir=Path(tmp)), director)
            tracker.begin(director.project["project_id"], "01", input_chars=1975)
            tracker.phase_start(director.project["project_id"], "01", "director_orchestrator_content")
            snapshot = tracker.snapshot(director.project["project_id"])

            names = [row["name"] for row in snapshot["steps"]]
            self.assertIn("故事事实解析", names)
            self.assertIn("剧情节点", names)
            self.assertIn("角色 / 地点 / 道具实体", names)
            self.assertIn("因果与连续性", names)
            self.assertIn("创作计划", names)
            self.assertGreaterEqual(snapshot["overall_percent"], 12)
            self.assertIsNotNone(snapshot["estimated_remaining_low_seconds"])
            self.assertIsNotNone(snapshot["estimated_remaining_high_seconds"])
            self.assertTrue(snapshot["eta_is_estimate"])

    async def test_installed_tracker_follows_real_message_boundaries_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            director = _Director()
            tracker = StageProgressTracker(SimpleNamespace(data_dir=Path(tmp)), director)
            tracker.install()

            await director.message(director.project["project_id"], "一段用于全链路验收的小说文本")
            snapshot = tracker.snapshot(director.project["project_id"])

            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["overall_percent"], 100.0)
            self.assertTrue(all(row["state"] == "completed" for row in snapshot["steps"]))

    async def test_unfinished_turn_waits_instead_of_faking_continued_progress(self) -> None:
        class WaitingDirector(_Director):
            async def message(self, project_id: str, user_text: str, *, native_control_action: str = "") -> dict[str, Any]:
                await self._tracked_llm_chat(
                    phase="director_orchestrator_content",
                    messages=[{"role": "user", "content": user_text}],
                    system_prompt="system",
                    temperature=0.5,
                    max_tokens=100,
                )
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            director = WaitingDirector()
            tracker = StageProgressTracker(SimpleNamespace(data_dir=Path(tmp)), director)
            tracker.install()
            await director.message(director.project["project_id"], "继续")
            first = tracker.snapshot(director.project["project_id"])
            self.assertEqual(first["status"], "waiting")
            frozen = first["overall_percent"]
            second = tracker.snapshot(director.project["project_id"])
            self.assertEqual(second["overall_percent"], frozen)

    async def test_recovered_waiting_stage_freezes_real_execution_time_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            director = _Director()
            tracker = AuthoringStageProgressTracker(SimpleNamespace(data_dir=Path(tmp)), director)
            project_id = director.project["project_id"]
            tracker.begin(project_id, "01", input_chars=466)
            record = tracker._read(project_id)
            self.assertIsNotNone(record)
            record["started_at"] = "2026-09-09T06:00:00+00:00"
            record["status"] = "waiting"
            record["current_phase"] = "waiting_next_internal_step"
            record["updated_at"] = "2026-09-09T06:02:15+00:00"
            tracker._path(project_id).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

            tracker.complete(project_id, "01")
            first = tracker.snapshot(project_id)
            second = tracker.snapshot(project_id)

            self.assertEqual(first["elapsed_seconds"], 135.0)
            self.assertEqual(second["elapsed_seconds"], 135.0)
            self.assertTrue(first["elapsed_is_frozen"])
            self.assertTrue(first["excluded_idle_wait"])


if __name__ == "__main__":
    unittest.main()
