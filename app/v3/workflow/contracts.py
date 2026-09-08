from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


StepKind = Literal["completed", "semantic_failure", "review_required"]
WorkflowStatus = Literal["completed", "semantic_failure", "review_rejected", "cancelled"]


def _required(value: str, name: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True)
class ProductionStep:
    step_id: str
    skill_id: str
    operation: str
    payload_ref: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _required(self.step_id, "step_id")
        _required(self.skill_id, "skill_id")
        _required(self.operation, "operation")
        _required(self.payload_ref, "payload_ref")
        _required(self.idempotency_key, "idempotency_key")


@dataclass(frozen=True)
class ProductionWorkflowInput:
    workflow_id: str
    project_id: str
    steps: tuple[ProductionStep, ...]

    def __post_init__(self) -> None:
        _required(self.workflow_id, "workflow_id")
        _required(self.project_id, "project_id")
        if not self.steps:
            raise ValueError("production workflow requires at least one step")
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("production step_id values must be unique")
        keys = [step.idempotency_key for step in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError("production idempotency_key values must be unique")


@dataclass(frozen=True)
class StepActivityInput:
    workflow_id: str
    project_id: str
    step: ProductionStep


@dataclass(frozen=True)
class StepActivityResult:
    kind: StepKind
    output_ref: str = ""
    error_code: str = ""
    message: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in {"completed", "semantic_failure", "review_required"}:
            raise ValueError(f"invalid step result kind: {self.kind}")
        if self.kind == "semantic_failure" and not self.error_code:
            raise ValueError("semantic_failure requires error_code")


@dataclass(frozen=True)
class ReviewDecision:
    step_id: str
    accepted: bool
    output_ref: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        _required(self.step_id, "step_id")


@dataclass(frozen=True)
class ProductionWorkflowResult:
    status: WorkflowStatus
    completed_step_ids: tuple[str, ...]
    output_refs: tuple[str, ...]
    failed_step_id: str = ""
    error_code: str = ""
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"completed", "semantic_failure", "review_rejected", "cancelled"}:
            raise ValueError(f"invalid workflow status: {self.status}")
