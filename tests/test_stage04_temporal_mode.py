from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

from app import stage04_v238_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
REAL_FIXTURE = (
    ROOT / "tests/fixtures/ee229ac_stage04_static_outcome_scene1_beat3.json"
)
GENERIC_FIXTURE = (
    ROOT / "tests/fixtures/stage04_temporal_mode_generic_cases.json"
)


def _anchor(text: str, anchor_id: str = "E001", start: int = 0) -> dict:
    return {
        "id": anchor_id,
        "text": text,
        "source_start": start,
        "source_end": start + len(text),
    }


def _beat(text: str, evidence_ids: list[str] | None = None) -> dict:
    ids = evidence_ids or ["E001"]
    return {
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ids,
        "source_evidence_ids": ids,
    }


def _static_row(text: str, evidence_ids: list[str] | None = None) -> dict:
    ids = evidence_ids or ["E001"]
    return {
        "title": "稳定结果",
        "duration_seconds": 3,
        "summary": "模型不应把表现推断留在摘要",
        "action": "模型不应把表现推断留在动作",
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": (
            "selected evidence states an established result but not the action "
            "or process by which that result becomes visible"
        ),
        "temporal_mode_evidence_ids": ids,
        "source_fact": text,
        "narrative_start_state": text,
        "narrative_state": text,
        "narrative_end_state": text,
        "visual_realization": "稳定叙事状态的中景构图，主体关系和剧情事实不变。",
        "realization_scope": "presentation_only",
        "realization_assumptions": ["使用中景构图和缓慢镜头运动表现既成状态"],
        "visual_start_frame": "中景固定构图，稳定状态已经成立，镜头尚未移动。",
        "representative_frame": "中景缓慢推近，稳定状态不变，构图略微收紧。",
        "visual_end_frame": "较紧中景停住，稳定状态不变，环境光影轻微变化。",
        "visual_motion": "镜头缓慢推近并停住；只允许环境光影轻微变化。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ids,
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _observable_row(text: str) -> dict:
    return {
        "title": "可见转移",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "evidence explicitly describes opening then entering",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "video_start_state": "门仍在打开，人物站在门外。",
        "representative_state": "门已经打开，人物正跨过门槛。",
        "video_end_state": "人物已经进入门内。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _validate(row: dict, text: str, *, anchors: list[dict] | None = None) -> dict:
    anchor_rows = anchors or [_anchor(text)]
    ids = [item["id"] for item in anchor_rows]
    return runtime.validate_rows(
        {},
        raw_rows=[row],
        compact_beats=[_beat(text, ids)],
        allowed_chars=set(),
        allowed_props=set(),
        anchors=anchor_rows,
        scene_id="scene-1",
        episode_id="episode-1",
    )[0]


class Stage04TemporalModeContractTests(unittest.TestCase):
    def test_generic_fixtures_are_schema_classifications_not_keyword_rules(self) -> None:
        cases = json.loads(GENERIC_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            [case["expected_temporal_mode"] for case in cases],
            ["static_outcome"] * 4 + ["observable_transition"],
        )
        source = (ROOT / "app/stage04_v238_runtime.py").read_text(encoding="utf-8")
        for case in cases:
            self.assertNotIn(case["source_evidence"], source)

    def test_observable_transition_keeps_three_distinct_narrative_states(self) -> None:
        text = "门缓缓打开，他走了进去。"
        row = _validate(_observable_row(text), text)
        self.assertEqual(row["temporal_mode"], "observable_transition")
        self.assertEqual(len({row[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS}), 3)

    def test_static_outcome_does_not_force_narrative_transition(self) -> None:
        text = "众人认可了他的决定。"
        row = _validate(_static_row(text), text)
        self.assertEqual(row["temporal_mode"], "static_outcome")
        self.assertEqual(
            {row[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )

    def test_static_presentation_frames_and_prompts_remain_distinct(self) -> None:
        text = "危机解除。"
        row = _validate(_static_row(text), text)
        self.assertEqual(
            len({row[key] for key in runtime._STATIC_PRESENTATION_FIELDS}), 3
        )
        self.assertEqual(len({row[key] for key in runtime._SHOT_PROMPT_FIELDS}), 3)
        self.assertEqual(
            row["prompt_compiler"],
            "strict-shot-v2-static-presentation-derived",
        )

    def test_presentation_inference_cannot_become_source_fact_or_action(self) -> None:
        text = "他成为新的首领。"
        row = _validate(_static_row(text), text)
        self.assertEqual(row["source_fact"], text)
        self.assertEqual(row["summary"], text)
        self.assertEqual(row["action"], "")
        self.assertNotIn(row["visual_motion"], row["source_fact"])

    def test_new_entity_is_rejected_and_visual_audit_routes_to_regroup(self) -> None:
        text = "他终于明白了真相。"
        invalid = _static_row(text)
        invalid["character_entity_ids"] = ["invented-character"]
        with self.assertRaisesRegex(RuntimeError, "非法 character entity id"):
            _validate(invalid, text)
        issues = runtime._issues_for_shot(
            {
                "valid": False,
                "visual_realization_valid": False,
                "violations": [
                    {"shot_index": 1, "type": "visual_realization_violation"}
                ],
            },
            1,
        )
        self.assertEqual(
            {runtime._canonical_audit_code(item) for item in issues},
            {"visual_realization"},
        )

    def test_insufficient_visual_evidence_is_not_accepted_as_a_shot(self) -> None:
        text = "某种无法被当前证据视觉化的状态。"
        row = _static_row(text)
        row.update({
            "temporal_mode": "insufficient_visual_evidence",
            "temporal_mode_reason": "no source-grounded visual state is available",
        })
        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            _validate(row, text)
        self.assertEqual(
            captured.exception.metadata["evidence_sufficiency"],
            "insufficient_visual_evidence",
        )
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )

    def test_real_beat3_fixture_replays_as_static_outcome(self) -> None:
        data = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
        evidence = data["regroup"]["evidence"]
        anchors = [
            {
                "id": item["id"],
                "text": item["text"],
                "source_start": item["source_start"],
                "source_end": item["source_end"],
            }
            for item in evidence
        ]
        row = _static_row(
            data["target_beat"]["summary"],
            [item["id"] for item in anchors],
        )
        validated = _validate(
            row,
            data["target_beat"]["summary"],
            anchors=anchors,
        )
        self.assertEqual(validated["temporal_mode"], data["expected_temporal_mode"])
        self.assertEqual(validated["summary"], data["target_beat"]["summary"])
        self.assertNotIn("低头", validated["source_fact"])
        self.assertNotIn("对视", validated["source_fact"])

    def test_dynamic_strict_contract_is_not_weakened(self) -> None:
        text = "门缓缓打开，他走了进去。"
        row = _observable_row(text)
        row["representative_state"] = row["video_start_state"]
        with self.assertRaises(runtime.Stage04RepairInvariantError):
            _validate(row, text)


class Stage04TemporalModeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_insufficient_classification_enters_one_regroup_without_retry(self) -> None:
        text = "抽象状态。"
        error = runtime.Stage04ShotRepairError(
            "insufficient",
            metadata={
                "evidence_sufficiency": "insufficient_visual_evidence",
                "repair_progress": "needs_regrouping_or_evidence_selection",
            },
        )
        recovered = _static_row(text)
        audit = {
            "valid": True,
            "visual_realization_valid": True,
            "violations": [],
        }
        recover = mock.AsyncMock(return_value=([recovered], audit))

        def evidence_builder(**_kwargs):
            return text, [_anchor(text)], {1: ["E001"]}

        with (
            mock.patch.object(
                runtime,
                "_generate_missing_beat_shots",
                mock.AsyncMock(side_effect=error),
            ) as generate,
            mock.patch.object(
                runtime,
                "_recover_single_beat_after_scoped_repair",
                recover,
            ),
        ):
            rows = await runtime._produce_batch(
                {
                    "_studio_v2371e_batch_evidence": evidence_builder,
                    "_studio_v2371_audit_batch": mock.AsyncMock(),
                },
                batch=[_beat(text)],
                all_beats=[_beat(text)],
                batch_index=0,
                batch_total=1,
                source=text,
                scene={"scene_id": "scene-1", "episode_id": "episode-1"},
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
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(recover.await_count, 1)
        self.assertEqual(rows[0]["source_audit"], audit)


class Stage04TemporalModeActiveContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")

    async def test_active_audit_requires_visual_realization_valid(self) -> None:
        captured: list[dict] = []

        async def qwen_call(**kwargs):
            captured.append(kwargs)
            result = {
                "valid": True,
                "evidence_entailment_ok": True,
                "beat_coverage_ok": True,
                "temporal_monotonic": True,
                "no_future_event_preconsumption": True,
                "no_result_duplication": True,
                "state_order_valid": True,
                "entity_visibility_valid": True,
                "visual_realization_valid": True,
                "violations": [],
            }
            return result, result, {}

        text = "危机解除。"
        shot = _validate(_static_row(text), text)
        with mock.patch.object(
            self.main,
            "_studio_v2371a_qwen_call",
            qwen_call,
        ):
            audit = await self.main._studio_v2371_audit_batch(
                source_window=text,
                compact_beats=[_beat(text)],
                shots=[shot],
            )
        self.assertTrue(audit["valid"])
        self.assertIn("visual_realization_valid", captured[0]["contract"])
        self.assertIn("static_outcome", captured[0]["system_prompt"])
        self.assertIn("visual_realization", captured[0]["messages"][0]["content"])

    def test_stage05_accepts_locked_static_prompt_contract(self) -> None:
        text = "众人认可了他的决定。"
        shot = _validate(_static_row(text), text)
        shot.update({
            "shot_id": "shot-static-1",
            "stage04_contract_version": "strict-shot-v2",
            "runtime_version": "2.39.6.3-stage04-full-pipeline-preflight",
            "text_model_policy": "qwen3-32b",
            "source_provenance": {
                "source_evidence": [text],
                "source_evidence_ids": ["E001"],
                "temporal_mode": "static_outcome",
                "source_fact": text,
            },
            "batch_audit": {"valid": True},
            "narrative_audit": {"valid": True},
            "scene_global_audit": {"valid": True},
            "forward_overlap_audit": {"valid": True},
        })
        self.main._studio_v2371_require_strict_shot(shot)
        before = self.main._studio_shot_contract_fingerprint(shot)
        changed = dict(shot, visual_motion="镜头缓慢拉远；叙事状态仍保持不变。")
        self.assertNotEqual(
            before,
            self.main._studio_shot_contract_fingerprint(changed),
        )


if __name__ == "__main__":
    unittest.main()
