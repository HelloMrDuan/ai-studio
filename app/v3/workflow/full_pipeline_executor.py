from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from app.config import Settings
from app.core.gpu_orchestrator import GPUOrchestrator
from app.models import GPUOwner
from app.v3.adapters.openai_compatible import OpenAICompatibleAdapter
from app.v3.contracts import Capability, ProviderTransport
from app.v3.media.bgm import BGMStore, validate_audio_file
from app.v3.media.composition import CompositionRequest, FFmpegCompositionService
from app.v3.media.subtitle import proportional_cues, write_srt
from app.v3.media.task_artifacts import atomic_write_json, read_json

from .contracts import ProductionStep, StepActivityInput, StepActivityResult
from .materialized_generation import MaterializedDomainExecutor


_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{3,180}")
_FULL_OPERATIONS = {
    "full.llm.screenplay",
    "full.llm.characters",
    "full.llm.storyboard",
    "full.image.generate_from_storyboard",
    "full.h3.generate_from_storyboard",
    "full.tts.generate_from_screenplay",
    "full.subtitle.generate",
    "full.bgm.select",
    "full.composition.render",
}
_VISUAL_OPERATIONS = {
    "generation.image.generate_candidate",
    "generation.h3.generate_candidate",
}


class FullPipelineSemanticError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FullPipelineArtifactStore:
    """Durable artifact manifest for one story-to-final-video workflow.

    JSON persistence reuses the V3 MoneyPrinterTurbo-derived atomic task artifact
    writer so Temporal retries never observe partially written manifests.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "full-pipeline"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe(value: str, name: str) -> str:
        cleaned = str(value or "").strip()
        if not _SAFE_ID.fullmatch(cleaned):
            raise ValueError(f"invalid {name}")
        return cleaned

    def workflow_dir(self, project_id: str, workflow_id: str) -> Path:
        path = self.root / self._safe(project_id, "project_id") / self._safe(workflow_id, "workflow_id")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def json_path(self, project_id: str, workflow_id: str, name: str) -> Path:
        safe = self._safe(name, "artifact name")
        return self.workflow_dir(project_id, workflow_id) / f"{safe}.json"

    def write_json(self, project_id: str, workflow_id: str, name: str, payload: dict[str, Any]) -> Path:
        target = self.json_path(project_id, workflow_id, name)
        atomic_write_json(target, payload)
        return target

    def read_json(self, project_id: str, workflow_id: str, name: str) -> dict[str, Any]:
        target = self.json_path(project_id, workflow_id, name)
        if not target.is_file():
            raise FileNotFoundError(f"full pipeline artifact unavailable: {name}")
        return read_json(target)

    def manifest_path(self, project_id: str, workflow_id: str) -> Path:
        return self.workflow_dir(project_id, workflow_id) / "manifest.json"

    def patch_manifest(self, project_id: str, workflow_id: str, **updates: Any) -> dict[str, Any]:
        path = self.manifest_path(project_id, workflow_id)
        current: dict[str, Any] = read_json(path) if path.is_file() else {
            "schema_version": "xiaoduan_full_pipeline_v1",
            "project_id": project_id,
            "workflow_id": workflow_id,
        }
        current.update(updates)
        atomic_write_json(path, current)
        return current

    def manifest(self, project_id: str, workflow_id: str) -> dict[str, Any]:
        path = self.manifest_path(project_id, workflow_id)
        return read_json(path) if path.is_file() else {}


class FullPipelineExecutor:
    """Story -> LLM -> visual -> TTS -> subtitle -> BGM -> final MP4 executor.

    The visual/materialization path is delegated to the already validated V3
    MaterializedDomainExecutor. Subtitle/BGM/composition call the V3 ports of
    MoneyPrinterTurbo directly instead of introducing parallel implementations.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        visual: MaterializedDomainExecutor | None = None,
        gpu: GPUOrchestrator | None = None,
    ) -> None:
        self.settings = settings
        self.visual = visual or MaterializedDomainExecutor(settings)
        self.gpu = gpu or GPUOrchestrator(settings)
        self.artifacts = FullPipelineArtifactStore(settings.data_dir)
        self.bgm = BGMStore(settings.data_dir)
        self.composition = FFmpegCompositionService()

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
        if operation in _VISUAL_OPERATIONS:
            async with self.gpu.use(GPUOwner.comfyui):
                return await self.visual(input)
        if operation not in _FULL_OPERATIONS:
            return await self.visual(input)

        cached = self.visual.base.results.get(input.project_id, input.step.idempotency_key)
        if cached is not None:
            return cached

        try:
            payload = self.visual.base.payloads.resolve(input.project_id, input.step.payload_ref)
            if operation == "full.llm.screenplay":
                result = await self._screenplay(input, payload)
            elif operation == "full.llm.characters":
                result = await self._characters(input, payload)
            elif operation == "full.llm.storyboard":
                result = await self._storyboard(input, payload)
            elif operation == "full.image.generate_from_storyboard":
                result = await self._image_from_storyboard(input, payload)
            elif operation == "full.h3.generate_from_storyboard":
                result = await self._h3_from_storyboard(input, payload)
            elif operation == "full.tts.generate_from_screenplay":
                result = await self._tts_from_screenplay(input, payload)
            elif operation == "full.subtitle.generate":
                result = self._subtitle(input, payload)
            elif operation == "full.bgm.select":
                result = self._bgm_select(input, payload)
            else:
                result = self._compose(input, payload)
        except FullPipelineSemanticError as exc:
            result = StepActivityResult(
                kind="semantic_failure",
                error_code=exc.code,
                message=str(exc),
                metadata={"executor": "v3-full-pipeline-executor"},
            )
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            result = StepActivityResult(
                kind="semantic_failure",
                error_code="FULL_PIPELINE_CONTRACT_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                metadata={"executor": "v3-full-pipeline-executor"},
            )

        self.visual.base.results.put(input.project_id, input.step.idempotency_key, result)
        return result

    @staticmethod
    def _required(payload: dict[str, Any], name: str) -> str:
        value = str(payload.get(name) or "").strip()
        if not value:
            raise FullPipelineSemanticError("INVALID_PIPELINE_INPUT", f"{name} is required")
        return value

    @staticmethod
    def _json_content(value: str) -> dict[str, Any]:
        text = str(value or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise FullPipelineSemanticError("LLM_SCHEMA_INVALID", "LLM structured output must be a JSON object")
        return parsed

    @asynccontextmanager
    async def _provider_workspace(self, transport: ProviderTransport, provider_id: str) -> AsyncIterator[None]:
        if transport == ProviderTransport.local_openai_compatible and provider_id == "local-qwen":
            async with self.gpu.use(GPUOwner.gemma):
                yield
        else:
            yield

    async def _llm_json(
        self,
        payload: dict[str, Any],
        *,
        system: str,
        user: str,
        max_tokens: int,
    ) -> tuple[dict[str, Any], str, str]:
        provider_id = str(payload.get("provider_id") or "local-qwen").strip()
        model_id = str(payload.get("model_id") or self.settings.stage04_required_model_alias or self.settings.gemma_model).strip()
        selected = self.visual.base.providers.resolve(
            {Capability.text, Capability.structured_output},
            provider_id=provider_id,
            model_id=model_id,
        )
        adapter = OpenAICompatibleAdapter(
            selected.spec,
            secret_resolver=lambda name: os.environ.get(name),
            timeout_seconds=float(self.settings.gemma_timeout_seconds),
        )
        async with self._provider_workspace(selected.spec.transport, selected.spec.provider_id):
            response = await adapter.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=float(payload.get("temperature") or 0.55),
                max_tokens=max_tokens,
                require_structured_output=True,
            )
        return self._json_content(str(response["content"])), selected.spec.provider_id, selected.spec.model_id

    async def _screenplay(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        story = self._required(payload, "story_text")
        data, provider_id, model_id = await self._llm_json(
            payload,
            system=(
                "你是短片编剧。只输出 JSON。把用户故事改造成可直接进入视频制作的短片剧本。"
                "必须包含 title, summary, narration, scenes。narration 是简短中文旁白；"
                "scenes 是数组，每项包含 scene_id, description, action。不要输出解释。"
            ),
            user=f"故事：\n{story}\n\n要求：保持原故事核心事实，生成一个短而完整、可视化的单场景验证版剧本。",
            max_tokens=int(payload.get("max_tokens") or 2048),
        )
        if not str(data.get("narration") or "").strip() or not isinstance(data.get("scenes"), list) or not data["scenes"]:
            raise FullPipelineSemanticError("SCREENPLAY_SCHEMA_INVALID", "screenplay requires narration and scenes")
        path = self.artifacts.write_json(input.project_id, input.workflow_id, "screenplay", data)
        self.artifacts.patch_manifest(input.project_id, input.workflow_id, screenplay_path=str(path))
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/screenplay",
            metadata={"provider_id": provider_id, "model_id": model_id, "artifact_path": str(path)},
        )

    async def _characters(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        screenplay = self.artifacts.read_json(input.project_id, input.workflow_id, "screenplay")
        data, provider_id, model_id = await self._llm_json(
            payload,
            system=(
                "你是角色设定师。只输出 JSON。根据剧本提取可复用角色卡。"
                "格式必须是 {\"characters\":[...]}，每个角色包含 character_id, name, appearance, clothing, identity_prompt。"
            ),
            user=json.dumps(screenplay, ensure_ascii=False),
            max_tokens=int(payload.get("max_tokens") or 2048),
        )
        characters = data.get("characters")
        if not isinstance(characters, list) or not characters:
            raise FullPipelineSemanticError("CHARACTERS_SCHEMA_INVALID", "characters output requires a non-empty characters array")
        path = self.artifacts.write_json(input.project_id, input.workflow_id, "characters", data)
        self.artifacts.patch_manifest(input.project_id, input.workflow_id, characters_path=str(path))
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/characters",
            metadata={"provider_id": provider_id, "model_id": model_id, "artifact_path": str(path)},
        )

    async def _storyboard(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        screenplay = self.artifacts.read_json(input.project_id, input.workflow_id, "screenplay")
        characters = self.artifacts.read_json(input.project_id, input.workflow_id, "characters")
        max_shots = max(1, min(1, int(payload.get("max_shots") or 1)))
        data, provider_id, model_id = await self._llm_json(
            payload,
            system=(
                "你是电影分镜师。只输出 JSON，格式 {\"shots\":[...]}。"
                "每个镜头必须包含 shot_id, image_prompt, video_prompt, narration, duration_seconds, camera_direction, action。"
                "image_prompt/video_prompt 用英文且必须保持角色身份和服装连续；narration 用中文。"
            ),
            user=(
                "剧本：\n" + json.dumps(screenplay, ensure_ascii=False) +
                "\n角色卡：\n" + json.dumps(characters, ensure_ascii=False) +
                f"\n只生成 {max_shots} 个可真实制作的镜头。"
            ),
            max_tokens=int(payload.get("max_tokens") or 3072),
        )
        shots = data.get("shots")
        if not isinstance(shots, list) or not shots:
            raise FullPipelineSemanticError("STORYBOARD_SCHEMA_INVALID", "storyboard requires a non-empty shots array")
        shot = shots[0]
        if not isinstance(shot, dict) or not str(shot.get("image_prompt") or "").strip() or not str(shot.get("video_prompt") or "").strip():
            raise FullPipelineSemanticError("STORYBOARD_SCHEMA_INVALID", "storyboard shot requires image_prompt and video_prompt")
        data["shots"] = [shot]
        path = self.artifacts.write_json(input.project_id, input.workflow_id, "storyboard", data)
        self.artifacts.patch_manifest(input.project_id, input.workflow_id, storyboard_path=str(path), shot_count=1)
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/storyboard",
            metadata={"provider_id": provider_id, "model_id": model_id, "artifact_path": str(path), "shot_count": "1"},
        )

    def _shot(self, input: StepActivityInput) -> dict[str, Any]:
        storyboard = self.artifacts.read_json(input.project_id, input.workflow_id, "storyboard")
        shots = storyboard.get("shots")
        if not isinstance(shots, list) or not shots or not isinstance(shots[0], dict):
            raise FullPipelineSemanticError("STORYBOARD_UNAVAILABLE", "no storyboard shot available")
        return dict(shots[0])

    async def _delegate(
        self,
        input: StepActivityInput,
        *,
        operation: str,
        payload: dict[str, Any],
        suffix: str,
        use_comfy: bool = False,
    ) -> StepActivityResult:
        payload_ref = self.visual.base.payloads.put(
            input.project_id,
            f"{input.workflow_id}-{suffix}",
            payload,
        )
        step = ProductionStep(
            step_id=f"{input.step.step_id}:{suffix}",
            skill_id=input.step.skill_id,
            operation=operation,
            payload_ref=payload_ref,
            idempotency_key=f"{input.step.idempotency_key}:{suffix}",
        )
        nested = StepActivityInput(workflow_id=input.workflow_id, project_id=input.project_id, step=step)
        if use_comfy:
            async with self.gpu.use(GPUOwner.comfyui):
                return await self.visual(nested)
        return await self.visual(nested)

    async def _image_from_storyboard(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        shot = self._shot(input)
        logical_key = self._required(payload, "logical_key")
        reference_id = self._required(payload, "reference_id")
        result = await self._delegate(
            input,
            operation="generation.image.generate_candidate",
            suffix="image-materialized",
            use_comfy=True,
            payload={
                "logical_key": logical_key,
                "provider_id": str(payload.get("provider_id") or "local-comfyui-image"),
                "model_id": str(payload.get("model_id") or "configured-image-workflow"),
                "shot_id": str(shot.get("shot_id") or "shot-1"),
                "source_text": str(shot.get("image_prompt") or "").strip(),
                "reference_ids": [reference_id],
                "entity_ids": [str(payload.get("entity_id") or "char_hero")],
                "camera_direction": str(shot.get("camera_direction") or ""),
                "action": str(shot.get("action") or ""),
                "duration_seconds": float(shot.get("duration_seconds") or 3.0),
                "metadata": {"source": "full_pipeline", "storyboard_shot_id": str(shot.get("shot_id") or "shot-1")},
            },
        )
        if result.kind == "completed":
            self.artifacts.patch_manifest(input.project_id, input.workflow_id, image_logical_key=logical_key)
        return result

    async def _h3_from_storyboard(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        shot = self._shot(input)
        logical_key = self._required(payload, "logical_key")
        image_logical_key = self._required(payload, "image_logical_key")
        length = int(payload.get("length") or 56)
        result = await self._delegate(
            input,
            operation="generation.h3.generate_candidate",
            suffix="h3-materialized",
            use_comfy=True,
            payload={
                "logical_key": logical_key,
                "provider_id": str(payload.get("provider_id") or "local-h3-video"),
                "model_id": str(payload.get("model_id") or "minimax-h3"),
                "shot_id": str(shot.get("shot_id") or "shot-1"),
                "prompt": str(shot.get("video_prompt") or "").strip(),
                "first_frame_logical_key": image_logical_key,
                "entity_ids": [str(payload.get("entity_id") or "char_hero")],
                "width": int(payload.get("width") or 512),
                "height": int(payload.get("height") or 320),
                "length": length,
                "steps": int(payload.get("steps") or 12),
                "seed": int(payload.get("seed") or 0),
                "metadata": {"source": "full_pipeline", "upstream_image_logical_key": image_logical_key},
            },
        )
        if result.kind == "completed":
            self.artifacts.patch_manifest(
                input.project_id,
                input.workflow_id,
                video_logical_key=logical_key,
                target_duration_seconds=max(0.1, length / 24.0),
            )
        return result

    async def _tts_from_screenplay(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        screenplay = self.artifacts.read_json(input.project_id, input.workflow_id, "screenplay")
        storyboard = self.artifacts.read_json(input.project_id, input.workflow_id, "storyboard")
        shots = storyboard.get("shots") if isinstance(storyboard.get("shots"), list) else []
        narration = str((shots[0].get("narration") if shots and isinstance(shots[0], dict) else "") or screenplay.get("narration") or "").strip()
        if not narration:
            raise FullPipelineSemanticError("NARRATION_UNAVAILABLE", "screenplay/storyboard contains no narration")
        fmt = str(payload.get("response_format") or "mp3").strip().lower().lstrip(".")
        result = await self._delegate(
            input,
            operation="tts.generate",
            suffix="tts",
            payload={
                "provider_id": str(payload.get("provider_id") or "edge-tts-gateway"),
                "model_id": str(payload.get("model_id") or "edge-tts"),
                "text": narration,
                "voice": str(payload.get("voice") or "zh-CN-XiaoxiaoNeural"),
                "response_format": fmt,
                "speed": float(payload.get("speed") or 1.0),
                "instructions": str(payload.get("instructions") or ""),
            },
        )
        if result.kind == "completed":
            prefix = "artifact://tts/"
            artifact_id = result.output_ref[len(prefix):] if result.output_ref.startswith(prefix) else ""
            voice_path = Path(self.settings.data_dir) / "v3" / "media" / "tts" / f"{artifact_id}.{fmt}"
            if not voice_path.is_file() or voice_path.stat().st_size <= 0:
                raise FileNotFoundError(f"TTS artifact unavailable: {voice_path}")
            self.artifacts.patch_manifest(input.project_id, input.workflow_id, voice_path=str(voice_path), narration=narration)
        return result

    def _subtitle(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        manifest = self.artifacts.manifest(input.project_id, input.workflow_id)
        narration = str(manifest.get("narration") or "").strip()
        if not narration:
            screenplay = self.artifacts.read_json(input.project_id, input.workflow_id, "screenplay")
            narration = str(screenplay.get("narration") or "").strip()
        if not narration:
            raise FullPipelineSemanticError("NARRATION_UNAVAILABLE", "subtitle generation requires narration")
        duration = float(manifest.get("target_duration_seconds") or payload.get("duration_seconds") or 3.0)
        pieces = [item.strip() for item in re.split(r"(?<=[。！？!?；;])", narration) if item.strip()]
        cues = proportional_cues(tuple(pieces or [narration]), duration)
        target = self.artifacts.workflow_dir(input.project_id, input.workflow_id) / "subtitles.srt"
        write_srt(cues, target)
        self.artifacts.patch_manifest(input.project_id, input.workflow_id, subtitle_path=str(target), subtitle_cue_count=len(cues))
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/subtitles",
            metadata={"artifact_path": str(target), "cue_count": str(len(cues))},
        )

    def _bgm_select(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        requested = str(payload.get("bgm_path") or "").strip()
        if requested:
            path = Path(requested)
            validate_audio_file(path, timeout_seconds=30)
        else:
            files = self.bgm.list_files()
            if not files:
                raise FullPipelineSemanticError(
                    "BGM_UNAVAILABLE",
                    "BGM library is empty; upload a BGM file before starting full-pipeline validation",
                )
            path = files[0]
            validate_audio_file(path, timeout_seconds=30)
        self.artifacts.patch_manifest(input.project_id, input.workflow_id, bgm_path=str(path))
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/bgm",
            metadata={"artifact_path": str(path)},
        )

    def _compose(self, input: StepActivityInput, payload: dict[str, Any]) -> StepActivityResult:
        manifest = self.artifacts.manifest(input.project_id, input.workflow_id)
        video_logical = str(manifest.get("video_logical_key") or payload.get("video_logical_key") or "").strip()
        if not video_logical:
            raise FullPipelineSemanticError("VIDEO_UNAVAILABLE", "composition requires video logical key")
        adopted = self.visual.base.resources.adopted(input.project_id, video_logical)
        if not adopted:
            raise FullPipelineSemanticError("VIDEO_UNAVAILABLE", "composition requires an adopted video resource")
        metadata = adopted.get("metadata") if isinstance(adopted.get("metadata"), dict) else {}
        video_path = Path(str(metadata.get("artifact_path") or ""))
        voice_path = Path(str(manifest.get("voice_path") or ""))
        subtitle_path = Path(str(manifest.get("subtitle_path") or ""))
        bgm_path = Path(str(manifest.get("bgm_path") or ""))
        final_path = self.artifacts.workflow_dir(input.project_id, input.workflow_id) / "final.mp4"
        receipt = self.composition.compose(
            CompositionRequest(
                video_clips=(video_path,),
                output_path=final_path,
                voice_path=voice_path,
                bgm_path=bgm_path,
                subtitle_path=subtitle_path,
                voice_volume=float(payload.get("voice_volume") or 1.0),
                bgm_volume=float(payload.get("bgm_volume") or 0.18),
                preferred_video_codec=str(payload.get("preferred_video_codec") or "libx264"),
            )
        )
        self.artifacts.patch_manifest(
            input.project_id,
            input.workflow_id,
            final_path=str(receipt.output_path),
            final_bytes=receipt.output_path.stat().st_size,
            final_video_codec=receipt.video_codec,
            final_audio_codec=receipt.audio_codec,
            status="completed",
        )
        return StepActivityResult(
            kind="completed",
            output_ref=f"artifact://full/{input.workflow_id}/final.mp4",
            metadata={
                "artifact_path": str(receipt.output_path),
                "video_codec": receipt.video_codec,
                "audio_codec": receipt.audio_codec,
                "bytes": str(receipt.output_path.stat().st_size),
            },
        )
