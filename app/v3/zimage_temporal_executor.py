from __future__ import annotations

import json
import re
from copy import deepcopy
from io import BytesIO
from pathlib import Path
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
          "one eye visible, complete body and both feet visible, plain seamless background"
    )
    back_sample["inputs"]["prompt"] = (
        identity
        + ", render only one strict 180-degree rear view, face completely invisible, back of head, hair and "
          "garment construction visible, complete body and both feet visible, plain seamless background"
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
        face_uploaded = await self.adapter.upload_reference(
            filename=f"turnaround-face-{face_path.stem}.png",
            content=_letterbox_reference(face_path), overwrite=True,
        )
        side_uploaded = await self.adapter.upload_reference(
            filename=f"turnaround-side-{path.stem}.png",
            content=_turnaround_pose_png("side"), overwrite=True,
        )
        back_uploaded = await self.adapter.upload_reference(
            filename=f"turnaround-back-{path.stem}.png",
            content=_turnaround_pose_png("back"), overwrite=True,
        )

        def uploaded_name_of(value: dict[str, Any]) -> str:
            folder = str(value.get("subfolder") or "").strip("/\\")
            return f"{folder}/{value['name']}" if folder else str(value["name"])
        workflow = json.loads(TURNAROUND_WORKFLOW_PATH.read_text(encoding="utf-8"))
        compiled = compile_zimage_turnaround_workflow(
            workflow,
            adopted_costume_name=uploaded_name,
            adopted_face_name=uploaded_name_of(face_uploaded),
            side_pose_name=uploaded_name_of(side_uploaded),
            back_pose_name=uploaded_name_of(back_uploaded),
            positive_prompt=positive_prompt,
            negative_prompt=negative_prompt,
            seed=seed,
            filename_prefix=filename_prefix,
        )
        return await self.adapter.queue_workflow(compiled)

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
]
