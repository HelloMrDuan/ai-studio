from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase, mock


ROOT = Path(__file__).resolve().parents[1]


def _completion(
    *, ready: bool, missing: list[str] | None = None,
    missing_requirements: list[str] | None = None,
) -> dict:
    missing = list(missing or [])
    return {
        "ready": ready,
        "required_artifact_ids": ["CHARACTER_BIBLE", "CHARACTER_CONTINUITY"],
        "missing_artifact_ids": missing,
        "active_requirement_ids": [],
        "missing_requirement_ids": list(missing_requirements or []),
    }


def _project(*, stage: str = "02", ready: bool = False, missing: list[str] | None = None) -> dict:
    return {
        "project_id": "p" * 24,
        "title": "测试项目",
        "status": "active",
        "current_stage": stage,
        "completed_stages": ["01"] if stage in {"02", "03"} else [],
        "confirmed_outputs": {
            "01": {
                "handoff": "已确认剧本",
                "confirmed_at": "2026-08-25T00:00:00+00:00",
                "production_asset_ids": ["script-1"],
            }
        },
        "stage_state": {
            stage: {
                "stage_ready": ready,
                "handoff": "角色阶段有效交接" if ready else "",
                "last_handoff_audit": {
                    "valid": ready,
                    "provenance_verified": ready,
                    "contract_version": "verbatim_evidence_v1" if ready else "",
                },
                "skill_contract": {
                    "output_groups": [{
                        "artifacts": [
                            {"artifact_id": "CHARACTER_BIBLE", "asset_type": "TEXT"},
                            {"artifact_id": "CHARACTER_CONTINUITY", "asset_type": "TEXT"},
                        ]
                    }],
                },
                "skill_runtime": {"completion": _completion(ready=ready, missing=missing)},
            }
        },
    }


class Stage02RuntimeTestBase:
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")
        cls.director_module = importlib.import_module("app.services.director")


class Stage02ExecutionPlanTests(Stage02RuntimeTestBase, IsolatedAsyncioTestCase):
    @staticmethod
    def closure(
        fingerprint: str, *, terminal: bool,
        missing_assets: list[str] | None = None,
        missing_requirements: list[str] | None = None,
        ready_artifact_ids: list[str] | None = None,
    ) -> dict:
        completion_ready = terminal
        return {
            "fingerprint": fingerprint,
            "terminal_ready": terminal,
            "missing_assets": list(missing_assets or []),
            "missing_requirements": list(missing_requirements or []),
            "completion_ready": completion_ready,
            "stage_ready": terminal,
            "handoff_state": "valid" if terminal else "missing",
            "audit_state": "valid" if terminal else "pending",
            "ready_artifact_ids": list(ready_artifact_ids or []),
            "unresolved": [] if terminal else [
                *[f"artifact:{value}" for value in (missing_assets or [])],
                *[f"requirement:{value}" for value in (missing_requirements or [])],
            ],
            "completion_reason": "ready" if terminal else "仍有缺失项",
        }

    async def test_ready_character_bible_reuses_without_qwen_or_background_task(self) -> None:
        project = _project(ready=True)
        readiness = {
            "CHARACTER_BIBLE": ["asset-bible"],
            "CHARACTER_CONTINUITY": ["asset-continuity"],
        }
        saved_jobs: list[dict] = []
        with mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main.director, "_skill_md", return_value="stage02 skill v1"), \
             mock.patch.object(self.main.director.production, "contract_asset_readiness", return_value=readiness), \
             mock.patch.object(self.main.director.production, "list_assets", return_value=[]), \
             mock.patch.object(self.main, "_studio_active_job", return_value=None), \
             mock.patch.object(self.main, "_studio_save_job", side_effect=lambda job: saved_jobs.append(dict(job))), \
             mock.patch.object(self.main.director, "_save_project"), \
             mock.patch.object(self.main.director, "message", new_callable=mock.AsyncMock) as qwen, \
             mock.patch.object(self.main._studio_asyncio, "create_task") as create_task:
            result = await self.main.studio_run_stage(project["project_id"], {"input": "继续生成角色"})

        self.assertTrue(result["reused"])
        self.assertFalse(result["background"])
        self.assertFalse(create_task.called)
        qwen.assert_not_awaited()
        perf = result["job"]["metadata"]["stage02_performance"]
        self.assertEqual(perf["summary"]["qwen_calls"], 0)
        self.assertTrue(perf["phases"][0]["cache_hit"])
        self.assertTrue(saved_jobs)

    def test_missing_contract_artifact_selects_scoped_completion_only(self) -> None:
        project = _project(ready=False, missing=["CHARACTER_CONTINUITY"])
        readiness = {"CHARACTER_BIBLE": ["asset-bible"], "CHARACTER_CONTINUITY": []}
        with mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main.director, "_skill_md", return_value="stage02 skill v1"), \
             mock.patch.object(self.main.director.production, "contract_asset_readiness", return_value=readiness), \
             mock.patch.object(self.main.director.production, "list_assets", return_value=[]):
            plan = self.main._studio_stage02_execution_plan(project["project_id"], "继续生成角色")

        self.assertEqual(plan["mode"], "scoped_completion")
        self.assertEqual(plan["character_bible_asset_ids"], ["asset-bible"])
        self.assertEqual(plan["missing_artifact_ids"], ["CHARACTER_CONTINUITY"])

    def test_ready_assets_without_valid_handoff_are_not_idempotent_terminal(self) -> None:
        project = _project(ready=True)
        project["stage_state"]["02"]["handoff"] = ""
        readiness = {
            "CHARACTER_BIBLE": ["asset-bible"],
            "CHARACTER_CONTINUITY": ["asset-continuity"],
        }
        with mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main.director, "_skill_md", return_value="stage02 skill v1"), \
             mock.patch.object(self.main.director.production, "contract_asset_readiness", return_value=readiness), \
             mock.patch.object(self.main.director.production, "list_assets", return_value=[]):
            plan = self.main._studio_stage02_execution_plan(project["project_id"], "继续生成角色")
        self.assertEqual(plan["mode"], "scoped_completion")
        self.assertFalse(plan["handoff_ready"])

    async def test_one_job_runs_20_to_100_and_scopes_the_second_round(self) -> None:
        project = _project(ready=False)
        plan = {
            "mode": "full_generation",
            "cache_key": "cache-key",
            "missing_artifact_ids": [],
            "missing_requirement_ids": [],
            "character_bible_asset_ids": [],
        }
        job = {
            "job_id": "job-1", "project_id": project["project_id"], "stage": "02",
            "status": "queued", "turns": [], "created_at": "2026-08-25T00:00:00+00:00",
            "metadata": {"stage02_execution_plan": plan},
        }
        self.main._studio_stage02_set_progress(
            job, completed_steps=1, current_step=2,
            current_step_name="判断角色生产模式",
        )
        persisted = dict(job)
        saved = [copy.deepcopy(job)]

        @asynccontextmanager
        async def gpu_use(_owner):
            yield

        @contextmanager
        def phase_telemetry(_telemetry, request_cache=None):
            yield {"request_cache": request_cache}

        async def role_mode(_project_id, *, cache_key=""):
            return {"reference_image_required": False, "cache_hit": True}

        producer_results = [
            {
                "content": "Character Bible 已生成",
                "control": {"stage_ready": False},
                "control_event": {"action": "other"},
                "skill_runtime": {"completion": _completion(
                    ready=False, missing_requirements=["CONTINUITY_RULES"],
                )},
            },
            {
                "content": "仅补齐连续性规则并完成 handoff",
                "control": {"stage_ready": True},
                "control_event": {"action": "advance"},
                "skill_runtime": {"completion": _completion(ready=True)},
            },
        ]
        closure_results = [
            self.closure(
                "fp-1", terminal=False,
                missing_requirements=["CONTINUITY_RULES"],
                ready_artifact_ids=["asset-bible"],
            ),
            self.closure("fp-2", terminal=True, ready_artifact_ids=["asset-bible"]),
        ]

        def save_job(value):
            persisted.update(value)
            saved.append(copy.deepcopy(value))

        with mock.patch.object(self.main, "_studio_load_job", side_effect=lambda _job_id: persisted), \
             mock.patch.object(self.main, "_studio_save_job", side_effect=save_job), \
             mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main, "_studio_character_role_mode", side_effect=role_mode), \
             mock.patch.object(self.main.gpu, "use", side_effect=gpu_use), \
             mock.patch.object(self.main.director, "phase_telemetry", side_effect=phase_telemetry), \
             mock.patch.object(self.main, "_studio_stage_closure_snapshot", side_effect=closure_results), \
             mock.patch.object(self.main.director, "message", new_callable=mock.AsyncMock, side_effect=producer_results) as producer, \
             mock.patch.object(self.main.director, "_save_project"):
            await self.main._studio_run_stage_job(
                job_id="job-1", project_id=project["project_id"], user_input="继续生成角色", max_turns=16,
            )

        self.assertEqual(producer.await_count, 2)
        prompt = producer.await_args_list[1].args[1]
        self.assertIn("STAGE02_SCOPED_COMPLETION", prompt)
        self.assertIn("CONTINUITY_RULES", prompt)
        self.assertIn("asset-bible", prompt)
        self.assertIn("禁止重新生成、覆盖或删除", prompt)
        self.assertEqual(persisted["status"], "waiting_confirm")
        self.assertEqual(persisted["stage02_progress"]["completed_steps"], 5)
        self.assertEqual(persisted["stage02_progress"]["percent"], 100)
        percentages = [
            int((row.get("stage02_progress") or {}).get("percent") or -1)
            for row in saved
        ]
        for expected in (20, 40, 60, 80, 100):
            self.assertIn(expected, percentages)
        eighty_rows = [
            row for row in saved
            if (row.get("stage02_progress") or {}).get("percent") == 80
        ]
        self.assertTrue(eighty_rows)
        self.assertTrue(all(row["status"] == "running" for row in eighty_rows))

    async def test_two_identical_closure_rounds_stop_without_third_qwen_call(self) -> None:
        project = _project(ready=False, missing=["CHARACTER_CONTINUITY"])
        job = {
            "job_id": "job-stalled", "project_id": project["project_id"], "stage": "02",
            "status": "queued", "turns": [], "created_at": "2026-08-25T00:00:00+00:00",
            "metadata": {"stage02_execution_plan": {
                "mode": "scoped_completion", "cache_key": "cache-key",
                "missing_artifact_ids": ["CHARACTER_CONTINUITY"],
                "missing_requirement_ids": [], "character_bible_asset_ids": ["asset-bible"],
            }},
        }
        persisted = dict(job)

        @asynccontextmanager
        async def gpu_use(_owner):
            yield

        @contextmanager
        def phase_telemetry(_telemetry, request_cache=None):
            yield {"request_cache": request_cache}

        async def role_mode(_project_id, *, cache_key=""):
            return {"reference_image_required": False, "cache_hit": True}

        stalled_result = {
            "content": "未能补齐",
            "control": {"stage_ready": False},
            "control_event": {"action": "other"},
            "skill_runtime": {"completion": _completion(
                ready=False, missing=["CHARACTER_CONTINUITY"],
            )},
        }
        stalled_closure = self.closure(
            "same-fingerprint", terminal=False,
            missing_assets=["CHARACTER_CONTINUITY"],
            ready_artifact_ids=["asset-bible"],
        )
        with mock.patch.object(self.main, "_studio_load_job", return_value=persisted), \
             mock.patch.object(self.main, "_studio_save_job", side_effect=lambda value: persisted.update(value)), \
             mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main, "_studio_character_role_mode", side_effect=role_mode), \
             mock.patch.object(self.main.gpu, "use", side_effect=gpu_use), \
             mock.patch.object(self.main.director, "phase_telemetry", side_effect=phase_telemetry), \
             mock.patch.object(self.main, "_studio_stage_closure_snapshot", side_effect=[stalled_closure, stalled_closure]), \
             mock.patch.object(self.main.director, "message", new_callable=mock.AsyncMock, return_value=stalled_result) as producer, \
             mock.patch.object(self.main.director, "_save_project"):
            await self.main._studio_run_stage_job(
                job_id="job-stalled", project_id=project["project_id"],
                user_input="继续生成角色", max_turns=16,
            )

        self.assertEqual(producer.await_count, 2)
        self.assertEqual(persisted["status"], "failed")
        self.assertEqual(persisted["failure_kind"], "stage_closure_no_progress")
        self.assertIn("CHARACTER_CONTINUITY", persisted["reason"])
        self.assertEqual(persisted["closure_round"], 2)

    async def test_stage02_and_stage03_confirm_do_not_call_qwen(self) -> None:
        for stage, next_stage in (("02", "03"), ("03", "04")):
            project = _project(stage=stage, ready=True)
            confirmed = {**project, "current_stage": next_stage}
            with self.subTest(stage=stage), \
                 mock.patch.object(self.main.director, "refresh_production_completion"), \
                 mock.patch.object(self.main.director, "get_project", return_value=project), \
                 mock.patch.object(self.main.director, "confirm_stage", new_callable=mock.AsyncMock, return_value=confirmed), \
                 mock.patch.object(self.main.director, "message", new_callable=mock.AsyncMock) as qwen:
                result = await self.main.studio_confirm_stage(project["project_id"])
            self.assertEqual(result["project"]["current_stage"], next_stage)
            qwen.assert_not_awaited()

    async def test_stage03_generation_path_remains_background_and_stage02_plan_is_not_used(self) -> None:
        project = _project(stage="03", ready=False)
        fake_task = SimpleNamespace()

        def capture_task(coro):
            coro.close()
            return fake_task

        with mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main, "_studio_active_job", return_value=None), \
             mock.patch.object(self.main, "_studio_save_job"), \
             mock.patch.object(self.main, "_studio_stage02_execution_plan") as stage02_plan, \
             mock.patch.object(self.main._studio_asyncio, "create_task", side_effect=capture_task):
            result = await self.main.studio_run_stage(project["project_id"], {"input": "生成视觉"})
        self.assertTrue(result["background"])
        stage02_plan.assert_not_called()

    async def test_stage03_worker_also_runs_until_waiting_confirm(self) -> None:
        project = _project(stage="03", ready=False)
        job = {
            "job_id": "job-stage03", "project_id": project["project_id"], "stage": "03",
            "status": "queued", "turns": [], "created_at": "2026-08-25T00:00:00+00:00",
            "metadata": {},
        }
        persisted = dict(job)

        @asynccontextmanager
        async def gpu_use(_owner):
            yield

        results = [
            {
                "content": "视觉主方案",
                "control": {"stage_ready": False}, "control_event": {"action": "other"},
                "skill_runtime": {"completion": _completion(ready=False, missing=["VISUAL_PACKAGE"])},
            },
            {
                "content": "视觉方案闭合",
                "control": {"stage_ready": True}, "control_event": {"action": "advance"},
                "skill_runtime": {"completion": _completion(ready=True)},
            },
        ]
        closures = [
            self.closure("visual-1", terminal=False, missing_assets=["VISUAL_PACKAGE"]),
            self.closure("visual-2", terminal=True),
        ]
        with mock.patch.object(self.main, "_studio_load_job", return_value=persisted), \
             mock.patch.object(self.main, "_studio_save_job", side_effect=lambda value: persisted.update(value)), \
             mock.patch.object(self.main.director, "get_project", return_value=project), \
             mock.patch.object(self.main.gpu, "use", side_effect=gpu_use), \
             mock.patch.object(self.main, "_studio_stage_closure_snapshot", side_effect=closures), \
             mock.patch.object(self.main.director, "message", new_callable=mock.AsyncMock, side_effect=results) as producer:
            await self.main._studio_run_stage_job(
                job_id="job-stage03", project_id=project["project_id"],
                user_input="生成视觉", max_turns=16,
            )

        self.assertEqual(producer.await_count, 2)
        self.assertEqual(persisted["status"], "waiting_confirm")
        self.assertIn("STAGE03_SCOPED_COMPLETION", producer.await_args_list[1].args[1])


class DirectorStage02CacheAndTelemetryTests(Stage02RuntimeTestBase, IsolatedAsyncioTestCase):
    async def test_same_turn_duplicate_structured_phase_calls_qwen_once(self) -> None:
        service = object.__new__(self.director_module.DirectorService)
        service.llm = mock.AsyncMock()
        service.llm.chat.return_value = {
            "content": '{"ok":true}',
            "llm_metrics": {
                "usage": {"prompt_tokens": 21, "completion_tokens": 4},
                "request_attempts": 1,
                "request_retries": 0,
            },
        }
        service._llm_call_budget = mock.AsyncMock(return_value={"output_tokens": 80})
        telemetry = {"phases": []}
        kwargs = {
            "phase": "reference_router",
            "messages": [{"role": "user", "content": "same"}],
            "system_prompt": "system",
            "temperature": 0.0,
            "max_tokens": 80,
            "contract": '{"ok":true}',
        }
        request_cache: dict = {}
        with service.phase_telemetry(telemetry, request_cache=request_cache):
            first = await service._structured_json_call(**kwargs)
            second = await service._structured_json_call(**kwargs)
        service.finalize_phase_telemetry(telemetry)

        self.assertEqual(first, second)
        service.llm.chat.assert_awaited_once()
        self.assertEqual(telemetry["summary"]["qwen_calls"], 1)
        self.assertEqual(telemetry["summary"]["prompt_tokens"], 21)
        self.assertEqual(telemetry["summary"]["completion_tokens"], 4)
        self.assertEqual(telemetry["summary"]["cache_hits"], 1)
        self.assertEqual(
            set(telemetry["phases"][0]),
            {
                "phase", "start_time", "end_time", "duration_ms", "qwen_calls",
                "prompt_tokens", "completion_tokens", "retry", "cache_hit",
            },
        )

    def test_stage02_scope_preserves_visual_data_for_stage03(self) -> None:
        scope = self.main._STUDIO_STAGE02_SCOPE
        self.assertEqual(
            scope["canonical_fields"],
            ["identity", "personality", "relationships", "motivation", "continuity_rules"],
        )
        self.assertIn("appearance_details", scope["stage03_consumer_fields"])
        self.assertIn("image_prompt", scope["stage03_consumer_fields"])
        self.assertEqual(scope["policy"], "preserve_existing_visual_data_and_mark_for_stage03")

    def test_stage04_runtime_bytes_are_unchanged(self) -> None:
        digest = hashlib.sha256((ROOT / "app/stage04_v238_runtime.py").read_bytes()).hexdigest()
        self.assertEqual(digest, "e668321b8eccf9f8adaf02452ffd5c9a0c1f0b890db4ca53ff28bd718fbdf332")


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))
