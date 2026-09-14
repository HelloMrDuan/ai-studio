from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from app.v3.audit.semantic import (
    AuditProtocolError,
    SemanticAuditResult,
    parse_semantic_audit,
    semantic_audit_passed,
)

from .contracts import BeatContract, StoryboardShot, TemporalMode
from .deterministic_audit import AuditViolation, audit_shot


class StoryboardPipelineError(RuntimeError):
    """Base V3 storyboard-domain failure."""


class StoryboardSemanticFailure(StoryboardPipelineError):
    """A valid semantic audit rejected the shot after bounded recovery."""


class StoryboardDeterministicFailure(StoryboardPipelineError):
    """Deterministic strict-shot invariants could not be recovered."""


class ShotGenerator(Protocol):
    async def __call__(
        self,
        beat: BeatContract,
        *,
        attempt: int,
        recovery_reason: str,
        requested_temporal_mode: TemporalMode | None,
    ) -> StoryboardShot: ...


class SemanticAuditor(Protocol):
    async def __call__(
        self,
        beat: BeatContract,
        shot: StoryboardShot,
    ) -> str | dict[str, Any] | SemanticAuditResult: ...


@dataclass(frozen=True)
class BeatExecutionReceipt:
    beat_order: int
    shot: StoryboardShot
    attempts: int
    deterministic_violations_seen: tuple[str, ...]
    semantic_violations_seen: tuple[str, ...]


@dataclass(frozen=True)
class StoryboardExecutionResult:
    shots: tuple[StoryboardShot, ...]
    receipts: tuple[BeatExecutionReceipt, ...]


_RECOVERABLE_DETERMINISTIC_CODES = frozenset(
    {
        "STATIC_NARRATIVE_DRIFT",
        "STATIC_PRESENTATION_COLLAPSE",
        "STATIC_REALIZATION_SCOPE_INVALID",
        "OBSERVABLE_STATE_MISSING",
        "OBSERVABLE_STATE_COLLAPSE",
    }
)

_NON_RECOVERABLE_BOUNDARY_CODES = frozenset(
    {
        "BEAT_ORDER_MISMATCH",
        "BEAT_COVERAGE_MISSING",
        "FUTURE_BEAT_PRECONSUMPTION",
        "FOREIGN_EVIDENCE",
        "FOREIGN_TEMPORAL_EVIDENCE",
        "SOURCE_EVIDENCE_MISSING",
        "TEMPORAL_EVIDENCE_MISSING",
        "UNBOUND_ENTITY",
        "VISUAL_EVIDENCE_INSUFFICIENT",
    }
)


def _audit_result(payload: str | dict[str, Any] | SemanticAuditResult) -> SemanticAuditResult:
    if isinstance(payload, SemanticAuditResult):
        return payload
    return parse_semantic_audit(payload)


def _codes(violations: tuple[AuditViolation, ...]) -> tuple[str, ...]:
    return tuple(item.code for item in violations)


def _recovery_mode(shot: StoryboardShot, violations: tuple[AuditViolation, ...]) -> TemporalMode | None:
    codes = set(_codes(violations))
    if "STATIC_NARRATIVE_DRIFT" in codes or "STATIC_PRESENTATION_COLLAPSE" in codes:
        return TemporalMode.static_outcome
    if "OBSERVABLE_STATE_MISSING" in codes or "OBSERVABLE_STATE_COLLAPSE" in codes:
        return TemporalMode.observable_transition
    return shot.temporal_mode


def _semantic_recovery_reason(result: SemanticAuditResult) -> str:
    if result.violations:
        return "semantic:" + " | ".join(result.violations)
    failed = [
        field
        for field in (
            "evidence_entailment_ok",
            "beat_coverage_ok",
            "temporal_monotonic",
            "no_future_event_preconsumption",
            "no_result_duplication",
            "state_order_valid",
            "entity_visibility_valid",
            "visual_realization_valid",
        )
        if getattr(result, field) is not True
    ]
    return "semantic:" + ",".join(failed)


class StoryboardPipeline:
    """V3 replacement for the active Stage04 repair/regroup runtime.

    Core invariants:
    - the generator receives exactly one BeatContract; adjacent Beats are never
      passed into generation or recovery;
    - deterministic boundary violations never widen evidence and fail closed;
    - only current-Beat temporal/presentation failures are eligible for a
      bounded regeneration;
    - a valid semantic audit failure can request the same current Beat again;
    - malformed semantic-auditor output raises AuditProtocolError immediately
      and is never converted into semantic recovery.

    Infrastructure retries belong to Temporal Activities, outside this domain
    controller. This controller only owns bounded semantic regeneration.
    """

    def __init__(self, *, max_semantic_attempts: int = 2) -> None:
        if max_semantic_attempts < 1 or max_semantic_attempts > 4:
            raise ValueError("max_semantic_attempts must be between 1 and 4")
        self.max_semantic_attempts = max_semantic_attempts

    async def execute(
        self,
        beats: tuple[BeatContract, ...] | list[BeatContract],
        *,
        generator: ShotGenerator,
        semantic_auditor: SemanticAuditor | None = None,
    ) -> StoryboardExecutionResult:
        ordered = tuple(beats)
        if not ordered:
            raise StoryboardPipelineError("storyboard requires at least one Beat")
        if tuple(item.order for item in ordered) != tuple(sorted(item.order for item in ordered)):
            raise StoryboardPipelineError("Beats must be supplied in source order")
        if len({item.order for item in ordered}) != len(ordered):
            raise StoryboardPipelineError("duplicate Beat order")

        shots: list[StoryboardShot] = []
        receipts: list[BeatExecutionReceipt] = []
        for beat in ordered:
            shot, receipt = await self._execute_beat(
                beat,
                generator=generator,
                semantic_auditor=semantic_auditor,
            )
            shots.append(shot)
            receipts.append(receipt)
        return StoryboardExecutionResult(shots=tuple(shots), receipts=tuple(receipts))

    async def _execute_beat(
        self,
        beat: BeatContract,
        *,
        generator: ShotGenerator,
        semantic_auditor: SemanticAuditor | None,
    ) -> tuple[StoryboardShot, BeatExecutionReceipt]:
        deterministic_seen: list[str] = []
        semantic_seen: list[str] = []
        reason = "initial"
        requested_mode: TemporalMode | None = None

        for attempt in range(1, self.max_semantic_attempts + 1):
            shot = await generator(
                beat,
                attempt=attempt,
                recovery_reason=reason,
                requested_temporal_mode=requested_mode,
            )
            if not isinstance(shot, StoryboardShot):
                raise StoryboardPipelineError("shot generator must return StoryboardShot")

            deterministic = audit_shot(beat, shot)
            if not deterministic.ok:
                codes = _codes(deterministic.violations)
                deterministic_seen.extend(codes)
                boundary = set(codes) & _NON_RECOVERABLE_BOUNDARY_CODES
                if boundary:
                    raise StoryboardDeterministicFailure(
                        f"Beat {beat.order} violates locked boundary: {sorted(boundary)}"
                    )
                unknown = set(codes) - _RECOVERABLE_DETERMINISTIC_CODES
                if unknown:
                    raise StoryboardDeterministicFailure(
                        f"Beat {beat.order} has non-recoverable deterministic violations: {sorted(unknown)}"
                    )
                if attempt >= self.max_semantic_attempts:
                    raise StoryboardDeterministicFailure(
                        f"Beat {beat.order} deterministic recovery budget exhausted: {list(codes)}"
                    )
                reason = "deterministic:" + ",".join(codes)
                requested_mode = _recovery_mode(shot, deterministic.violations)
                continue

            if semantic_auditor is None:
                receipt = BeatExecutionReceipt(
                    beat_order=beat.order,
                    shot=shot,
                    attempts=attempt,
                    deterministic_violations_seen=tuple(deterministic_seen),
                    semantic_violations_seen=tuple(semantic_seen),
                )
                return shot, receipt

            # AuditProtocolError deliberately escapes this method. A malformed
            # model protocol is infrastructure/model-protocol failure, not a
            # reason to regenerate the shot or widen evidence.
            semantic = _audit_result(await semantic_auditor(beat, shot))
            if semantic_audit_passed(semantic):
                receipt = BeatExecutionReceipt(
                    beat_order=beat.order,
                    shot=shot,
                    attempts=attempt,
                    deterministic_violations_seen=tuple(deterministic_seen),
                    semantic_violations_seen=tuple(semantic_seen),
                )
                return shot, receipt

            semantic_seen.extend(semantic.violations or [_semantic_recovery_reason(semantic)])
            if attempt >= self.max_semantic_attempts:
                raise StoryboardSemanticFailure(
                    f"Beat {beat.order} semantic recovery budget exhausted: {_semantic_recovery_reason(semantic)}"
                )
            reason = _semantic_recovery_reason(semantic)
            requested_mode = shot.temporal_mode

        raise AssertionError("unreachable storyboard recovery loop")


__all__ = [
    "AuditProtocolError",
    "BeatExecutionReceipt",
    "SemanticAuditor",
    "ShotGenerator",
    "StoryboardDeterministicFailure",
    "StoryboardExecutionResult",
    "StoryboardPipeline",
    "StoryboardPipelineError",
    "StoryboardSemanticFailure",
]
