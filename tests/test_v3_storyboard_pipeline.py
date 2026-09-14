from __future__ import annotations

import pathlib
import unittest

from app.v3.audit.semantic import AuditProtocolError
from app.v3.storyboard.contracts import BeatContract, EvidenceSpan, StoryboardShot, TemporalMode
from app.v3.storyboard.pipeline import (
    StoryboardDeterministicFailure,
    StoryboardPipeline,
    StoryboardSemanticFailure,
)


def beat(order: int = 1) -> BeatContract:
    evidence = EvidenceSpan(
        evidence_id=f"E{order:03d}",
        beat_order=order,
        source_start=order * 10,
        source_end=order * 10 + 10,
        text=f"Beat {order} evidence",
    )
    return BeatContract(
        order=order,
        summary=f"Beat {order}",
        state_change=f"Beat {order} changes",
        evidence=(evidence,),
        character_entity_ids=("char_hero",),
    )


def valid_shot(order: int = 1, *, mode: TemporalMode = TemporalMode.observable_transition) -> StoryboardShot:
    eid = f"E{order:03d}"
    if mode == TemporalMode.static_outcome:
        fact = f"Beat {order} stable fact"
        return StoryboardShot(
            shot_id=f"shot-{order}", beat_order=order, summary=fact, source_fact=fact,
            temporal_mode=mode, temporal_mode_reason="stable current Beat evidence",
            temporal_mode_evidence_ids=(eid,), source_evidence_ids=(eid,),
            covered_beat_orders=(order,), entity_ids=("char_hero",),
            narrative_start_state=fact, narrative_state=fact, narrative_end_state=fact,
            visual_start_frame=f"wide presentation of {fact}",
            representative_frame=f"medium presentation of {fact}",
            visual_end_frame=f"detail presentation of {fact}", realization_scope="presentation_only",
        )
    return StoryboardShot(
        shot_id=f"shot-{order}", beat_order=order, summary=f"Beat {order}", source_fact=f"Beat {order} fact",
        temporal_mode=mode, temporal_mode_reason="visible transition in current Beat evidence",
        temporal_mode_evidence_ids=(eid,), source_evidence_ids=(eid,),
        covered_beat_orders=(order,), entity_ids=("char_hero",),
        narrative_start_state="before", narrative_state="during", narrative_end_state="after",
        visual_start_frame="before frame", representative_frame="during frame", visual_end_frame="after frame",
    )


PASS_AUDIT = {
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


class StoryboardV3PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_beats_receive_only_their_own_contract(self) -> None:
        seen = []

        async def generator(current, **kwargs):
            seen.append((current.order, tuple(e.evidence_id for e in current.evidence)))
            return valid_shot(current.order)

        async def auditor(current, shot):
            return PASS_AUDIT

        result = await StoryboardPipeline(max_semantic_attempts=2).execute(
            [beat(1), beat(2), beat(3)], generator=generator, semantic_auditor=auditor
        )
        self.assertEqual([item.shot_id for item in result.shots], ["shot-1", "shot-2", "shot-3"])
        self.assertEqual(seen, [(1, ("E001",)), (2, ("E002",)), (3, ("E003",))])

    async def test_future_beat_borrowing_fails_without_regeneration(self) -> None:
        calls = 0

        async def generator(current, **kwargs):
            nonlocal calls
            calls += 1
            shot = valid_shot(current.order)
            return shot.model_copy(update={"covered_beat_orders": (current.order, current.order + 1)})

        with self.assertRaises(StoryboardDeterministicFailure):
            await StoryboardPipeline().execute([beat(1)], generator=generator)
        self.assertEqual(calls, 1)

    async def test_observable_collapse_gets_one_current_beat_regeneration(self) -> None:
        calls = []

        async def generator(current, *, attempt, recovery_reason, requested_temporal_mode):
            calls.append((current.order, attempt, recovery_reason, requested_temporal_mode))
            shot = valid_shot(current.order)
            if attempt == 1:
                return shot.model_copy(update={"narrative_state": "before"})
            return shot

        result = await StoryboardPipeline(max_semantic_attempts=2).execute([beat(2)], generator=generator)
        self.assertEqual(result.receipts[0].attempts, 2)
        self.assertEqual(calls[1][0], 2)
        self.assertIn("OBSERVABLE_STATE_COLLAPSE", calls[1][2])
        self.assertEqual(calls[1][3], TemporalMode.observable_transition)

    async def test_static_recovery_stays_static_and_current_beat_only(self) -> None:
        calls = []

        async def generator(current, *, attempt, recovery_reason, requested_temporal_mode):
            calls.append((current.order, attempt, requested_temporal_mode))
            shot = valid_shot(current.order, mode=TemporalMode.static_outcome)
            if attempt == 1:
                return shot.model_copy(update={"visual_end_frame": shot.representative_frame})
            return shot

        result = await StoryboardPipeline(max_semantic_attempts=2).execute([beat(3)], generator=generator)
        self.assertEqual(result.receipts[0].attempts, 2)
        self.assertEqual(calls[1], (3, 2, TemporalMode.static_outcome))

    async def test_malformed_semantic_audit_is_protocol_error_not_recovery(self) -> None:
        generator_calls = 0

        async def generator(current, **kwargs):
            nonlocal generator_calls
            generator_calls += 1
            return valid_shot(current.order)

        async def auditor(current, shot):
            return {"code": "bad_gateway", "message": "schema missing", "source": "model"}

        with self.assertRaises(AuditProtocolError):
            await StoryboardPipeline(max_semantic_attempts=2).execute(
                [beat(1)], generator=generator, semantic_auditor=auditor
            )
        self.assertEqual(generator_calls, 1)

    async def test_valid_semantic_failure_is_bounded(self) -> None:
        calls = 0

        async def generator(current, **kwargs):
            nonlocal calls
            calls += 1
            return valid_shot(current.order)

        async def auditor(current, shot):
            payload = dict(PASS_AUDIT)
            payload["evidence_entailment_ok"] = False
            payload["violations"] = ["shot adds an unsupported event"]
            return payload

        with self.assertRaises(StoryboardSemanticFailure):
            await StoryboardPipeline(max_semantic_attempts=2).execute(
                [beat(1)], generator=generator, semantic_auditor=auditor
            )
        self.assertEqual(calls, 2)

    def test_v3_runtime_never_imports_legacy_stage04(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1] / "app" / "v3"
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "stage04_v238_runtime" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
