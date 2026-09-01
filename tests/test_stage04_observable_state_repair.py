from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def anchor(text: str) -> dict:
    return {
        "id": "E001",
        "text": text,
        "source_start": 0,
        "source_end": len(text),
    }


def beat(text: str) -> dict:
    return {
        "order": 2,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
    }


def collapsed_dynamic(text: str) -> dict:
    return {
        "title": "向前推进",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "evidence describes a visible forward transition",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "守护者仍挡在通道前。",
        "narrative_state": "守护者开始侧身让开。",
        "narrative_end_state": "守护者开始侧身让开。",
        "video_start_state": "守护者仍挡在通道前。",
        "representative_state": "守护者开始侧身让开。",
        "video_end_state": "守护者开始侧身让开。",
        "covered_beat_orders": [2],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


class ObservableStateRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_representative_equals_end_repairs_end_only(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        row = collapsed_dynamic(text)
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=(
            {},
            {"patch": {"video_end_state": "守护者已经完全让开通道。"}},
            {},
        ))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_observable_transition_state_consistency(
                {}, row=row, compact_beats=[beat(text)], anchors=[anchor(text)],
                context="unit-observable-end",
            )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(repaired["video_start_state"], before["video_start_state"])
        self.assertEqual(repaired["representative_state"], before["representative_state"])
        self.assertEqual(repaired["video_end_state"], "守护者已经完全让开通道。")
        self.assertEqual(repaired["narrative_end_state"], repaired["video_end_state"])
        self.assertEqual(len({repaired[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS}), 3)

    async def test_valid_observable_does_not_spend_repair_call(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        row = collapsed_dynamic(text)
        row["video_end_state"] = "守护者已经完全让开通道。"
        row["narrative_end_state"] = row["video_end_state"]
        qwen = mock.AsyncMock()
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_observable_transition_state_consistency(
                {}, row=row, compact_beats=[beat(text)], anchors=[anchor(text)],
                context="unit-observable-valid",
            )
        self.assertEqual(qwen.await_count, 0)
        self.assertEqual(repaired["temporal_mode"], "observable_transition")

    async def test_empty_state_patch_routes_to_regroup_without_loop(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"video_end_state": ""}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._repair_observable_transition_state_consistency(
                    {}, row=collapsed_dynamic(text), compact_beats=[beat(text)],
                    anchors=[anchor(text)], context="unit-observable-insufficient",
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(
            captured.exception.metadata["evidence_sufficiency"],
            "insufficient_for_observable_transition",
        )
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "needs_regrouping_or_evidence_selection",
        )

    async def test_missing_beat_real_path_repairs_collapse_instead_of_three_full_retries(self) -> None:
        text = "守护者从挡路到完全让开通道。"
        generation = {"shots": [collapsed_dynamic(text)]}
        repair = {"patch": {"video_end_state": "守护者已经完全让开通道。"}}
        qwen = mock.AsyncMock(side_effect=[({}, generation, {}), ({}, repair, {})])
        with (
            mock.patch.object(runtime, "_qwen", qwen),
            mock.patch.object(
                runtime,
                "_select_targeted_evidence_ids",
                mock.AsyncMock(return_value=(["E001"], "test-evidence")),
            ),
            mock.patch.object(
                runtime,
                "_plan_targeted_duration",
                mock.AsyncMock(return_value=(3.0, "test-duration")),
            ),
        ):
            rows = await runtime._generate_missing_beat_shots(
                {},
                missing_orders=[2],
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                previous_shot=None,
                next_beat=None,
                allowed_chars=set(),
                allowed_props=set(),
                scene_id="scene-1",
                episode_id="episode-1",
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video_end_state"], "守护者已经完全让开通道。")
        self.assertEqual(len({rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS}), 3)


if __name__ == "__main__":
    unittest.main()
