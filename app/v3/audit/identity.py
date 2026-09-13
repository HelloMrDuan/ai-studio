from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class IdentityAuditDecision(str, Enum):
    pass_ = "pass"
    review_required = "review_required"
    fail = "fail"


class IdentityAuditPolicy(BaseModel):
    """Calibrated identity thresholds for one recognition model/domain.

    Thresholds are deliberately caller-provided. There is no universal
    InsightFace/ArcFace cosine threshold that is safe to hard-code for every
    project, image domain, crop quality, pose distribution and false-accept
    target.
    """

    policy_id: str = Field(min_length=1)
    recognition_model: str = Field(min_length=1)
    pass_threshold: float = Field(ge=-1.0, le=1.0)
    fail_threshold: float = Field(ge=-1.0, le=1.0)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "IdentityAuditPolicy":
        if self.fail_threshold >= self.pass_threshold:
            raise ValueError("fail_threshold must be lower than pass_threshold")
        return self


class IdentityAuditResult(BaseModel):
    cosine_similarity: float = Field(ge=-1.0, le=1.0)
    decision: IdentityAuditDecision
    policy_id: str
    recognition_model: str
    pass_threshold: float
    fail_threshold: float


def evaluate_identity_similarity(
    cosine_similarity: float,
    *,
    policy: IdentityAuditPolicy,
) -> IdentityAuditResult:
    """Apply only a calibrated deterministic policy to a measured cosine score."""

    score = float(cosine_similarity)
    if not -1.0 <= score <= 1.0:
        raise ValueError("cosine_similarity must be within [-1, 1]")

    if score >= policy.pass_threshold:
        decision = IdentityAuditDecision.pass_
    elif score <= policy.fail_threshold:
        decision = IdentityAuditDecision.fail
    else:
        decision = IdentityAuditDecision.review_required

    return IdentityAuditResult(
        cosine_similarity=score,
        decision=decision,
        policy_id=policy.policy_id,
        recognition_model=policy.recognition_model,
        pass_threshold=policy.pass_threshold,
        fail_threshold=policy.fail_threshold,
    )
