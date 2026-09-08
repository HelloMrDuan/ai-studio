from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.contracts import Capability
from app.v3.generation_executor import GenerationExecutionReceipt, ReferenceAssetStore


@dataclass(frozen=True)
class H3WorkflowConfig:
    fl2va_model: str
    ref2va_model: str
    text_encoder: str
    video_vae: str
    audio_vae: str
    sampler: str = "euler"
    scheduler: str = "simple"
    fps: float = 24.0


class H3ReferenceError(RuntimeError):
    pass


class H3WorkflowCompiler:
    """V3-native MiniMax H3 workflow compiler.

    The node graph is extracted from the previously proven H3 service, but V3
    receives explicit adopted frame/reference IDs through GenerationContract.
    No Stage04 or legacy director dependency is involved.
    """

    def __init__(self, config: H3WorkflowConfig) -> None:
        self.config = config

    @staticmethod
    def _validate(width: int, height: int, length: int, steps: int) -> None:
        if width < 256 or width > 1344 or width % 32:
            raise ValueError("H3 width must be 256..1344 and divisible by 32")
        if height < 256 or height > 1344 or height % 32:
            raise ValueError("H3 height must be 256..1344 and divisible by 32")
        if length < 5 or length > 3600 or (length - 5) % 17:
            raise ValueError("H3 length must satisfy 5 + 17*N")
        if steps < 1 or steps > 100:
            raise ValueError("H3 steps must be 1..100")

    def first_last_frame(
        self,
        *,
        prompt: str,
        first_name: str,
        last_name: str | None = None,
        width: int = 768,
        height: int = 448,
        length: int = 124,
        steps: int = 20,
        seed: int = 0,
        filename_prefix: str = "xiaoduan_v3_h3",
    ) -> dict[str, Any]:
        self._validate(width, height, length, steps)
        if not str(first_name or "").strip():
            raise H3ReferenceError("H3 first-frame generation requires first_name")
        i2v_inputs: dict[str, Any] = {
            "clip": ["3", 0], "vae": ["4", 0], "prompt": prompt,
            "width": width, "height": height, "length": length,
            "first_frame": ["5", 0],
        }
        workflow: dict[str, Any] = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": self.config.fl2va_model, "weight_dtype": "default"}},
            "2": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["1", 0], "shift_video": 12.0, "shift_audio": 3.0}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.config.text_encoder, "type": "minimax", "device": "default"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": self.config.video_vae}},
            "5": {"class_type": "LoadImage", "inputs": {"image": first_name}},
            "7": {"class_type": "MiniMaxH3ImageToVideo", "inputs": i2v_inputs},
            "8": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["7", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": self.config.sampler}},
            "11": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": self.config.scheduler, "steps": steps, "denoise": 1.0}},
            "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0], "sigmas": ["11", 0], "latent_image": ["7", 1]}},
            "13": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["12", 0]}},
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
            "15": {"class_type": "VAELoader", "inputs": {"vae_name": self.config.audio_vae}},
            "16": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 1], "vae": ["15", 0]}},
            "17": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "audio": ["16", 0], "fps": self.config.fps, "bit_depth": 8}},
            "18": {"class_type": "SaveVideo", "inputs": {"video": ["17", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}},
        }
        if last_name:
            workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": last_name}}
            i2v_inputs["last_frame"] = ["6", 0]
        return workflow

    def reference_to_video(
        self,
        *,
        prompt: str,
        reference_name: str,
        ref_image_size: str = "large",
        width: int = 768,
        height: int = 448,
        length: int = 124,
        steps: int = 20,
        seed: int = 0,
        filename_prefix: str = "xiaoduan_v3_h3_ref",
    ) -> dict[str, Any]:
        self._validate(width, height, length, steps)
        if not str(reference_name or "").strip():
            raise H3ReferenceError("H3 reference mode requires one adopted reference")
        return {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": self.config.ref2va_model, "weight_dtype": "default"}},
            "2": {"class_type": "MiniMaxH3SigmaShift", "inputs": {"model": ["1", 0], "shift_video": 12.0, "shift_audio": 3.0}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": self.config.text_encoder, "type": "minimax", "device": "default"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": self.config.video_vae}},
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": self.config.audio_vae}},
            "6": {"class_type": "LoadImage", "inputs": {"image": reference_name}},
            "7": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "clip": ["3", 0], "vae": ["4", 0], "audio_vae": ["5", 0],
                "prompt": prompt, "width": width, "height": height, "length": length,
                "ref_image_size": ref_image_size, "ref_images": {"ref_image_0": ["6", 0]},
            }},
            "8": {"class_type": "BasicGuider", "inputs": {"model": ["2", 0], "conditioning": ["7", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
            "10": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": self.config.sampler}},
            "11": {"class_type": "BasicScheduler", "inputs": {"model": ["2", 0], "scheduler": self.config.scheduler, "steps": steps, "denoise": 1.0}},
            "12": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["10", 0], "sigmas": ["11", 0], "latent_image": ["7", 1]}},
            "13": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["12", 0]}},
            "14": {"class_type": "VAEDecode", "inputs": {"samples": ["13", 0], "vae": ["4", 0]}},
            "16": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["13", 1], "vae": ["5", 0]}},
            "17": {"class_type": "CreateVideo", "inputs": {"images": ["14", 0], "audio": ["16", 0], "fps": self.config.fps, "bit_depth": 8}},
            "18": {"class_type": "SaveVideo", "inputs": {"video": ["17", 0], "filename_prefix": filename_prefix, "format": "auto", "codec": "auto"}},
        }


class H3ReferenceFirstExecutor:
    def __init__(self, *, adapter: ComfyUIAdapter, references: ReferenceAssetStore, compiler: H3WorkflowCompiler) -> None:
        if Capability.video_generation not in adapter.spec.capabilities:
            raise ValueError("H3 adapter must expose video_generation")
        self.adapter = adapter
        self.references = references
        self.compiler = compiler

    async def execute_first_frame(
        self,
        contract,
        *,
        prompt: str,
        width: int = 768,
        height: int = 448,
        length: int = 124,
        steps: int = 20,
        seed: int = 0,
    ) -> GenerationExecutionReceipt:
        refs = self.references.resolve_many(contract.provider_reference_ids)
        if len(refs) not in {1, 2}:
            raise H3ReferenceError("H3 first/last-frame mode requires one or two provider frame references")
        uploaded: list[str] = []
        for asset in refs:
            response = await self.adapter.upload_reference(filename=asset.path.name, content=asset.path.read_bytes())
            folder = str(response.get("subfolder") or "xiaoduan-v3").strip("/")
            name = str(response.get("name") or "").strip()
            uploaded.append(f"{folder}/{name}" if folder else name)
        workflow = self.compiler.first_last_frame(
            prompt=prompt, first_name=uploaded[0], last_name=(uploaded[1] if len(uploaded) == 2 else None),
            width=width, height=height, length=length, steps=steps, seed=seed,
        )
        queued = await self.adapter.queue_workflow(workflow)
        return GenerationExecutionReceipt(
            prompt_id=str(queued["prompt_id"]), provider_id=contract.provider_id, model_id=contract.model_id,
            provider_reference_ids=tuple(contract.provider_reference_ids), uploaded_reference_names=tuple(uploaded),
        )
