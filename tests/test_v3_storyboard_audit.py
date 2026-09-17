from __future__ import annotations

import unittest

from app.v3.storyboard import BeatContract, EvidenceSpan, StoryboardShot, TemporalMode, audit_shot


class XiaoduanV3StoryboardAuditTests(unittest.TestCase):
    def _beat(self, order: int = 2) -> BeatContract:
        return BeatContract(
            order=order,
            summary="少年在雪山遇到守护神兽",
            evidence=(
                EvidenceSpan(
                    evidence_id=f"E{order:03d}",
                    beat_order=order,
                    source_start=20,
                    source_end=32,
                    text="途中遇到守护神兽",
                ),
            ),
            character_entity_ids=("char_hero",),
            creature_entity_ids=("creature_guardian",),
            location_entity_ids=("loc_snow_mountain",),
        )

    def _observable(self) -> StoryboardShot:
        return StoryboardShot(
            shot_id="shot-002",
            beat_order=2,
            summary="少年在雪山遇到守护神兽",
            source_fact="途中遇到守护神兽",
            temporal_mode=TemporalMode.observable_transition,
            temporal_mode_reason="当前 Beat 证据支持可见遭遇过程",
            temporal_mode_evidence_ids=("E002",),
            source_evidence_ids=("E002",),
            covered_beat_orders=(2,),
            entity_ids=("char_hero", "creature_guardian", "loc_snow_mountain"),
            narrative_start_state="少年独自在雪地前行",
            narrative_state="少年停步看见守护神兽",
            narrative_end_state="少年与守护神兽隔雪对峙",
        )

    def test_valid_observable_transition_passes_deterministic_boundary(self) -> None:
        result = audit_shot(self._beat(), self._observable())
        self.assertTrue(result.ok)
        self.assertTrue(result.semantic_audit_required)

    def test_future_beat_is_never_borrowed(self) -> None:
        shot = self._observable().model_copy(update={"covered_beat_orders": (2, 3)})
        result = audit_shot(self._beat(), shot)
        self.assertFalse(result.ok)
        self.assertIn("FUTURE_BEAT_PRECONSUMPTION", [item.code for item in result.violations])

    def test_previous_or_foreign_evidence_is_rejected(self) -> None:
        shot = self._observable().model_copy(
            update={"source_evidence_ids": ("E001", "E002"), "temporal_mode_evidence_ids": ("E001",)}
        )
        result = audit_shot(self._beat(), shot)
        codes = {item.code for item in result.violations}
        self.assertIn("FOREIGN_EVIDENCE", codes)
        self.assertIn("FOREIGN_TEMPORAL_EVIDENCE", codes)

    def test_observable_transition_state_collapse_is_rejected(self) -> None:
        shot = self._observable().model_copy(
            update={"narrative_state": "少年独自在雪地前行"}
        )
        result = audit_shot(self._beat(), shot)
        self.assertIn("OBSERVABLE_STATE_COLLAPSE", [item.code for item in result.violations])

    def test_static_outcome_locks_narrative_fact_but_allows_presentation_variation(self) -> None:
        beat = self._beat()
        fact = "守护神兽守在雪山古道上"
        shot = StoryboardShot(
            shot_id="shot-static",
            beat_order=2,
            summary=fact,
            source_fact=fact,
            temporal_mode=TemporalMode.static_outcome,
            temporal_mode_reason="证据只证明稳定结果",
            temporal_mode_evidence_ids=("E002",),
            source_evidence_ids=("E002",),
            covered_beat_orders=(2,),
            entity_ids=("creature_guardian", "loc_snow_mountain"),
            narrative_start_state=fact,
            narrative_state=fact,
            narrative_end_state=fact,
            visual_start_frame="远景：雪山古道与守护神兽形成稳定构图",
            representative_frame="中景：守护神兽保持同一姿态，突出轮廓",
            visual_end_frame="侧向构图：同一稳定事实，仅改变呈现角度",
            realization_scope="presentation_only",
        )
        result = audit_shot(beat, shot)
        self.assertTrue(result.ok)

    def test_static_narrative_drift_is_rejected(self) -> None:
        fact = "守护神兽守在雪山古道上"
        shot = StoryboardShot(
            shot_id="shot-static",
            beat_order=2,
            summary=fact,
            source_fact=fact,
            temporal_mode=TemporalMode.static_outcome,
            temporal_mode_reason="证据只证明稳定结果",
            temporal_mode_evidence_ids=("E002",),
            source_evidence_ids=("E002",),
            covered_beat_orders=(2,),
            entity_ids=("creature_guardian",),
            narrative_start_state="神兽尚未出现",
            narrative_state=fact,
            narrative_end_state=fact,
            visual_start_frame="A",
            representative_frame="B",
            visual_end_frame="C",
            realization_scope="presentation_only",
        )
        result = audit_shot(self._beat(), shot)
        self.assertIn("STATIC_NARRATIVE_DRIFT", [item.code for item in result.violations])

    def test_unbound_entity_is_rejected(self) -> None:
        shot = self._observable().model_copy(update={"entity_ids": ("char_hero", "wolf_other")})
        result = audit_shot(self._beat(), shot)
        self.assertIn("UNBOUND_ENTITY", [item.code for item in result.violations])


if __name__ == "__main__":
    unittest.main()
