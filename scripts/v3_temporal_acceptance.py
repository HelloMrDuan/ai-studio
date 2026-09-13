#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from temporalio.client import Client

from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.temporal import ProductionWorkflow


async def main() -> None:
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    timeout_seconds = float(os.environ.get("XIAODUAN_TEMPORAL_ACCEPTANCE_TIMEOUT", "30"))
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

    handle = await client.start_workflow(
        ProductionWorkflow.run,
        request,
        id=workflow_id,
        task_queue=task_queue,
    )
    print("TEMPORAL ACCEPTANCE: STARTED", flush=True)
    print("workflow_id:", workflow_id, flush=True)
    print("task_queue:", task_queue, flush=True)

    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise SystemExit(
            "TEMPORAL ACCEPTANCE TIMEOUT: "
            f"workflow_id={workflow_id} task_queue={task_queue} timeout={timeout_seconds:.0f}s"
        ) from exc

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
