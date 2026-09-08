"""Xiaoduan V3 storyboard domain; this is the Stage04 replacement boundary."""

from .contracts import BeatContract, EvidenceSpan, StoryboardShot, TemporalMode
from .deterministic_audit import AuditViolation, DeterministicShotAudit, audit_shot
from .pipeline import (
    BeatExecutionReceipt,
    StoryboardDeterministicFailure,
    StoryboardExecutionResult,
    StoryboardPipeline,
    StoryboardPipelineError,
    StoryboardSemanticFailure,
)

__all__ = [
    "AuditViolation",
    "BeatContract",
    "BeatExecutionReceipt",
    "DeterministicShotAudit",
    "EvidenceSpan",
    "StoryboardDeterministicFailure",
    "StoryboardExecutionResult",
    "StoryboardPipeline",
    "StoryboardPipelineError",
    "StoryboardSemanticFailure",
    "StoryboardShot",
    "TemporalMode",
    "audit_shot",
]
