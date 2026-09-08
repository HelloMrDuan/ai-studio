from __future__ import annotations

from collections.abc import Awaitable, Callable

from temporalio import activity

from .contracts import StepActivityInput, StepActivityResult


StepExecutor = Callable[[StepActivityInput], Awaitable[StepActivityResult]]


class ProductionActivities:
    """Binds Temporal Activities to the Xiaoduan domain execution layer.

    Domain services own idempotency and semantic recovery. The Activity is an
    at-least-once transport boundary and must not invent business success.
    """

    def __init__(self, executor: StepExecutor) -> None:
        self.executor = executor

    @activity.defn(name="xiaoduan_execute_step")
    async def execute_step(self, input: StepActivityInput) -> StepActivityResult:
        activity.heartbeat(
            {
                "workflow_id": input.workflow_id,
                "project_id": input.project_id,
                "step_id": input.step.step_id,
                "idempotency_key": input.step.idempotency_key,
            }
        )
        result = await self.executor(input)
        if not isinstance(result, StepActivityResult):
            raise TypeError("domain step executor must return StepActivityResult")
        return result
