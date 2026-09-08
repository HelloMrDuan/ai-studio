#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.v3.workflow.activities import ProductionActivities
from app.v3.workflow.contracts import StepActivityInput, StepActivityResult
from app.v3.workflow.worker import run_worker


async def execute_step(input: StepActivityInput) -> StepActivityResult:
    """Minimal V3 worker dispatcher with fail-closed unsupported operations.

    The acceptance operation proves durable Temporal transport without faking
    production-domain success. Real image/video/audio/resource operations will
    be bound here as the production executor is completed.
    """
    step = input.step
    if step.operation == "acceptance_echo":
        return StepActivityResult(
            kind="completed",
            output_ref=step.payload_ref,
            metadata={"executor": "v3-temporal-worker", "acceptance": "true"},
        )
    return StepActivityResult(
        kind="semantic_failure",
        error_code="UNSUPPORTED_OPERATION",
        message=f"V3 Temporal worker has no production executor for operation: {step.operation}",
        metadata={"executor": "v3-temporal-worker"},
    )


async def main() -> None:
    await run_worker(
        temporal_address=os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        temporal_namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        task_queue=os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production"),
        activities=ProductionActivities(execute_step),
    )


if __name__ == "__main__":
    asyncio.run(main())
