from __future__ import annotations

from pathlib import Path

RUNTIME = Path("app/stage04_v238_runtime.py")
TEST = Path("tests/test_stage04_first_beat_temporal_reconsideration.py")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


def main() -> None:
    runtime = RUNTIME.read_text(encoding="utf-8")

    marker = '''                prior_metadata["edge_temporal_reconsideration"] = {
                    "repair_progress": "edge_temporal_reconsideration_failed_strict_audit",
                    "audit": copy.deepcopy(edge_audit),
                }

    _stage04_progress(
        env, 5, "Regroup recovery", "正在重新选择镜头证据"
    )
'''

    replacement = '''                prior_metadata["edge_temporal_reconsideration"] = {
                    "repair_progress": "edge_temporal_reconsideration_failed_strict_audit",
                    "audit": copy.deepcopy(edge_audit),
                }

    # Beat 1 cannot expand evidence backward because no previous Beat exists.
    # If temporal reconsideration did not close the Shot, regenerate one fresh
    # Shot from the current Beat's locked evidence only.  This is not evidence
    # expansion: NEXT_BEAT is deliberately hidden and the evidence fingerprint
    # must remain unchanged.  The regenerated Shot still has to pass the normal
    # strict-shot-v2 validator and final semantic audit.
    if target_order == 1 and previous_beat is None:
        edge_compact = next((
            copy.deepcopy(beat)
            for beat in current_compact_beats
            if isinstance(beat, dict)
            and int(beat.get("order") or 0) == target_order
        ), None)
        if edge_compact is None:
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only regeneration 缺少锁定 Beat",
                metadata={
                    "repair_progress": "edge_target_only_regeneration_scope_incomplete",
                    "prior_repair": copy.deepcopy(prior_metadata),
                    "recovery_scope": "target_evidence_only_no_future_borrowing",
                },
            )

        edge_allowed_ids = set(_id_list(
            edge_compact.get("allowed_source_evidence_ids")
            or edge_compact.get("source_evidence_ids")
        ))
        edge_anchors = [
            copy.deepcopy(anchor)
            for anchor in current_anchors
            if isinstance(anchor, dict)
            and str(anchor.get("id") or "") in edge_allowed_ids
        ]
        if not edge_allowed_ids or len(edge_anchors) != len(edge_allowed_ids):
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only regeneration 无法完整锁定当前 evidence",
                metadata={
                    "repair_progress": "edge_target_only_regeneration_scope_incomplete",
                    "prior_repair": copy.deepcopy(prior_metadata),
                    "evidence_ids": sorted(edge_allowed_ids),
                    "recovery_scope": "target_evidence_only_no_future_borrowing",
                },
            )

        edge_source_window = "\\n".join(
            str(anchor.get("text") or "")
            for anchor in edge_anchors
        )
        edge_fingerprint = _evidence_fingerprint(
            compact_beats=[edge_compact],
            anchors=edge_anchors,
        )
        edge_recovery = {
            "recovery_budget": copy.deepcopy(_SHOT_RECOVERY_BUDGET),
            "recovery_usage": {
                "scoped_repair": 1,
                "edge_temporal_reconsideration": 1 if current_rows else 0,
                "evidence_regroup": 0,
                "shot_regeneration": 1,
                "final_strict_audit": 0,
            },
            "repair_progress": "edge_target_only_regeneration_started",
            "prior_repair": copy.deepcopy(prior_metadata),
            "evidence_ids": sorted(edge_allowed_ids),
            "evidence_fingerprint_before": edge_fingerprint,
            "evidence_fingerprint_after": edge_fingerprint,
            "recovery_scope": {
                "target_beat_order": target_order,
                "mode": "target_only_shot_regeneration_no_future_borrowing",
            },
        }
        _stage04_progress(
            env, 5, "Edge recovery", "正在使用当前 Beat 锁定证据重生首镜头"
        )
        try:
            edge_regenerated = await _regenerate_shot_from_reselected_evidence(
                env,
                target_order=target_order,
                compact_beat=edge_compact,
                anchors=edge_anchors,
                previous_shot=None,
                next_beat=None,
                allowed_chars=allowed_chars,
                allowed_props=allowed_props,
                scene_id=scene_id,
                episode_id=episode_id,
            )
        except Exception as exc:
            edge_recovery.update({
                "repair_progress": "edge_target_only_regeneration_failed",
                "regroup_reason": f"{type(exc).__name__}: {exc}",
            })
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only Shot 重生失败：{exc}",
                metadata=edge_recovery,
            ) from exc

        edge_recovery["recovery_usage"]["final_strict_audit"] = 1
        edge_final_audit = await audit_fn(
            source_window=edge_source_window,
            compact_beats=[edge_compact],
            shots=edge_regenerated,
        )
        edge_recovery["final_audit"] = copy.deepcopy(edge_final_audit)
        if not _audit_ok(env, edge_final_audit):
            edge_recovery.update({
                "repair_progress": "edge_target_only_regeneration_failed_strict_audit",
                "regroup_reason": "target-only regenerated Shot did not pass strict-shot-v2",
            })
            raise Stage04ShotRepairError(
                f"Beat {target_order} target-only Shot 仍未通过 strict-shot-v2："
                + _audit_issues(edge_final_audit),
                metadata=edge_recovery,
            )

        edge_recovery["repair_progress"] = (
            "edge_target_only_regeneration_passed_strict_audit"
        )
        for edge_row in edge_regenerated:
            edge_row["_regroup_recovery_diagnostics"] = copy.deepcopy(edge_recovery)
        return edge_regenerated, edge_final_audit

    _stage04_progress(
        env, 5, "Regroup recovery", "正在重新选择镜头证据"
    )
'''
    runtime = replace_once(
        runtime,
        marker,
        replacement,
        "first-beat target-only runtime insertion",
    )
    RUNTIME.write_text(runtime, encoding="utf-8")

    tests = TEST.read_text(encoding="utf-8")
    class_marker = '''\n\nclass FirstBeatTemporalReconsiderationTests(unittest.IsolatedAsyncioTestCase):\n'''
    static_generation_helper = r'''


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
'''
    tests = replace_once(
        tests,
        class_marker,
        static_generation_helper + class_marker,
        "static generation helper insertion",
    )

    old_test = r'''    async def test_first_beat_observable_no_progress_stays_fail_closed_without_future_borrowing(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        classification = {
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "classifier still believes the evidence proves a transition",
            "temporal_mode_evidence_ids": ["E001"],
        }
        qwen = mock.AsyncMock(return_value=({}, classification, {}))

        def forbidden_builder(**_kwargs):
            raise AssertionError("no previous Beat means recovery must fail before borrowing future evidence")

        env = {"_studio_v2371e_batch_evidence": forbidden_builder}
        with mock.patch.object(runtime, "_qwen", qwen):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._recover_single_beat_after_scoped_repair(
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
                    audit_fn=mock.AsyncMock(),
                    prior_metadata={"repair_progress": "needs_regrouping_or_evidence_selection"},
                    current_rows=[_observable_row(text)],
                )
        self.assertEqual(qwen.await_count, 1)
        self.assertIn("无可用前序相邻证据", str(captured.exception))
'''

    new_test = r'''    async def test_first_beat_observable_no_progress_regenerates_from_target_only_evidence(self) -> None:
        text = "一个少年在雪山寻找失落古剑。"
        classification = {
            "temporal_mode": "observable_transition",
            "temporal_mode_reason": "classifier still believes the evidence proves a transition",
            "temporal_mode_evidence_ids": ["E001"],
        }
        generation = {"shots": [_static_generation_row(text)]}
        qwen = mock.AsyncMock(side_effect=[
            ({}, classification, {}),
            ({}, generation, {}),
        ])

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
                prior_metadata={"repair_progress": "needs_regrouping_or_evidence_selection"},
                current_rows=[_observable_row(text)],
            )
        self.assertEqual(qwen.await_count, 2)
        self.assertTrue(audit["valid"])
        self.assertEqual(rows[0]["temporal_mode"], "static_outcome")
        self.assertEqual(rows[0]["source_evidence_ids"], ["E001"])
        second_prompt = qwen.await_args_list[1].kwargs["prompt"]
        self.assertNotIn("守护神兽", second_prompt)
        diagnostics = rows[0]["_regroup_recovery_diagnostics"]
        self.assertEqual(
            diagnostics["repair_progress"],
            "edge_target_only_regeneration_passed_strict_audit",
        )
        self.assertEqual(diagnostics["recovery_usage"]["evidence_regroup"], 0)
        self.assertEqual(diagnostics["recovery_usage"]["shot_regeneration"], 1)
        self.assertEqual(
            diagnostics["evidence_fingerprint_before"],
            diagnostics["evidence_fingerprint_after"],
        )
'''
    tests = replace_once(
        tests,
        old_test,
        new_test,
        "first-beat no-progress regression replacement",
    )
    TEST.write_text(tests, encoding="utf-8")


if __name__ == "__main__":
    main()
