from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from temporalio import activity

from .contracts import StepActivityInput, StepActivityResult


StepExecutor = Callable[[StepActivityInput], Awaitable[StepActivityResult]]


class ProductionActivities:
    """Binds Temporal Activities to the Xiaoduan domain execution layer.

    Domain services own idempotency and semantic recovery. The Activity is an
    at-least-once transport boundary and must not invent business success.
    """

    def __init__(self, executor: StepExecutor, *, heartbeat_seconds: float = 20.0) -> None:
        self.executor = executor
        self.heartbeat_seconds = heartbeat_seconds

    @activity.defn(name="xiaoduan_execute_step")
    async def execute_step(self, input: StepActivityInput) -> StepActivityResult:
        details = {
            "workflow_id": input.workflow_id,
            "project_id": input.project_id,
            "step_id": input.step.step_id,
            "operation": input.step.operation,
            "idempotency_key": input.step.idempotency_key,
        }
        activity.heartbeat(details)

        async def heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                activity.heartbeat(details)

        heartbeat_task = asyncio.create_task(heartbeat_loop())
        try:
            result = await self.executor(input)
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

        if not isinstance(result, StepActivityResult):
            raise TypeError("domain step executor must return StepActivityResult")
        return result
