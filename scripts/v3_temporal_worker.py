#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import get_settings
from app.v3.workflow.activities import ProductionActivities
from app.v3.workflow.contracts import StepActivityInput, StepActivityResult
from app.v3.workflow.domain_executor import DomainStepExecutor
from app.v3.workflow.worker import run_worker


settings = get_settings()
domain_executor = DomainStepExecutor(settings)


async def execute_step(input: StepActivityInput) -> StepActivityResult:
    """Dispatch durable Temporal Activities into the V3 production domain layer."""
    step = input.step
    print(
        f"ACTIVITY START workflow={input.workflow_id} step={step.step_id} operation={step.operation}",
        flush=True,
    )

    # Keep the transport-only acceptance operation for the existing smoke test.
    if step.operation == "acceptance_echo":
        result = StepActivityResult(
            kind="completed",
            output_ref=step.payload_ref,
            metadata={"executor": "v3-temporal-worker", "acceptance": "true"},
        )
    else:
        result = await domain_executor(input)

    print(
        "ACTIVITY RESULT "
        f"workflow={input.workflow_id} step={step.step_id} "
        f"kind={result.kind} output={result.output_ref or '-'} code={result.error_code or '-'}",
        flush=True,
    )
    return result


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    print(
        f"V3 TEMPORAL WORKER START address={address} namespace={namespace} task_queue={task_queue}",
        flush=True,
    )
    await run_worker(
        temporal_address=address,
        temporal_namespace=namespace,
        task_queue=task_queue,
        activities=ProductionActivities(execute_step),
    )


if __name__ == "__main__":
    asyncio.run(main())
