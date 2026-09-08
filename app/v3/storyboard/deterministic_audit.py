from __future__ import annotations

from dataclasses import dataclass

from .contracts import BeatContract, StoryboardShot, TemporalMode


@dataclass(frozen=True)
class AuditViolation:
    code: str
    message: str


@dataclass(frozen=True)
class DeterministicShotAudit:
    ok: bool
    violations: tuple[AuditViolation, ...]
    semantic_audit_required: bool


def _normalized(value: str) -> str:
    return " ".join(str(value or "").split())


def audit_shot(beat: BeatContract, shot: StoryboardShot) -> DeterministicShotAudit:
    """Audit strict rules that do not require an LLM.

    This is the V3 extraction boundary for stable `strict-shot-v2` invariants.
    Semantic entailment remains a separate semantic-audit concern and malformed
    semantic-auditor output must never be routed into temporal recovery.
    """

    violations: list[AuditViolation] = []
    allowed_evidence = beat.allowed_evidence_ids
    source_evidence = set(shot.source_evidence_ids)
    temporal_evidence = set(shot.temporal_mode_evidence_ids)

    if shot.beat_order != beat.order:
        violations.append(AuditViolation("BEAT_ORDER_MISMATCH", "shot beat_order does not match target Beat"))

    if not shot.covered_beat_orders or beat.order not in shot.covered_beat_orders:
        violations.append(AuditViolation("BEAT_COVERAGE_MISSING", "target Beat is not covered by the shot"))

    future_orders = sorted(order for order in shot.covered_beat_orders if order > beat.order)
    if future_orders:
        violations.append(
            AuditViolation(
                "FUTURE_BEAT_PRECONSUMPTION",
                f"shot covers future Beat orders: {future_orders}",
            )
        )

    foreign_source = sorted(source_evidence - allowed_evidence)
    if foreign_source:
        violations.append(
            AuditViolation(
                "FOREIGN_EVIDENCE",
                f"shot uses evidence outside current Beat: {foreign_source}",
            )
        )

    foreign_temporal = sorted(temporal_evidence - allowed_evidence)
    if foreign_temporal:
        violations.append(
            AuditViolation(
                "FOREIGN_TEMPORAL_EVIDENCE",
                f"temporal mode uses evidence outside current Beat: {foreign_temporal}",
            )
        )

    if not source_evidence:
        violations.append(AuditViolation("SOURCE_EVIDENCE_MISSING", "shot has no locked source evidence"))
    if not temporal_evidence:
        violations.append(AuditViolation("TEMPORAL_EVIDENCE_MISSING", "shot has no temporal evidence"))

    unexpected_entities = sorted(set(shot.entity_ids) - beat.allowed_entity_ids)
    if unexpected_entities:
        violations.append(
            AuditViolation(
                "UNBOUND_ENTITY",
                f"shot contains entities not bound to the Beat: {unexpected_entities}",
            )
        )

    start = _normalized(shot.narrative_start_state)
    middle = _normalized(shot.narrative_state)
    end = _normalized(shot.narrative_end_state)
    source_fact = _normalized(shot.source_fact)

    if shot.temporal_mode == TemporalMode.static_outcome:
        if not source_fact or not (start == middle == end == source_fact):
            violations.append(
                AuditViolation(
                    "STATIC_NARRATIVE_DRIFT",
                    "static_outcome must keep start/representative/end equal to source_fact",
                )
            )
        frames = tuple(
            _normalized(value)
            for value in (
                shot.visual_start_frame,
                shot.representative_frame,
                shot.visual_end_frame,
            )
        )
        if any(not value for value in frames) or len(set(frames)) != 3:
            violations.append(
                AuditViolation(
                    "STATIC_PRESENTATION_COLLAPSE",
                    "static_outcome requires three distinct presentation frames without narrative progression",
                )
            )
        if shot.realization_scope != "presentation_only":
            violations.append(
                AuditViolation(
                    "STATIC_REALIZATION_SCOPE_INVALID",
                    "static_outcome presentation variation must be presentation_only",
                )
            )

    elif shot.temporal_mode == TemporalMode.observable_transition:
        if not start or not middle or not end:
            violations.append(
                AuditViolation(
                    "OBSERVABLE_STATE_MISSING",
                    "observable_transition requires start, representative and end states",
                )
            )
        elif start == middle or middle == end or start == end:
            violations.append(
                AuditViolation(
                    "OBSERVABLE_STATE_COLLAPSE",
                    "observable_transition states must form three distinguishable narrative states",
                )
            )

    elif shot.temporal_mode == TemporalMode.insufficient_visual_evidence:
        # This mode is an explicit fail-closed outcome. It is valid as a
        # semantic record but not sufficient to proceed directly to generation.
        violations.append(
            AuditViolation(
                "VISUAL_EVIDENCE_INSUFFICIENT",
                "shot requires review/re-resolution before visual generation",
            )
        )

    return DeterministicShotAudit(
        ok=not violations,
        violations=tuple(violations),
        semantic_audit_required=not violations,
    )
