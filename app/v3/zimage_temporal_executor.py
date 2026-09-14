from __future__ import annotations

import json
import re
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


TURNAROUND_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "z_image_turbo_turnaround_api.json"


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


def compile_zimage_turnaround_workflow(
    workflow: dict[str, Any],
    *,
    adopted_costume_name: str,
    positive_prompt: str,
    negative_prompt: str,
    seed: int,
    filename_prefix: str,
) -> dict[str, Any]:
    """Compile front/side/back views from one adopted costume image.

    The front panel is the adopted image byte-for-byte inside the graph. Side
    and back branches start from that same image latent. Their calibrated
    denoise values are intentionally different: a true 90-degree profile needs
    more geometric change than a rear view while the latter keeps more garment
    construction from the adopted front.
    """
    costume_name = str(adopted_costume_name or "").strip()
    positive = str(positive_prompt or "").strip()
    if not costume_name or not positive:
        raise ZImageWorkflowError("turnaround requires an adopted costume image and prompt")
    compiled = deepcopy(workflow)
    load = _required_node(compiled, "1", "LoadImage")
    unet = _required_node(compiled, "3", "UNETLoader")
    vae = _required_node(compiled, "4", "VAELoader")
    clip = _required_node(compiled, "5", "CLIPLoader")
    side_prompt = _required_node(compiled, "6", "CLIPTextEncode")
    back_prompt = _required_node(compiled, "7", "CLIPTextEncode")
    negative_node = _required_node(compiled, "8", "CLIPTextEncode")
    side_sampler = _required_node(compiled, "9", "KSampler")
    back_sampler = _required_node(compiled, "10", "KSampler")
    _required_node(compiled, "13", "ImageStitch")
    _required_node(compiled, "14", "ImageStitch")
    save = _required_node(compiled, "15", "SaveImage")

    load["inputs"]["image"] = costume_name
    unet["inputs"]["unet_name"] = ZIMAGE_TURBO_UNET
    vae["inputs"]["vae_name"] = ZIMAGE_TURBO_VAE
    clip["inputs"]["clip_name"] = ZIMAGE_TURBO_CLIP
    # The project-level reference prompt describes a complete model sheet. Feeding
    # that text into each img2img branch makes Z-Image draw another collage (often
    # with labels) inside the side/back panel. Keep only typed identity facts here;
    # the adopted costume pixels are the visual source of truth for the outfit.
    identity_facts: list[str] = []
    for pattern in (
        r"STRICT CHARACTER IDENTITY:\s*([^,\n]+)",
        r"STRICT VISUAL AGE:\s*([^,\n]+)",
        r"STRICT HAIRSTYLE ANCHOR:\s*([^,\n]+)",
        r"stable_profile\.阶段正式设定[:：]\s*([^;\n]+)",
        r"stable_profile\.专业角色合同\.(?:服装|鞋履|固定身份锚点)[:：]\s*([^;\n]+)",
    ):
        for match in re.finditer(pattern, positive, flags=re.IGNORECASE):
            value = " ".join(match.group(1).split()).strip(" ,")
            if value and value not in identity_facts:
                identity_facts.append(value)
    identity = (
        "Use the supplied adopted costume image as the sole visual identity and outfit source; "
        "preserve the exact same person, apparent age, gender, face, hairstyle, body proportions, "
        "garment layers, fabric, colors, belt, footwear and fixed accessories"
    )
    if identity_facts:
        identity += "; confirmed identity facts: " + "; ".join(identity_facts)
    side_prompt["inputs"]["text"] = (
        identity
        + ", render only one strict left-facing 90-degree side profile, head and entire body rotated left, "
          "one eye visible, complete body and both feet visible, plain seamless background"
    )
    back_prompt["inputs"]["text"] = (
        identity
        + ", render only one strict 180-degree rear view, face completely invisible, back of head, hair and "
          "garment construction visible, complete body and both feet visible, plain seamless background"
    )
    negative_node["inputs"]["text"] = (
        "different person, identity drift, age drift, gender drift, face redesign, hairstyle change, "
        "costume redesign, changed outfit, changed colors, modern clothing, extra person, duplicate person, "
        "multiple views in one panel, model sheet, collage, grid, text, typography, labels, annotations, "
        "logo, watermark, cropped head, cropped feet"
    )
    side_sampler["inputs"].update({"seed": int(seed), "steps": 9, "cfg": 1.0, "denoise": 0.95})
    back_sampler["inputs"].update({"seed": int(seed) + 1, "steps": 9, "cfg": 1.0, "denoise": 0.85})
    save["inputs"]["filename_prefix"] = str(filename_prefix or "Xiaoduan/ZImageTurbo/Turnaround")
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

    async def queue_turnaround(
        self,
        *,
        adopted_costume_path: Path,
        positive_prompt: str,
        negative_prompt: str,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        path = Path(adopted_costume_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ZImageWorkflowError("adopted costume image is unavailable")
        uploaded = await self.adapter.upload_reference(
            filename=f"turnaround-{path.name}",
            content=path.read_bytes(),
            overwrite=True,
        )
        name = str(uploaded["name"])
        subfolder = str(uploaded.get("subfolder") or "").strip("/\\")
        uploaded_name = f"{subfolder}/{name}" if subfolder else name
        workflow = json.loads(TURNAROUND_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_zimage_turnaround_workflow(
            workflow,
            adopted_costume_name=uploaded_name,
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(compiled)


__all__ = [
    "ZImageTemporalExecutor",
    "ZImageWorkflowError",
    "compile_zimage_workflow",
    "compile_zimage_turnaround_workflow",
]
