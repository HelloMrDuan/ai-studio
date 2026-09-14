from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import httpx
from PIL import Image

from app.config import Settings
from app.models import GPUOwner
from app.services.facefusion import FaceFusionService
from app.v3.character_prompt_integration import install_character_prompt_integration
from app.v3.character_reference_hardening import install_character_reference_hardening
from app.v3.audit.identity import IdentityAuditPolicy, IdentityAuditDecision, evaluate_identity_similarity
from app.v3.generation_executor import ComfyWorkflowBindingError, ReferenceAsset, ReferenceAssetError
from app.v3.provider_gateway import ProviderResolutionError
from app.v3.reference_role_policy import install_reference_role_policy
from app.v3.resource_store import ResourceStoreError

from .contracts import StepActivityInput, StepActivityResult
from .production_cached_executor import ProductionCachedFullPipelineExecutor, _MATERIALIZED
from .unified_image_executor import UnifiedImageDomainExecutor


_CHARACTER_PACKAGE_PHASES = {"costume", "turnaround"}
_FACE_IDENTITY_ROLES = {
    "character_face_anchor",
    "character_identity",
    "character_consistency",
    "character_reference",
}


class ProductionWorkerExecutor(ProductionCachedFullPipelineExecutor):
    """Production worker with an explicit Z-Image -> FaceFusion character chain.

    Z-Image remains the pixel renderer for character face/costume/turnaround
    assets. For costume and turnaround stages, the adopted face anchor is then
    applied as a separate identity post-process. This keeps model responsibility
    clear: Z-Image owns design/rendering, FaceFusion owns identity preservation.
    """

    def __init__(self, settings: Settings) -> None:
        install_character_prompt_integration()
        install_character_reference_hardening()
        install_reference_role_policy()
        super().__init__(settings, visual=UnifiedImageDomainExecutor(settings))
        self.facefusion = FaceFusionService(settings)

    def _remember_failure(
        self,
        input: StepActivityInput,
        *,
        code: str,
        message: str,
    ) -> StepActivityResult:
        result = StepActivityResult(
            kind="semantic_failure",
            error_code=code,
            message=message,
            metadata={"executor": "v3-production-worker"},
        )
        self.visual.base.results.put(input.project_id, input.step.idempotency_key, result)
        return result

    @staticmethod
    def _reference_phase(payload: dict) -> str:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return str(
            metadata.get("reference_phase")
            or payload.get("reference_phase")
            or ""
        ).strip().lower()

    def _face_identity_reference(self, payload: dict) -> ReferenceAsset:
        refs = [str(value).strip() for value in payload.get("reference_ids") or [] if str(value).strip()]
        for reference_id in refs:
            asset = self.visual.base.references.resolve(reference_id)
            role = str(asset.role or "").strip().lower()
            if role in _FACE_IDENTITY_ROLES:
                return asset
        raise ReferenceAssetError(
            "角色定装/三视图缺少已采用的 character_face_anchor，拒绝把服装图当作身份脸参考"
        )

    async def _identity_tool(
        self,
        script_name: str,
        *arguments: Path,
        result_json: Path,
        log,
    ) -> dict:
        script = Path(__file__).resolve().parents[3] / "scripts" / script_name
        python = Path(self.settings.identity_runtime_python)
        if not python.is_file() or not script.is_file():
            raise RuntimeError(f"身份质量工具不可用：python={python} script={script}")
        process = await asyncio.create_subprocess_exec(
            str(python), str(script), *[str(value) for value in arguments],
            "--result-json", str(result_json),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=float(self.settings.facefusion_task_timeout_seconds)
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"身份质量工具超时：{script_name}")
        detail = (stdout + stderr).decode("utf-8", errors="replace").strip()
        if detail:
            await log(f"{script_name}: {detail[-3000:]}")
        if process.returncode != 0 or not result_json.is_file():
            raise RuntimeError(
                f"身份质量工具失败：{script_name} returncode={process.returncode} detail={detail[-3000:]}"
            )
        return json.loads(result_json.read_text(encoding="utf-8"))

    async def _apply_character_identity(
        self,
        input: StepActivityInput,
        payload: dict,
        result: StepActivityResult,
    ) -> StepActivityResult:
        if result.kind != "completed":
            return result
        phase = self._reference_phase(payload)
        references = [str(value).strip() for value in payload.get("reference_ids") or [] if str(value).strip()]
        if phase not in _CHARACTER_PACKAGE_PHASES or not references:
            return result
        if result.metadata.get("media_cache_hit") == "true":
            return result

        artifact_path = Path(str(result.metadata.get("artifact_path") or ""))
        if not artifact_path.is_file() or artifact_path.stat().st_size <= 0:
            raise FileNotFoundError("Z-Image 角色候选生成完成但产物文件不存在")
        face = self._face_identity_reference(payload)
        output_dir = (
            Path(self.settings.data_dir)
            / "v3"
            / "identity-postprocess"
            / input.project_id
            / input.step.idempotency_key
        )
        log_tail: list[str] = []

        async def log(line: str) -> None:
            text = str(line or "").strip()
            if text:
                log_tail.append(text)
                if len(log_tail) > 80:
                    del log_tail[:-80]
            print(
                "FACEFUSION_IDENTITY "
                f"project_id={input.project_id} workflow={input.workflow_id} "
                f"step={input.step.step_id} phase={phase} face_ref={face.reference_id} {text}",
                flush=True,
            )

        async def run_swap(
            target: Path,
            destination: Path,
            *,
            model: str,
            pixel_boost: str,
        ) -> Path:
            return await self.facefusion.run(
                processor="face_swapper",
                source_path=face.path,
                target_path=target,
                output_dir=destination,
                params={
                    "face_selector_mode": "one",
                    "face_mask_types": ["box"],
                    "output_quality": 95,
                    "face_swapper_model": model,
                    "face_swapper_pixel_boost": pixel_boost,
                    "face_swapper_weight": 1.0,
                },
                log=log,
            )

        async def swap_with_small_face_retry(target: Path, destination: Path) -> Path:
            try:
                return await run_swap(
                    target, destination,
                    model="hyperswap_1a_256", pixel_boost="512x512",
                )
            except RuntimeError as first_error:
                await log(f"首次身份锁定失败，启用小脸兼容重试：{first_error}")
                retry_dir = destination / "small-face-retry"
                retry_dir.mkdir(parents=True, exist_ok=True)
                retry_target = retry_dir / "target-upscaled.png"
                with Image.open(target) as image:
                    original_size = image.size
                    image.convert("RGB").resize(
                        (image.width * 2, image.height * 2), Image.Resampling.LANCZOS,
                    ).save(retry_target)
                try:
                    retry_processed = await run_swap(
                        retry_target, retry_dir / "facefusion",
                        model="inswapper_128_fp16", pixel_boost="1024x1024",
                    )
                except RuntimeError as second_error:
                    detail = " | ".join(log_tail[-24:])
                    raise RuntimeError(
                        "FaceFusion 身份锁定两次失败；"
                        f"首次={first_error}；重试={second_error}；日志尾部={detail}"
                    ) from second_error
                normalized = retry_dir / "result-normalized.png"
                with Image.open(retry_processed) as retry_image:
                    retry_image.convert("RGB").resize(
                        original_size, Image.Resampling.LANCZOS,
                    ).save(normalized)
                await log("小脸兼容重试成功")
                return normalized

        # FullPipelineExecutor releases the ComfyUI lease before this method runs.
        # The same orchestrator can therefore safely reclaim VRAM for FaceFusion.
        async with self.gpu.use(GPUOwner.facefusion):
            if phase == "turnaround":
                panels_dir = output_dir / "panels"
                panels_dir.mkdir(parents=True, exist_ok=True)
                with Image.open(artifact_path) as sheet:
                    sheet = sheet.convert("RGB")
                    panel_width = sheet.width // 3
                    panels = [
                        sheet.crop((index * panel_width, 0, (index + 1) * panel_width, sheet.height))
                        for index in range(3)
                    ]
                panel_paths: list[Path] = []
                for index, panel in enumerate(panels):
                    path = panels_dir / f"panel-{index}.png"
                    panel.save(path)
                    panel_paths.append(path)
                locked_front = await swap_with_small_face_retry(panel_paths[0], panels_dir / "front")
                locked_side = await swap_with_small_face_retry(panel_paths[1], panels_dir / "side")
                with Image.open(locked_front) as front, Image.open(locked_side) as side, Image.open(panel_paths[2]) as back:
                    combined = Image.new("RGB", (panel_width * 3, panels[0].height), "white")
                    combined.paste(front.convert("RGB"), (0, 0))
                    combined.paste(side.convert("RGB"), (panel_width, 0))
                    combined.paste(back.convert("RGB"), (panel_width * 2, 0))
                    processed = output_dir / "facefusion-turnaround.png"
                    combined.save(processed)
            else:
                processed = await swap_with_small_face_retry(artifact_path, output_dir)

            restored = output_dir / "identity-restored.png"
            await self._identity_tool(
                "v3_face_identity_restore.py", face.path, processed, restored,
                result_json=output_dir / "identity-restore.json", log=log,
            )
            audit_payload = await self._identity_tool(
                "v3_identity_score.py", face.path, restored,
                result_json=output_dir / "identity-audit.json", log=log,
            )
            audit = evaluate_identity_similarity(
                float(audit_payload["cosine_similarity"]),
                policy=IdentityAuditPolicy(
                    policy_id="character-reference-buffalo-l-v1",
                    recognition_model=str(audit_payload["recognition_model"]),
                    pass_threshold=float(self.settings.character_identity_pass_threshold),
                    fail_threshold=0.80,
                ),
            )
            if audit.decision != IdentityAuditDecision.pass_:
                raise RuntimeError(
                    "角色身份相似度未达到生产门槛："
                    f"score={audit.cosine_similarity:.6f} threshold={audit.pass_threshold:.2f}"
                )
            processed = restored

        if not processed.is_file() or processed.stat().st_size <= 0:
            raise RuntimeError("FaceFusion 身份锁定没有产生有效图片")
        if processed.suffix.lower() != artifact_path.suffix.lower():
            # Z-Image normally materializes PNG. If an environment emits JPEG,
            # normalize the post-processed bytes back to that exact extension.
            converted = output_dir / f"result-normalized{artifact_path.suffix.lower()}"
            with Image.open(processed) as image:
                image.convert("RGB").save(converted)
            processed = converted
        temp = artifact_path.with_name(f".{artifact_path.name}.facefusion.tmp{artifact_path.suffix}")
        shutil.copy2(processed, temp)
        temp.replace(artifact_path)

        metadata = dict(result.metadata)
        metadata.update(
            {
                "runtime_image_backend": "z_image_turbo_facefusion",
                "primary_renderer": "z-image-turbo",
                "identity_postprocess": "facefusion",
                "identity_postprocess_required": "false",
                "identity_reference_id": face.reference_id,
                "identity_similarity": f"{audit.cosine_similarity:.6f}",
                "identity_audit_policy": audit.policy_id,
                "reference_phase": phase,
                "artifact_path": str(artifact_path),
                "bytes_written": str(artifact_path.stat().st_size),
            }
        )
        job = self.visual.jobs.get(input.project_id, input.step.idempotency_key) or {}
        candidate = self.visual._candidate_once(
            input, payload, provider_id="local-zimage-image", model_id="z-image-turbo",
            reference_ids=references, artifact_ref=result.output_ref,
            artifact_path=artifact_path, prompt_id=str(job.get("prompt_id") or ""), kind="image",
        )
        metadata["resource_id"] = str(candidate["resource_id"])
        metadata["logical_key"] = str(candidate["logical_key"])
        final = StepActivityResult(
            kind="completed",
            output_ref=result.output_ref,
            message=result.message,
            metadata=metadata,
        )
        self.visual.base.results.put(input.project_id, input.step.idempotency_key, final)

        # CachedMaterializedDomainExecutor stores the Z-Image artifact immediately
        # after rendering. Rewrite the same signature only after identity locking,
        # otherwise a later task could reuse the pre-FaceFusion bytes.
        signature = self.visual.signature(input.project_id, input.step.operation, payload)
        if signature:
            payload_meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            self.visual.content_cache.put(
                input.project_id,
                signature,
                {
                    "kind": "image",
                    "operation": input.step.operation,
                    "provider_id": "local-zimage-image",
                    "model_id": "z-image-turbo",
                    "reference_ids": references,
                    "artifact_ref": final.output_ref,
                    "artifact_path": str(artifact_path),
                    "generation_task_id": input.step.idempotency_key,
                    "quality_stage": str(payload_meta.get("quality_stage") or "preview"),
                    "identity_postprocess": "facefusion",
                    "reference_phase": phase,
                },
            )
        return final

    async def __call__(self, input: StepActivityInput) -> StepActivityResult:
        operation = input.step.operation
        payload: dict | None = None
        try:
            if operation in _MATERIALIZED:
                # FullPipelineExecutor normally acquires the Comfy workspace before
                # delegating visual work. Exact content reuse needs no GPU at all,
                # so inspect the content-addressed cache first.
                cached_task = self.visual.base.results.get(input.project_id, input.step.idempotency_key)
                if cached_task is not None:
                    return cached_task
                payload = self.visual.base.payloads.resolve(input.project_id, input.step.payload_ref)
                signature = self.visual.signature(input.project_id, operation, payload)
                if signature:
                    record = self.visual.content_cache.get(input.project_id, signature)
                    if record is not None:
                        return self.visual._cache_hit_result(input, payload, signature, record)

            result = await super().__call__(input)
            if operation == "generation.image.generate_candidate" and payload is not None:
                result = await self._apply_character_identity(input, payload, result)
            return result
        except httpx.HTTPStatusError as exc:
            response = exc.response
            status = int(response.status_code) if response is not None else 0
            # ComfyUI 4xx responses are deterministic workflow/input rejection,
            # not transient infrastructure faults. Preserve the response body so
            # the workbench shows the actual missing node/model/input instead of
            # the useless Temporal text "Workflow execution failed".
            if 400 <= status < 500:
                body = ""
                try:
                    body = (response.text or "").strip().replace("\x00", "")
                except Exception:
                    body = ""
                detail = body[:3000] or str(exc)
                return self._remember_failure(
                    input,
                    code="COMFY_REQUEST_REJECTED",
                    message=f"ComfyUI HTTP {status}: {detail}",
                )
            raise
        except (
            ComfyWorkflowBindingError,
            ReferenceAssetError,
            ProviderResolutionError,
            ResourceStoreError,
            FileNotFoundError,
            ValueError,
        ) as exc:
            return self._remember_failure(
                input,
                code="REFERENCE_WORKFLOW_CONTRACT_ERROR",
                message=f"{type(exc).__name__}: {exc}",
            )
        except RuntimeError as exc:
            return self._remember_failure(
                input,
                code="IDENTITY_POSTPROCESS_FAILED",
                message=f"{type(exc).__name__}: {exc}",
            )


__all__ = ["ProductionWorkerExecutor"]
