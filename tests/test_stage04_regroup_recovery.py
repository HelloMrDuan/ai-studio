from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path
from unittest import mock

from app import stage04_v238_runtime as runtime


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/e7a89a6_stage04_regroup_scene1_beat3_shot1.json"


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def evidence_builder(*, source: str, batch: list[dict], max_context_chars: int = 1900):
    del max_context_chars
    anchors = []
    mapping = {}
    for beat in batch:
        ids = []
        for span in beat.get("source_evidence_spans") or []:
            anchor_id = f"E{len(anchors) + 1:03d}"
            anchors.append({
                "id": anchor_id,
                "text": span["text"],
                "beat_order": beat["order"],
                "source_start": span["start"],
                "source_end": span["end"],
            })
            ids.append(anchor_id)
        mapping[beat["order"]] = ids
    return source, anchors, mapping


class Stage04RegroupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_real_string_violations_recover_canonical_rules(self) -> None:
        issues = runtime._issues_for_shot(fixture()["audit"], 1)
        self.assertEqual(
            {runtime._canonical_audit_code(issue) for issue in issues},
            {"evidence_entailment", "future_preconsumption"},
        )

    def test_real_beat3_adjacent_evidence_changes_fingerprint(self) -> None:
        data = fixture()
        source_window, anchors, beats, metadata = runtime._reselect_adjacent_evidence(
            {"_studio_v2371e_batch_evidence": evidence_builder},
            source=data["source"],
            target_beat=data["target_beat"],
            all_beats=[data["previous_beat"], data["target_beat"]],
            current_compact_beats=[data["covered_beat"]],
            current_anchors=[data["anchor"]],
        )
        self.assertEqual(source_window, data["source"])
        self.assertNotEqual(
            metadata["evidence_fingerprint_before"],
            metadata["evidence_fingerprint_after"],
        )
        self.assertEqual(beats[0]["lineage_beat_orders"], [2, 3])
        self.assertEqual(
            beats[0]["source_evidence"],
            ["途中遇到守护神兽，", "最终获得认可。"],
        )
        self.assertEqual(len(anchors), 2)

    def test_regroup_without_adjacent_evidence_fails_once_without_qwen(self) -> None:
        data = fixture()
        qwen = mock.AsyncMock()
        with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
            runtime._reselect_adjacent_evidence(
                {
                    "_studio_v2371e_batch_evidence": evidence_builder,
                    "_studio_v2371a_qwen_call": qwen,
                },
                source=data["source"],
                target_beat=data["target_beat"],
                all_beats=[data["target_beat"]],
                current_compact_beats=[data["covered_beat"]],
                current_anchors=[data["anchor"]],
            )
        self.assertEqual(qwen.await_count, 0)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "evidence_regroup_no_progress",
        )
        self.assertEqual(
            captured.exception.metadata["evidence_fingerprint_before"],
            captured.exception.metadata["evidence_fingerprint_after"],
        )

    async def test_no_progress_enters_recovery_and_regenerates_new_shot_once(self) -> None:
        data = fixture()
        original = data["pre_repair_shot"]
        regenerated = {
            **original,
            "summary": "少年遇到守护神兽后获得认可。",
            "action": "守护神兽由注视少年推进到明确认可少年。",
            "video_start_state": "少年与守护神兽已经相遇，神兽正在注视少年。",
            "representative_state": "守护神兽观察少年的表现。",
            "video_end_state": "守护神兽明确认可少年。",
            "source_evidence_ids": ["E001", "E002"],
        }
        audits = [data["audit"], {"valid": True, "violations": []}]

        async def audit_fn(**_kwargs):
            return audits.pop(0)

        async def keep_rows(_env, *, rows, **_kwargs):
            return rows

        repair_error = runtime.Stage04ShotRepairError(
            "no progress",
            metadata={
                "repair_progress": "needs_regrouping_or_evidence_selection",
                "failed_rules": ["evidence_entailment", "future_preconsumption"],
            },
        )
        repair = mock.AsyncMock(side_effect=repair_error)
        regenerate = mock.AsyncMock(return_value=[regenerated])
        previous_shot = {
            **original,
            "shot_id": "Shot passed Beat 2",
            "covered_beat_orders": [2],
            "source_evidence_ids": ["E001"],
        }
        env = {
            "_studio_v2371e_batch_evidence": evidence_builder,
            "_studio_v2371_audit_batch": audit_fn,
        }
        with (
            mock.patch.object(
                runtime,
                "_generate_missing_beat_shots",
                mock.AsyncMock(return_value=[original]),
            ),
            mock.patch.object(runtime, "_ensure_batch_coverage", keep_rows),
            mock.patch.object(runtime, "_repair_batch", repair),
            mock.patch.object(
                runtime,
                "_regenerate_shot_from_reselected_evidence",
                regenerate,
            ),
            mock.patch.object(
                runtime,
                "_boundary_audit",
                mock.AsyncMock(return_value={"valid": True}),
            ),
        ):
            rows = await runtime._produce_batch(
                env,
                batch=[data["target_beat"]],
                all_beats=[data["previous_beat"], data["target_beat"]],
                batch_index=0,
                batch_total=1,
                source=data["source"],
                scene={"scene_id": "scene-1", "episode_id": "ep-1"},
                scene_index=1,
                scene_total=1,
                previous_shot=previous_shot,
                allowed_chars=set(),
                allowed_props=set(),
                entity_rows=[],
                resolved_text="",
                character_anchor="",
                visual_anchor="",
                user_input="",
            )

        self.assertEqual(rows[0]["summary"], regenerated["summary"])
        self.assertEqual(repair.await_count, 1)
        self.assertEqual(regenerate.await_count, 1)
        call = regenerate.await_args.kwargs
        self.assertEqual(call["target_order"], 3)
        self.assertEqual(call["previous_shot"]["shot_id"], "Shot passed Beat 2")
        self.assertEqual(call["compact_beat"]["lineage_beat_orders"], [2, 3])
        self.assertEqual(len(audits), 0)
        diagnostics = rows[0]["_regroup_recovery_diagnostics"]
        self.assertEqual(
            diagnostics["repair_progress"],
            "regenerated_shot_passed_strict_audit",
        )
        self.assertEqual(diagnostics["recovery_usage"], {
            "scoped_repair": 1,
            "evidence_regroup": 1,
            "shot_regeneration": 1,
            "final_strict_audit": 1,
        })
        self.assertEqual(runtime._SHOT_RECOVERY_BUDGET, {
            "scoped_repair": 1,
            "evidence_regroup": 1,
            "shot_regeneration": 1,
            "final_strict_audit": 1,
        })

    async def test_recovery_final_audit_failure_does_not_repair_regenerated_shot(self) -> None:
        data = fixture()
        regenerate = mock.AsyncMock(return_value=[data["pre_repair_shot"]])
        audit = mock.AsyncMock(return_value=data["audit"])
        with mock.patch.object(
            runtime,
            "_regenerate_shot_from_reselected_evidence",
            regenerate,
        ):
            with self.assertRaises(runtime.Stage04ShotRepairError) as captured:
                await runtime._recover_single_beat_after_scoped_repair(
                    {"_studio_v2371e_batch_evidence": evidence_builder},
                    source=data["source"],
                    target_beat=data["target_beat"],
                    all_beats=[data["previous_beat"], data["target_beat"]],
                    current_compact_beats=[data["covered_beat"]],
                    current_anchors=[data["anchor"]],
                    previous_shot=None,
                    next_beat=None,
                    allowed_chars=set(),
                    allowed_props=set(),
                    scene_id="scene-1",
                    episode_id="ep-1",
                    audit_fn=audit,
                    prior_metadata={"repair_progress": "no_semantic_progress"},
                )
        self.assertEqual(regenerate.await_count, 1)
        self.assertEqual(audit.await_count, 1)
        self.assertEqual(
            captured.exception.metadata["repair_progress"],
            "regenerated_shot_failed_strict_audit",
        )


class Stage04RecoveryProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tools.preflight_runtime_inspect import (
            _configure_isolated_paths,
            _install_import_shims,
        )

        _configure_isolated_paths()
        _install_import_shims()
        cls.main = importlib.import_module("app.main")

    def test_progress_adapter_displays_real_recovery_phase(self) -> None:
        task = {
            "status": "running",
            "phase_index": 5,
            "phase_total": 6,
            "phase_name": "Regroup recovery",
            "message": "正在重新选择镜头证据",
            "scene_total": 1,
            "scene_done": 0,
        }
        project = {
            "project_id": "b" * 24,
            "current_stage": "04",
            "completed_stages": ["01", "02", "03"],
            "stage_state": {"04": {}},
        }
        with mock.patch.object(
            self.main,
            "_studio_v23963_current_stage04_task",
            return_value=task,
        ):
            row = self.main._studio_stage04_progress(project, None)
        self.assertEqual(row["current_step"], 5)
        self.assertEqual(row["total_steps"], 6)
        self.assertEqual(row["completed_steps"], 4)
        self.assertEqual(row["percent"], 66)
        self.assertEqual(row["current_step_name"], "Regroup recovery")
        self.assertEqual(row["current_action"], "正在重新选择镜头证据")


if __name__ == "__main__":
    unittest.main()
