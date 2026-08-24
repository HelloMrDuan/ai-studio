import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from app.core.gpu_orchestrator import GPUOrchestrator
from app.core.task_store import TaskStore
from app.models import GPUOwner, TaskStatus
from app.services.assets import AssetService
from app.services.comfyui import ComfyUIService
from app.services.facefusion import FaceFusionService
from app.services.h3_video import H3VideoService


class TaskRunner:
    def __init__(
        self,
        store: TaskStore,
        assets: AssetService,
        gpu: GPUOrchestrator,
        comfyui: ComfyUIService,
        h3_video: H3VideoService,
        facefusion: FaceFusionService,
    ) -> None:
        self.store = store
        self.assets = assets
        self.gpu = gpu
        self.comfyui = comfyui
        self.h3_video = h3_video
        self.facefusion = facefusion
        self._tasks: set[asyncio.Task] = set()

    def submit_image(
        self,
        *,
        params: dict[str, Any],
    ):
        task_id = uuid.uuid4().hex
        record = self.store.create(
            task_id=task_id,
            module="image",
            operation="generate",
            title="图片生成",
            params=params,
            input_files=[],
        )
        self._spawn(self._run_image(task_id, params))
        return record

    def submit_facefusion(
        self,
        *,
        processor: str,
        params: dict[str, Any],
        source_path: Path | None,
        target_path: Path,
    ):
        task_id = uuid.uuid4().hex
        inputs = [str(target_path)]
        if source_path:
            inputs.insert(0, str(source_path))
        record = self.store.create(
            task_id=task_id,
            module="facefusion",
            operation=processor,
            title="人物与画面处理",
            params=params,
            input_files=inputs,
        )
        self._spawn(
            self._run_facefusion(
                task_id, processor, params, source_path, target_path
            )
        )
        return record

    def submit_video(
        self,
        *,
        params: dict[str, Any],
        first_frame: Path | None,
        last_frame: Path | None,
        reference_image: Path | None,
    ):
        task_id = uuid.uuid4().hex
        inputs = [str(p) for p in (first_frame, last_frame, reference_image) if p is not None]
        record = self.store.create(
            task_id=task_id,
            module="video",
            operation=str(params.get("mode", "fl2va")),
            title="视频生成",
            params=params,
            input_files=inputs,
        )
        self._spawn(
            self._run_video(
                task_id=task_id,
                params=params,
                first_frame=first_frame,
                last_frame=last_frame,
                reference_image=reference_image,
            )
        )
        return record

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _log(self, task_id: str, line: str) -> None:
        self.store.add_log(task_id, line)

    async def _run_image(self, task_id: str, params: dict[str, Any]) -> None:
        try:
            self.store.update(
                task_id,
                status=TaskStatus.switching_gpu,
                progress=5,
                message="正在切换到图片生成 GPU 工作区",
            )
            await self.gpu.ensure_ready(GPUOwner.comfyui)
            self.store.update(
                task_id,
                status=TaskStatus.running,
                progress=15,
                message=("正在使用 Z-Image-Turbo 生成工作分辨率画面" if str(params.get("model_key")) == "z_image_turbo" else "正在按全比例质量配置生成并执行 AI 超分"),
            )
            await self._log(
                task_id,
                f"生成模型：{params.get('model_label', params.get('model_key', ''))}；"
                f"checkpoint：{params.get('checkpoint', '')}；"
                f"请求方式：{params.get('requested_model_key', 'smart')}；"
                f"姿态控制：{params.get('requested_pose_control', 'auto')} → {params.get('pose_control', 'off')} / {params.get('pose_template', 'off')}；"
                f"人物外貌增强：{params.get('appearance_label', '关闭')}",
            )
            await self._log(
                task_id,
                f"比例：{params.get('aspect_label', params.get('aspect_ratio', ''))}；"
                f"风格：{params.get('style_label', params.get('style_name', ''))}；"
                f"强度：{params.get('style_strength_label', params.get('style_strength', ''))}",
            )
            compiler_status = str(params.get("prompt_compiler_status", "unknown"))
            cache = params.get("prompt_compiler_cache", {})
            cache_key = str(cache.get("key", ""))[:12] if isinstance(cache, dict) else ""
            await self._log(
                task_id,
                f"语义编译器：{compiler_status}；Schema={params.get('prompt_compiler_schema', '1.0')}；"
                f"缓存={'命中' if compiler_status == 'cache' else '未命中'}"
                + (f"；key={cache_key}" if cache_key else ""),
            )
            if params.get("prompt_compiler_error"):
                await self._log(
                    task_id,
                    f"语义编译回退原因：{params.get('prompt_compiler_error')}",
                )
            await self._log(
                task_id,
                "结构化语义：" + json.dumps(
                    params.get("prompt_semantic", {}),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            await self._log(
                task_id,
                f"最终正向提示词：{params.get('positive_prompt', '')}",
            )
            await self._log(
                task_id,
                f"最终反向提示词：{params.get('negative_prompt', '')}",
            )
            async with self.gpu.use(GPUOwner.comfyui):
                output_dir = self.store.task_dir(task_id) / "outputs"
                paths = await self.comfyui.generate(
                    positive=str(params["positive_prompt"]),
                    negative=str(params.get("negative_prompt", "")),
                    user_positive=str(params.get("user_positive_prompt", "")),
                    model_key=str(params["model_key"]),
                    model_label=str(params.get("model_label", params["model_key"])),
                    checkpoint=str(params["checkpoint"]),
                    subject_is_human=bool(params.get("subject_is_human", False)),
                    pose_control=str(params.get("pose_control", "off")),
                    pose_template=str(params.get("pose_template", "off")),
                    face_detailer_enabled=bool(params.get("face_detailer", True)),
                    face_detailer_denoise=float(params.get("face_detailer_denoise", 0.22)),
                    appearance_enabled=bool(params.get("appearance_enabled", False)),
                    appearance_label=str(params.get("appearance_label", "关闭")),
                    appearance_lora_name=str(params.get("appearance_lora_name", "")),
                    appearance_lora_trigger=str(params.get("appearance_lora_trigger", "")),
                    appearance_lora_trigger_weight=float(params.get("appearance_lora_trigger_weight", 0.75)),
                    appearance_lora_strength_model=float(params.get("appearance_lora_strength_model", 0.30)),
                    appearance_lora_strength_clip=float(params.get("appearance_lora_strength_clip", 0.30)),
                    upscaler=str(params.get("upscaler", "4x-UltraSharp.pth")),
                    aspect_ratio=str(params.get("aspect_ratio", "16:9")),
                    base_width=int(params["base_width"]),
                    base_height=int(params["base_height"]),
                    output_width=int(params["output_width"]),
                    output_height=int(params["output_height"]),
                    steps=int(params.get("steps", 30)),
                    cfg=float(params.get("cfg", 7)),
                    seed=int(params.get("seed", -1)),
                    sampler=str(params.get("sampler", "euler")),
                    scheduler=str(params.get("scheduler", "normal")),
                    count=int(params.get("count", 1)),
                    style_name=str(params.get("style_name", "portrait_photo")),
                    output_dir=output_dir,
                    log=lambda line: self._log(task_id, line),
                )
            urls = [self.assets.url(path) for path in paths]
            self.store.update(
                task_id,
                status=TaskStatus.completed,
                progress=100,
                message=("Z-Image-Turbo 工作分辨率图片生成完成" if str(params.get("model_key")) == "z_image_turbo" else "最终 4K 图片生成完成（Gemma 结构化语义编译、动态模型、配置化 LoRA、可选姿态控制与 AI 超分已应用）"),
                output_files=urls,
            )
        except Exception as exc:
            self.store.update(
                task_id,
                status=TaskStatus.failed,
                message="图片生成失败",
                error=str(exc),
            )

    async def _run_video(
        self,
        *,
        task_id: str,
        params: dict[str, Any],
        first_frame: Path | None,
        last_frame: Path | None,
        reference_image: Path | None,
    ) -> None:
        async def set_progress(progress: int, message: str) -> None:
            self.store.update(task_id,status=TaskStatus.running,progress=max(0,min(int(progress),99)),message=message)
        try:
            self.store.update(task_id,status=TaskStatus.switching_gpu,progress=5,message="正在切换到 ComfyUI 视频生成工作区")
            await self.gpu.ensure_ready(GPUOwner.comfyui)
            self.store.update(task_id,status=TaskStatus.running,progress=15,message="正在准备 MiniMax H3")
            mode=str(params.get("mode","fl2va"))
            video_profile=str(params.get("video_profile","standard")).strip().lower()
            if video_profile not in {"standard","turbo"}: raise ValueError("video_profile 只能是 standard 或 turbo")
            output_dir=self.store.task_dir(task_id)/"outputs"
            common=dict(prompt=str(params["prompt"]),width=int(params["width"]),height=int(params["height"]),length=int(params["length"]),steps=4 if video_profile=="turbo" else int(params["steps"]),seed=int(params["seed"]),output_dir=output_dir,progress=set_progress,log=lambda line:self._log(task_id,line),video_profile=video_profile)
            async with self.gpu.use(GPUOwner.comfyui):
                if mode=="t2va": result=await self.h3_video.generate_t2va(**common)
                elif mode=="fl2va":
                    if first_frame is None: raise ValueError("FL2VA 必须提供首帧图片")
                    result=await self.h3_video.generate_fl2va(first_frame=first_frame,last_frame=last_frame,**common)
                elif mode=="ref2va":
                    if reference_image is None: raise ValueError("REF2VA 必须提供参考图片")
                    result=await self.h3_video.generate_ref2va(reference_image=reference_image,ref_image_size=str(params.get("ref_image_size","match")),**common)
                else: raise ValueError(f"不支持的视频模式：{mode}")
            result_params=dict(params); result_params["video_profile"]=video_profile; result_params["effective_steps"]=4 if video_profile=="turbo" else int(params["steps"]); result_params["result_meta"]=result.get("metadata",{})
            path=Path(result["path"])
            self.store.update(task_id,status=TaskStatus.completed,progress=100,message=f"MiniMax H3 {mode.upper()} {video_profile.upper()} 视频生成完成",output_files=[self.assets.url(path)],params=result_params)
        except Exception as exc:
            self.store.update(task_id,status=TaskStatus.failed,message="视频生成失败",error=f"{type(exc).__name__}: {exc}")

    async def _run_facefusion(
        self,
        task_id: str,
        processor: str,
        params: dict[str, Any],
        source_path: Path | None,
        target_path: Path,
    ) -> None:
        try:
            self.store.update(
                task_id,
                status=TaskStatus.switching_gpu,
                progress=5,
                message="正在切换到人物与画面处理 GPU 工作区",
            )
            await self.gpu.ensure_ready(GPUOwner.facefusion)
            self.store.update(
                task_id,
                status=TaskStatus.running,
                progress=15,
                message="FaceFusion 正在处理",
            )
            async with self.gpu.use(GPUOwner.facefusion):
                output_dir = self.store.task_dir(task_id) / "outputs"
                path = await self.facefusion.run(
                    processor=processor,
                    source_path=source_path,
                    target_path=target_path,
                    output_dir=output_dir,
                    params=params,
                    log=lambda line: self._log(task_id, line),
                )
            self.store.update(
                task_id,
                status=TaskStatus.completed,
                progress=100,
                message="处理完成",
                output_files=[self.assets.url(path)],
            )
        except Exception as exc:
            self.store.update(
                task_id,
                status=TaskStatus.failed,
                message="处理失败",
                error=str(exc),
            )
