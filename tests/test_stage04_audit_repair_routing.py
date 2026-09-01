from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from app import stage04_v238_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    ROOT
    / "tests/fixtures/e491294_stage04_state_order_scene1_beat2_shot1.json"
)


def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class AuditViolationRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_known_violation_types_have_canonical_codes(self) -> None:
        expected = {
            "state_order_violation": "state_order",
            "evidence_entailment": "evidence_entailment",
            "no_result_duplication": "no_result_duplication",
            "redundant_representation": "redundant_representation",
            "representative_state": "representative_state",
            "entity_visibility": "entity_visibility",
            "future_preconsumption": "future_preconsumption",
            "beat_coverage": "beat_coverage",
        }
        for raw, canonical in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    runtime._canonical_audit_code({"type": raw}),
                    canonical,
                )
                self.assertNotEqual(canonical, "unknown")
        self.assertEqual(
            runtime._canonical_audit_code({
                "code": "unknown",
                "type": "state_order_violation",
            }),
            "state_order",
        )

    def test_state_order_repairs_only_failed_end_transition(self) -> None:
        issue = fixture()["audit"]["violations"]
        self.assertEqual(
            runtime._repair_fields_for_issues(issue),
            ("video_end_state",),
        )

    async def test_real_fixture_empty_end_routes_to_regroup_once(self) -> None:
        data = fixture()
        calls: list[dict] = []

        async def qwen_call(**kwargs):
            calls.append(kwargs)
            parsed = {"patch": data["repair_patch"]}
            return parsed, parsed, {}

        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            await runtime._repair_batch(
                {"_studio_v2371a_qwen_call": qwen_call},
                current_rows=[data["pre_repair_shot"]],
                audit=data["audit"],
                source_window=data["source"],
                anchors=[data["anchor"]],
                compact_beats=[data["covered_beat"]],
                previous_shot=None,
                next_beat=None,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["contract"], '{"patch":{"video_end_state":""}}')
        prompt = calls[0]["messages"][0]["content"]
        self.assertIn("representative_state_to_video_end_state", prompt)
        self.assertIn("physical/visual state", calls[0]["system_prompt"])

        metadata = captured.exception.metadata
        self.assertEqual(metadata["failed_rules"], ["state_order"])
        self.assertNotIn("unknown", metadata["failed_rules"])
        self.assertEqual(
            metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )
        self.assertEqual(metadata["evidence_sufficiency"], "insufficient")
        self.assertEqual(metadata["source_evidence"], ["途中遇到守护神兽，"])
        self.assertEqual(metadata["evidence_ids"], ["C01E002"])
        self.assertEqual(metadata["source_spans"], data["pre_repair_shot"]["source_evidence_spans"])
        self.assertEqual(metadata["covered_beat"], [data["covered_beat"]])
        self.assertEqual(metadata["raw_violations"], data["audit"]["violations"])
        self.assertEqual(metadata["repair_patch"], {"video_end_state": ""})
        self.assertEqual(metadata["repair_changed_fields"], [])
        self.assertEqual(
            metadata["pre_repair_states"],
            metadata["post_repair_states"],
        )
        self.assertTrue(metadata["regroup_reason"])

    async def test_sufficient_evidence_changes_only_video_end_state(self) -> None:
        data = fixture()
        current = dict(data["pre_repair_shot"])
        evidence = {
            "id": "E-END",
            "text": "守护神兽点头认可少年。",
            "start": 24,
            "end": 35,
        }
        current["source_evidence_ids"] = ["E-END"]
        current["source_evidence"] = [evidence["text"]]
        current["source_evidence_spans"] = [dict(evidence)]
        beat = {
            **data["covered_beat"],
            "source_evidence_ids": ["E-END"],
            "allowed_source_evidence_ids": ["E-END"],
            "source_evidence": [evidence["text"]],
            "source_evidence_spans": [dict(evidence)],
        }
        expected_end = "守护神兽点头认可少年。"

        async def qwen_call(**_kwargs):
            parsed = {"patch": {"video_end_state": expected_end}}
            return parsed, parsed, {}

        repaired = await runtime._repair_batch(
            {"_studio_v2371a_qwen_call": qwen_call},
            current_rows=[current],
            audit=data["audit"],
            source_window=evidence["text"],
            anchors=[evidence],
            compact_beats=[beat],
            previous_shot=None,
            next_beat=None,
        )
        row = repaired[0]
        self.assertEqual(row["video_end_state"], expected_end)
        self.assertEqual(
            row["video_start_state"],
            current["video_start_state"],
        )
        self.assertEqual(
            row["representative_state"],
            current["representative_state"],
        )
        self.assertEqual(row["image_prompt"], current["representative_state"])
        self.assertEqual(row["video_start_prompt"], current["video_start_state"])
        self.assertIn(expected_end, row["video_prompt"])
        diagnostics = row["_directional_repair_diagnostics"]
        self.assertEqual(diagnostics["repair_changed_fields"], ["video_end_state"])
        self.assertEqual(
            diagnostics["evidence_sufficiency"],
            "sufficient_for_scoped_repair",
        )

    async def test_nonempty_no_progress_stops_after_one_request(self) -> None:
        data = fixture()
        calls = 0

        async def qwen_call(**_kwargs):
            nonlocal calls
            calls += 1
            parsed = {
                "patch": {
                    "video_end_state":
                        data["pre_repair_shot"]["video_end_state"],
                }
            }
            return parsed, parsed, {}

        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            await runtime._repair_batch(
                {"_studio_v2371a_qwen_call": qwen_call},
                current_rows=[data["pre_repair_shot"]],
                audit=data["audit"],
                source_window=data["source"],
                anchors=[data["anchor"]],
                compact_beats=[data["covered_beat"]],
                previous_shot=None,
                next_beat=None,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )

    async def test_beat_coverage_routes_without_qwen(self) -> None:
        data = fixture()

        async def qwen_call(**_kwargs):
            self.fail("Beat coverage must not enter locked directional Qwen repair")

        audit = {
            "valid": False,
            "violations": [{"type": "beat_coverage_violation"}],
        }
        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            await runtime._repair_batch(
                {"_studio_v2371a_qwen_call": qwen_call},
                current_rows=[data["pre_repair_shot"]],
                audit=audit,
                source_window=data["source"],
                anchors=[data["anchor"]],
                compact_beats=[data["covered_beat"]],
                previous_shot=None,
                next_beat=None,
            )
        self.assertEqual(captured.exception.metadata["failed_rules"], ["beat_coverage"])
        self.assertFalse(runtime._audit_ok({}, data["audit"]))

    async def test_same_violation_is_not_repaired_twice(self) -> None:
        data = fixture()
        current = dict(data["pre_repair_shot"])
        changed = {
            **current,
            "video_end_state": "守护神兽停在少年面前。",
            "_directional_repair_diagnostics": {
                "failed_rules": ["state_order"],
                "repair_progress": "semantic_fields_changed",
                "source_evidence": current["source_evidence"],
                "covered_beat": [data["covered_beat"]],
            },
        }
        audit_calls = 0

        async def audit_fn(**_kwargs):
            nonlocal audit_calls
            audit_calls += 1
            return data["audit"]

        async def keep_rows(_env, *, rows, **_kwargs):
            return rows

        repair = mock.AsyncMock(return_value=[changed])
        env = {
            "_studio_v2371e_batch_evidence": lambda **_kwargs: (
                data["source"],
                [data["anchor"]],
                {2: ["C01E002"]},
            ),
            "_studio_v2371_audit_batch": audit_fn,
        }
        with (
            mock.patch.object(
                runtime,
                "_generate_missing_beat_shots",
                mock.AsyncMock(return_value=[current]),
            ),
            mock.patch.object(runtime, "_ensure_batch_coverage", keep_rows),
            mock.patch.object(runtime, "_repair_batch", repair),
            mock.patch.object(
                runtime,
                "validate_rows",
                side_effect=lambda _env, *, raw_rows, **_kwargs: raw_rows,
            ),
        ):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._produce_batch(
                    env,
                    batch=[data["covered_beat"]],
                    all_beats=[data["covered_beat"]],
                    batch_index=0,
                    batch_total=1,
                    source=data["source"],
                    scene={"scene_id": data["origin"]["scene_id"], "episode_id": "ep"},
                    scene_index=1,
                    scene_total=1,
                    previous_shot=None,
                    allowed_chars=set(),
                    allowed_props=set(),
                    entity_rows=[],
                    resolved_text="",
                    character_anchor="",
                    visual_anchor="",
                    user_input="",
                )

        self.assertEqual(repair.await_count, 1)
        # The unified controller performs one final strict audit before its
        # single target-only regeneration budget; it never repeats the same
        # directional repair.
        self.assertEqual(audit_calls, 3)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "controller_rejected",
        )


if __name__ == "__main__":
    unittest.main()
