from __future__ import annotations

import ast
import asyncio
import base64
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
CONFIG = ROOT / "app" / "config.py"
GEMMA = ROOT / "app" / "services" / "gemma.py"
RUNTIME = ROOT / "app" / "stage04_v238_runtime.py"
START_LLM = ROOT / "scripts" / "start_llm.sh"
INSTALLER = ROOT / "deliverables" / "install_ai_studio_v2_39_6_qwen_runtime_contract.py"
VERIFIER = ROOT / "deliverables" / "verify_ai_studio_v2_39_6_real_e2e.py"


def _module_function(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected one {name}, found {len(matches)}")
    node = copy.deepcopy(matches[0])
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class QwenContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.contract = _module_function(
            MAIN, "_studio_v2396_qwen_runtime_contract"
        )
        self.selected = {
            "id": "qwen3-32b-abliterated",
            "alias": "qwen3-32b",
            "path": "/models/qwen.gguf",
            "installed": True,
        }
        self.status = {
            "ready": True,
            "resolved_model": "qwen3-32b",
            "models": ["qwen3-32b"],
        }

    def _bind(self, selected=None, status=None, chat=None):
        fn = self.contract
        fn.__globals__.update(
            settings=SimpleNamespace(
                stage04_required_model_id="qwen3-32b-abliterated",
                stage04_required_model_alias="qwen3-32b",
            ),
            llm_registry=SimpleNamespace(
                selected_model=lambda: selected or dict(self.selected)
            ),
            gemma=SimpleNamespace(
                status=AsyncMock(return_value=status or dict(self.status)),
                chat=AsyncMock(
                    return_value=chat
                    or {"content": "QWEN_OK", "model": "qwen3-32b"}
                ),
            ),
        )
        return fn

    async def test_exact_contract_and_chat_response_pass(self):
        result = await self._bind()(verify_chat_response=True)
        self.assertEqual(result["selected_model_id"], "qwen3-32b-abliterated")
        self.assertEqual(result["response_model"], "qwen3-32b")

    async def test_wrong_selected_model_fails_closed(self):
        selected = {**self.selected, "id": "gemma-4-31b", "alias": "gemma"}
        with self.assertRaisesRegex(RuntimeError, "已选择模型不是"):
            await self._bind(selected=selected)()

    async def test_wrong_resolved_model_fails_closed(self):
        status = {**self.status, "resolved_model": "gemma"}
        with self.assertRaisesRegex(RuntimeError, "resolved model"):
            await self._bind(status=status)()

    async def test_models_contains_qwen_is_not_sufficient(self):
        status = {**self.status, "models": ["gemma", "qwen3-32b"]}
        with self.assertRaisesRegex(RuntimeError, "必须且只能"):
            await self._bind(status=status)()

    async def test_wrong_chat_response_model_fails_closed(self):
        chat = {"content": "QWEN_OK", "model": "gemma"}
        with self.assertRaisesRegex(RuntimeError, "chat response model"):
            await self._bind(chat=chat)(verify_chat_response=True)

    async def test_missing_gguf_fails_closed(self):
        selected = {**self.selected, "installed": False}
        with self.assertRaisesRegex(RuntimeError, "GGUF 不存在"):
            await self._bind(selected=selected)()


class TimeoutContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validate = _module_function(CONFIG, "validate_llm_timeout_contract")

    def test_timeout_relation_accepts_startup_plus_margin(self):
        value = SimpleNamespace(
            gemma_start_timeout_seconds=600,
            llm_startup_timeout_margin_seconds=60,
            gpu_switch_timeout_seconds=660,
        )
        self.assertIs(self.validate(value), value)

    def test_timeout_relation_rejects_outer_timeout_too_short(self):
        value = SimpleNamespace(
            gemma_start_timeout_seconds=600,
            llm_startup_timeout_margin_seconds=60,
            gpu_switch_timeout_seconds=659,
        )
        with self.assertRaisesRegex(ValueError, "659"):
            self.validate(value)


class StartupRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_stage04_selection_reconciliation_is_serialized(self):
        ensure = _module_function(MAIN, "_ensure_selected_llm_loaded")
        owner = SimpleNamespace(gemma="gemma")
        state = SimpleNamespace(
            owner="gemma",
            phase="READY",
            active_tasks={"gemma": 0},
        )
        model_loaded = False
        reload_calls = 0

        async def status_payload():
            return {
                "matches_selection": model_loaded,
                "selected_model": {"alias": "qwen3-32b"},
                "active_alias": "qwen3-32b" if model_loaded else "gemma",
            }

        async def reload_owner(_target):
            nonlocal model_loaded, reload_calls
            await asyncio.sleep(0)
            reload_calls += 1
            model_loaded = True

        ensure.__globals__.update(
            _llm_activation_lock=asyncio.Lock(),
            GPUOwner=owner,
            gpu=SimpleNamespace(
                snapshot=AsyncMock(return_value=state),
                reload_owner=reload_owner,
            ),
            _llm_status_payload=status_payload,
        )
        await asyncio.gather(ensure(), ensure())
        self.assertEqual(reload_calls, 1)


class StartScriptFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        source = START_LLM.read_text(encoding="utf-8")
        match = re.search(
            r"python3 - \"\$REGISTRY\" \"\$SELECTION\".*?<<'PY'\n(.*?)\nPY\n",
            source,
            re.S,
        )
        if not match:
            raise AssertionError("start_llm.sh model resolver heredoc not found")
        cls.resolver = match.group(1)

    def _run_resolver(self, registry: dict, selection: dict):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)
            registry_path = root / "registry.json"
            selection_path = root / "selection.json"
            registry_path.write_text(json.dumps(registry), encoding="utf-8")
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self.resolver,
                    str(registry_path),
                    str(selection_path),
                    "qwen3-32b-abliterated",
                    "qwen3-32b",
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_selected_qwen_missing_registry_entry_does_not_fallback(self):
        registry = {
            "default_model": "gemma-4-31b",
            "models": [
                {
                    "id": "gemma-4-31b",
                    "alias": "gemma",
                    "path": str(Path(__file__)),
                }
            ],
        }
        result = self._run_resolver(
            registry, {"selected_model": "qwen3-32b-abliterated"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("registry entry is missing", result.stderr)

    def test_selected_qwen_wrong_alias_does_not_fallback(self):
        registry = {
            "default_model": "gemma-4-31b",
            "models": [
                {
                    "id": "qwen3-32b-abliterated",
                    "alias": "gemma",
                    "path": str(Path(__file__)),
                },
                {
                    "id": "gemma-4-31b",
                    "alias": "gemma",
                    "path": str(Path(__file__)),
                },
            ],
        }
        result = self._run_resolver(
            registry, {"selected_model": "qwen3-32b-abliterated"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("alias mismatch", result.stderr)

    def test_selected_qwen_missing_gguf_does_not_fallback(self):
        registry = {
            "default_model": "gemma-4-31b",
            "models": [
                {
                    "id": "qwen3-32b-abliterated",
                    "alias": "qwen3-32b",
                    "path": "definitely-missing-qwen.gguf",
                },
                {
                    "id": "gemma-4-31b",
                    "alias": "gemma",
                    "path": str(Path(__file__)),
                },
            ],
        }
        result = self._run_resolver(
            registry, {"selected_model": "qwen3-32b-abliterated"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)

    def test_no_explicit_selection_preserves_legacy_default_fallback(self):
        registry = {
            "default_model": "gemma-4-31b",
            "models": [
                {
                    "id": "gemma-4-31b",
                    "alias": "gemma",
                    "path": str(Path(__file__)),
                }
            ],
        }
        result = self._run_resolver(registry, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], "gemma-4-31b")


class StaticRegressionTests(unittest.TestCase):
    def test_single_qwen_wrapper_uses_direct_chat(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        wrappers = [
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_studio_v2371a_qwen_call"
        ]
        self.assertEqual(len(wrappers), 1)
        text = ast.unparse(wrappers[0])
        self.assertIn("director.llm.chat", text)
        called_attributes = {
            node.func.attr
            for node in ast.walk(wrappers[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("_structured_json_call", called_attributes)
        self.assertIn("stage04_phase_caps", text)
        self.assertIn("response_model", text)

    def test_stage04_preflight_precedes_background_task_creation(self):
        source = MAIN.read_text(encoding="utf-8")
        endpoint = ast.get_source_segment(
            source,
            next(
                node
                for node in ast.parse(source).body
                if isinstance(node, ast.AsyncFunctionDef)
                and node.name == "studio_rebuild_stage04_production"
            ),
        )
        self.assertLess(
            endpoint.index("await _studio_v2396_prepare_stage04_qwen()"),
            endpoint.index("create_task"),
        )

    def test_stage04_runtime_call_graph_still_uses_qwen_wrapper(self):
        source = RUNTIME.read_text(encoding="utf-8")
        self.assertIn('env.get("_studio_v2371a_qwen_call")', source)
        self.assertNotIn("director.llm.chat", source)

    def test_v2395_semantic_protections_are_present(self):
        source = RUNTIME.read_text(encoding="utf-8")
        required = (
            "forward_with_replayed_prefix",
            "forward-overlap novel suffix ID 投影失败",
            "adjacent_projection",
            "source_evidence_spans",
            "representative_state",
            "video_start_state",
            "video_end_state",
            "image_prompt",
            "video_start_prompt",
            "video_prompt",
            "scene_global_audit",
            "evidence_locked_repair",
            "Classify ORIGINAL adjacent Beats only. No mutation and no merged text.",
            "Complete structural fields without regenerating the semantic Shot.",
            "Cross-batch boundary is mandatory and independently audited.",
        )
        for marker in required:
            self.assertIn(marker, source)

    def test_no_story_specific_hardcoding(self):
        combined = MAIN.read_text(encoding="utf-8") + RUNTIME.read_text(
            encoding="utf-8"
        )
        for marker in ("孙悟空", "猪八戒", "唐僧", "西游记"):
            self.assertNotIn(marker, combined)

    def test_chat_uses_actual_response_model(self):
        source = GEMMA.read_text(encoding="utf-8")
        self.assertIn('response_model = _text(body.get("model"))', source)
        self.assertIn("return self._extract_content(body), response_model, metrics", source)

    def test_performance_observability_does_not_remove_semantic_guards(self):
        main_source = MAIN.read_text(encoding="utf-8")
        runtime_source = RUNTIME.read_text(encoding="utf-8")
        for marker in (
            "stage04-perf-v1",
            "workspace_start_seconds",
            "qwen_ready_wait_seconds",
            "anchor_classification_batch",
            "anchor_extraction",
            "shot_batch",
            "STAGE04_PERF_PHASE",
            "_studio_v2396_qwen_contract_cached",
            "qwen_contract_verified",
        ):
            self.assertIn(marker, main_source + runtime_source)
        self.assertIn("await _scene_global_audit", runtime_source)
        self.assertIn("await _repair_batch", runtime_source)
        self.assertIn("await _ensure_batch_coverage", runtime_source)


class PerformanceProfilerTests(unittest.TestCase):
    def test_contract_cache_requires_verified_rebuild_wide_workspace_guard(self):
        from app import stage04_v238_runtime as runtime

        profile = {
            "qwen_contract_verified": True,
            "_workspace_guard_active": True,
        }
        token = runtime._PERF_CONTEXT.set(profile)
        try:
            self.assertTrue(runtime._perf_contract_cached())
            profile["_workspace_guard_active"] = False
            self.assertFalse(runtime._perf_contract_cached())
        finally:
            runtime._PERF_CONTEXT.reset(token)

    def test_real_usage_timings_and_retries_are_aggregated_by_phase(self):
        from app import stage04_v238_runtime as runtime

        profile = {
            "phases": {},
            "categories": {},
            "repairs": {},
            "scenes": [],
            "llm_calls": 0,
            "llm_retries": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        token = runtime._PERF_CONTEXT.set(profile)
        try:
            runtime._perf_record_llm(
                phase="studio_stage04_batched_anchor_classification_qwen32b",
                seconds=4.0,
                result={
                    "llm_metrics": {
                        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
                        "timings": {
                            "prompt_n": 999,
                            "predicted_n": 999,
                            "prompt_ms": 500,
                            "predicted_ms": 250,
                        },
                        "request_attempts": 2,
                        "request_retries": 1,
                    }
                },
            )
            runtime._perf_observe(
                "anchor_classification_batch",
                4.2,
                anchor_count=8,
            )
            final = runtime._perf_finalize(profile, 5.0)
        finally:
            runtime._PERF_CONTEXT.reset(token)

        phase = final["phases"]["studio_stage04_batched_anchor_classification_qwen32b"]
        self.assertEqual(phase["calls"], 2)
        self.assertEqual(phase["retries"], 1)
        self.assertEqual(phase["input_tokens"], 120)
        self.assertEqual(phase["output_tokens"], 30)
        self.assertEqual(phase["token_sources"], ["usage"])
        self.assertEqual(phase["server_total_seconds"], 0.75)
        category = final["categories"]["anchor_classification"]
        self.assertEqual(category["batch_count"], 1)
        self.assertEqual(category["retry_count"], 1)
        self.assertEqual(final["llm_calls"], 2)

    def test_llama_timings_are_used_when_usage_is_absent(self):
        from app import stage04_v238_runtime as runtime

        profile = {"phases": {}, "categories": {}, "repairs": {}}
        token = runtime._PERF_CONTEXT.set(profile)
        try:
            runtime._perf_record_llm(
                phase="studio_stage04_direct_shot_generation_qwen32b",
                seconds=2.0,
                result={
                    "llm_metrics": {
                        "usage": {},
                        "timings": {"prompt_n": 80, "predicted_n": 20},
                        "request_attempts": 1,
                        "request_retries": 0,
                    }
                },
            )
        finally:
            runtime._PERF_CONTEXT.reset(token)
        phase = profile["phases"]["studio_stage04_direct_shot_generation_qwen32b"]
        self.assertEqual((phase["input_tokens"], phase["output_tokens"]), (80, 20))
        self.assertEqual(phase["token_sources"], ["timings"])


class DeliverableTests(unittest.TestCase):
    def test_installer_embeds_frozen_v2396_targets(self):
        tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "FILES" for target in node.targets)
        )
        files = ast.literal_eval(assignment.value)
        self.assertEqual(
            set(files),
            {
                "app/config.py",
                "app/services/gemma.py",
                "app/main.py",
                "app/stage04_v238_runtime.py",
                "scripts/start_llm.sh",
                ".env.example",
            },
        )
        frozen_targets = {
            ".env.example": "3eaab61ed003ada6c2123072f81ae0b0cd947a0baaa981b1e19940169e1da4d0",
            "app/config.py": "5604ea7ded64174082ecd9686dd192fae509b98f305a55970bc1241bc8184698",
            "app/main.py": "c43c0778af97a443ba593e32ebb9b71c24a51bec3a226913c491773534976db7",
            "app/services/gemma.py": "daba394e03dc906be3957619bf4bb90b2980b04eafb9ce0cca5f8f5527de7876",
            "app/stage04_v238_runtime.py": "3d5ece6055f5e3341256818d6e76f480403c0aa946e94316b98d432c11bfa2e7",
            "scripts/start_llm.sh": "8871eec1e0f2c150af70144b628470b7b1996dfe4ab63d5bbdca1b2a20d60e15",
        }
        for rel, spec in files.items():
            payload = zlib.decompress(base64.b85decode(spec["payload"]))
            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(digest, frozen_targets[rel])
            self.assertEqual(digest, spec["target_sha256"])

    def test_installer_cannot_submit_business_tasks(self):
        source = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn('method="POST"', source)
        self.assertNotIn("generate-image", source)
        self.assertNotIn("generate-video", source)
        for marker in ("baseline SHA256 mismatch", "check_active_tasks", "py_compile", "bash", "ROLLBACK COMPLETE"):
            self.assertIn(marker, source)

    def test_real_verifier_requires_explicit_confirmation(self):
        source = VERIFIER.read_text(encoding="utf-8")
        self.assertIn("COLD_START_AND_REBUILD_STAGE04", source)
        self.assertIn("semantic_assertions", source)
        self.assertIn("validate_performance", source)
        self.assertIn("《V2.39.7 Stage04 性能优化建议》", source)
        self.assertIn("预计收益", source)
        self.assertIn("是否改变语义", source)
        self.assertNotIn("generate-image", source)
        self.assertNotIn("generate-video", source)


if __name__ == "__main__":
    unittest.main()
