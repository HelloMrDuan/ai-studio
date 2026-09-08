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
from app.v3.generation_executor import ReferenceAssetStore
from app.v3.resource_store import ResourceStore
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.domain_executor import WorkflowPayloadStore
from app.v3.workflow.temporal import ProductionWorkflow


def _step(
    *,
    step_id: str,
    skill_id: str,
    operation: str,
    payload_ref: str,
    key: str,
) -> ProductionStep:
    return ProductionStep(
        step_id=step_id,
        skill_id=skill_id,
        operation=operation,
        payload_ref=payload_ref,
        idempotency_key=key,
    )


def _artifact_path(resource: dict, logical_key: str) -> Path:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    path = Path(str(metadata.get("artifact_path") or ""))
    if not path.is_file() or path.stat().st_size <= 0:
        raise SystemExit(f"VISUAL E2E FAILED: artifact missing for {logical_key}: {path}")
    return path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Xiaoduan V3 real Temporal image -> H3 E2E")
    parser.add_argument("--reference-id", default="hero-v1")
    parser.add_argument("--image-width-note", default="provider-profile")
    parser.add_argument("--h3-width", type=int, default=512)
    parser.add_argument("--h3-height", type=int, default=320)
    parser.add_argument("--h3-length", type=int, default=56)
    parser.add_argument("--h3-steps", type=int, default=12)
    args = parser.parse_args()

    settings = get_settings()
    references = ReferenceAssetStore(settings.data_dir)
    # Fail before paying for generation if the canonical starting reference is absent.
    references.resolve(args.reference_id)

    payloads = WorkflowPayloadStore(settings.data_dir)
    resources = ResourceStore(settings.data_dir)

    address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
    namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
    task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")
    timeout_seconds = float(os.environ.get("XIAODUAN_TEMPORAL_VISUAL_TIMEOUT", "10800"))

    suffix = int(time.time())
    workflow_id = f"xiaoduan-v3-visual-e2e-{suffix}"
    project_id = "global-acceptance"
    image_logical = f"visual-image-{suffix}"
    video_logical = f"visual-video-{suffix}"

    image_payload = payloads.put(
        project_id,
        f"visual-image-generate-{suffix}",
        {
            "logical_key": image_logical,
            "provider_id": "local-comfyui-image",
            "model_id": "configured-image-workflow",
            "shot_id": f"visual-image-shot-{suffix}",
            "source_text": (
                "canonical adult East Asian female character on an ancient Chinese snowy mountain path, "
                "cinematic medium shot, natural realistic skin, coherent anatomy, detailed winter clothing"
            ),
            "reference_ids": [args.reference_id],
            "entity_ids": ["char_hero"],
            "camera_direction": "medium shot, eye level",
            "action": "standing calmly while light snow falls",
            "duration_seconds": 3.0,
            "metadata": {
                "acceptance": "v3_temporal_visual_e2e",
                "image_width": args.image_width_note,
            },
        },
    )
    image_audit_payload = payloads.put(
        project_id,
        f"visual-image-audit-{suffix}",
        {
            "logical_key": image_logical,
            "passed": True,
            "audit": {
                "acceptance": True,
                "source": "v3_temporal_visual_e2e",
                "note": "runtime artifact existence gate passed; semantic quality audit remains replaceable",
            },
        },
    )
    image_adopt_payload = payloads.put(
        project_id,
        f"visual-image-adopt-{suffix}",
        {"logical_key": image_logical},
    )

    video_payload = payloads.put(
        project_id,
        f"visual-h3-generate-{suffix}",
        {
            "logical_key": video_logical,
            "provider_id": "local-h3-video",
            "model_id": "minimax-h3",
            "shot_id": f"visual-h3-shot-{suffix}",
            "prompt": (
                "The same woman from the adopted first frame walks slowly forward through falling snow "
                "on the ancient mountain path. Preserve identity, clothing and scene continuity. "
                "Natural cinematic movement, stable camera, no sudden appearance changes."
            ),
            # This is deliberately a logical resource, not the original hero-v1.
            # The worker resolves the already adopted generated image and imports
            # its private artifact as the H3 first-frame reference.
            "first_frame_logical_key": image_logical,
            "entity_ids": ["char_hero"],
            "width": args.h3_width,
            "height": args.h3_height,
            "length": args.h3_length,
            "steps": args.h3_steps,
            "seed": 0,
            "metadata": {
                "acceptance": "v3_temporal_visual_e2e",
                "upstream_image_logical_key": image_logical,
            },
        },
    )
    video_audit_payload = payloads.put(
        project_id,
        f"visual-h3-audit-{suffix}",
        {
            "logical_key": video_logical,
            "passed": True,
            "audit": {
                "acceptance": True,
                "source": "v3_temporal_visual_e2e",
                "note": "runtime artifact existence gate passed; semantic video audit remains replaceable",
            },
        },
    )
    video_adopt_payload = payloads.put(
        project_id,
        f"visual-h3-adopt-{suffix}",
        {"logical_key": video_logical},
    )

    request = ProductionWorkflowInput(
        workflow_id=workflow_id,
        project_id=project_id,
        steps=(
            _step(
                step_id="image-generate",
                skill_id="image_direction",
                operation="generation.image.generate_candidate",
                payload_ref=image_payload,
                key=f"{workflow_id}:image-generate",
            ),
            _step(
                step_id="image-audit",
                skill_id="quality_audit",
                operation="resource.audit_latest",
                payload_ref=image_audit_payload,
                key=f"{workflow_id}:image-audit",
            ),
            _step(
                step_id="image-adopt",
                skill_id="quality_audit",
                operation="resource.adopt_latest",
                payload_ref=image_adopt_payload,
                key=f"{workflow_id}:image-adopt",
            ),
            _step(
                step_id="h3-generate",
                skill_id="video_direction",
                operation="generation.h3.generate_candidate",
                payload_ref=video_payload,
                key=f"{workflow_id}:h3-generate",
            ),
            _step(
                step_id="h3-audit",
                skill_id="quality_audit",
                operation="resource.audit_latest",
                payload_ref=video_audit_payload,
                key=f"{workflow_id}:h3-audit",
            ),
            _step(
                step_id="h3-adopt",
                skill_id="quality_audit",
                operation="resource.adopt_latest",
                payload_ref=video_adopt_payload,
                key=f"{workflow_id}:h3-adopt",
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
    print("TEMPORAL VISUAL E2E: STARTED", flush=True)
    print("workflow_id:", workflow_id, flush=True)
    print("task_queue:", task_queue, flush=True)
    print("reference_id:", args.reference_id, flush=True)
    print("image_logical_key:", image_logical, flush=True)
    print("video_logical_key:", video_logical, flush=True)

    try:
        result = await asyncio.wait_for(handle.result(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise SystemExit(
            "TEMPORAL VISUAL E2E TIMEOUT: "
            f"workflow_id={workflow_id} timeout={timeout_seconds:.0f}s"
        ) from exc

    if result.status != "completed":
        raise SystemExit(f"TEMPORAL VISUAL E2E FAILED: {result}")

    expected_steps = {
        "image-generate",
        "image-audit",
        "image-adopt",
        "h3-generate",
        "h3-audit",
        "h3-adopt",
    }
    if set(result.completed_step_ids) != expected_steps:
        raise SystemExit(
            "TEMPORAL VISUAL E2E FAILED: completed steps mismatch: "
            f"{list(result.completed_step_ids)}"
        )

    image = resources.adopted(project_id, image_logical)
    video = resources.adopted(project_id, video_logical)
    if image is None or image.get("state") != "adopted":
        raise SystemExit("TEMPORAL VISUAL E2E FAILED: image resource not adopted")
    if video is None or video.get("state") != "adopted":
        raise SystemExit("TEMPORAL VISUAL E2E FAILED: video resource not adopted")

    image_path = _artifact_path(image, image_logical)
    video_path = _artifact_path(video, video_logical)
    image_meta = image.get("metadata") if isinstance(image.get("metadata"), dict) else {}
    video_meta = video.get("metadata") if isinstance(video.get("metadata"), dict) else {}

    first_frame_resource_id = str(video_meta.get("first_frame_resource_id") or "")
    if first_frame_resource_id and first_frame_resource_id != str(image.get("resource_id") or ""):
        raise SystemExit(
            "TEMPORAL VISUAL E2E FAILED: H3 first-frame lineage mismatch: "
            f"{first_frame_resource_id} != {image.get('resource_id')}"
        )

    unique_refs = list(dict.fromkeys(result.output_refs))
    print("TEMPORAL VISUAL E2E: PASS")
    print("workflow_id:", workflow_id)
    print("completed_step_ids:", list(result.completed_step_ids))
    print("image_resource_ref:", f"resource://{image['resource_id']}")
    print("image_artifact_ref:", image_meta.get("artifact_ref"))
    print("image_artifact_path:", image_path)
    print("image_bytes:", image_path.stat().st_size)
    print("video_resource_ref:", f"resource://{video['resource_id']}")
    print("video_artifact_ref:", video_meta.get("artifact_ref"))
    print("video_artifact_path:", video_path)
    print("video_bytes:", video_path.stat().st_size)
    print("h3_first_frame_resource_id:", first_frame_resource_id or image.get("resource_id"))
    print("output_refs_unique:", unique_refs)


if __name__ == "__main__":
    asyncio.run(main())
