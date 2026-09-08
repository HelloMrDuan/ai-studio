#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import time

from temporalio.client import Client

from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.temporal import ProductionWorkflow


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    client = await Client.connect(address, namespace=namespace)

    suffix = int(time.time())
    workflow_id = f"xiaoduan-v3-acceptance-{suffix}"
    payload_ref = f"resource://acceptance/{suffix}"
    request = ProductionWorkflowInput(
        workflow_id=workflow_id,
        project_id="global-acceptance",
        steps=(
            ProductionStep(
                step_id="temporal-acceptance",
                skill_id="quality_audit",
                operation="acceptance_echo",
                payload_ref=payload_ref,
                idempotency_key=f"{workflow_id}:acceptance",
            ),
        ),
    )

    result = await client.execute_workflow(
        ProductionWorkflow.run,
        request,
        id=workflow_id,
        task_queue=task_queue,
    )
    if result.status != "completed":
        raise SystemExit(f"TEMPORAL ACCEPTANCE FAILED: {result}")
    if payload_ref not in result.output_refs:
        raise SystemExit(f"TEMPORAL ACCEPTANCE FAILED: missing output_ref: {result}")
    print("TEMPORAL ACCEPTANCE: PASS")
    print("workflow_id:", workflow_id)
    print("completed_step_ids:", list(result.completed_step_ids))
    print("output_refs:", list(result.output_refs))


if __name__ == "__main__":
    asyncio.run(main())
