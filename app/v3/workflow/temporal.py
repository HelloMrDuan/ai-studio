from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import (
    ProductionWorkflowInput,
    ProductionWorkflowResult,
    ReviewDecision,
    StepActivityInput,
    StepActivityResult,
)


@workflow.defn(name="xiaoduanProductionWorkflow")
class ProductionWorkflow:
    """Durable control-plane owner for long production steps.

    Infrastructure faults are expressed as Activity failures and are retried by
    Temporal. Semantic failures are returned as typed business results and are
    never blindly replayed by the infrastructure retry policy.
    """

    def __init__(self) -> None:
        self._review_decisions: dict[str, ReviewDecision] = {}
        self._cancel_reason = ""

    @workflow.signal
    def review(self, decision: ReviewDecision) -> None:
        self._review_decisions[decision.step_id] = decision

    @workflow.signal
    def cancel(self, reason: str) -> None:
        self._cancel_reason = str(reason or "cancelled")

    @workflow.run
    async def run(self, input: ProductionWorkflowInput) -> ProductionWorkflowResult:
        completed: list[str] = []
        output_refs: list[str] = []

        for step in input.steps:
            if self._cancel_reason:
                return ProductionWorkflowResult(
                    status="cancelled",
                    completed_step_ids=tuple(completed),
                    output_refs=tuple(output_refs),
                    failed_step_id=step.step_id,
                    error_code="WORKFLOW_CANCELLED",
                    message=self._cancel_reason,
                )

            result = await workflow.execute_activity(
                "xiaoduan_execute_step",
                StepActivityInput(
                    workflow_id=input.workflow_id,
                    project_id=input.project_id,
                    step=step,
                ),
                start_to_close_timeout=timedelta(hours=6),
                heartbeat_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=1),
                    maximum_attempts=3,
                ),
            )
            if not isinstance(result, StepActivityResult):
                raise RuntimeError("xiaoduan_execute_step returned invalid result")

            if result.kind == "semantic_failure":
                return ProductionWorkflowResult(
                    status="semantic_failure",
                    completed_step_ids=tuple(completed),
                    output_refs=tuple(output_refs),
                    failed_step_id=step.step_id,
                    error_code=result.error_code or "SEMANTIC_VALIDATION_FAILURE",
                    message=result.message,
                )

            if result.kind == "review_required":
                await workflow.wait_condition(
                    lambda: step.step_id in self._review_decisions or bool(self._cancel_reason)
                )
                if self._cancel_reason:
                    return ProductionWorkflowResult(
                        status="cancelled",
                        completed_step_ids=tuple(completed),
                        output_refs=tuple(output_refs),
                        failed_step_id=step.step_id,
                        error_code="WORKFLOW_CANCELLED",
                        message=self._cancel_reason,
                    )
                decision = self._review_decisions[step.step_id]
                if not decision.accepted:
                    return ProductionWorkflowResult(
                        status="review_rejected",
                        completed_step_ids=tuple(completed),
                        output_refs=tuple(output_refs),
                        failed_step_id=step.step_id,
                        error_code="USER_REVIEW_REJECTED",
                        message=decision.reason,
                    )
                if decision.output_ref:
                    output_refs.append(decision.output_ref)
            elif result.output_ref:
                output_refs.append(result.output_ref)

            completed.append(step.step_id)

        return ProductionWorkflowResult(
            status="completed",
            completed_step_ids=tuple(completed),
            output_refs=tuple(output_refs),
        )
