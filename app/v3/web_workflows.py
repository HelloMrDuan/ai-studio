from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from temporalio.client import Client

from app.config import Settings
from app.v3.generation_executor import ReferenceAssetStore
from app.v3.resource_store import ResourceStore
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.domain_executor import WorkflowPayloadStore
from app.v3.workflow.temporal import ProductionWorkflow


_SAFE_WORKFLOW_ID = re.compile(r"[A-Za-z0-9._:-]{3,180}")


class WebVisualWorkflowService:
    """Thin HTTP-facing wrapper over the already validated V3 Temporal workflow."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.payloads = WorkflowPayloadStore(settings.data_dir)
        self.resources = ResourceStore(settings.data_dir)
        self.references = ReferenceAssetStore(settings.data_dir)
        self.root = Path(settings.data_dir) / "v3" / "web-workflows"
        self.root.mkdir(parents=True, exist_ok=True)
        self.temporal_address = os.environ.get("TEMPORAL_ADDRESS", "127.0.0.1:7233")
        self.temporal_namespace = os.environ.get("TEMPORAL_NAMESPACE", "default")
        self.task_queue = os.environ.get("XIAODUAN_TEMPORAL_TASK_QUEUE", "xiaoduan-v3-production")

    def _path(self, workflow_id: str) -> Path:
        value = str(workflow_id or "").strip()
        if not _SAFE_WORKFLOW_ID.fullmatch(value):
            raise ValueError("invalid workflow_id")
        return self.root / f"{value}.json"

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(str(record["workflow_id"]))
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def _read(self, workflow_id: str) -> dict[str, Any]:
        path = self._path(workflow_id)
        if not path.is_file():
            raise FileNotFoundError(f"web workflow not found: {workflow_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid web workflow record")
        return raw

    @staticmethod
    def _step(step_id: str, skill_id: str, operation: str, payload_ref: str, workflow_id: str) -> ProductionStep:
        return ProductionStep(
            step_id=step_id,
            skill_id=skill_id,
            operation=operation,
            payload_ref=payload_ref,
            idempotency_key=f"{workflow_id}:{step_id}",
        )

    def prepare(
        self,
        *,
        project_id: str,
        reference_id: str,
        source_text: str,
        video_prompt: str,
        h3_width: int = 512,
        h3_height: int = 320,
        h3_length: int = 56,
        h3_steps: int = 12,
    ) -> tuple[ProductionWorkflowInput, dict[str, Any]]:
        self.references.resolve(reference_id)
        if not source_text.strip():
            raise ValueError("source_text is required")
        if not video_prompt.strip():
            raise ValueError("video_prompt is required")

        suffix = secrets.token_hex(8)
        workflow_id = f"xiaoduan-v3-web-{suffix}"
        image_logical = f"web-image-{suffix}"
        video_logical = f"web-video-{suffix}"

        image_generate = self.payloads.put(
            project_id,
            f"web-image-generate-{suffix}",
            {
                "logical_key": image_logical,
                "provider_id": "local-comfyui-image",
                "model_id": "configured-image-workflow",
                "shot_id": f"web-image-shot-{suffix}",
                "source_text": source_text,
                "reference_ids": [reference_id],
                "entity_ids": ["char_hero"],
                "camera_direction": "cinematic medium shot, eye level",
                "action": "natural restrained movement",
                "duration_seconds": 3.0,
                "metadata": {"source": "v3_web_validation"},
            },
        )
        image_audit = self.payloads.put(
            project_id,
            f"web-image-audit-{suffix}",
            {
                "logical_key": image_logical,
                "passed": True,
                "audit": {
                    "acceptance": True,
                    "source": "v3_web_validation",
                    "note": "runtime artifact gate; manual semantic review remains replaceable",
                },
            },
        )
        image_adopt = self.payloads.put(
            project_id,
            f"web-image-adopt-{suffix}",
            {"logical_key": image_logical},
        )
        video_generate = self.payloads.put(
            project_id,
            f"web-video-generate-{suffix}",
            {
                "logical_key": video_logical,
                "provider_id": "local-h3-video",
                "model_id": "minimax-h3",
                "shot_id": f"web-video-shot-{suffix}",
                "prompt": video_prompt,
                "first_frame_logical_key": image_logical,
                "entity_ids": ["char_hero"],
                "width": int(h3_width),
                "height": int(h3_height),
                "length": int(h3_length),
                "steps": int(h3_steps),
                "seed": 0,
                "metadata": {
                    "source": "v3_web_validation",
                    "upstream_image_logical_key": image_logical,
                },
            },
        )
        video_audit = self.payloads.put(
            project_id,
            f"web-video-audit-{suffix}",
            {
                "logical_key": video_logical,
                "passed": True,
                "audit": {
                    "acceptance": True,
                    "source": "v3_web_validation",
                    "note": "runtime artifact gate; manual semantic review remains replaceable",
                },
            },
        )
        video_adopt = self.payloads.put(
            project_id,
            f"web-video-adopt-{suffix}",
            {"logical_key": video_logical},
        )

        request = ProductionWorkflowInput(
            workflow_id=workflow_id,
            project_id=project_id,
            steps=(
                self._step("image-generate", "image_direction", "generation.image.generate_candidate", image_generate, workflow_id),
                self._step("image-audit", "quality_audit", "resource.audit_latest", image_audit, workflow_id),
                self._step("image-adopt", "resource_management", "resource.adopt_latest", image_adopt, workflow_id),
                self._step("h3-generate", "video_direction", "generation.h3.generate_candidate", video_generate, workflow_id),
                self._step("h3-audit", "quality_audit", "resource.audit_latest", video_audit, workflow_id),
                self._step("h3-adopt", "resource_management", "resource.adopt_latest", video_adopt, workflow_id),
            ),
        )
        record = {
            "workflow_id": workflow_id,
            "project_id": project_id,
            "reference_id": reference_id,
            "image_logical_key": image_logical,
            "video_logical_key": video_logical,
            "task_queue": self.task_queue,
            "status": "prepared",
        }
        self._write(record)
        return request, record

    async def start(self, **kwargs: Any) -> dict[str, Any]:
        request, record = self.prepare(**kwargs)
        try:
            client = await Client.connect(self.temporal_address, namespace=self.temporal_namespace)
            await client.start_workflow(
                ProductionWorkflow.run,
                request,
                id=request.workflow_id,
                task_queue=self.task_queue,
            )
        except Exception:
            record["status"] = "start_failed"
            self._write(record)
            raise
        record["status"] = "running"
        self._write(record)
        return dict(record)

    @staticmethod
    def _media(resource: dict[str, Any] | None, kind: str) -> dict[str, Any] | None:
        if not resource:
            return None
        metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
        artifact_ref = str(metadata.get("artifact_ref") or "")
        prefix = f"artifact://{kind}/"
        artifact_id = artifact_ref[len(prefix):] if artifact_ref.startswith(prefix) else ""
        return {
            "resource_id": resource.get("resource_id"),
            "state": resource.get("state"),
            "artifact_ref": artifact_ref,
            "artifact_path": metadata.get("artifact_path"),
            "media_url": f"/api/v3/media/{kind}/{artifact_id}" if artifact_id else "",
            "reference_ids": list(resource.get("reference_ids") or []),
        }

    async def status(self, workflow_id: str) -> dict[str, Any]:
        record = self._read(workflow_id)
        client = await Client.connect(self.temporal_address, namespace=self.temporal_namespace)
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        temporal_status = str(getattr(description.status, "name", description.status)).lower()

        image = self.resources.adopted(str(record["project_id"]), str(record["image_logical_key"]))
        video = self.resources.adopted(str(record["project_id"]), str(record["video_logical_key"]))
        if video:
            stage = "completed"
        elif image:
            stage = "h3-generation"
        else:
            stage = "image-generation"

        result: dict[str, Any] = {
            **record,
            "temporal_status": temporal_status,
            "stage": stage,
            "image": self._media(image, "image"),
            "video": self._media(video, "video"),
        }

        if temporal_status == "completed":
            workflow_result = await handle.result()
            result["status"] = workflow_result.status
            result["completed_step_ids"] = list(workflow_result.completed_step_ids)
            result["output_refs"] = list(workflow_result.output_refs)
            result["failed_step_id"] = workflow_result.failed_step_id
            result["error_code"] = workflow_result.error_code
            result["message"] = workflow_result.message
        elif temporal_status in {"failed", "cancelled", "terminated", "timed_out"}:
            result["status"] = temporal_status
        else:
            result["status"] = "running"

        self._write({key: value for key, value in result.items() if key in record or key == "status"})
        return result
