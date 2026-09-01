from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def _anchor(text: str) -> dict:
    return {
        "id": "E001",
        "text": text,
        "beat_order": 1,
        "source_start": 0,
        "source_end": len(text),
    }


def _beat(text: str) -> dict:
    return {
        "order": 1,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": ["E001"],
        "source_evidence_ids": ["E001"],
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "E001", "start": 0, "end": len(text), "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _next_beat() -> dict:
    text = "途中遇到守护神兽。"
    return {
        "order": 2,
        "summary": text,
        "state_change": "少年从寻找推进到遇到守护神兽。",
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "C02", "start": 13, "end": 22, "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _observable_row(text: str) -> dict:
    return {
        "title": "雪山寻剑",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "the source contains an observable activity",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": "少年在雪山寻找失落古剑。",
        "narrative_state": "少年仍在雪山寻找失落古剑。",
        "narrative_end_state": "少年继续在雪山寻找失落古剑。",
        "visual_realization": "",
        "realization_scope": "presentation_only",
        "realization_assumptions": [],
        "visual_start_frame": "",
        "representative_frame": "",
        "visual_end_frame": "",
        "visual_motion": "",
        "video_start_state": "少年在雪山寻找失落古剑。",
        "representative_state": "少年仍在雪山寻找失落古剑。",
        "video_end_state": "少年继续在雪山寻找失落古剑。",
        "covered_beat_orders": [1],
        "source_evidence_ids": ["E001"],
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": "E001", "start": 0, "end": len(text), "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def _static_patch(text: str) -> dict:
    return {
        "patch": {
            "source_fact": text,
            "narrative_state": text,
            "visual_realization": "用构图、景别和镜头运动呈现正在持续的寻剑状态，剧情事实不变。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["只改变构图与镜头运动，不新增寻剑结果"],
            "visual_start_frame": "雪山环境中景，少年处于寻剑状态。",
            "representative_frame": "镜头轻推，少年仍处于同一寻剑状态。",
            "visual_end_frame": "较紧景别停住，寻剑状态持续。",
            "visual_motion": "镜头缓慢推近后停住。",
        }
    }



def _static_generation_row(text: str) -> dict:
    row = _observable_row(text)
    row.update({
        "action": "",
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": "target evidence proves an ongoing stable activity without internal milestones",
        "temporal_mode_evidence_ids": ["E001"],
        "source_fact": text,
        "narrative_start_state": text,
        "narrative_state": text,
        "narrative_end_state": text,
        "visual_realization": "只用景别与镜头运动表现持续寻剑状态。",
        "realization_scope": "presentation_only",
        "realization_assumptions": ["不新增寻剑结果"],
        "visual_start_frame": "雪山远景建立，少年处于持续寻剑状态。",
        "representative_frame": "中景突出少年，寻剑状态保持不变。",
        "visual_end_frame": "较紧景别收束，寻剑状态仍保持不变。",
        "visual_motion": "镜头缓慢推近后停住，仅表现层变化。",
        "video_start_state": text,
        "representative_state": text,
        "video_end_state": text,
    })
    return row


class FirstBeatTemporalReconsiderationTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_beat_stable_ongoing_activity_reclassifies_without_future_evidence(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        row = _observable_row(text)
        classification = {
            "temporal_mode": "static_outcome",
            "temporal_mode_reason": "evidence proves an ongoing search state but no internal temporal milestones",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(side_effect=[
            ({}, classification, {}),
            ({}, _static_patch(text), {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            repaired = await runtime._reconsider_temporal_mode_after_regeneration(
                {},
                row=row,
                compact_beats=[_beat(text)],
                anchors=[_anchor(text)],
                context="unit-first-beat",
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertEqual(repaired["temporal_mode"], "static_outcome")
        self.assertEqual(repaired["source_evidence_ids"], ["E001"])
        self.assertEqual(repaired["summary"], text)
        self.assertEqual(repaired["action"], "")
        self.assertEqual(
            {
                repaired["video_start_state"],
                repaired["representative_state"],
                repaired["video_end_state"],
            },
            {row["narrative_state"]},
        )

    async def test_first_beat_recovery_passes_without_borrowing_next_beat(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        row = _observable_row(text)
        qwen = mock.AsyncMock(return_value=({}, {"shots": [_static_generation_row(text)]}, {}))

        def forbidden_builder(**_kwargs):
            raise AssertionError("first-beat edge recovery must not borrow adjacent/future evidence")

        async def audit_fn(**kwargs):
            self.assertEqual(kwargs["shots"][0]["source_evidence_ids"], ["E001"])
            self.assertNotIn("守护神兽", kwargs["shots"][0]["summary"])
            return {
                "valid": kwargs["shots"][0]["temporal_mode"] == "static_outcome",
                "violations": [] if kwargs["shots"][0]["temporal_mode"] == "static_outcome" else [
                    {"type": "state_order", "shot_index": 1}
                ],
            }

        env = {"_studio_v2371e_batch_evidence": forbidden_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            rows, audit = await runtime._recover_single_beat_after_scoped_repair(
                env,
                source=text + "\n途中遇到守护神兽。",
                target_beat=_beat(text),
                all_beats=[_beat(text), _next_beat()],
                current_compact_beats=[_beat(text)],
                current_anchors=[_anchor(text)],
                previous_shot=None,
                next_beat=_next_beat(),
                allowed_chars=set(),
                allowed_props=set(),
                scene_id="scene-1",
                episode_id="episode-1",
                audit_fn=audit_fn,
                prior_metadata={
                    "repair_progress": "needs_regrouping_or_evidence_selection",
                    "failed_rule": "observable_transition_state_consistency",
                },
                current_rows=[copy.deepcopy(row)],
            )
        self.assertTrue(audit["valid"])
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["source_evidence_ids"], ["E001"])
        self.assertEqual(
            rows[0]["_regroup_recovery_diagnostics"]["repair_progress"],
            "controller_recovered",
        )
        self.assertEqual(
            rows[0]["_regroup_recovery_diagnostics"]["recovery_usage"]["evidence_regroup"],
            0,
        )

    async def test_first_beat_observable_no_progress_regenerates_from_target_only_evidence(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        generation = {"shots": [_static_generation_row(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, {"patch": {"video_end_state": ""}}, {}),
            ({}, generation, {}),
        ])
        collapsed = _observable_row(text)
        collapsed["video_end_state"] = collapsed["representative_state"]
        collapsed["narrative_end_state"] = collapsed["representative_state"]

        def forbidden_builder(**_kwargs):
            raise AssertionError("first-beat target-only recovery must not borrow adjacent/future evidence")

        async def audit_fn(**kwargs):
            self.assertEqual(kwargs["shots"][0]["source_evidence_ids"], ["E001"])
            self.assertNotIn("守护神兽", kwargs["source_window"])
            self.assertNotIn("守护神兽", kwargs["shots"][0]["summary"])
            return {"valid": True, "violations": []}

        env = {"_studio_v2371e_batch_evidence": forbidden_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            rows, audit = await runtime._recover_single_beat_after_scoped_repair(
                env,
                source=text + "\n途中遇到守护神兽。",
                target_beat=_beat(text),
                all_beats=[_beat(text), _next_beat()],
                current_compact_beats=[_beat(text)],
                current_anchors=[_anchor(text)],
                previous_shot=None,
                next_beat=_next_beat(),
                allowed_chars=set(),
                allowed_props=set(),
                scene_id="scene-1",
                episode_id="episode-1",
                audit_fn=audit_fn,
                prior_metadata={
                    "repair_progress": "needs_regrouping_or_evidence_selection",
                    "failed_rule": "observable_transition_state_consistency",
                },
                current_rows=[collapsed],
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertTrue(audit["valid"])
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["source_evidence_ids"], ["E001"])
        target_prompt = qwen.await_args_list[1].kwargs["prompt"]
        self.assertNotIn("守护神兽", target_prompt)
        diagnostics = rows[0]["_regroup_recovery_diagnostics"]
        self.assertEqual(
            diagnostics["repair_progress"],
            "controller_recovered",
        )
        self.assertEqual(diagnostics["recovery_usage"]["evidence_regroup"], 0)
        self.assertEqual(diagnostics["recovery_usage"]["target_only_regeneration"], 1)
        self.assertEqual(
            diagnostics["evidence_fingerprint"],
            diagnostics["evidence_fingerprint"],
        )


if __name__ == "__main__":
    unittest.main()
