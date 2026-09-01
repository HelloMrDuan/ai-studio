from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def _anchor(text: str = "门缓缓打开，他走了进去。") -> dict:
    return {"id": "E001", "text": text, "source_start": 0, "source_end": len(text)}


def _beat(text: str = "门缓缓打开，他走了进去。") -> dict:
    return {
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
    }


def _dynamic_candidate(mode: str = "initial") -> dict:
    text = "门缓缓打开，他走了进去。"
    return {
        "title": "进入",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": mode,
        "temporal_mode_reason": "model emitted an out-of-contract label",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "video_start_state": "门仍在开启，人物站在门外。",
        "representative_state": "门已经打开，人物正跨过门槛。",
        "video_end_state": "人物已经进入门内。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _static_candidate(mode: str = "initial") -> dict:
    text = "众人认可了他的决定。"
    return {
        "title": "认可",
        "duration_seconds": 3,
        "summary": text,
        "action": "",
        "temporal_mode": mode,
        "temporal_mode_reason": "model emitted an out-of-contract label",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": text,
        "narrative_state": text,
        "narrative_end_state": text,
        "visual_realization": "稳定结果的中景构图，剧情事实保持不变。",
        "realization_scope": "presentation_only",
        "realization_assumptions": ["使用中景和轻微镜头运动呈现已成立状态"],
        "visual_start_frame": "中景固定构图，既成状态保持。",
        "representative_frame": "中景轻微推近，既成状态保持。",
        "visual_end_frame": "较紧中景停住，既成状态保持。",
        "visual_motion": "镜头轻微推近后停住。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


class TemporalModeEnumContractTests(unittest.TestCase):
    def test_visible_contract_exposes_closed_temporal_enum(self) -> None:
        visible = runtime._visible_output_contract(runtime._shot_generation_contract(1))
        self.assertIn(
            "<enum:observable_transition|static_outcome|insufficient_visual_evidence>",
            visible,
        )

    def test_validator_still_rejects_unknown_mode_without_alias_mapping(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "temporal_mode 非法"):
            runtime._normalize_temporal_contract(
                _dynamic_candidate("initial"), evidence_ids=["E001"], raw_index=1
            )


class TemporalModeFieldRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_mode_repairs_to_observable_without_mutating_locked_fields(self) -> None:
        row = _dynamic_candidate()
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=(
            {},
            {
                "temporal_mode": "observable_transition",
                "temporal_mode_reason": "evidence explicitly contains an opening-to-entry transition",
                "temporal_mode_evidence_ids": ["E001"],
            },
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_invalid_temporal_mode_classification(
                {}, row=row, compact_beats=[_beat()], anchors=[_anchor()], context="test-dynamic"
            )
        self.assertEqual(repaired["temporal_mode"], "observable_transition")
        self.assertEqual(qwen.await_count, 1)
        for key, value in before.items():
            if key not in {"temporal_mode", "temporal_mode_reason", "temporal_mode_evidence_ids"}:
                self.assertEqual(repaired[key], value, key)
        normalized = runtime._normalize_temporal_contract(
            repaired, evidence_ids=["E001"], raw_index=1
        )
        self.assertEqual(normalized["temporal_mode"], "observable_transition")

    async def test_invalid_mode_repairs_to_static_outcome(self) -> None:
        text = "众人认可了他的决定。"
        qwen = mock.AsyncMock(return_value=(
            {},
            {
                "temporal_mode": "static_outcome",
                "temporal_mode_reason": "evidence states an established result without its transition process",
                "temporal_mode_evidence_ids": ["E001"],
            },
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_invalid_temporal_mode_classification(
                {}, row=_static_candidate(), compact_beats=[_beat(text)], anchors=[_anchor(text)], context="test-static"
            )
        normalized = runtime._normalize_temporal_contract(
            repaired, evidence_ids=["E001"], raw_index=1
        )
        self.assertEqual(normalized["temporal_mode"], "static_outcome")
        self.assertEqual(normalized["narrative_state"], text)

    async def test_repair_can_route_to_insufficient_visual_evidence(self) -> None:
        text = "某种无法被当前证据视觉化的状态。"
        row = _dynamic_candidate()
        row["summary"] = text
        row["source_fact"] = text
        qwen = mock.AsyncMock(return_value=(
            {},
            {
                "temporal_mode": "insufficient_visual_evidence",
                "temporal_mode_reason": "selected evidence does not support a grounded visual state",
                "temporal_mode_evidence_ids": ["E001"],
            },
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_invalid_temporal_mode_classification(
                {}, row=row, compact_beats=[_beat(text)], anchors=[_anchor(text)], context="test-insufficient"
            )
        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            runtime._normalize_temporal_contract(repaired, evidence_ids=["E001"], raw_index=1)
        self.assertEqual(captured.exception.metadata["evidence_sufficiency"], "insufficient_visual_evidence")

    async def test_same_invalid_mode_stops_after_one_field_repair(self) -> None:
        qwen = mock.AsyncMock(return_value=(
            {},
            {
                "temporal_mode": "initial",
                "temporal_mode_reason": "unchanged invalid classifier output",
                "temporal_mode_evidence_ids": ["E001"],
            },
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._repair_invalid_temporal_mode_classification(
                    {}, row=_dynamic_candidate(), compact_beats=[_beat()], anchors=[_anchor()], context="test-no-progress"
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(captured.exception.metadata["raw_temporal_mode"], "initial")
        self.assertEqual(captured.exception.metadata["repair_progress"], "rejected_no_progress")
        self.assertEqual(captured.exception.metadata["failed_rule"], "temporal_mode_contract")

    async def test_missing_beat_path_does_not_full_retry_after_no_progress(self) -> None:
        generation = {"shots": [_dynamic_candidate()]}
        invalid_repair = {
            "temporal_mode": "initial",
            "temporal_mode_reason": "unchanged invalid classifier output",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(side_effect=[({}, generation, {}), ({}, invalid_repair, {})])
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._generate_missing_beat_shots(
                    {},
                    missing_orders=[1],
                    compact_beats=[_beat()],
                    anchors=[_anchor()],
                    previous_shot=None,
                    next_beat=None,
                    allowed_chars=set(),
                    allowed_props=set(),
                    scene_id="scene-1",
                    episode_id="episode-1",
                )
        self.assertEqual(qwen.await_count, 2)
        self.assertEqual(captured.exception.metadata["repair_progress"], "rejected_no_progress")


if __name__ == "__main__":
    unittest.main()
