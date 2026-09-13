from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import replace
from pathlib import Path
from typing import Any

from temporalio.client import Client

from app.config import Settings
from app.v3.generation_executor import ReferenceAssetStore
from app.v3.media.bgm import BGMStore
from app.v3.resource_store import ResourceStore
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.domain_executor import WorkflowPayloadStore
from app.v3.workflow.full_pipeline_executor import FullPipelineArtifactStore
from app.v3.workflow.temporal import ProductionWorkflow


_SAFE_WORKFLOW_ID = re.compile(r"[A-Za-z0-9._:-]{3,180}")


class FullMovieWorkflowService:
    """HTTP-facing builder/status service for the real story-to-final-MP4 Temporal path."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.payloads = WorkflowPayloadStore(settings.data_dir)
        self.resources = ResourceStore(settings.data_dir)
        self.references = ReferenceAssetStore(settings.data_dir)
        self.artifacts = FullPipelineArtifactStore(settings.data_dir)
        self.bgm = BGMStore(settings.data_dir)
        self.root = Path(settings.data_dir) / "v3" / "full-pipeline-web"
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
        target = self._path(str(record["workflow_id"]))
        temp = target.with_suffix(".tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(target)

    def _read(self, workflow_id: str) -> dict[str, Any]:
        target = self._path(workflow_id)
        if not target.is_file():
            raise FileNotFoundError(f"full movie workflow not found: {workflow_id}")
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("invalid full movie workflow record")
        return value

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
        story_text: str,
        reference_id: str,
        bgm_path: str = "",
        llm_provider_id: str = "local-qwen",
        llm_model_id: str = "",
        tts_provider_id: str = "edge-tts-gateway",
        tts_model_id: str = "edge-tts",
        tts_voice: str = "zh-CN-XiaoxiaoNeural",
        h3_width: int = 512,
        h3_height: int = 320,
        h3_length: int = 56,
        h3_steps: int = 12,
    ) -> tuple[ProductionWorkflowInput, dict[str, Any]]:
        story = str(story_text or "").strip()
        if not story:
            raise ValueError("story_text is required")
        self.references.resolve(reference_id)

        suffix = secrets.token_hex(8)
        workflow_id = f"xiaoduan-v3-full-{suffix}"
        image_logical = f"full-image-{suffix}"
        video_logical = f"full-video-{suffix}"
        llm_model = str(llm_model_id or self.settings.stage04_required_model_alias or self.settings.gemma_model)

        llm_common = {
            "provider_id": llm_provider_id,
            "model_id": llm_model,
            "temperature": 0.55,
        }
        screenplay = self.payloads.put(
            project_id,
            f"{workflow_id}-screenplay",
            {**llm_common, "story_text": story, "max_tokens": 2048},
        )
        characters = self.payloads.put(
            project_id,
            f"{workflow_id}-characters",
            {**llm_common, "max_tokens": 2048},
        )
        storyboard = self.payloads.put(
            project_id,
            f"{workflow_id}-storyboard",
            {**llm_common, "max_tokens": 3072, "max_shots": 1},
        )
        image_generate = self.payloads.put(
            project_id,
            f"{workflow_id}-image-generate",
            {
                "logical_key": image_logical,
                "reference_id": reference_id,
                "entity_id": "char_hero",
                "provider_id": "local-comfyui-image",
                "model_id": "configured-image-workflow",
            },
        )
        image_audit = self.payloads.put(
            project_id,
            f"{workflow_id}-image-audit",
            {
                "logical_key": image_logical,
                "passed": True,
                "audit": {
                    "source": "full_pipeline_web",
                    "runtime_artifact_gate": True,
                },
            },
        )
        image_adopt = self.payloads.put(
            project_id,
            f"{workflow_id}-image-adopt",
            {"logical_key": image_logical},
        )
        h3_generate = self.payloads.put(
            project_id,
            f"{workflow_id}-h3-generate",
            {
                "logical_key": video_logical,
                "image_logical_key": image_logical,
                "entity_id": "char_hero",
                "provider_id": "local-h3-video",
                "model_id": "minimax-h3",
                "width": int(h3_width),
                "height": int(h3_height),
                "length": int(h3_length),
                "steps": int(h3_steps),
                "seed": 0,
            },
        )
        h3_audit = self.payloads.put(
            project_id,
            f"{workflow_id}-h3-audit",
            {
                "logical_key": video_logical,
                "passed": True,
                "audit": {
                    "source": "full_pipeline_web",
                    "runtime_artifact_gate": True,
                },
            },
        )
        h3_adopt = self.payloads.put(
            project_id,
            f"{workflow_id}-h3-adopt",
            {"logical_key": video_logical},
        )
        tts = self.payloads.put(
            project_id,
            f"{workflow_id}-tts",
            {
                "provider_id": tts_provider_id,
                "model_id": tts_model_id,
                "voice": tts_voice,
                "response_format": "mp3",
                "speed": 1.0,
            },
        )
        subtitle = self.payloads.put(
            project_id,
            f"{workflow_id}-subtitle",
            {"duration_seconds": max(0.1, int(h3_length) / 24.0)},
        )
        bgm = self.payloads.put(
            project_id,
            f"{workflow_id}-bgm",
            {"bgm_path": str(bgm_path or "")},
        )
        composition = self.payloads.put(
            project_id,
            f"{workflow_id}-composition",
            {
                "video_logical_key": video_logical,
                "voice_volume": 1.0,
                "bgm_volume": 0.18,
                "preferred_video_codec": "libx264",
            },
        )

        request = ProductionWorkflowInput(
            workflow_id=workflow_id,
            project_id=project_id,
            steps=(
                self._step("screenplay", "screenplay", "full.llm.screenplay", screenplay, workflow_id),
                self._step("characters", "character", "full.llm.characters", characters, workflow_id),
                self._step("storyboard", "storyboard", "full.llm.storyboard", storyboard, workflow_id),
                self._step("image-generate", "image_direction", "full.image.generate_from_storyboard", image_generate, workflow_id),
                self._step("image-audit", "quality_audit", "resource.audit_latest", image_audit, workflow_id),
                self._step("image-adopt", "resource_management", "resource.adopt_latest", image_adopt, workflow_id),
                self._step("h3-generate", "video_direction", "full.h3.generate_from_storyboard", h3_generate, workflow_id),
                self._step("h3-audit", "quality_audit", "resource.audit_latest", h3_audit, workflow_id),
                self._step("h3-adopt", "resource_management", "resource.adopt_latest", h3_adopt, workflow_id),
                self._step("tts", "audio_generation", "full.tts.generate_from_screenplay", tts, workflow_id),
                self._step("subtitle", "subtitle", "full.subtitle.generate", subtitle, workflow_id),
                self._step("bgm", "audio_generation", "full.bgm.select", bgm, workflow_id),
                self._step("composition", "composition", "full.composition.render", composition, workflow_id),
            ),
        )
        dependencies = {
            "screenplay": (), "characters": ("screenplay",), "storyboard": ("characters",),
            "image-generate": ("storyboard",), "tts": ("storyboard",), "bgm": ("storyboard",),
            "image-audit": ("image-generate",), "image-adopt": ("image-audit",),
            "h3-generate": ("image-adopt",), "h3-audit": ("h3-generate",), "h3-adopt": ("h3-audit",),
            "subtitle": ("h3-adopt", "tts"), "composition": ("subtitle", "bgm"),
        }
        request = replace(request, steps=tuple(replace(step, depends_on=dependencies[step.step_id]) for step in request.steps))
        record = {
            "workflow_id": workflow_id,
            "project_id": project_id,
            "story_text": story,
            "reference_id": reference_id,
            "image_logical_key": image_logical,
            "video_logical_key": video_logical,
            "bgm_path": str(bgm_path or ""),
            "task_queue": self.task_queue,
            "status": "prepared",
        }
        self._write(record)
        self.artifacts.patch_manifest(
            project_id,
            workflow_id,
            status="prepared",
            reference_id=reference_id,
            image_logical_key=image_logical,
            video_logical_key=video_logical,
        )
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
        self.artifacts.patch_manifest(request.project_id, request.workflow_id, status="running")
        return dict(record)

    def save_bgm(self, filename: str, source: Any) -> Path:
        return self.bgm.save_upload(filename, source)

    def final_path(self, workflow_id: str) -> Path:
        record = self._read(workflow_id)
        manifest = self.artifacts.manifest(str(record["project_id"]), workflow_id)
        path = Path(str(manifest.get("final_path") or ""))
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError("final movie not available")
        return path

    @staticmethod
    def _stage(manifest: dict[str, Any], image: dict[str, Any] | None, video: dict[str, Any] | None) -> str:
        if manifest.get("final_path"):
            return "completed"
        if not manifest.get("screenplay_path"):
            return "screenplay"
        if not manifest.get("characters_path"):
            return "characters"
        if not manifest.get("storyboard_path"):
            return "storyboard"
        if not image:
            return "image"
        if not video:
            return "h3"
        if not manifest.get("voice_path"):
            return "tts"
        if not manifest.get("subtitle_path"):
            return "subtitle"
        if not manifest.get("bgm_path"):
            return "bgm"
        return "composition"

    async def status(self, workflow_id: str) -> dict[str, Any]:
        record = self._read(workflow_id)
        project_id = str(record["project_id"])
        manifest = self.artifacts.manifest(project_id, workflow_id)
        client = await Client.connect(self.temporal_address, namespace=self.temporal_namespace)
        handle = client.get_workflow_handle(workflow_id)
        description = await handle.describe()
        temporal_status = str(getattr(description.status, "name", description.status)).lower()
        image = self.resources.adopted(project_id, str(record["image_logical_key"]))
        video = self.resources.adopted(project_id, str(record["video_logical_key"]))

        result: dict[str, Any] = {
            **record,
            "temporal_status": temporal_status,
            "stage": self._stage(manifest, image, video),
            "manifest": manifest,
            "image_resource_id": image.get("resource_id") if image else None,
            "video_resource_id": video.get("resource_id") if video else None,
            "final_url": f"/api/v3/full-pipeline/{workflow_id}/final" if manifest.get("final_path") else "",
        }
        for name in ("screenplay", "characters", "storyboard"):
            path = self.artifacts.json_path(project_id, workflow_id, name)
            if path.is_file():
                result[name] = json.loads(path.read_text(encoding="utf-8"))

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
        return result
