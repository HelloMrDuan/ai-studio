from __future__ import annotations

import ast
import base64
import copy
import hashlib
import unittest
import zlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
RUNTIME = ROOT / "app" / "stage04_v238_runtime.py"
INSTALLER = (
    ROOT
    / "deliverables"
    / "install_ai_studio_v2_39_6_2_stage04_narrative_lineage_closure.py"
)


def _last_function(name: str):
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if not matches:
        raise AssertionError(f"function not found: {name}")
    node = copy.deepcopy(matches[-1])
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, str(MAIN), "exec"), namespace)
    return namespace[name]


def _anchors() -> list[dict]:
    return [
        {"id": "E1", "text": "先发生的状态。", "start": 10, "end": 18},
        {"id": "E2", "text": "中间状态。", "start": 30, "end": 36},
        {"id": "E3", "text": "后发生的动作。", "start": 50, "end": 58},
    ]


class NarrativeLineageClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        order_ids = _last_function("_studio_v23962_order_evidence_ids")
        close_beats = _last_function("_studio_v23962_close_validated_beats")
        close_beats.__globals__["_studio_v23962_order_evidence_ids"] = order_ids
        audit_beats = _last_function("_studio_v23962_audit_beats")
        merge_boundary = _last_function("_studio_v2374_merge_boundary")
        merge_boundary.__globals__["_studio_v2374_copy"] = copy
        cls.order_ids = staticmethod(order_ids)
        cls.close_beats = staticmethod(close_beats)
        cls.audit_beats = staticmethod(audit_beats)
        cls.merge_boundary = staticmethod(merge_boundary)

    def test_source_offset_temporal_ordering_preservation(self):
        self.assertEqual(
            self.order_ids(["E3", "E1", "E2"], anchors=_anchors()),
            ["E1", "E2", "E3"],
        )

    def test_beat_merge_preserves_evidence_lineage(self):
        merged = self.merge_boundary(
            accumulated=[{
                "summary": "先后状态闭包",
                "state_change": "先状态建立",
                "source_evidence_ids": ["E1"],
            }],
            current_rows=[{
                "summary": "先后状态闭包",
                "state_change": "后动作完成",
                "source_evidence_ids": ["E3"],
                "merge_with_previous": True,
            }],
        )
        closed = self.close_beats(merged, anchors=_anchors())
        self.assertEqual(closed[0]["source_evidence_ids"], ["E1", "E3"])
        self.assertEqual(
            closed[0]["source_evidence"],
            ["先发生的状态。", "后发生的动作。"],
        )

    def test_deterministic_pass_cannot_drop_required_evidence(self):
        closed = self.close_beats([{
            "summary": "完整状态变化",
            "state_change": "从先到后",
            "source_evidence_ids": ["E3", "E1", "E2"],
        }], anchors=_anchors())
        self.assertEqual(
            set(closed[0]["source_evidence_ids"]),
            {"E1", "E2", "E3"},
        )
        self.assertEqual(len(closed[0]["source_evidence_spans"]), 3)

    def test_beat_semantics_and_evidence_move_as_one_closure(self):
        closed = self.close_beats([
            {
                "summary": "后动作",
                "state_change": "后动作完成",
                "source_evidence_ids": ["E3"],
            },
            {
                "summary": "先状态",
                "state_change": "先状态建立",
                "source_evidence_ids": ["E1"],
            },
        ], anchors=_anchors())
        self.assertEqual(closed[0]["summary"], "先状态")
        self.assertEqual(closed[0]["source_evidence_ids"], ["E1"])
        self.assertEqual(closed[1]["summary"], "后动作")
        self.assertEqual(closed[1]["source_evidence_ids"], ["E3"])

    def test_out_of_order_model_beats_are_deterministically_reordered(self):
        model_rows = [
            {"summary": "third", "state_change": "third", "source_evidence_ids": ["E3"]},
            {"summary": "first", "state_change": "first", "source_evidence_ids": ["E1"]},
            {"summary": "second", "state_change": "second", "source_evidence_ids": ["E2"]},
        ]
        closed = self.close_beats(model_rows, anchors=_anchors())
        self.assertEqual([row["summary"] for row in closed], ["first", "second", "third"])

    def test_evidence_merge_order_is_source_order_not_append_order(self):
        closed = self.close_beats([{
            "summary": "merged",
            "state_change": "merged",
            "source_evidence_ids": ["E2", "E3", "E1", "E2"],
        }], anchors=_anchors())
        self.assertEqual(closed[0]["source_evidence_ids"], ["E1", "E2", "E3"])
        self.assertEqual(
            [span["start"] for span in closed[0]["source_evidence_spans"]],
            [10, 30, 50],
        )

    def test_cross_beat_state_evidence_isolation(self):
        with self.assertRaisesRegex(RuntimeError, "cross-Beat evidence overlap"):
            self.close_beats([
                {"summary": "a", "state_change": "a", "source_evidence_ids": ["E1"]},
                {"summary": "b", "state_change": "b", "source_evidence_ids": ["E1"]},
            ], anchors=_anchors())

    def test_narrative_audit_input_keeps_offset_lineage(self):
        closed = self.close_beats([
            {"summary": "later", "state_change": "later", "source_evidence_ids": ["E3"]},
            {"summary": "earlier", "state_change": "earlier", "source_evidence_ids": ["E1"]},
        ], anchors=_anchors())
        audit_rows = self.audit_beats(list(reversed(closed)))
        self.assertEqual([row["index"] for row in audit_rows], [1, 2])
        self.assertEqual(audit_rows[0]["summary"], "earlier")
        self.assertEqual(audit_rows[0]["source_evidence_spans"][0]["start"], 10)

    def test_deterministic_closure_adds_no_llm_calls(self):
        source = MAIN.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for name in (
            "_studio_v23962_order_evidence_ids",
            "_studio_v23962_close_validated_beats",
            "_studio_v23962_audit_beats",
        ):
            node = next(
                row for row in ast.walk(tree)
                if isinstance(row, ast.FunctionDef) and row.name == name
            )
            calls = [
                row for row in ast.walk(node)
                if isinstance(row, ast.Name)
                and "qwen" in row.id.lower()
            ]
            self.assertEqual(calls, [], name)


class NarrativeLineageStaticRegressionTests(unittest.TestCase):
    def test_v23961_and_v23910_guards_remain_present(self):
        main = MAIN.read_text(encoding="utf-8")
        runtime = RUNTIME.read_text(encoding="utf-8")
        for marker in (
            "SUPERCHUNK_NARRATIVE_AUDIT",
            "deterministic partition failed",
            "source_evidence_spans",
            "MembershipRepair",
            "2.39.6.2-stage04-narrative-lineage-closure",
        ):
            self.assertIn(marker, main + runtime)
        for marker in (
            "_merge_shot_repair_patch",
            "_missing_shot_state_fields",
            "forward_with_replayed_prefix",
            "await _scene_global_audit",
        ):
            self.assertIn(marker, runtime)

    def test_grouping_contract_requires_semantic_evidence_closure(self):
        source = MAIN.read_text(encoding="utf-8")
        self.assertIn("summary/state_change/source_evidence_ids 必须作为闭包迁移", source)
        self.assertIn("ID 必须按 source offset 递增", source)


class NarrativeLineageInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INSTALLER.read_text(encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.files = ast.literal_eval(next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "FILES"
                for target in node.targets
            )
        ))

    def test_installer_uses_exact_v23961_baseline_and_rollback(self):
        expected = {
            "app/main.py": "799818142338d46b779bd9386a1a3a2efdc2f16630f71a2378bdae07d7c617e2",
            "app/stage04_v238_runtime.py": "04e8a9bc20b2d1143d6d7046e30400fee9c023354588a03ca3c472cf4ca1eaac",
        }
        self.assertEqual(set(self.files), set(expected))
        for rel, baseline_sha in expected.items():
            spec = self.files[rel]
            rollback = zlib.decompress(base64.b85decode(spec["baseline_payload"]))
            self.assertEqual(spec["baseline_sha256"], baseline_sha)
            self.assertEqual(hashlib.sha256(rollback).hexdigest(), baseline_sha)

    def test_installer_target_payload_remains_a_valid_frozen_v23962_snapshot(self):
        for rel, spec in self.files.items():
            target = zlib.decompress(base64.b85decode(spec["target_payload"]))
            self.assertEqual(
                hashlib.sha256(target).hexdigest(),
                spec["target_sha256"],
            )
            self.assertIn(b"2.39.6.2-stage04-narrative-lineage-closure", target)

    def test_installer_is_transactional_and_runs_no_stage04(self):
        self.assertIn("baseline SHA256 mismatch", self.source)
        self.assertIn("ROLLBACK COMPLETE", self.source)
        self.assertIn("INSTALLER SELF-TEST PASS", self.source)
        self.assertNotIn('method="POST"', self.source)
        self.assertNotIn("generate-image", self.source)
        self.assertNotIn("generate-video", self.source)


if __name__ == "__main__":
    unittest.main()
