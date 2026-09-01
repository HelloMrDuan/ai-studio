from __future__ import annotations

import copy
import unittest
from unittest import mock

from app import stage04_v238_runtime as runtime


def anchor(order: int, text: str) -> dict:
    return {
        "id": f"E{order:03d}",
        "text": text,
        "beat_order": order,
        "source_start": order * 10,
        "source_end": order * 10 + len(text),
    }


def beat(order: int, text: str) -> dict:
    evidence_id = f"E{order:03d}"
    return {
        "order": order,
        "summary": text,
        "state_change": text,
        "allowed_source_evidence_ids": [evidence_id],
        "source_evidence_ids": [evidence_id],
        "source_evidence": [text],
        "source_evidence_spans": [
            {"id": evidence_id, "start": order * 10, "end": order * 10 + len(text), "text": text}
        ],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def observable(order: int, text: str, *, collapse: str = "") -> dict:
    evidence_id = f"E{order:03d}"
    start = f"{text}之前的可见状态"
    representative = f"{text}正在发生的可见状态"
    end = f"{text}已经完成的可见状态"
    if collapse == "start_representative":
        representative = start
    elif collapse == "representative_end":
        end = representative
    return {
        "title": f"Beat {order}",
        "duration_seconds": 3,
        "summary": text,
        "action": text,
        "temporal_mode": "observable_transition",
        "temporal_mode_reason": "locked evidence proves a visible transition",
        "temporal_mode_evidence_ids": [evidence_id],
        "source_fact": text,
        "narrative_start_state": start,
        "narrative_state": representative,
        "narrative_end_state": end,
        "video_start_state": start,
        "representative_state": representative,
        "video_end_state": end,
        "covered_beat_orders": [order],
        "source_evidence_ids": [evidence_id],
        "character_entity_ids": [],
        "prop_entity_ids": [],
    }


def static_outcome(order: int, text: str, *, mismatch: bool = False, collapse_frames: bool = False) -> dict:
    evidence_id = f"E{order:03d}"
    row = observable(order, text)
    row.update({
        "action": "",
        "temporal_mode": "static_outcome",
        "temporal_mode_reason": "evidence proves a stable fact without internal milestones",
        "temporal_mode_evidence_ids": [evidence_id],
        "source_fact": text,
        "summary": text,
        "narrative_start_state": text if not mismatch else "错误的前态",
        "narrative_state": text,
        "narrative_end_state": text if not mismatch else "错误的后态",
        "video_start_state": text if not mismatch else "错误的前态",
        "representative_state": text,
        "video_end_state": text if not mismatch else "错误的后态",
        "visual_realization": "只用景别、构图和光影呈现稳定事实。",
        "realization_scope": "presentation_only",
        "realization_assumptions": [],
        "visual_start_frame": "同一画面" if collapse_frames else "远景建立稳定事实。",
        "representative_frame": "同一画面" if collapse_frames else "中景突出稳定事实。",
        "visual_end_frame": "同一画面" if collapse_frames else "近景收束稳定事实。",
        "visual_motion": "镜头只改变景别和构图。",
    })
    return row


def presentation_patch() -> dict:
    return {
        "patch": {
            "visual_realization": "只用远中近景、机位和光影呈现同一稳定事实。",
            "realization_scope": "presentation_only",
            "realization_assumptions": ["不新增剧情事实"],
            "visual_start_frame": "远景建立同一稳定事实。",
            "representative_frame": "中景突出同一稳定事实。",
            "visual_end_frame": "近景以光影收束同一稳定事实。",
            "visual_motion": "镜头缓慢推近；只改变表现层。",
        }
    }


class TemporalRecoveryMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def _recover_order(self, order: int) -> tuple[dict, mock.AsyncMock]:
        text = f"事件{order}只由当前证据支持。"
        current = observable(order, text, collapse="representative_end")
        generated = static_outcome(order, text)
        qwen = mock.AsyncMock(side_effect=[
            ({}, {"patch": {"video_end_state": ""}}, {}),
            ({}, {"shots": [generated]}, {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=current, beat=beat(order, text),
                locked_evidence=[anchor(order, text)],
                audit_failure_metadata={"failed_rule": "state_order"},
                audit_fn=mock.AsyncMock(return_value={"valid": True, "violations": []}),
                context=f"matrix Beat {order}",
            )
        self.assertEqual(result["decision"], runtime._TEMPORAL_RECOVERY_RECOVERED)
        self.assertEqual(result["shot"]["source_evidence_ids"], [f"E{order:03d}"])
        return result, qwen

    async def test_01_invalid_initial_is_field_reclassified_not_string_mapped(self) -> None:
        row = observable(1, "门打开后人物进入。")
        row["temporal_mode"] = "initial"
        before = copy.deepcopy(row)
        qwen = mock.AsyncMock(return_value=({}, {
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "evidence proves the transition",
            "temporal_mode_evidence_ids": ["E001"],
        }, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=row, beat=beat(1, row["source_fact"]),
                locked_evidence=[anchor(1, row["source_fact"])],
                audit_failure_metadata={}, allow_regeneration=False,
                validate_candidate=False, context="matrix invalid mode",
            )
        self.assertEqual(qwen.await_count, 1)
        self.assertEqual(result["decision"], runtime._TEMPORAL_RECOVERY_RECOVERED)
        for field in ("source_fact", "covered_beat_orders", "source_evidence_ids", "duration_seconds"):
            self.assertEqual(result["shot"][field], before[field])

    async def test_02_static_narrative_mismatch_repairs_presentation_only(self) -> None:
        text = "众人认可了既定决定。"
        row = static_outcome(2, text, mismatch=True)
        qwen = mock.AsyncMock(return_value=({}, presentation_patch(), {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=row, beat=beat(2, text), locked_evidence=[anchor(2, text)],
                audit_failure_metadata={}, allow_regeneration=False,
                validate_candidate=False, context="matrix static mismatch",
            )
        self.assertEqual(result["shot"]["source_fact"], text)
        self.assertEqual(result["shot"]["narrative_state"], text)
        self.assertEqual(qwen.await_count, 1)
        self.assertNotIn("source_fact", qwen.await_args.kwargs["contract"])
        self.assertNotIn("narrative_state", qwen.await_args.kwargs["contract"])

    async def test_03_static_frame_collapse_is_differentiated(self) -> None:
        text = "城门保持关闭状态。"
        row = static_outcome(3, text, collapse_frames=True)
        qwen = mock.AsyncMock(return_value=({}, presentation_patch(), {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=row, beat=beat(3, text), locked_evidence=[anchor(3, text)],
                audit_failure_metadata={}, allow_regeneration=False,
                validate_candidate=False, context="matrix frame collapse",
            )
        frames = [result["shot"][field] for field in runtime._STATIC_PRESENTATION_FIELDS]
        self.assertEqual(len(set(frames)), 3)

    async def test_04_observable_start_representative_collapse_requests_target_only_regeneration(self) -> None:
        text = "人物穿过门口。"
        row = observable(4, text, collapse="start_representative")
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"representative_state": ""}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=row, beat=beat(4, text), locked_evidence=[anchor(4, text)],
                audit_failure_metadata={}, allow_regeneration=False,
                validate_candidate=False, context="matrix start collapse",
            )
        self.assertEqual(result["decision"], runtime._TEMPORAL_RECOVERY_REGENERATE)

    async def test_05_observable_representative_end_collapse_requests_target_only_regeneration(self) -> None:
        text = "人物穿过门口。"
        row = observable(5, text, collapse="representative_end")
        qwen = mock.AsyncMock(return_value=({}, {"patch": {"video_end_state": ""}}, {}))
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=row, beat=beat(5, text), locked_evidence=[anchor(5, text)],
                audit_failure_metadata={}, allow_regeneration=False,
                validate_candidate=False, context="matrix end collapse",
            )
        self.assertEqual(result["decision"], runtime._TEMPORAL_RECOVERY_REGENERATE)

    async def test_06_beat1_uses_unified_controller(self) -> None:
        result, _qwen = await self._recover_order(1)
        self.assertEqual(result["metadata"]["target_beat_order"], 1)

    async def test_07_beat2_uses_same_unified_controller(self) -> None:
        text = "事件2只由当前证据支持。"
        current = observable(2, text, collapse="representative_end")
        qwen = mock.AsyncMock(side_effect=[
            ({}, {"patch": {"video_end_state": ""}}, {}),
            ({}, {"shots": [static_outcome(2, text)]}, {}),
        ])

        def forbidden_adjacent_builder(**_kwargs):
            raise AssertionError("temporal recovery must not borrow Beat1 evidence")

        async def audit_fn(**_kwargs):
            return {"valid": True, "violations": []}

        env = {"_studio_v2371e_batch_evidence": forbidden_adjacent_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            rows, audit = await runtime._recover_single_beat_after_scoped_repair(
                env,
                source="previous-only-sentinel\n" + text + "\nnext-only-sentinel",
                target_beat=beat(2, text),
                all_beats=[beat(1, "previous-only-sentinel"), beat(2, text), beat(3, "next-only-sentinel")],
                current_compact_beats=[beat(2, text)],
                current_anchors=[anchor(2, text)],
                previous_shot={"summary": "previous-only-sentinel"},
                next_beat=beat(3, "next-only-sentinel"),
                allowed_chars=set(), allowed_props=set(),
                scene_id="scene", episode_id="episode", audit_fn=audit_fn,
                prior_metadata={
                    "failed_rule": "observable_transition_state_consistency",
                    "repair_progress": "needs_regrouping_or_evidence_selection",
                },
                current_rows=[current],
            )
        self.assertTrue(audit["valid"])
        self.assertEqual(rows[0]["source_evidence_ids"], ["E002"])
        self.assertEqual(
            rows[0]["_regroup_recovery_diagnostics"]["target_beat_order"], 2
        )
        target_prompt = qwen.await_args_list[1].kwargs["prompt"]
        self.assertNotIn("previous-only-sentinel", target_prompt)
        self.assertNotIn("next-only-sentinel", target_prompt)

    async def test_08_beatN_uses_same_unified_controller(self) -> None:
        result, _qwen = await self._recover_order(9)
        self.assertEqual(result["metadata"]["target_beat_order"], 9)

    async def test_09_target_only_prompt_cannot_borrow_previous_evidence(self) -> None:
        _result, qwen = await self._recover_order(2)
        prompt = qwen.await_args_list[1].kwargs["prompt"]
        self.assertNotIn("PREVIOUS", prompt)
        self.assertNotIn("previous-only-sentinel", prompt)

    async def test_10_target_only_prompt_cannot_borrow_next_evidence(self) -> None:
        _result, qwen = await self._recover_order(3)
        prompt = qwen.await_args_list[1].kwargs["prompt"]
        self.assertNotIn("NEXT", prompt)
        self.assertNotIn("next-only-sentinel", prompt)

    async def test_11_no_progress_stops_after_one_regeneration_and_one_reconsideration(self) -> None:
        text = "证据只重复同一个状态。"
        collapsed = observable(6, text, collapse="representative_end")
        qwen = mock.AsyncMock(side_effect=[
            ({}, {"patch": {"video_end_state": collapsed["video_end_state"]}}, {}),
            ({}, {"shots": [copy.deepcopy(collapsed)]}, {}),
            ({}, {
                "temporal_mode": "observable_transition",
                "temporal_mode_reason": "no different supported classification",
                "temporal_mode_evidence_ids": ["E006"],
            }, {}),
        ])
        with mock.patch.object(runtime, "_qwen", qwen):
            result = await runtime._temporal_recovery_controller(
                {}, candidate=collapsed, beat=beat(6, text),
                locked_evidence=[anchor(6, text)],
                audit_failure_metadata={"failed_rule": "state_order"},
                context="matrix no progress",
            )
        self.assertEqual(result["decision"], runtime._TEMPORAL_RECOVERY_REJECT)
        self.assertEqual(qwen.await_count, 3)
        self.assertEqual(result["metadata"]["repair_progress"], "controller_rejected_no_progress")

    async def test_12_strict_shot_v2_original_collapse_rule_is_unchanged(self) -> None:
        text = "同一状态不能伪装成时间推进。"
        row = observable(12, text, collapse="representative_end")
        with self.assertRaises(runtime.Stage04RepairInvariantError):
            runtime.validate_rows(
                {}, raw_rows=[row], compact_beats=[beat(12, text)],
                allowed_chars=set(), allowed_props=set(),
                anchors=[anchor(12, text)], scene_id="scene", episode_id="episode",
            )


if __name__ == "__main__":
    unittest.main()
