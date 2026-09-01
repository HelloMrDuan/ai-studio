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
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
    }


def invalid_static(text: str) -> dict:
    return {
        "title": "既成结果",
        "duration_seconds": 3,
        "summary": text,
        "action": "错误地伪造了动作过程",
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": "evidence states an established result without its transition process",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "众人尚未认可他的决定。",
        "narrative_state": text,
        "narrative_end_state": "众人最终认可了他的决定。",
        "visual_realization": "中景表现既成关系。",
        "realization_scope": "presentation_only",
        "realization_assumptions": ["使用中景表现稳定结果"],
        "visual_start_frame": "中景静止。",
        "representative_frame": "中景轻微推近。",
        "visual_end_frame": "较紧中景停住。",
        "visual_motion": "镜头轻微推近。",
        "video_start_state": "众人尚未认可他的决定。",
        "representative_state": text,
        "video_end_state": "众人最终认可了他的决定。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def repair_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "中景锁定既成认可关系，叙事事实保持不变。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["只使用构图与镜头运动呈现既成状态"],
            "visual_start_frame": "中景固定构图，认可关系已经成立。",
            "representative_frame": "镜头缓慢推近，认可关系保持不变。",
            "visual_end_frame": "较紧中景停住，认可关系仍保持不变。",
            "visual_motion": "镜头缓慢推近后停住。",
        }
    }



def duplicate_frame_repair_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "保持同一既成叙事状态，只允许表现层变化。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["不增加任何剧情事实"],
            "visual_start_frame": "同一画面保持不变。",
            "representative_frame": "同一画面保持不变。",
            "visual_end_frame": "同一画面保持不变。",
            "visual_motion": "镜头轻微推进后停住，仅表现层变化。",
        }
    }


class StaticOutcomePayloadRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_static_transition_payload_repairs_only_static_block(self) -> None:
        text = "众人认可了他的决定。"
        row = invalid_static(text)
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=({}, repair_patch(text), {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_static_outcome_payload_consistency(
                {},
                row=row,
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                context="unit-static",
            )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")
        self.assertEqual(repaired["source_evidence_ids"], before["source_evidence_ids"])
        self.assertEqual(repaired["covered_beat_orders"], before["covered_beat_orders"])
        self.assertEqual(repaired["duration_seconds"], before["duration_seconds"])
        self.assertEqual(repaired["summary"], text)
        self.assertEqual(repaired["action"], "")
        self.assertEqual(
            {repaired[key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )
        self.assertEqual(
            {
                repaired["narrative_start_state"],
                repaired["narrative_state"],
                repaired["narrative_end_state"],
            },
            {text},
        )
        self.assertEqual(
            len({repaired[key] for key in runtime._STATIC_PRESENTATION_FIELDS}),
            3,
        )

    async def test_valid_static_payload_does_not_spend_repair_call(self) -> None:
        text = "众人认可了他的决定。"
        row = invalid_static(text)
        patch = repair_patch(text)["patch"]
        row.update(patch)
        row.update({
            "summary": text,
            "action": "",
            "narrative_start_state": text,
            "narrative_state": text,
            "narrative_end_state": text,
            "video_start_state": text,
            "representative_state": text,
            "video_end_state": text,
        })
        qwen = mock.AsyncMock()
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._repair_static_outcome_payload_consistency(
                {},
                row=row,
                compact_beats=[beat(text)],
                anchors=[anchor(text)],
                context="unit-valid-static",
            )
        self.assertEqual(qwen.await_count, 0)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")

    async def test_regroup_regeneration_repairs_static_transition_before_validate(self) -> None:
        text = "众人认可了他的决定。"
        generation = {"shots": [invalid_static(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, generation, {}),
            ({}, repair_patch(text), {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            rows = await runtime._regenerate_shot_from_reselected_evidence(
                {},
                target_order=1,
                compact_beat=beat(text),
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
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["summary"], text)
        self.assertEqual(
            {rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )

    async def test_regroup_duplicate_static_frames_close_deterministically_without_extra_qwen(self) -> None:
        text = "众人认可了他的决定。"
        generation = {"shots": [invalid_static(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, generation, {}),
            ({}, duplicate_frame_repair_patch(text), {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            rows = await runtime._regenerate_shot_from_reselected_evidence(
                {},
                target_order=1,
                compact_beat=beat(text),
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
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["summary"], text)
        self.assertEqual(rows[0]["source_fact"], text)
        self.assertEqual(
            {rows[0][key] for key in runtime._SHOT_TEMPORAL_STATE_FIELDS},
            {text},
        )
        self.assertEqual(
            len({
                runtime._semantic_text_key(rows[0][key])
                for key in runtime._STATIC_PRESENTATION_FIELDS
            }),
            3,
        )
    async def test_failed_static_repair_does_not_loop(self) -> None:
        text = "众人认可了他的决定。"
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"source_fact": text}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._repair_static_outcome_payload_consistency(
                    {},
                    row=invalid_static(text),
                    compact_beats=[beat(text)],
                    anchors=[anchor(text)],
                    context="unit-fail-static",
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "static_payload_repair_invalid_output",
        )


if __name__ == "__main__":
    unittest.main()
