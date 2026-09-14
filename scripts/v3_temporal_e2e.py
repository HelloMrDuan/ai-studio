#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from temporalio.client import Client

from app.config import get_settings
from app.v3.resource_store import ResourceStore
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.domain_executor import WorkflowPayloadStore
from app.v3.workflow.temporal import ProductionWorkflow


async def main() -> None:
    settings = get_settings()
    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    timeout_seconds = float(os.environ.get("XIAODUAN_TEMPORAL_E2E_TIMEOUT", "180"))
    provider_id = os.environ.get("XIAODUAN_TTS_PROVIDER_ID", "edge-tts-gateway")
    model_id = os.environ.get("XIAODUAN_TTS_MODEL_ID", "edge-tts")
    voice = os.environ.get("XIAODUAN_TTS_VOICE", "zh-CN-XiaoxiaoNeural")

    suffix = int(time.time())
    workflow_id = f"xiaoduan-v3-business-e2e-{suffix}"
    project_id = "temporal-e2e"
    logical_key = f"voiceover-{suffix}"
    tts_key = f"{workflow_id}:tts"
    artifact_id = "tts_" + hashlib.sha256(tts_key.encode("utf-8")).hexdigest()[:24]
    artifact_ref = f"artifact://tts/{artifact_id}"

    payloads = WorkflowPayloadStore(settings.data_dir)
    tts_payload_ref = payloads.put(
        project_id,
        f"tts-{suffix}",
        {
            "provider_id": provider_id,
            "model_id": model_id,
            "text": "小段映画 V3 Temporal 真实业务链路验收通过。",
            "voice": voice,
            "response_format": "mp3",
            "speed": 1.0,
        },
    )
    candidate_payload_ref = payloads.put(
        project_id,
        f"candidate-{suffix}",
        {
            "logical_key": logical_key,
            "generation_task_id": workflow_id,
            "provider_id": provider_id,
            "model_id": model_id,
            "reference_ids": [artifact_ref],
            "metadata": {
                "source": "v3_temporal_e2e",
                "artifact_ref": artifact_ref,
                "media_type": "tts",
            },
        },
    )
    audit_payload_ref = payloads.put(
        project_id,
        f"audit-{suffix}",
        {
            "logical_key": logical_key,
            "passed": True,
            "audit": {"acceptance": True, "source": "v3_temporal_e2e"},
        },
    )
    adopt_payload_ref = payloads.put(
        project_id,
        f"adopt-{suffix}",
        {"logical_key": logical_key},
    )

    request = ProductionWorkflowInput(
        workflow_id=workflow_id,
        project_id=project_id,
        steps=(
            ProductionStep(
                step_id="tts-generate",
                skill_id="audio_generation",
                operation="tts.generate",
                payload_ref=tts_payload_ref,
                idempotency_key=tts_key,
            ),
            ProductionStep(
                step_id="resource-candidate",
                skill_id="resource_management",
                operation="resource.candidate.create",
                payload_ref=candidate_payload_ref,
                idempotency_key=f"{workflow_id}:candidate",
            ),
            ProductionStep(
                step_id="resource-audit",
                skill_id="quality_audit",
                operation="resource.audit_latest",
                payload_ref=audit_payload_ref,
                idempotency_key=f"{workflow_id}:audit",
            ),
            ProductionStep(
                step_id="resource-adopt",
                skill_id="resource_management",
                operation="resource.adopt_latest",
                payload_ref=adopt_payload_ref,
                idempotency_key=f"{workflow_id}:adopt",
            ),
        ),
    )

    client = await Client.connect(address, namespace=namespace)
    handle = await client.start_workflow(
        ProductionWorkflow.run,
        request,
        id=workflow_id,
        task_queue=task_queue,
    )
    print("TEMPORAL BUSINESS E2E: STARTED", flush=True)
    print("workflow_id:", workflow_id, flush=True)
    print("task_queue:", task_queue, flush=True)

    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise SystemExit(
            "TEMPORAL BUSINESS E2E TIMEOUT: "
            f"workflow_id={workflow_id} task_queue={task_queue} timeout={timeout_seconds:.0f}s"
        ) from exc

    if result.status != "completed":
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: {result}")
    expected_steps = ["tts-generate", "resource-candidate", "resource-audit", "resource-adopt"]
    if list(result.completed_step_ids) != expected_steps:
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: completed_step_ids={result.completed_step_ids}")
    if artifact_ref not in result.output_refs:
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: missing artifact ref {artifact_ref}")

    artifact_path = Path(settings.data_dir) / "v3" / "media" / "tts" / f"{artifact_id}.mp3"
    if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: TTS artifact unavailable: {artifact_path}")

    adopted = ResourceStore(settings.data_dir).adopted(project_id, logical_key)
    if not adopted or adopted.get("state") != "adopted":
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: adopted resource missing: {adopted}")
    if artifact_ref not in (adopted.get("reference_ids") or []):
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: adopted resource lost artifact ref: {adopted}")
    adopted_ref = f"resource://{adopted['resource_id']}"
    if adopted_ref not in result.output_refs:
        raise SystemExit(f"TEMPORAL BUSINESS E2E FAILED: missing adopted output ref {adopted_ref}")

    print("TEMPORAL BUSINESS E2E: PASS")
    print("workflow_id:", workflow_id)
    print("completed_step_ids:", list(result.completed_step_ids))
    print("artifact_ref:", artifact_ref)
    print("artifact_path:", artifact_path)
    print("adopted_resource_ref:", adopted_ref)
    print("output_refs:", list(result.output_refs))


if __name__ == "__main__":
    asyncio.run(main())
