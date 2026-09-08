#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
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
from app.v3.workflow.materialized_generation import GenerationJobStore
from app.v3.workflow.temporal import ProductionWorkflow


def _artifact_path(resource: dict, logical_key: str) -> Path:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    path = Path(str(metadata.get("artifact_path") or ""))
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"H3 RESUME FAILED: artifact missing for {logical_key}: {path}")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Resume Xiaoduan V3 H3 generation from an already adopted image")
    parser.add_argument("--image-logical-key", required=True)
    parser.add_argument("--h3-width", type=int, default=512)
    parser.add_argument("--h3-height", type=int, default=320)
    parser.add_argument("--h3-length", type=int, default=56)
    parser.add_argument("--h3-steps", type=int, default=12)
    args = parser.parse_args()

    settings = get_settings()
    project_id = "global-acceptance"
    resources = ResourceStore(settings.data_dir)
    image = resources.adopted(project_id, args.image_logical_key)
    if image is None or image.get("state") != "adopted":
        raise SystemExit(
            f"H3 RESUME FAILED: image logical resource is not adopted: {args.image_logical_key}"
        )
    image_path = _artifact_path(image, args.image_logical_key)

    payloads = WorkflowPayloadStore(settings.data_dir)
    jobs = GenerationJobStore(settings.data_dir)
    suffix = int(time.time())
    workflow_id = f"xiaoduan-v3-h3-resume-{suffix}"
    video_logical = f"visual-video-{suffix}"
    h3_key = f"{workflow_id}:h3-generate"

    generate_payload = payloads.put(
        project_id,
        f"h3-resume-generate-{suffix}",
        {
            "logical_key": video_logical,
            "provider_id": "local-h3-video",
            "model_id": "minimax-h3",
            "shot_id": f"h3-resume-shot-{suffix}",
            "prompt": (
                "The same woman from the adopted first frame walks slowly forward through falling snow "
                "on the ancient mountain path. Preserve identity, clothing and scene continuity. "
                "Natural cinematic movement, stable camera, no sudden appearance changes."
            ),
            "first_frame_logical_key": args.image_logical_key,
            "entity_ids": ["char_hero"],
            "width": args.h3_width,
            "height": args.h3_height,
            "length": args.h3_length,
            "steps": args.h3_steps,
            "seed": 0,
            "metadata": {
                "acceptance": "v3_temporal_h3_resume",
                "upstream_image_logical_key": args.image_logical_key,
            },
        },
    )
    audit_payload = payloads.put(
        project_id,
        f"h3-resume-audit-{suffix}",
        {
            "logical_key": video_logical,
            "passed": True,
            "audit": {"acceptance": True, "source": "v3_temporal_h3_resume"},
        },
    )
    adopt_payload = payloads.put(
        project_id,
        f"h3-resume-adopt-{suffix}",
        {"logical_key": video_logical},
    )

    request = ProductionWorkflowInput(
        workflow_id=workflow_id,
        project_id=project_id,
        steps=(
            ProductionStep(
                step_id="h3-generate",
                skill_id="video_direction",
                operation="generation.h3.generate_candidate",
                payload_ref=generate_payload,
                idempotency_key=h3_key,
            ),
            ProductionStep(
                step_id="h3-audit",
                skill_id="quality_audit",
                operation="resource.audit_latest",
                payload_ref=audit_payload,
                idempotency_key=f"{workflow_id}:h3-audit",
            ),
            ProductionStep(
                step_id="h3-adopt",
                skill_id="quality_audit",
                operation="resource.adopt_latest",
                payload_ref=adopt_payload,
                idempotency_key=f"{workflow_id}:h3-adopt",
            ),
        ),
    )

    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    timeout_seconds = float(os.environ.get("XIAODUAN_TEMPORAL_VISUAL_TIMEOUT", "10800"))
    client = await Client.connect(address, namespace=namespace)
    handle = await client.start_workflow(
        ProductionWorkflow.run,
        request,
        id=workflow_id,
        task_queue=task_queue,
    )

    print("TEMPORAL H3 RESUME: STARTED", flush=True)
    print("workflow_id:", workflow_id, flush=True)
    print("image_logical_key:", args.image_logical_key, flush=True)
    print("image_resource_id:", image.get("resource_id"), flush=True)
    print("image_artifact_path:", image_path, flush=True)
    print("video_logical_key:", video_logical, flush=True)

    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise SystemExit(f"TEMPORAL H3 RESUME TIMEOUT: workflow_id={workflow_id}") from exc
    if result.status != "completed":
        raise SystemExit(f"TEMPORAL H3 RESUME FAILED: {result}")

    video = resources.adopted(project_id, video_logical)
    if video is None or video.get("state") != "adopted":
        raise SystemExit("TEMPORAL H3 RESUME FAILED: video resource not adopted")
    video_path = _artifact_path(video, video_logical)

    job = jobs.get(project_id, h3_key)
    first_frame_resource_id = str((job or {}).get("first_frame_resource_id") or "")
    expected_image_resource_id = str(image.get("resource_id") or "")
    if not first_frame_resource_id:
        raise SystemExit("TEMPORAL H3 RESUME FAILED: H3 job has no first-frame lineage")
    if first_frame_resource_id != expected_image_resource_id:
        raise SystemExit(
            "TEMPORAL H3 RESUME FAILED: first-frame lineage mismatch: "
            f"{first_frame_resource_id} != {expected_image_resource_id}"
        )

    print("TEMPORAL H3 RESUME: PASS")
    print("completed_step_ids:", list(result.completed_step_ids))
    print("video_resource_ref:", f"resource://{video['resource_id']}")
    print("video_artifact_path:", video_path)
    print("video_bytes:", video_path.stat().st_size)
    print("h3_first_frame_resource_id:", first_frame_resource_id)
    print("expected_image_resource_id:", expected_image_resource_id)


if __name__ == "__main__":
    asyncio.run(main())
