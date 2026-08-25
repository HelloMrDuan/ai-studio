from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = (
    ROOT
    / "deliverables"
    / "install_ai_studio_v2_39_6_3_stage04_performance_optimization.py"
)
FULL_PIPELINE_INSTALLER_PATH = (
    ROOT / "deliverables/install_ai_studio_v2_39_6_3_stage04_full_pipeline_preflight.py"
)
QWEN_COMPAT_INSTALLER_PATH = (
    ROOT / "deliverables/install_ai_studio_v2_39_6_3_qwen_request_compat.py"
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load performance installer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_installer():
    return load_module("v23963_perf_installer", INSTALLER_PATH)


def function_namespace(source: str, names: set[str]) -> dict:
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name in names
    ]
    namespace = {"_studio_json": json}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "target-main-fragment", "exec"), namespace)
    return namespace


class PerformanceOptimizationInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = load_installer()
        full_pipeline = load_module("v23963_full_pipeline_frozen", FULL_PIPELINE_INSTALLER_PATH)
        qwen_compat = load_module("v23963_qwen_compat_frozen", QWEN_COMPAT_INSTALLER_PATH)
        cls.main_baseline = full_pipeline.target(full_pipeline.FILES["app/main.py"])
        cls.gemma_baseline = qwen_compat.target_bytes()
        cls.main_target = cls.installer.build_target("app/main.py", cls.main_baseline)
        cls.gemma_target = cls.installer.build_target(
            "app/services/gemma.py", cls.gemma_baseline
        )
        cls.main_text = cls.main_target.decode("utf-8")
        cls.gemma_text = cls.gemma_target.decode("utf-8")

    def test_frozen_predecessor_payloads_are_exact_baseline(self) -> None:
        self.assertEqual(
            self.installer.sha(self.main_baseline),
            self.installer.FILES["app/main.py"]["baseline_sha256"],
        )
        self.assertEqual(
            self.installer.sha(self.gemma_baseline),
            self.installer.FILES["app/services/gemma.py"]["baseline_sha256"],
        )

    def test_partial_classification_has_one_primary_call_and_unresolved_only_repair(self) -> None:
        start = self.main_text.index("async def _studio_v2374_classify_batch(")
        end = self.main_text.index("async def _studio_v2374_classify_all(", start)
        body = self.main_text[start:end]
        self.assertEqual(
            body.count("studio_stage04_\"\n                \"batched_anchor_classification_qwen32b"),
            1,
        )
        self.assertNotIn("for attempt in range(2)", body)
        self.assertIn("plan = candidate if isinstance(candidate, dict) else {}", body)
        self.assertIn("_studio_v23963_partial_classification_plan", self.main_text)
        self.assertIn("unresolved = list", body)
        self.assertIn("requested_ids=unresolved", body)
        self.assertIn(
            '"studio_stage04_batched_anchor_classification_qwen32b": 750,',
            self.main_text,
        )

    def test_membership_repair_is_line_only_and_bounded_to_five_calls(self) -> None:
        start = self.main_text.index("async def _studio_v2374_resolve_group_membership(")
        end = self.main_text.index("async def _studio_v2374_group_batch(", start)
        body = self.main_text[start:end]
        self.assertIn("repair_call_budget = 5", body)
        self.assertIn('round_modes = (\n            ("line", 360),', body)
        self.assertNotIn('("json", 450)', body)
        self.assertNotIn('("strict", 450)', body)
        self.assertIn('enumerate(("line",), 1)', body)
        self.assertIn("consume_repair_call_budget()", body)
        self.assertIn(
            'globals()["_studio_v23963_membership_repair_calls"] = 0',
            self.main_text,
        )

    def test_truncated_classification_recovers_only_explicit_requested_ids(self) -> None:
        ns = function_namespace(
            self.main_text,
            {"_studio_v23963_partial_classification_plan"},
        )
        ns["_studio_v2374_ordered_anchors"] = lambda rows: rows
        ns["_studio_v2372d_collect_texts"] = lambda raw: [str(raw)]
        recover = ns["_studio_v23963_partial_classification_plan"]
        plan = recover(
            raw=(
                '{"beat_ids":["A1","A2"],'
                '"support_evidence_ids":["A3"'
            ),
            anchors=[{"id": key} for key in ("A1", "A2", "A3", "A4")],
        )
        self.assertEqual(plan["beat_ids"], ["A1", "A2"])
        self.assertEqual(plan["support_evidence_ids"], ["A3"])
        self.assertNotIn("A4", plan["beat_ids"] + plan["support_evidence_ids"])

    def _audit_namespace(self) -> dict:
        return function_namespace(
            self.main_text,
            {
                "_studio_v23962_audit_beats",
                "_studio_v23963_compact_audit_anchors",
                "_studio_v23963_compact_audit_beats",
                "_studio_v23963_render_audit_prompt",
                "_studio_v23963_prepare_audit_prompt",
                "_studio_v23963_schema_completion_payload",
                "_studio_v23963_prepare_schema_completion_prompt",
            },
        )

    @staticmethod
    def _audit_fixture():
        chunk = {"text": "SOURCE_ONCE"}
        anchors = [
            {"id": "A1", "start": 10, "end": 20, "text": "DUPLICATE_EVIDENCE_TEXT"},
            {"id": "A2", "start": 21, "end": 30, "text": "support detail"},
        ]
        beats = [
            {
                "summary": "state one",
                "state_change": "before to after",
                "source_evidence_ids": ["A1"],
                "source_evidence": ["DUPLICATE_EVIDENCE_TEXT"],
                "source_evidence_spans": [{"id": "A1", "start": 10, "end": 20}],
            }
        ]
        return chunk, anchors, beats, ["A2"]

    def test_audit_compaction_preserves_state_binding_and_removes_duplicate_text(self) -> None:
        ns = self._audit_namespace()
        chunk, anchors, beats, support = self._audit_fixture()
        render = ns["_studio_v23963_render_audit_prompt"]
        full = render(
            chunk=chunk, anchors=anchors, beats=beats, support_ids=support,
            compact=False,
        )
        compact = render(
            chunk=chunk, anchors=anchors, beats=beats, support_ids=support,
            compact=True,
        )
        self.assertIn("DUPLICATE_EVIDENCE_TEXT", full)
        self.assertNotIn("DUPLICATE_EVIDENCE_TEXT", compact)
        self.assertEqual(compact.count("SOURCE_ONCE"), 1)
        for value in ('"id":"A1"', '"id":"A2"', '"state":"state one"'):
            self.assertIn(value, compact)
        self.assertIn('"source_evidence_ids":["A1"]', compact)
        self.assertIn('"classification":"support"', compact)
        self.assertIn('"start":10', compact)

    def test_audit_payload_is_unchanged_below_limit_and_compacts_above_limit(self) -> None:
        ns = self._audit_namespace()
        chunk, anchors, beats, support = self._audit_fixture()
        render = ns["_studio_v23963_render_audit_prompt"]

        class Director:
            mode = "below"

            async def _count_prompt_tokens(self, *, system_prompt, messages):
                content = messages[0]["content"]
                if self.mode == "below":
                    return 6400, "llama_tokenize"
                return (
                    (7001, "llama_tokenize")
                    if "=== SOURCE_ANCHORS ===" in content
                    else (3200, "llama_tokenize")
                )

        director = Director()
        ns["director"] = director
        ns["_STUDIO_V23963_AUDIT_PROMPT_TOKEN_LIMIT"] = 6500
        prepare = ns["_studio_v23963_prepare_audit_prompt"]
        expected_full = render(
            chunk=chunk, anchors=anchors, beats=beats, support_ids=support,
            compact=False,
        )
        below = asyncio.run(prepare(
            phase="audit", system_prompt="system", chunk=chunk,
            anchors=anchors, beats=beats, support_ids=support,
        ))
        self.assertEqual(below, expected_full)

        director.mode = "above"
        above = asyncio.run(prepare(
            phase="audit", system_prompt="system", chunk=chunk,
            anchors=anchors, beats=beats, support_ids=support,
        ))
        self.assertIn("SOURCE_ANCHOR_BINDINGS_COMPACT", above)
        self.assertNotIn("DUPLICATE_EVIDENCE_TEXT", above)

    def test_schema_completion_ignores_8000_token_context_and_stays_under_6000(self) -> None:
        ns = self._audit_namespace()
        recorded = {}

        class Director:
            async def _count_prompt_tokens(self, *, system_prompt, messages):
                prompt = messages[0]["content"]
                recorded["prompt"] = prompt
                recorded["tokens"] = len(prompt.encode("utf-8")) // 3 + 128
                return recorded["tokens"], "llama_tokenize"

        ns["director"] = Director()
        ns["_STUDIO_V23963_SCHEMA_COMPLETION_TOKEN_LIMIT"] = 6000
        prepare = ns["_studio_v23963_prepare_schema_completion_prompt"]
        huge_source = "source_token " * 8000
        huge_evidence = "FULL_BEAT_EVIDENCE " * 8000
        prompt = asyncio.run(prepare(
            phase=(
                "studio_stage04_"
                "narrative_beat_audit_schema_completion_qwen32b"
            ),
            system_prompt="schema only",
            chunk={
                "scene_id": "scene-generic-001",
                "index": 2,
                "text": huge_source,
            },
            beats=[{
                "summary": "FULL_BEAT_SUMMARY_MUST_NOT_TRAVEL",
                "state_change": "FULL_BEAT_STATE_MUST_NOT_TRAVEL",
                "source_evidence_ids": ["A1", "A2"],
                "source_evidence": [huge_evidence],
                "source_evidence_spans": [
                    {"start": 10, "end": 20},
                    {"start": 10, "end": 20},
                ],
            }],
            support_ids=["A3"],
            prior_audit={
                "valid": True,
                "event_coverage_ok": True,
                "violations": [],
            },
            prior_missing=[
                "granularity_ok",
                "evidence_entailment_ok",
                "temporal_order_ok",
                "support_classification_ok",
            ],
        ))
        payload = json.loads(prompt)
        self.assertEqual(
            set(payload),
            {
                "scene_id",
                "audit_id",
                "missing_fields",
                "previous_audit_result",
                "required_schema",
            },
        )
        self.assertLessEqual(recorded["tokens"], 6000)
        self.assertEqual(payload["scene_id"], "scene-generic-001")
        self.assertEqual(
            payload["previous_audit_result"]["evidence_ids"],
            ["A3", "A1", "A2"],
        )
        self.assertEqual(
            payload["previous_audit_result"]["beat_binding"][0],
            {"beat_index": 1, "evidence_ids": ["A1", "A2"]},
        )
        self.assertEqual(
            payload["previous_audit_result"]["temporal_fields"][0],
            {"beat_index": 1, "source_start": 10, "source_end": 20},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "source_text",
            "full_anchors",
            "full_beats",
            "source_token",
            "FULL_BEAT_EVIDENCE",
            "FULL_BEAT_SUMMARY_MUST_NOT_TRAVEL",
            "FULL_BEAT_STATE_MUST_NOT_TRAVEL",
            "CORE_SOURCE_CHUNK",
            "SOURCE_ANCHORS",
            "PROPOSED_BEATS",
        ):
            self.assertNotIn(forbidden, serialized)

        schema_start = self.main_text.index(
            "async def _studio_v2372b_complete_audit_schema("
        )
        schema_end = self.main_text.index(
            "async def _studio_v2372_audit_extraction(", schema_start
        )
        schema_body = self.main_text[schema_start:schema_end]
        self.assertIn("_studio_v23963_prepare_schema_completion_prompt", schema_body)
        self.assertNotIn('"content": prompt +', schema_body)
        self.assertIn("cannot rewrite any", schema_body)

    def test_context_overflow_is_classified_for_immediate_propagation(self) -> None:
        self.assertIn("class LLMContextOverflowError", self.gemma_text)
        self.assertIn("if _is_context_overflow_response(exc.response):", self.gemma_text)
        self.assertIn('"request_attempts": attempt + 1', self.gemma_text)
        self.assertIn('"request_retries": attempt', self.gemma_text)
        self.assertLess(
            self.gemma_text.index("raise overflow from exc"),
            self.gemma_text.index("await asyncio.sleep(1)", self.gemma_text.index("async def _request_messages")),
        )

    def test_installer_retains_transactional_safety_contract(self) -> None:
        source = INSTALLER_PATH.read_text(encoding="utf-8")
        for marker in (
            "baseline SHA256 mismatch",
            "check_active_tasks(root)",
            "backup_live(root, backup)",
            "atomic_write(root / rel",
            '"-m", "py_compile"',
            "start_and_verify(root, TARGET_VERSION)",
            "target hash readback mismatch",
            "restore_exact_backup(root, backup, manifest)",
            "rollback hash mismatch",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
