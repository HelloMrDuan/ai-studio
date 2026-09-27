from __future__ import annotations

import json
import re
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageDraw

from app.services.comfyui import (
    ZIMAGE_TURBO_CLIP,
    ZIMAGE_TURBO_UNET,
    ZIMAGE_TURBO_VAE,
)
from app.v3.adapters.comfyui import ComfyUIAdapter
from app.v3.contracts import ProviderModelSpec


TURNAROUND_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "z_image_turbo_turnaround_api.json"
QWEN_EDIT_TURNAROUND_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "qwen_image_edit_turnaround_api.json"
CONTROLLED_LAYOUT_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "z_image_turbo_controlled_layout_api.json"
CONTROLLED_MASTER_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / "workflows" / "z_image_turbo_controlled_master_api.json"


def _appearance_facts(positive: str) -> list[str]:
    facts: list[str] = []
    appearance_tokens = (
        "岁", "年龄", "少年", "少女", "age", "year-old", "gender", "性别",
        "face", "facial", "脸", "五官", "妆", "makeup", "hair", "发型", "发色", "发饰", "发髻", "髻", "辫", "马尾", "簪",
        "costume", "clothing", "outfit", "garment", "robe", "hanfu", "collar", "cuff", "sleeve",
        "sash", "belt", "skirt", "shoe", "boot", "cyan", "blue", "green", "white", "black", "red",
        "服装", "衣", "袍", "裙", "领", "袖", "腰带", "鞋", "靴",
        "fabric", "material", "color", "颜色", "材质", "配色", "配饰",
    )
    layout_tokens = (
        "strict front", "front full-body", "front view", "正面全身", "正视镜头", "both eyes", "双眼",
        "shoulders square", "双肩正对", "side view", "侧面", "back view", "背面", "turnaround", "三视图",
        "model sheet", "panel", "分栏", "画布", "背景", "background",
    )
    for clause in re.split(r"[;,；\n]+", str(positive or "")):
        value = " ".join(clause.split()).strip(" ,。")
        # Reusable props have their own canonical reference assets.  A held or
        # carried prop makes the character sheet invent different hand poses and
        # accessories in each view, so it must not enter character appearance.
        value = re.sub(
            r"(?:手持|手握|握着|拿着|背负|携带)[^，,。.;；]*[。.]?",
            "",
            value,
        ).strip(" ,，。")
        value = re.sub(
            r"\b(?:holding|carrying|wielding)\b[^,.;]*[,.;]?",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip(" ,。")
        lowered = value.lower()
        if (
            lowered.startswith("keep the confirmed")
            or lowered.startswith("compose this as")
            or lowered.startswith("without any")
            or lowered.startswith("one person")
            or "logo or watermark" in lowered
            or lowered in {
                "age", "gender", "face", "facial", "makeup", "hairstyle",
                "hair accessories", "body proportions", "clothing layers",
                "collar geometry", "color palette", "footwear", "fixed accessories",
            }
        ):
            continue
        if not value or any(marker in value for marker in ("未明确", "待角色设计", "未指定", "待确认")):
            continue
        if any(token in lowered for token in layout_tokens):
            continue
        if (lowered.startswith("strict character identity") or any(token in lowered for token in appearance_tokens)) and value not in facts:
            facts.append(value)
        if len(facts) >= 24:
            break
    return facts


def compile_character_front_prompt(positive_prompt: str) -> str:
    """Reduce a project prompt to one clean identity source photograph.

    Terms such as identity master, turnaround and model sheet can make the
    reference-free first pass produce a collage even when they occur next to a
    one-person instruction.  Only typed appearance facts cross this boundary;
    the layout contract is written here and contains one subject and one view.
    """
    facts = _appearance_facts(positive_prompt)
    prompt = (
        "A clean full-length studio photograph of exactly one character standing alone, centered, "
        "facing the camera directly in a neutral symmetrical pose, both eyes visible, shoulders square, "
        "both arms relaxed and both hands visibly empty, head and both feet fully inside the frame. "
        "One person, one body, one face, one view, one plain "
        "light-gray seamless background. Show the face, hair, hair ornaments, collar, sleeves, waist, "
        "garment layers, hem and footwear clearly"
    )
    if facts:
        prompt += "; confirmed appearance: " + "; ".join(facts)
    prompt += (
        "; compose this as a single ordinary photograph with empty background around the one character, "
        "without any handheld object, weapon, reusable prop, added jewelry, arm band, inset portrait, "
        "duplicate, secondary figure, rear view, side view, panel, grid, collage, "
        "contact sheet, model sheet, turnaround sheet, poster, caption, label, typography, logo or watermark"
    )
    return prompt


def compile_zimage_controlled_master_workflow(
    workflow: dict[str, Any], *, front_source_name: str, face_source_name: str,
    pose_sheet_name: str, positive_prompt: str, negative_prompt: str,
    seed: int, filename_prefix: str,
) -> dict[str, Any]:
    """Compile one identity-LoRA sample containing fixed front/side/back poses.

    A pose ControlNet can enforce the three silhouettes, but it cannot preserve
    identity or garment construction.  The i2L hypernetwork turns the canonical
    front and its head crop into the LoRA used by the same full-sheet sample, so
    face, hair and costume conditioning remain active across all three panels.
    """
    compiled = deepcopy(workflow)
    front = _required_node(compiled, "1", "LoadImage")
    face = _required_node(compiled, "2", "LoadImage")
    loader = _required_node(compiled, "3", "ZImageI2LV2Loader")
    _required_node(compiled, "4", "ImageBatch")
    _required_node(compiled, "5", "ImageBatch")
    _required_node(compiled, "6", "ZImageI2LV2ExtractLoRA")
    pose = _required_node(compiled, "7", "LoadImage")
    sample = _required_node(compiled, "8", "ZImageI2LV2SampleControlNet")
    save = _required_node(compiled, "9", "SaveImage")

    front["inputs"]["image"] = str(front_source_name)
    face["inputs"]["image"] = str(face_source_name)
    pose["inputs"]["image"] = str(pose_sheet_name)
    loader["inputs"].update({
        "device": "cuda", "dtype": "bfloat16", "low_vram": True,
        "modelscope_cache": "", "load_controlnet": True,
        "base_model": "z-image-turbo",
    })

    facts = _appearance_facts(positive_prompt)
    prompt = (
        "One continuous studio character turnaround sheet with exactly three equal edge-to-edge panels: "
        "left is one strict front full-body view; center is one strict left-facing 90-degree side full-body view "
        "with one eye visible; right is one strict 180-degree rear full-body view with the face completely invisible. "
        "Use the supplied canonical front image and face crop as the exact identity and outfit source. The same person, "
        "visual age, gender, face, makeup, hairstyle, hair ornament, body, garment construction, collar, sleeves, "
        "sash, hem, footwear, fabric and colors must be identical in all three panels. No redesign"
    )
    if facts:
        prompt += "; confirmed appearance: " + "; ".join(facts)
    prompt += "; neutral standing pose, head and both feet visible in every panel, plain light-gray studio, no extra person, no fourth panel, no labels or text"
    prompt += (
        "; the front, profile and rear must share the exact same hairline, center part, bun height, bun shape, "
        "hair ornament position, loose-hair length, collar overlap, sleeve width, waist sash and skirt layers"
        "; both hands are empty in every view; do not add arm bands, bracelets, jewelry, weapons or handheld props"
    )
    sample["inputs"].update({
        "prompt": prompt,
        "negative_prompt": str(negative_prompt or "").strip(),
        "control_scale": 0.75,
        "seed": int(seed),
        "cfg_scale": 1.0,
        "num_inference_steps": 16,
        "sigma_shift": 0.0,
        "width": 2304,
        "height": 1024,
    })
    save["inputs"]["filename_prefix"] = str(filename_prefix or "Xiaoduan/ZImageTurbo/ControlledMaster")
    return compiled


def compile_zimage_controlled_layout_workflow(
    workflow: dict[str, Any], *, source_sheet_name: str, pose_sheet_name: str,
    positive_prompt: str, seed: int, filename_prefix: str,
) -> dict[str, Any]:
    """Compile the structural draft used by the identity-conditioned pass."""
    compiled = deepcopy(workflow)
    pose = _required_node(compiled, "1", "LoadImage")
    source = _required_node(compiled, "2", "LoadImage")
    unet = _required_node(compiled, "3", "UNETLoader")
    vae = _required_node(compiled, "4", "VAELoader")
    clip = _required_node(compiled, "5", "CLIPLoader")
    _required_node(compiled, "6", "ModelPatchLoader")
    control = _required_node(compiled, "7", "ZImageFunControlnet")
    text = _required_node(compiled, "9", "CLIPTextEncode")
    sampler = _required_node(compiled, "12", "KSampler")
    save = _required_node(compiled, "14", "SaveImage")
    pose["inputs"]["image"] = str(pose_sheet_name)
    source["inputs"]["image"] = str(source_sheet_name)
    unet["inputs"]["unet_name"] = ZIMAGE_TURBO_UNET
    vae["inputs"]["vae_name"] = ZIMAGE_TURBO_VAE
    clip["inputs"]["clip_name"] = ZIMAGE_TURBO_CLIP
    control["inputs"]["strength"] = 0.95
    facts = _appearance_facts(positive_prompt)
    prompt = (
        "One continuous studio character turnaround structure sheet with exactly three equal edge-to-edge panels: "
        "left strict front full-body, center strict left-facing 90-degree side full-body with one eye visible, "
        "right strict 180-degree rear full-body with the face completely invisible. Use the repeated canonical "
        "front as the subject source. Keep one person, one garment and one hairstyle across the sheet"
    )
    if facts:
        prompt += "; confirmed appearance: " + "; ".join(facts)
    prompt += (
        "; neutral standing pose with both hands empty, head and feet visible, plain light-gray studio, "
        "no handheld object, weapon, reusable prop, arm band or added jewelry, no text, no fourth panel"
    )
    text["inputs"]["text"] = prompt
    sampler["inputs"]["seed"] = int(seed)
    save["inputs"]["filename_prefix"] = str(filename_prefix or "Xiaoduan/ZImageTurbo/ControlledLayout")
    return compiled


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
    adopted_face_name: str,
    side_pose_name: str,
    back_pose_name: str,
    positive_prompt: str,
    negative_prompt: str,
    seed: int,
    filename_prefix: str,
) -> dict[str, Any]:
    """Compile front/side/back views from one adopted costume image.

    The front panel is the adopted image byte-for-byte inside the graph. Side
    and back branches combine a deterministic pose map with an appearance LoRA
    extracted from that adopted image. This gives pose and appearance separate
    conditioning channels instead of asking high-denoise img2img to redraw hair
    and garments from text.
    """
    costume_name = str(adopted_costume_name or "").strip()
    face_name = str(adopted_face_name or "").strip()
    side_pose = str(side_pose_name or "").strip()
    back_pose = str(back_pose_name or "").strip()
    positive = str(positive_prompt or "").strip()
    if not costume_name or not face_name or not side_pose or not back_pose or not positive:
        raise ZImageWorkflowError("turnaround requires adopted face/costume, side/back pose controls and prompt")
    compiled = deepcopy(workflow)
    load = _required_node(compiled, "1", "LoadImage")
    face_load = _required_node(compiled, "2", "LoadImage")
    side_load = _required_node(compiled, "3", "LoadImage")
    back_load = _required_node(compiled, "4", "LoadImage")
    loader = _required_node(compiled, "5", "ZImageI2LV2Loader")
    _required_node(compiled, "6", "ImageBatch")
    _required_node(compiled, "7", "ImageBatch")
    _required_node(compiled, "8", "ZImageI2LV2ExtractLoRA")
    side_sample = _required_node(compiled, "9", "ZImageI2LV2SampleControlNet")
    back_sample = _required_node(compiled, "10", "ZImageI2LV2SampleControlNet")
    _required_node(compiled, "11", "ImageStitch")
    _required_node(compiled, "12", "ImageStitch")
    save = _required_node(compiled, "13", "SaveImage")

    load["inputs"]["image"] = costume_name
    face_load["inputs"]["image"] = face_name
    side_load["inputs"]["image"] = side_pose
    back_load["inputs"]["image"] = back_pose
    loader["inputs"].update({
        "device": "cuda",
        "dtype": "bfloat16",
        "low_vram": True,
        "modelscope_cache": "",
        "load_controlnet": True,
        "base_model": "z-image-turbo",
    })
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
            if any(marker in value for marker in ("未明确", "待角色设计", "未指定", "待确认")):
                continue
            if value and value not in identity_facts:
                identity_facts.append(value)
    # PromptCompiler can flatten the stable profile differently depending on
    # whether an appearance asset already exists.  Preserve typed appearance
    # clauses regardless of that serialization, while rejecting view/layout
    # instructions that would fight the side/back ControlNet pose.
    appearance_tokens = (
        "岁", "年龄", "少年", "少女", "age", "year-old", "gender", "性别",
        "face", "facial", "脸", "五官", "妆", "makeup",
        "hair", "发型", "发色", "发饰", "簪",
        "costume", "clothing", "outfit", "garment", "robe",
        "服装", "衣", "袍", "裙", "领", "袖", "腰带", "鞋", "靴",
        "fabric", "material", "color", "颜色", "材质", "配色", "配饰",
    )
    layout_tokens = (
        "strict front", "front full-body", "front view", "正面全身", "正视镜头",
        "both eyes", "双眼", "shoulders square", "双肩正对",
        "side view", "侧面", "back view", "背面", "turnaround", "三视图",
        "model sheet", "panel", "分栏", "画布", "背景", "background",
    )
    for clause in re.split(r"[;,；\n]+", positive):
        value = " ".join(clause.split()).strip(" ,。")
        lowered = value.lower()
        if not value or any(marker in value for marker in ("未明确", "待角色设计", "未指定", "待确认")):
            continue
        if any(token in lowered for token in layout_tokens):
            continue
        if any(token in lowered for token in appearance_tokens) and value not in identity_facts:
            identity_facts.append(value)
        if len(identity_facts) >= 24:
            break
    identity = (
        "Use the supplied adopted costume image as the sole visual identity and outfit source; "
        "preserve the exact same person, apparent age, gender, face, hairstyle, body proportions, "
        "garment layers, fabric, colors, belt, footwear and fixed accessories"
    )
    if identity_facts:
        identity += "; confirmed identity facts: " + "; ".join(identity_facts)
    side_sample["inputs"]["prompt"] = (
        identity
        + ", render only one strict left-facing 90-degree side profile, head and entire body rotated left, "
          "one eye visible, complete body and both feet visible. Treat the canonical front pixels as a binding "
          "hair design: preserve the exact forehead exposure, hairline, temple hair, parting, tied-hair height, "
          "tie or ornament position and loose-hair length. Do not invent or remove bangs, fringe, braids, side locks, "
          "ponytails, buns or ornaments; keep every element exactly as present or absent in the canonical front. "
          "Use the exact collar overlap, sleeve width, sash, hem and footwear from the canonical front, plain seamless background"
    )
    back_sample["inputs"]["prompt"] = (
        identity
        + ", render only one strict 180-degree rear view, face completely invisible, back of head, hair and "
          "garment construction visible, complete body and both feet visible. Preserve the canonical front's exact "
          "tied-hair height, tie or ornament, bun or ponytail structure, loose-hair length, collar, shoulder width, "
          "sleeves, sash, hem and footwear. Do not invent or remove any braid, bun, ponytail, hair ornament or garment layer; "
          "keep every element exactly as present or absent in the canonical front, plain seamless background"
    )
    negative = (
        "different person, identity drift, age drift, gender drift, face redesign, hairstyle change, "
        "costume redesign, changed outfit, changed colors, modern clothing, extra person, duplicate person, "
        "multiple views in one panel, model sheet, collage, grid, text, typography, labels, annotations, "
        "logo, watermark, cropped head, cropped feet"
    )
    side_sample["inputs"].update({
        "control_scale": 0.75, "seed": int(seed), "cfg_scale": 1.0,
        "num_inference_steps": 12, "sigma_shift": 0.0,
        "width": 768, "height": 1024, "negative_prompt": negative,
    })
    back_sample["inputs"].update({
        "control_scale": 0.75, "seed": int(seed) + 1, "cfg_scale": 1.0,
        "num_inference_steps": 12, "sigma_shift": 0.0,
        "width": 768, "height": 1024, "negative_prompt": negative,
    })
    save["inputs"]["filename_prefix"] = str(filename_prefix or "Xiaoduan/ZImageTurbo/Turnaround")
    return compiled


def compile_qwen_edit_turnaround_workflow(
    workflow: dict[str, Any],
    *,
    adopted_costume_name: str,
    seed: int,
    filename_prefix: str,
) -> dict[str, Any]:
    """Compile two camera-angle edits from one canonical full-body image.

    Z-Image i2L predicts a style LoRA, so separate pose-controlled samples can
    still redesign bangs, hair ornaments and garment construction.  Qwen Image
    Edit receives the adopted front pixels as edit conditioning and the
    Multiple-Angles LoRA changes only the requested camera angle.  Side and
    back use the same source image, model instance and seed.
    """
    costume_name = str(adopted_costume_name or "").strip()
    if not costume_name:
        raise ZImageWorkflowError("Qwen turnaround requires the adopted costume image")
    compiled = deepcopy(workflow)
    source = _required_node(compiled, "1", "LoadImage")
    _required_node(compiled, "2", "UNETLoader")
    _required_node(compiled, "3", "LoraLoaderModelOnly")
    _required_node(compiled, "4", "ModelSamplingAuraFlow")
    _required_node(compiled, "5", "CFGNorm")
    _required_node(compiled, "6", "CLIPLoader")
    _required_node(compiled, "7", "VAELoader")
    _required_node(compiled, "8", "FluxKontextImageScale")
    _required_node(compiled, "9", "VAEEncode")
    negative = _required_node(compiled, "10", "TextEncodeQwenImageEditPlus")
    side_text = _required_node(compiled, "12", "TextEncodeQwenImageEditPlus")
    side_sample = _required_node(compiled, "14", "KSampler")
    back_text = _required_node(compiled, "15", "TextEncodeQwenImageEditPlus")
    back_sample = _required_node(compiled, "17", "KSampler")
    _required_node(compiled, "20", "ImageStitch")
    _required_node(compiled, "21", "ImageStitch")
    save = _required_node(compiled, "22", "SaveImage")

    source["inputs"]["image"] = costume_name
    # The upstream Multiple-Angles LoRA is trained on these exact camera tokens.
    # The rest of each instruction freezes every reusable identity attribute.
    preserve = (
        "Change the camera viewpoint only. Keep the exact same person, apparent age, gender, face, "
        "forehead exposure, hairline, parting, bangs or absence of bangs, side locks, tied-hair height, "
        "hair ornament, loose-hair length, body proportions, collar overlap, every garment layer, sleeve "
        "shape, sash, fabric, colors, hem and footwear from the input image. Do not add or remove any hair "
        "element, ornament, accessory or clothing part. One full-body person, head and both feet visible, "
        "neutral standing pose, same plain studio background."
    )
    negative["inputs"]["prompt"] = ""
    side_text["inputs"]["prompt"] = (
        "<sks> right side view eye-level shot wide shot\n" + preserve
        + " Show a strict 90-degree right profile with one eye visible."
    )
    back_text["inputs"]["prompt"] = (
        "<sks> back view eye-level shot wide shot\n" + preserve
        + " Show a strict 180-degree rear view with the face completely invisible."
    )
    for sampler in (side_sample, back_sample):
        sampler["inputs"].update({
            "seed": int(seed),
            "steps": 40,
            "cfg": 4.0,
            "sampler_name": "euler",
            "scheduler": "simple",
            "denoise": 1.0,
        })
    save["inputs"]["filename_prefix"] = str(
        filename_prefix or "Xiaoduan/QwenEdit/Turnaround"
    )
    return compiled


def compile_qwen_domain_reference_workflow(
    workflow: dict[str, Any], *, source_name: str, identity: str,
    kind: str, seed: int, filename_prefix: str,
) -> dict[str, Any]:
    """Refine a prop's components or clear occupants from a location plate.

    The source image supplies composition and appearance. The formal stable
    profile supplies the missing persistent structure; story action and other
    entities are deliberately absent from this edit contract.
    """
    if kind not in {"prop", "location"}:
        raise ZImageWorkflowError("domain reference edit requires prop or location")
    source_name = str(source_name or "").strip()
    identity = str(identity or "").strip()
    if not source_name or not identity:
        raise ZImageWorkflowError("domain reference edit requires source image and stable identity")
    keep = {"1", "2", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "18", "22"}
    compiled = {key: deepcopy(value) for key, value in workflow.items() if key in keep}
    source = _required_node(compiled, "1", "LoadImage")
    _required_node(compiled, "2", "UNETLoader")
    model = _required_node(compiled, "4", "ModelSamplingAuraFlow")
    _required_node(compiled, "5", "CFGNorm")
    _required_node(compiled, "6", "CLIPLoader")
    _required_node(compiled, "7", "VAELoader")
    _required_node(compiled, "8", "FluxKontextImageScale")
    _required_node(compiled, "9", "VAEEncode")
    negative = _required_node(compiled, "10", "TextEncodeQwenImageEditPlus")
    positive = _required_node(compiled, "12", "TextEncodeQwenImageEditPlus")
    sampler = _required_node(compiled, "14", "KSampler")
    decode = _required_node(compiled, "18", "VAEDecode")
    save = _required_node(compiled, "22", "SaveImage")
    source["inputs"]["image"] = source_name
    model["inputs"]["model"] = ["2", 0]
    negative["inputs"]["prompt"] = ""
    if kind == "prop":
        lower_identity = identity.lower()
        component_rules = []
        if any(token in lower_identity for token in ("剑鞘", "鞘", "scabbard", "sheath")):
            component_rules.append(
                "A specified scabbard is an opaque solid cover around the blade, "
                "joined to the hilt as one assembled item."
            )
            if any(token in lower_identity for token in ("纹路", "纹样", "刻纹", "engraving", "pattern")):
                component_rules.append(
                    "Put the specified visible engravings or patterns on the outside surface "
                    "of the scabbard itself, along its body rather than only on the hilt."
                )
            if any(token in lower_identity for token in ("暗银", "银色", "silver")):
                component_rules.append("The scabbard engravings have the specified dark-silver color.")
        if any(token in lower_identity for token in ("系绳", "绳", "cord", "strap", "lanyard")):
            component_rules.append(
                "A specified attachment cord or strap has its entire loop, knot "
                "and connection to the object visible within the canvas."
            )
        instruction = (
            "Edit this existing studio product photograph to match the confirmed stable design: "
            + identity
            + ". Show exactly one complete assembled object, with every attached component and connection "
              "visible inside the frame. Preserve its identity, centered placement and plain studio background. "
            + " ".join(component_rules)
            + " No other object, person, landscape, writing, logo or watermark."
        )
    else:
        instruction = (
            "Preserve this existing location photograph exactly: the same camera, complete environment layout, "
            "terrain, architecture, pathways, lighting, framing, materials and colors. "
            "Remove any human or animal figure, including tiny distant figures on paths, stairs or buildings. "
            "Fill only those pixels with matching empty environment. Keep the whole location entirely unoccupied. "
            "Do not redesign the location or add any new structure, city, prop, writing, logo or watermark."
        )
    positive["inputs"]["prompt"] = instruction
    sampler["inputs"].update({
        "seed": int(seed), "steps": 40, "cfg": 4.0,
        "sampler_name": "euler", "scheduler": "simple",
        "positive": ["13", 0], "negative": ["11", 0],
        "latent_image": ["9", 0], "denoise": 1.0,
    })
    decode["inputs"]["samples"] = ["14", 0]
    save["inputs"].update({
        "images": ["18", 0],
        "filename_prefix": str(filename_prefix or "Xiaoduan/QwenEdit/DomainReference"),
    })
    return compiled


def compile_qwen_shot_workflow(
    workflow: dict[str, Any], *, image_names: list[str],
    prompt: str, seed: int, filename_prefix: str,
) -> dict[str, Any]:
    """Bind every image in one Qwen edit pass to a real workflow node."""
    if not 1 <= len(image_names) <= 3:
        raise ZImageWorkflowError("Qwen shot edit accepts one to three images per pass")
    if not str(prompt or "").strip():
        raise ZImageWorkflowError("Qwen shot edit requires a formal shot prompt")
    compiled = compile_qwen_domain_reference_workflow(
        workflow,
        source_name=image_names[0],
        identity="formal shot location",
        kind="location",
        seed=seed,
        filename_prefix=filename_prefix,
    )
    for index, name in enumerate(image_names[1:], start=2):
        node_id = str(28 + index)
        compiled[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        for text_id in ("10", "12"):
            compiled[text_id]["inputs"][f"image{index}"] = [node_id, 0]
    compiled["12"]["inputs"]["prompt"] = str(prompt).strip()
    return compiled


def _turnaround_pose_png(view: str, *, width: int = 768, height: int = 1024) -> bytes:
    """Create a deterministic OpenPose-style control map for one model-sheet view."""
    image = Image.new("RGB", (width, height), "black")
    draw = ImageDraw.Draw(image)
    colors = [
        (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0),
        (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85),
        (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
        (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255),
        (255, 0, 170), (255, 0, 85),
    ]
    cx = width * 0.5
    narrow = view == "side"
    shoulder = width * (0.016 if narrow else 0.105)
    hip = width * (0.009 if narrow else 0.046)
    lean = -width * 0.014 if narrow else 0.0
    pts = {
        0: (cx + lean * 1.3, height * 0.103), 1: (cx, height * 0.186),
        2: (cx - shoulder, height * 0.239), 3: (cx - shoulder * 1.55, height * 0.381),
        4: (cx - shoulder * 1.80, height * 0.542), 5: (cx + shoulder, height * 0.239),
        6: (cx + shoulder * 1.55, height * 0.386), 7: (cx + shoulder * 1.80, height * 0.542),
        8: (cx - hip, height * 0.488), 9: (cx - hip * 1.5, height * 0.688),
        10: (cx - hip * 2.1, height * 0.908), 11: (cx + hip, height * 0.488),
        12: (cx + hip * 1.5, height * 0.688), 13: (cx + hip * 2.1, height * 0.908),
        14: (cx + lean * 2.2 - width * 0.014, height * 0.092),
        15: (cx + lean * 0.8 + width * 0.014, height * 0.094),
        16: (cx + lean * 2.8 - width * 0.030, height * 0.100),
        17: (cx + lean * 0.3 + width * 0.030, height * 0.101),
    }
    pts = {index: (int(x), int(y)) for index, (x, y) in pts.items()}
    limbs = [(1,2),(2,3),(3,4),(1,5),(5,6),(6,7),(1,8),(8,9),(9,10),(1,11),(11,12),(12,13),(1,0),(0,14),(14,16),(0,15),(15,17),(8,11)]
    for index, (a, b) in enumerate(limbs):
        draw.line([pts[a], pts[b]], fill=colors[index], width=9)
    for index, point in pts.items():
        draw.ellipse([point[0]-9, point[1]-9, point[0]+9, point[1]+9], fill=colors[index])
    output = BytesIO()
    image.save(output, format="PNG", compress_level=4)
    return output.getvalue()


def _letterbox_reference(path: Path, *, width: int = 768, height: int = 1024) -> bytes:
    """Fit a reference without stretching so i2L can batch face and costume images."""
    with Image.open(path) as source:
        rgb = source.convert("RGB")
        scale = min(width / rgb.width, height / rgb.height)
        resized = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), (236, 239, 241))
    canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    output = BytesIO()
    canvas.save(output, format="PNG", compress_level=4)
    return output.getvalue()


def normalize_controlled_layout(path: Path, output_path: Path) -> Path:
    """Turn an over-generated structural draft into exactly three controls.

    The native layout pass occasionally draws an extra fourth front figure even
    on a three-panel canvas.  This runs before identity diffusion: it extracts
    the first requested front/side/back silhouettes and places them into three
    fixed 768px panels.  Missing views fail instead of reaching publication.
    """
    with Image.open(path) as source:
        image = source.convert("RGB")
    scan_height = max(1, round(image.height * 0.9))
    pixels = image.load()
    border_pixels = list(image.crop((0, 0, 24, scan_height)).getdata())
    border_pixels += list(image.crop((image.width - 24, 0, image.width, scan_height)).getdata())
    background_intensity = median((r + g + b) / 3 for r, g, b in border_pixels)
    sample_step = 4
    sample_count = max(1, (scan_height + sample_step - 1) // sample_step)
    active: list[bool] = []
    for x in range(image.width):
        foreground_count = 0
        for y in range(0, scan_height, sample_step):
            r, g, b = pixels[x, y]
            saturation = max(r, g, b) - min(r, g, b)
            intensity = (r + g + b) / 3
            if saturation > 18 or intensity < background_intensity - 24:
                foreground_count += 1
        active.append(foreground_count > max(3, round(sample_count * 0.05)))
    radius = 8
    active = [
        any(active[max(0, index - radius): min(image.width, index + radius + 1)])
        for index in range(image.width)
    ]
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(active + [False]):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= max(32, round(image.width * 0.04)):
                runs.append((start, index))
            start = None
    if len(runs) < 3:
        raise ZImageWorkflowError(
            f"controlled layout requires three visible view silhouettes; detected {len(runs)}"
        )
    if len(runs) == 3 and image.size == (2304, 1024):
        image.save(output_path, format="PNG", compress_level=4)
        return output_path

    background_rgb = tuple(int(median(pixel[channel] for pixel in border_pixels)) for channel in range(3))
    canvas = Image.new("RGB", (2304, 1024), background_rgb)
    for panel_index, (left, right) in enumerate(runs[:3]):
        padding = max(24, round((right - left) * 0.12))
        crop_left = max(0, left - padding)
        crop_right = min(image.width, right + padding)
        crop = image.crop((crop_left, 0, crop_right, image.height))
        if crop.height != 1024:
            scale = 1024 / crop.height
            crop = crop.resize((max(1, round(crop.width * scale)), 1024), Image.Resampling.LANCZOS)
        if crop.width > 744:
            scale = 744 / crop.width
            crop = crop.resize((744, max(1, round(crop.height * scale))), Image.Resampling.LANCZOS)
        x = panel_index * 768 + (768 - crop.width) // 2
        y = (1024 - crop.height) // 2
        canvas.paste(crop, (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", compress_level=4)
    return output_path


def compose_shot_control_layout(
    *, location_path: Path, subjects: list[tuple[Path, tuple[float, float, float, float], str]],
    props: list[tuple[Path, tuple[float, float, float, float], str] |
                tuple[Path, tuple[float, float, float, float], str, str]],
    width: int, height: int,
) -> bytes:
    """Place adopted scene, character and prop pixels in the typed shot layout.

    Foreground extraction is used only to construct an inference-time control
    image. It never modifies adopted sources or a finished candidate.
    """
    import numpy as np
    from PIL import ImageFilter

    if not subjects or len(subjects) > 3 or len(props) > 3:
        raise ZImageWorkflowError("controlled shot requires one to three canonical characters")
    with Image.open(location_path) as opened:
        canvas = opened.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)

    def paste_reference(path: Path, box: tuple[float, float, float, float], *, prop: bool,
                        body_view: str = "front", attachment: str = "") -> None:
        left, top, right, bottom = box
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
            raise ZImageWorkflowError("invalid shot reference screen_box")
        with Image.open(path) as opened:
            source_rgba = opened.convert("RGBA")
            source = opened.convert("RGB")
            has_cutout_alpha = opened.mode in {"RGBA", "LA"} and source_rgba.getchannel("A").getextrema()[0] < 255
        if not prop and not has_cutout_alpha:
            if source.width >= source.height * 1.9:
                panel_width = source.width // 3
                panel = {"front": 0, "left_profile": 1, "right_profile": 1,
                         "back": 2}.get(body_view)
                if panel is None:
                    raise ZImageWorkflowError(f"unsupported character body_view: {body_view}")
                source = source.crop((panel * panel_width, 0,
                                      source.width if panel == 2 else (panel + 1) * panel_width,
                                      source.height))
                if body_view == "right_profile":
                    source = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            elif body_view != "front":
                raise ZImageWorkflowError(
                    f"character view {body_view} requires an adopted three-view reference: {path}"
                )
        if has_cutout_alpha:
            foreground_image = source_rgba
            alpha = np.asarray(source_rgba.getchannel("A"))
        else:
            rgb = np.asarray(source)
            source_height, source_width = rgb.shape[:2]
            edge_width = max(2, source_width // 32)
            side_pixels = np.concatenate((rgb[:, :edge_width], rgb[:, -edge_width:]), axis=1)
            studio_by_row = np.median(side_pixels, axis=1).astype(np.float32)
            distance = np.max(np.abs(rgb.astype(np.float32) - studio_by_row[:, None, :]), axis=2)
            alpha = np.where(distance > (16 if prop else 18), 255, 0).astype(np.uint8)
            foreground_image = source.convert("RGBA")
            foreground_image.putalpha(Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1.1)))
        coverage = float(np.count_nonzero(alpha)) / alpha.size
        if coverage < 0.001 or coverage > (0.65 if prop else 0.78):
            raise ZImageWorkflowError(f"reference lacks a reliable foreground mask: {path}")
        bounds = foreground_image.getbbox()
        if bounds is None:
            raise ZImageWorkflowError(f"reference foreground segmentation failed: {path}")
        cropped = foreground_image.crop(bounds)
        if prop and attachment == "back" and cropped.height >= cropped.width * 3:
            cropped = cropped.rotate(25, resample=Image.Resampling.BICUBIC, expand=True)
            rotated_bounds = cropped.getbbox()
            if rotated_bounds is None:
                raise ZImageWorkflowError("rotated back prop lost its foreground")
            cropped = cropped.crop(rotated_bounds)
        box_width, box_height = round((right - left) * width), round((bottom - top) * height)
        scale = min(box_width / cropped.width, box_height / cropped.height)
        if scale <= 0:
            raise ZImageWorkflowError("character control box has zero visible area")
        resized = cropped.resize((max(1, round(cropped.width * scale)),
                                  max(1, round(cropped.height * scale))), Image.Resampling.LANCZOS)
        x = round(left * width + (box_width - resized.width) / 2)
        y = round(bottom * height) - resized.height
        canvas.alpha_composite(resized, (x, y))

    for item in props:
        path, box, layer = item[:3]
        attachment = item[3] if len(item) == 4 else ""
        if layer == "behind_subject":
            paste_reference(path, box, prop=True, attachment=attachment)
    for path, box, body_view in subjects:
        paste_reference(path, box, prop=False, body_view=body_view)
    for item in props:
        path, box, layer = item[:3]
        attachment = item[3] if len(item) == 4 else ""
        if layer in {"front_of_subject", "world"}:
            paste_reference(path, box, prop=True, attachment=attachment)
        elif layer != "behind_subject":
            raise ZImageWorkflowError(f"unsupported prop placement layer: {layer}")
    buffer = BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


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
        adopted_face_path: Path,
        positive_prompt: str,
        negative_prompt: str,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        path = Path(adopted_costume_path)
        face_path = Path(adopted_face_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ZImageWorkflowError("adopted costume image is unavailable")
        if not face_path.is_file() or face_path.stat().st_size <= 0:
            raise ZImageWorkflowError("adopted face image is unavailable")
        uploaded = await self.adapter.upload_reference(
            filename=f"turnaround-{path.name}",
            content=path.read_bytes(),
            overwrite=True,
        )
        name = str(uploaded["name"])
        subfolder = str(uploaded.get("subfolder") or "").strip("/\\")
        uploaded_name = f"{subfolder}/{name}" if subfolder else name
        workflow = json.loads(QWEN_EDIT_TURNAROUND_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_qwen_edit_turnaround_workflow(
            workflow,
            adopted_costume_name=uploaded_name,
            seed=seed,
            filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(compiled)

    async def queue_domain_reference_edit(
        self, *, source_path: Path, identity: str, kind: str,
        seed: int, filename_prefix: str,
    ) -> dict[str, Any]:
        source_path = Path(source_path)
        if not source_path.is_file() or source_path.stat().st_size <= 0:
            raise ZImageWorkflowError("domain reference source image is unavailable")
        content = source_path.read_bytes()
        upload_name = f"domain-reference-{source_path.name}"
        if kind == "prop":
            # A cut-off cord, strap or thin component at the source border gives
            # the edit model no canvas in which to complete it. Enlarge the
            # conditioning canvas before inference when a product touches the
            # top edge; this is framing control, not post-generation repair.
            with Image.open(source_path) as opened:
                source = opened.convert("RGB")
            corners = [source.getpixel(point) for point in (
                (0, 0), (source.width - 1, 0),
                (0, source.height - 1), (source.width - 1, source.height - 1),
            )]
            background = tuple(int(median(pixel[channel] for pixel in corners)) for channel in range(3))
            top = source.crop((0, 0, source.width, max(1, source.height // 50)))
            foreground_at_top = sum(
                1 for pixel in top.getdata()
                if max(abs(pixel[channel] - background[channel]) for channel in range(3)) > 38
            )
            if foreground_at_top > max(8, round(top.width * top.height * 0.004)):
                resized = source.resize((round(source.width * 0.68), round(source.height * 0.68)), Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", source.size, background)
                canvas.paste(resized, ((source.width - resized.width) // 2, round(source.height * 0.29)))
                buffer = BytesIO()
                canvas.save(buffer, format="PNG", compress_level=4)
                content = buffer.getvalue()
                upload_name = f"domain-reference-framed-{source_path.stem}.png"
        uploaded = await self.adapter.upload_reference(
            filename=upload_name,
            content=content,
            overwrite=True,
        )
        name = str(uploaded["name"])
        subfolder = str(uploaded.get("subfolder") or "").strip("/\\")
        uploaded_name = f"{subfolder}/{name}" if subfolder else name
        workflow = json.loads(QWEN_EDIT_TURNAROUND_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_qwen_domain_reference_workflow(
            workflow, source_name=uploaded_name, identity=identity,
            kind=kind, seed=seed, filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(compiled)

    async def queue_shot_edit(
        self, *, image_paths: list[Path], prompt: str,
        seed: int, filename_prefix: str,
    ) -> dict[str, Any]:
        if not 1 <= len(image_paths) <= 3:
            raise ZImageWorkflowError("Qwen shot edit accepts one to three images per pass")
        names: list[str] = []
        suffix = Path(filename_prefix).name
        for index, source in enumerate(image_paths, start=1):
            path = Path(source)
            if not path.is_file() or path.stat().st_size <= 0:
                raise ZImageWorkflowError(f"shot reference image is unavailable: {path}")
            uploaded = await self.adapter.upload_reference(
                filename=f"shot-{suffix}-{index}-{path.name}",
                content=path.read_bytes(),
                overwrite=True,
            )
            name = str(uploaded["name"])
            subfolder = str(uploaded.get("subfolder") or "").strip("/\\")
            names.append(f"{subfolder}/{name}" if subfolder else name)
        workflow = json.loads(QWEN_EDIT_TURNAROUND_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_qwen_shot_workflow(
            workflow, image_names=names, prompt=prompt,
            seed=seed, filename_prefix=filename_prefix,
        )
        queued = await self.adapter.queue_workflow(compiled)
        queued["reference_bindings"] = list(names)
        return queued

    async def queue_shot_controlnet(
        self, *, layout_png: bytes, positive_prompt: str, seed: int,
        width: int, height: int, filename_prefix: str,
    ) -> dict[str, Any]:
        """Submit a real spatially conditioned Z-Image shot to ComfyUI."""
        if not layout_png or width < 512 or height < 512:
            raise ZImageWorkflowError("shot ControlNet requires a valid layout and render dimensions")
        await self.adapter.require_models({
            "UNETLoader": ("unet_name", "z_image_turbo_bf16.safetensors"),
            "CLIPLoader": ("clip_name", "zimage_qwen_3_4b.safetensors"),
            "VAELoader": ("vae_name", "zimage_ae.safetensors"),
            "ModelPatchLoader": ("name", "Z-Image-Turbo-Fun-Controlnet-Union.safetensors"),
        })
        uploaded = await self.adapter.upload_reference(
            filename=f"shot-layout-{Path(filename_prefix).name}.png",
            content=layout_png, overwrite=True,
        )
        name = str(uploaded["name"])
        subfolder = str(uploaded.get("subfolder") or "").strip("/\\")
        image_name = f"{subfolder}/{name}" if subfolder else name
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": image_name}},
            "2": {"class_type": "Canny", "inputs": {"image": ["1", 0], "low_threshold": 0.1, "high_threshold": 0.32}},
            "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "z_image_turbo_bf16.safetensors", "weight_dtype": "default"}},
            "4": {"class_type": "ModelPatchLoader", "inputs": {"name": "Z-Image-Turbo-Fun-Controlnet-Union.safetensors"}},
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": "zimage_ae.safetensors"}},
            "6": {"class_type": "QwenImageDiffsynthControlnet", "inputs": {
                "model": ["3", 0], "model_patch": ["4", 0], "vae": ["5", 0],
                "image": ["2", 0], "strength": 1.0,
            }},
            "7": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["6", 0], "shift": 3.0}},
            "8": {"class_type": "CLIPLoader", "inputs": {"clip_name": "zimage_qwen_3_4b.safetensors", "type": "lumina2", "device": "default"}},
            "9": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["8", 0], "text": positive_prompt}},
            "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["9", 0]}},
            "11": {"class_type": "VAEEncode", "inputs": {"pixels": ["1", 0], "vae": ["5", 0]}},
            "12": {"class_type": "KSampler", "inputs": {
                "model": ["7", 0], "positive": ["9", 0], "negative": ["10", 0],
                "latent_image": ["11", 0], "seed": seed, "steps": 8, "cfg": 1.0,
                "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 0.55,
            }},
            "13": {"class_type": "VAEDecode", "inputs": {"samples": ["12", 0], "vae": ["5", 0]}},
            "14": {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": filename_prefix}},
        }
        queued = await self.adapter.queue_workflow(graph)
        queued["layout_binding"] = image_name
        return queued

    async def queue_controlled_layout(
        self,
        *,
        front_source_path: Path,
        positive_prompt: str,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        """Generate a three-view structural draft from the canonical front."""
        path = Path(front_source_path)
        if not path.is_file() or path.stat().st_size <= 0:
            raise ZImageWorkflowError("canonical front source is unavailable")
        front = Image.open(BytesIO(_letterbox_reference(path))).convert("RGB")
        source_sheet = Image.new("RGB", (front.width * 3, front.height))
        for index in range(3):
            source_sheet.paste(front, (index * front.width, 0))
        source_bytes = BytesIO()
        source_sheet.save(source_bytes, format="PNG", compress_level=4)

        pose_images = [
            Image.open(BytesIO(_turnaround_pose_png(view))).convert("RGB")
            for view in ("front", "side", "back")
        ]
        pose_sheet = Image.new("RGB", (front.width * 3, front.height), "black")
        for index, pose_image in enumerate(pose_images):
            pose_sheet.paste(pose_image, (index * front.width, 0))
        pose_bytes = BytesIO()
        pose_sheet.save(pose_bytes, format="PNG", compress_level=4)

        source_uploaded = await self.adapter.upload_reference(
            filename=f"controlled-layout-source-{path.stem}.png",
            content=source_bytes.getvalue(),
            overwrite=True,
        )
        pose_uploaded = await self.adapter.upload_reference(
            filename=f"controlled-layout-poses-{path.stem}.png",
            content=pose_bytes.getvalue(),
            overwrite=True,
        )

        def uploaded_name(value: dict[str, Any]) -> str:
            folder = str(value.get("subfolder") or "").strip("/\\")
            return f"{folder}/{value['name']}" if folder else str(value["name"])

        workflow = json.loads(CONTROLLED_LAYOUT_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_zimage_controlled_layout_workflow(
            workflow,
            source_sheet_name=uploaded_name(source_uploaded),
            pose_sheet_name=uploaded_name(pose_uploaded),
            positive_prompt=positive_prompt,
            seed=seed,
            filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(compiled)

    async def queue_controlled_master(
        self,
        *,
        front_source_path: Path,
        face_source_path: Path,
        control_sheet_path: Path,
        positive_prompt: str,
        negative_prompt: str,
        seed: int,
        filename_prefix: str,
    ) -> dict[str, Any]:
        """Refine a structural draft using identity LoRA from the canonical front."""
        path = Path(front_source_path)
        face_path = Path(face_source_path)
        control_path = Path(control_sheet_path)
        for required, message in (
            (path, "canonical front source is unavailable"),
            (face_path, "canonical face crop is unavailable"),
            (control_path, "controlled layout sheet is unavailable"),
        ):
            if not required.is_file() or required.stat().st_size <= 0:
                raise ZImageWorkflowError(message)
        front_uploaded = await self.adapter.upload_reference(
            filename=f"controlled-master-front-{path.stem}.png",
            content=_letterbox_reference(path), overwrite=True,
        )
        face_uploaded = await self.adapter.upload_reference(
            filename=f"controlled-master-face-{path.stem}.png",
            content=_letterbox_reference(face_path), overwrite=True,
        )
        control_uploaded = await self.adapter.upload_reference(
            filename=f"controlled-master-layout-{path.stem}.png",
            content=control_path.read_bytes(), overwrite=True,
        )

        def uploaded_name(value: dict[str, Any]) -> str:
            folder = str(value.get("subfolder") or "").strip("/\\")
            return f"{folder}/{value['name']}" if folder else str(value["name"])

        workflow = json.loads(CONTROLLED_MASTER_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_zimage_controlled_master_workflow(
            workflow,
            front_source_name=uploaded_name(front_uploaded),
            face_source_name=uploaded_name(face_uploaded),
            pose_sheet_name=uploaded_name(control_uploaded),
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
    "compile_zimage_controlled_layout_workflow",
    "compile_zimage_controlled_master_workflow",
    "normalize_controlled_layout",
]
