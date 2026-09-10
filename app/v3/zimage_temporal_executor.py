from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.services.comfyui import (
    ZIMAGE_TURBO_CLIP,
    ZIMAGE_TURBO_UNET,
    ZIMAGE_TURBO_VAE,
)
from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.contracts import ProviderModelSpec


class ZImageWorkflowError(ValueError):
    """Deterministic Z-Image contract/configuration failure.

    This subclasses ValueError so the Temporal domain boundary classifies an
    invalid workflow/prompt/model contract as a semantic failure instead of
    retrying it as transient infrastructure noise.
    """


def _required_node(workflow: dict[str, Any], node_id: str, class_type: str) -> dict[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, dict) or node.get("class_type") != class_type:
        raise ZImageWorkflowError(f"Z-Image workflow node {node_id} must be {class_type}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise ZImageWorkflowError(f"Z-Image workflow node {node_id} inputs are missing")
    return node


def compile_zimage_workflow(
    workflow: dict[str, Any],
    *,
    positive_prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    seed: int,
    steps: int = 9,
    cfg: float = 1.0,
    sampler_name: str = "euler",
    scheduler: str = "simple",
    filename_prefix: str = "Xiaoduan/ZImageTurbo",
) -> dict[str, Any]:
    """Freeze one provider-ready Z-Image request into the real Comfy graph."""
    positive = str(positive_prompt or "").strip()
    if not positive:
        raise ZImageWorkflowError("Z-Image positive prompt is required")
    if width < 256 or height < 256:
        raise ZImageWorkflowError("Z-Image dimensions are too small")

    compiled = deepcopy(workflow)
    sampler = _required_node(compiled, "3", "KSampler")
    pos = _required_node(compiled, "6", "CLIPTextEncode")
    neg = _required_node(compiled, "7", "CLIPTextEncode")
    latent = _required_node(compiled, "13", "EmptySD3LatentImage")
    unet = _required_node(compiled, "16", "UNETLoader")
    vae = _required_node(compiled, "17", "VAELoader")
    clip = _required_node(compiled, "18", "CLIPLoader")
    save = _required_node(compiled, "9", "SaveImage")

    unet["inputs"]["unet_name"] = ZIMAGE_TURBO_UNET
    vae["inputs"]["vae_name"] = ZIMAGE_TURBO_VAE
    clip["inputs"]["clip_name"] = ZIMAGE_TURBO_CLIP
    pos["inputs"]["text"] = positive
    neg["inputs"]["text"] = str(negative_prompt or "").strip()
    latent["inputs"].update({"width": int(width), "height": int(height), "batch_size": 1})
    sampler["inputs"].update({
        "seed": int(seed),
        "steps": int(steps),
        "cfg": float(cfg),
        "sampler_name": str(sampler_name),
        "scheduler": str(scheduler),
        "denoise": 1.0,
    })
    save["inputs"]["filename_prefix"] = str(filename_prefix or "Xiaoduan/ZImageTurbo")

    if int(steps) != 9 or abs(float(cfg) - 1.0) > 1e-9:
        raise ZImageWorkflowError("Z-Image-Turbo requires steps=9 and cfg=1.0")
    if str(sampler_name) != "euler" or str(scheduler) != "simple":
        raise ZImageWorkflowError("Z-Image-Turbo requires sampler=euler scheduler=simple")
    return compiled


class ZImageTemporalExecutor:
    """Provider executor for reference-free Z-Image generation."""

    def __init__(self, spec: ProviderModelSpec, adapter: ComfyUIAdapter | None = None) -> None:
        self.spec = spec
        self.adapter = adapter or ComfyUIAdapter(spec)

    def _workflow(self) -> dict[str, Any]:
        path = Path(str(self.spec.metadata.get("workflow_path") or ""))
        if not path.is_file():
            raise ZImageWorkflowError(f"Z-Image workflow unavailable: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not raw:
            raise ZImageWorkflowError("Z-Image workflow must be a non-empty object")
        return raw

    async def queue(
        self,
        *,
        positive_prompt: str,
        negative_prompt: str,
        width: int,
        height: int,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        workflow = compile_zimage_workflow(
            self._workflow(),
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            width=width,
            height=height,
            seed=seed,
            steps=9,
            cfg=1.0,
            sampler_name="euler",
            scheduler="simple",
            filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(workflow)


__all__ = [
    "ZImageTemporalExecutor",
    "ZImageWorkflowError",
    "compile_zimage_workflow",
]
