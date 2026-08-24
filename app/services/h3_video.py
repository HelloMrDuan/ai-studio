from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from app.config import Settings


ProgressCallback = Callable[[int, str], Awaitable[None]]
LogCallback = Callable[[str], Awaitable[None]]


class H3VideoService:
    """MiniMax H3 video generation through the existing ComfyUI backend.

    H3 remains under the existing ComfyUI GPU owner. Routing is technical and
    configuration driven only; prompt text is never keyword-scanned.
    """

    REQUIRED_NODES = {
        "UNETLoader",
        "CLIPLoader",
        "VAELoader",
        "LoadImage",
        "MiniMaxH3ImageToVideo",
        "MiniMaxH3ReferenceToVideo",
        "MiniMaxH3SigmaShift",
        "RandomNoise",
        "KSamplerSelect",
        "BasicScheduler",
        "BasicGuider",
        "SamplerCustomAdvanced",
        "LTXVSeparateAVLatent",
        "VAEDecode",
        "VAEDecodeAudio",
        "CreateVideo",
        "SaveVideo",
    }

    TURBO_LORA = "minimax_h3_turbo_4步加速_comfyui.safetensors"
    TURBO_STEPS = 4

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.comfy_dir = Path(settings.h3_comfyui_dir)
        self.input_dir = self.comfy_dir / "input"
        self.output_dir = self.comfy_dir / "output"

    @property
    def base_url(self) -> str:
        return self.settings.comfyui_base_url.rstrip("/")

    async def _object_info(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.get(f"{self.base_url}/object_info")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("ComfyUI object_info 返回格式异常")
        return payload

    @staticmethod
    def _choices(node_info: dict[str, Any], node: str, field: str, *, optional: bool = False) -> list[str]:
        group = "optional" if optional else "required"
        try:
            spec = node_info[node]["input"][group][field]
        except (KeyError, TypeError, IndexError):
            return []
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return [str(item) for item in spec[0]]
        return []

    @staticmethod
    def _has_choice(choices: list[str], expected: str) -> bool:
        expected_lower = Path(expected).name.lower()
        return expected_lower in {Path(item).name.lower() for item in choices}

    @staticmethod
    def _require_choice(choices: list[str], expected: str, label: str) -> None:
        if not H3VideoService._has_choice(choices, expected):
            raise RuntimeError(f"ComfyUI 未识别 {label}：{expected}")

    async def capabilities(self) -> dict[str, Any]:
        try:
            info = await self._object_info()
            missing = sorted(self.REQUIRED_NODES - set(info))
            unets = self._choices(info, "UNETLoader", "unet_name")
            clips = self._choices(info, "CLIPLoader", "clip_name")
            vaes = self._choices(info, "VAELoader", "vae_name")
            loras = self._choices(info, "LoraLoaderModelOnly", "lora_name") if "LoraLoaderModelOnly" in info else []

            fl2va_ok = self._has_choice(unets, self.settings.h3_fl2va_model)
            ref2va_ok = self._has_choice(unets, self.settings.h3_ref2va_model)
            encoder_ok = self._has_choice(clips, self.settings.h3_text_encoder)
            video_vae_ok = self._has_choice(vaes, self.settings.h3_video_vae)
            audio_vae_ok = self._has_choice(vaes, self.settings.h3_audio_vae)
            turbo_node_ok = "LoraLoaderModelOnly" in info
            turbo_lora_ok = self._has_choice(loras, self.TURBO_LORA)

            common_ok = not missing and encoder_ok and video_vae_ok and audio_vae_ok
            t2va_ok = common_ok and fl2va_ok
            fl2va_ready = common_ok and fl2va_ok
            ref2va_ready = common_ok and ref2va_ok
            available = t2va_ok or fl2va_ready or ref2va_ready
            turbo_ready = available and turbo_node_ok and turbo_lora_ok

            return {
                "available": available,
                "message": "MiniMax H3 视频能力已就绪" if available else "MiniMax H3 环境不完整",
                "fps": 24,
                "tested_profile": {"width": 768, "height": 448, "length": 124, "steps": 20},
                "profiles": {
                    "standard": {"enabled": available, "label": "标准", "steps": 20},
                    "turbo": {
                        "enabled": turbo_ready,
                        "label": "Turbo LoRA · 4步",
                        "steps": self.TURBO_STEPS,
                        "lora": self.TURBO_LORA,
                        "node": "LoraLoaderModelOnly",
                    },
                },
                "modes": {
                    "t2va": {"enabled": t2va_ok, "label": "文本生成视频"},
                    "fl2va": {"enabled": fl2va_ready, "label": "首尾帧生成视频"},
                    "ref2va": {"enabled": ref2va_ready, "label": "参考图生成视频", "reference_types": ["image"]},
                },
                "models": {
                    "fl2va": self.settings.h3_fl2va_model,
                    "ref2va": self.settings.h3_ref2va_model,
                    "text_encoder": self.settings.h3_text_encoder,
                    "video_vae": self.settings.h3_video_vae,
                    "audio_vae": self.settings.h3_audio_vae,
                    "turbo_lora": self.TURBO_LORA,
                },
                "checks": {
                    "missing_nodes": missing,
                    "fl2va_model": fl2va_ok,
                    "ref2va_model": ref2va_ok,
                    "text_encoder": encoder_ok,
                    "video_vae": video_vae_ok,
                    "audio_vae": audio_vae_ok,
                    "turbo_lora_node": turbo_node_ok,
                    "turbo_lora_model": turbo_lora_ok,
                },
            }
        except Exception as exc:
            return {
                "available": False,
                "message": f"H3 能力检查失败：{type(exc).__name__}: {exc}",
                "fps": 24,
                "profiles": {
                    "standard": {"enabled": False, "label": "标准", "steps": 20},
                    "turbo": {"enabled": False, "label": "Turbo LoRA · 4步", "steps": self.TURBO_STEPS, "lora": self.TURBO_LORA},
                },
                "modes": {
                    "t2va": {"enabled": False, "label": "文本生成视频"},
                    "fl2va": {"enabled": False, "label": "首尾帧生成视频"},
                    "ref2va": {"enabled": False, "label": "参考图生成视频"},
                },
            }

    async def _validate_runtime(self, model_name: str, video_profile: str = "standard") -> tuple[str, str]:
        info = await self._object_info()
        missing = sorted(self.REQUIRED_NODES - set(info))
        if missing:
            raise RuntimeError("ComfyUI 缺少 H3 必需节点：" + ", ".join(missing))

        unets = self._choices(info, "UNETLoader", "unet_name")
        clips = self._choices(info, "CLIPLoader", "clip_name")
        vaes = self._choices(info, "VAELoader", "vae_name")

        self._require_choice(unets, model_name, "H3 diffusion 模型")
        self._require_choice(clips, self.settings.h3_text_encoder, "H3 文本编码器")
        self._require_choice(vaes, self.settings.h3_video_vae, "H3 Video VAE")
        self._require_choice(vaes, self.settings.h3_audio_vae, "H3 Audio VAE")

        profile = str(video_profile or "standard").strip().lower()
        if profile not in {"standard", "turbo"}:
            raise ValueError("video_profile 只能是 standard 或 turbo")
        if profile == "turbo":
            if "LoraLoaderModelOnly" not in info:
                raise RuntimeError("ComfyUI 缺少 Turbo 所需内置节点 LoraLoaderModelOnly")
            loras = self._choices(info, "LoraLoaderModelOnly", "lora_name")
            self._require_choice(loras, self.TURBO_LORA, "H3 Turbo LoRA")

        sampler = str(self.settings.h3_sampler).strip()
        scheduler = str(self.settings.h3_scheduler).strip()
        if not sampler:
            raise RuntimeError("H3 sampler 配置为空")
        if not scheduler:
            raise RuntimeError("H3 scheduler 配置为空")
        return sampler, scheduler

    @staticmethod
    def _validate_dimensions(width: int, height: int, length: int, steps: int) -> None:
        if width < 256 or width > 1344 or width % 32 != 0:
            raise ValueError("视频宽度必须为 256～1344 且是 32 的整数倍")
        if height < 256 or height > 1344 or height % 32 != 0:
            raise ValueError("视频高度必须为 256～1344 且是 32 的整数倍")
        if length < 5 or length > 3600 or (length - 5) % 17 != 0:
            raise ValueError("H3 帧数必须满足 5 + 17×N，范围 5～3600")
        if steps < 1 or steps > 100:
            raise ValueError("生成步数必须为 1～100")

    def _copy_input(self, source: Path, token: str, role: str) -> str:
        if not source.is_file() or source.stat().st_size == 0:
            raise FileNotFoundError(f"{role}不存在或为空：{source}")
        self.input_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() or ".png"
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            suffix = ".png"
        name = f"aistudio_h3_{token}_{role}{suffix}"
        target = self.input_dir / name
        shutil.copy2(source, target)
        return name

    def _build_image_workflow(
        self,
        *,
        prompt: str,
        first_name: str | None,
        last_name: str | None,
        width: int,
        height: int,
        length: int,
        steps: int,
        seed: int,
        sampler: str,
        scheduler: str,
        filename_prefix: str,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        i2v_inputs: dict[str, Any] = {
            "clip": ["3", 0],
            "vae": ["4", 0],
            "prompt": prompt,
            "width": width,
            "height": height,
            "length": length,
        }
        if first_name:
            i2v_inputs["first_frame"] = ["5", 0]
        if last_name:
            i2v_inputs["last_frame"] = ["6", 0]

        model_ref: list[Any] = ["1", 0]
        workflow: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": self.settings.h3_fl2va_model, "weight_dtype": "default"}},
            "2": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": model_ref, "shift_video": 12.0, "shift_audio": 3.0}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.settings.h3_text_encoder, "type": "minimax", "device": "default"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": self.settings.h3_video_vae}},
            "7": {"class_type": "MiniMaxH3ImageToVideo", "inputs": i2v_inputs},
            "8": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["7", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler}},
            "11": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": scheduler, "steps": steps, "denoise": 1.0}},
            "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0], "sigmas": ["11", 0], "latent_image": ["7", 1]}},
            "13": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["12", 0]}},
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
            "15": {"class_type": "VAELoader", "inputs": {"vae_name": self.settings.h3_audio_vae}},
            "16": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 1], "vae": ["15", 0]}},
            "17": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "audio": ["16", 0], "fps": 24.0, "bit_depth": 8}},
            "18": {"class_type": "SaveVideo", "inputs": {"video": ["17", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}},
        }
        if first_name:
            workflow["5"] = {"class_type": "LoadImage", "inputs": {"image": first_name}}
        if last_name:
            workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": last_name}}
        if str(video_profile).strip().lower() == "turbo":
            workflow["19"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": ["1", 0], "lora_name": self.TURBO_LORA, "strength_model": 1.0},
            }
            workflow["2"]["inputs"]["model"] = ["19", 0]
        return workflow

    def _build_ref_workflow(
        self,
        *,
        prompt: str,
        reference_name: str,
        ref_image_size: str,
        width: int,
        height: int,
        length: int,
        steps: int,
        seed: int,
        sampler: str,
        scheduler: str,
        filename_prefix: str,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        workflow = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": self.settings.h3_ref2va_model, "weight_dtype": "default"}},
            "2": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["1", 0], "shift_video": 12.0, "shift_audio": 3.0}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.settings.h3_text_encoder, "type": "minimax", "device": "default"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": self.settings.h3_video_vae}},
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": self.settings.h3_audio_vae}},
            "6": {"class_type": "LoadImage", "inputs": {"image": reference_name}},
            "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "clip": ["3", 0], "vae": ["4", 0], "audio_vae": ["5", 0],
                "prompt": prompt, "width": width, "height": height, "length": length,
                "ref_image_size": ref_image_size,
                "ref_images": {"ref_image_0": ["6", 0]},
            }},
            "8": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["7", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": sampler}},
            "11": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": scheduler, "steps": steps, "denoise": 1.0}},
            "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0], "sigmas": ["11", 0], "latent_image": ["7", 1]}},
            "13": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["12", 0]}},
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
            "16": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 1], "vae": ["5", 0]}},
            "17": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "audio": ["16", 0], "fps": 24.0, "bit_depth": 8}},
            "18": {"class_type": "SaveVideo", "inputs": {"video": ["17", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}},
        }
        if str(video_profile).strip().lower() == "turbo":
            workflow["19"] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": ["1", 0], "lora_name": self.TURBO_LORA, "strength_model": 1.0},
            }
            workflow["2"]["inputs"]["model"] = ["19", 0]
        return workflow

    async def _probe_media(self, path: Path) -> dict[str, Any]:
        if not shutil.which("ffprobe"):
            return {"ffprobe": False}
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,sample_rate,channels",
            "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={k: v for k, v in __import__('os').environ.items() if k != 'OMP_NUM_THREADS'},
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return {"ffprobe": False, "error": stderr.decode("utf-8", errors="replace")[-1200:]}
        payload = json.loads(stdout.decode("utf-8"))
        streams = payload.get("streams", [])
        video = next((x for x in streams if x.get("codec_type") == "video"), {})
        audio = next((x for x in streams if x.get("codec_type") == "audio"), {})
        fmt = payload.get("format", {})
        return {
            "ffprobe": True,
            "duration": float(fmt["duration"]) if fmt.get("duration") else None,
            "size": int(fmt["size"]) if fmt.get("size") else path.stat().st_size,
            "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
            "video": {"codec": video.get("codec_name"), "width": video.get("width"), "height": video.get("height"), "fps": video.get("avg_frame_rate") or video.get("r_frame_rate")},
            "audio": {"present": bool(audio), "codec": audio.get("codec_name"), "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None, "channels": audio.get("channels")},
        }

    async def _submit_and_collect(
        self,
        *,
        mode: str,
        workflow: dict[str, Any],
        basename: str,
        output_dir: Path,
        width: int,
        height: int,
        length: int,
        steps: int,
        seed: int,
        sampler: str,
        scheduler: str,
        progress: ProgressCallback,
        log: LogCallback,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        await progress(30, f"正在提交 H3 {mode.upper()} 到 ComfyUI")
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.post(f"{self.base_url}/prompt", json={"prompt": workflow, "client_id": str(uuid.uuid4())})
            if response.status_code >= 400:
                raise RuntimeError(f"ComfyUI 拒绝 H3 工作流：{response.text[-4000:]}")
            submitted = response.json()

        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            raise RuntimeError("ComfyUI 未返回 H3 prompt_id")
        await log(f"ComfyUI prompt_id={prompt_id}")
        await progress(40, "H3 正在生成视频与音频")

        deadline = time.monotonic() + self.settings.h3_task_timeout_seconds
        last_log = 0.0
        history_entry: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.get(f"{self.base_url}/history/{prompt_id}")
                response.raise_for_status()
                payload = response.json()
            entry = payload.get(prompt_id) if isinstance(payload, dict) else None
            if isinstance(entry, dict):
                status = entry.get("status", {})
                status_str = status.get("status_str")
                if status_str == "error":
                    raise RuntimeError("H3 生成失败：" + json.dumps(status.get("messages", []), ensure_ascii=False)[-6000:])
                if status.get("completed"):
                    if status_str != "success":
                        raise RuntimeError("H3 生成失败：" + json.dumps(status.get("messages", []), ensure_ascii=False)[-6000:])
                    history_entry = entry
                    break
            now = time.monotonic()
            if now - last_log >= 15:
                await log(f"H3 正在推理：已等待 {int(now - started)} 秒")
                last_log = now
            await asyncio.sleep(2)

        if history_entry is None:
            raise TimeoutError(f"等待 H3 生成超过 {self.settings.h3_task_timeout_seconds} 秒")

        await progress(90, "H3 推理完成，正在收集成片")
        out_dir = self.output_dir / "h3"
        candidates = []
        if out_dir.is_dir():
            candidates = sorted(
                (p for p in out_dir.glob(basename + "*") if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv"}),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        if not candidates:
            raise RuntimeError("H3 工作流执行成功，但未在 ComfyUI/output/h3 找到生成视频")

        source_output = candidates[0]
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / f"h3_{mode}_{width}x{height}_{length}f{source_output.suffix.lower()}"
        shutil.copy2(source_output, target)
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError("H3 成片复制失败或输出为空")

        metadata = await self._probe_media(target)
        metadata.update({
            "mode": mode,
            "requested_width": width,
            "requested_height": height,
            "length": length,
            "fps": 24,
            "steps": steps,
            "seed": seed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "prompt_id": prompt_id,
            "sampler": sampler,
            "scheduler": scheduler,
            "video_profile": video_profile,
            "turbo_lora": self.TURBO_LORA if video_profile == "turbo" else "",
        })
        await log("H3 成片媒体信息：" + json.dumps(metadata, ensure_ascii=False))
        return {"path": target, "metadata": metadata}

    async def generate_t2va(
        self, *, prompt: str, width: int, height: int, length: int, steps: int, seed: int,
        output_dir: Path, progress: ProgressCallback, log: LogCallback,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("视频提示词不能为空")
        profile = str(video_profile or "standard").strip().lower()
        if profile == "turbo":
            steps = self.TURBO_STEPS
        self._validate_dimensions(width, height, length, steps)
        sampler, scheduler = await self._validate_runtime(self.settings.h3_fl2va_model, profile)
        if seed < 0:
            seed = secrets.randbits(63)
        token = uuid.uuid4().hex[:16]
        basename = f"AIStudioH3T2V_{token}"
        await progress(20, "正在准备 H3 T2VA 工作流")
        await log(f"H3 T2VA：{width}×{height}，length={length}，24 FPS，steps={steps}，seed={seed}，profile={profile}")
        workflow = self._build_image_workflow(
            prompt=prompt, first_name=None, last_name=None, width=width, height=height, length=length,
            steps=steps, seed=seed, sampler=sampler, scheduler=scheduler, filename_prefix=f"h3/{basename}",
            video_profile=profile,
        )
        return await self._submit_and_collect(
            mode="t2va", workflow=workflow, basename=basename, output_dir=output_dir,
            width=width, height=height, length=length, steps=steps, seed=seed,
            sampler=sampler, scheduler=scheduler, progress=progress, log=log, video_profile=profile,
        )

    async def generate_fl2va(
        self, *, prompt: str, first_frame: Path, last_frame: Path | None,
        width: int, height: int, length: int, steps: int, seed: int,
        output_dir: Path, progress: ProgressCallback, log: LogCallback,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("视频提示词不能为空")
        profile = str(video_profile or "standard").strip().lower()
        if profile == "turbo":
            steps = self.TURBO_STEPS
        self._validate_dimensions(width, height, length, steps)
        sampler, scheduler = await self._validate_runtime(self.settings.h3_fl2va_model, profile)
        if seed < 0:
            seed = secrets.randbits(63)
        token = uuid.uuid4().hex[:16]
        copied: list[Path] = []
        await progress(20, "正在准备 H3 FL2VA 工作流")
        await log(f"H3 FL2VA：{width}×{height}，length={length}，24 FPS，steps={steps}，seed={seed}，profile={profile}")
        try:
            first_name = self._copy_input(first_frame, token, "first")
            copied.append(self.input_dir / first_name)
            last_name = None
            if last_frame is not None:
                last_name = self._copy_input(last_frame, token, "last")
                copied.append(self.input_dir / last_name)
            basename = f"AIStudioH3FL2VA_{token}"
            workflow = self._build_image_workflow(
                prompt=prompt, first_name=first_name, last_name=last_name, width=width, height=height,
                length=length, steps=steps, seed=seed, sampler=sampler, scheduler=scheduler,
                filename_prefix=f"h3/{basename}", video_profile=profile,
            )
            return await self._submit_and_collect(
                mode="fl2va", workflow=workflow, basename=basename, output_dir=output_dir,
                width=width, height=height, length=length, steps=steps, seed=seed,
                sampler=sampler, scheduler=scheduler, progress=progress, log=log, video_profile=profile,
            )
        finally:
            for path in copied:
                path.unlink(missing_ok=True)

    async def generate_ref2va(
        self, *, prompt: str, reference_image: Path, ref_image_size: str,
        width: int, height: int, length: int, steps: int, seed: int,
        output_dir: Path, progress: ProgressCallback, log: LogCallback,
        video_profile: str = "standard",
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("视频提示词不能为空")
        if ref_image_size not in {"match", "max"}:
            raise ValueError("ref_image_size 只能是 match 或 max")
        profile = str(video_profile or "standard").strip().lower()
        if profile == "turbo":
            steps = self.TURBO_STEPS
        self._validate_dimensions(width, height, length, steps)
        sampler, scheduler = await self._validate_runtime(self.settings.h3_ref2va_model, profile)
        if seed < 0:
            seed = secrets.randbits(63)
        token = uuid.uuid4().hex[:16]
        copied: list[Path] = []
        await progress(20, "正在准备 H3 REF2VA 工作流")
        await log(f"H3 REF2VA：{width}×{height}，length={length}，24 FPS，steps={steps}，seed={seed}，ref_image_size={ref_image_size}，profile={profile}")
        try:
            reference_name = self._copy_input(reference_image, token, "reference")
            copied.append(self.input_dir / reference_name)
            basename = f"AIStudioH3REF2VA_{token}"
            workflow = self._build_ref_workflow(
                prompt=prompt, reference_name=reference_name, ref_image_size=ref_image_size,
                width=width, height=height, length=length, steps=steps, seed=seed,
                sampler=sampler, scheduler=scheduler, filename_prefix=f"h3/{basename}", video_profile=profile,
            )
            return await self._submit_and_collect(
                mode="ref2va", workflow=workflow, basename=basename, output_dir=output_dir,
                width=width, height=height, length=length, steps=steps, seed=seed,
                sampler=sampler, scheduler=scheduler, progress=progress, log=log, video_profile=profile,
            )
        finally:
            for path in copied:
                path.unlink(missing_ok=True)

