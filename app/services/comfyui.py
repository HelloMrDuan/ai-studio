import asyncio
import copy
import json
import random
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageDraw, ImageOps, ImageStat

from app.config import Settings
from app.services.image_models import ImageModelRegistry


ASPECT_RATIO_PRESETS: dict[str, dict[str, int | str]] = {
    "1:1": {
        "label": "1:1 正方形",
        "description": "头像、封面、方形配图",
        "base_width": 1344,
        "base_height": 1344,
        "output_width": 4096,
        "output_height": 4096,
        "composition_prompt": "balanced square composition, centered visual hierarchy",
    },
    "2:3": {
        "label": "2:3 社交媒体",
        "description": "自拍、人物写真、社交平台",
        "base_width": 1088,
        "base_height": 1632,
        "output_width": 2730,
        "output_height": 4096,
        "composition_prompt": "vertical portrait composition, full subject framing, social media photography layout",
    },
    "3:4": {
        "label": "3:4 经典比例",
        "description": "经典拍照、人物与商品",
        "base_width": 1152,
        "base_height": 1536,
        "output_width": 3072,
        "output_height": 4096,
        "composition_prompt": "classic vertical photography composition, clear foreground and background separation",
    },
    "4:3": {
        "label": "4:3 文章配图",
        "description": "文章插图、传统照片、场景",
        "base_width": 1536,
        "base_height": 1152,
        "output_width": 4096,
        "output_height": 3072,
        "composition_prompt": "classic horizontal composition, editorial illustration layout, strong visual balance",
    },
    "9:16": {
        "label": "9:16 手机竖屏",
        "description": "手机壁纸、竖屏海报、人像",
        "base_width": 1008,
        "base_height": 1792,
        "output_width": 2160,
        "output_height": 3840,
        "composition_prompt": "tall vertical composition, mobile wallpaper framing, strong top-to-bottom visual flow",
    },
    "16:9": {
        "label": "16:9 桌面横屏",
        "description": "桌面壁纸、风景、电影画幅",
        "base_width": 1792,
        "base_height": 1008,
        "output_width": 3840,
        "output_height": 2160,
        "composition_prompt": "wide cinematic composition, strong horizontal depth, landscape framing",
    },
}


STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "portrait_photo": {
        "label": "人像摄影",
        "icon": "📷",
        "description": "自然肤质与真实镜头感",
        "positive": "professional portrait photograph, single clearly visible adult subject when a person is requested, complete unobstructed face, both eyes clearly visible, natural facial proportions, natural skin texture, realistic hair strands, soft flattering light, shallow depth of field, clean background separation, no handheld camera or object covering the face",
        "negative": "plastic skin, excessive smoothing, waxy face, unnatural facial proportions, distorted face, duplicate face, asymmetrical eyes, extra eyes, face obscured, face covered, object blocking face, camera covering face, hands covering face, glitch, tearing, ghosting, double image, oversharpening",
        "recommended_cfg": 6.5,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "cinematic_photo": {
        "label": "电影写真",
        "icon": "🎬",
        "description": "电影光影与胶片调色",
        "positive": "cinematic film still, dramatic lighting, film color grading, atmospheric depth, premium editorial photography",
        "negative": "flat lighting, washed out colors, cheap digital look",
        "recommended_cfg": 7.0,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m_sde",
        "scheduler": "karras",
    },
    "chinese_style": {
        "label": "中国风",
        "icon": "🏯",
        "description": "东方审美与古典意境",
        "positive": "traditional Chinese aesthetic, oriental composition, elegant classical atmosphere, refined Chinese cultural details, poetic lighting",
        "negative": "modern western clothing mismatch, random cultural symbols, cluttered ornament",
        "recommended_cfg": 7.0,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "anime": {
        "label": "动漫",
        "icon": "✨",
        "description": "清晰线稿与动画质感",
        "positive": "high quality anime illustration, clean line art, expressive character design, vivid colors, polished cel shading",
        "negative": "photorealistic skin, muddy line art, malformed hands, inconsistent eyes",
        "recommended_cfg": 7.5,
        "recommended_steps": 30,
        "sampler": "euler_ancestral",
        "scheduler": "normal",
    },
    "render_3d": {
        "label": "3D 渲染",
        "icon": "🧊",
        "description": "材质、灯光与空间体积",
        "positive": "premium 3D render, physically based materials, global illumination, detailed surfaces, studio quality rendering",
        "negative": "low polygon, flat materials, broken geometry, noisy render",
        "recommended_cfg": 7.0,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "cyberpunk": {
        "label": "赛博朋克",
        "icon": "🌃",
        "description": "霓虹都市与未来科技",
        "positive": "cyberpunk atmosphere, neon city lights, futuristic technology, cinematic rain, high contrast color lighting",
        "negative": "plain daylight, rural atmosphere, low contrast, empty background",
        "recommended_cfg": 7.5,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m_sde",
        "scheduler": "karras",
    },
    "cg_animation": {
        "label": "CG 动画",
        "icon": "🎞️",
        "description": "精致角色与动画电影感",
        "positive": "high-end CG animation frame, expressive stylized character, polished lighting, detailed cinematic environment",
        "negative": "unfinished render, stiff pose, flat texture, low detail",
        "recommended_cfg": 7.0,
        "recommended_steps": 30,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "ink_wash": {
        "label": "水墨画",
        "icon": "🖌️",
        "description": "留白、墨韵与东方笔触",
        "positive": "Chinese ink wash painting, expressive brushwork, rice paper texture, elegant negative space, poetic monochrome atmosphere",
        "negative": "photorealistic rendering, plastic texture, neon colors, hard digital edges",
        "recommended_cfg": 6.5,
        "recommended_steps": 30,
        "sampler": "euler_ancestral",
        "scheduler": "normal",
    },
    "oil_painting": {
        "label": "油画",
        "icon": "🎨",
        "description": "厚重笔触与画布质感",
        "positive": "fine art oil painting, visible impasto brush strokes, rich pigments, canvas texture, museum quality composition",
        "negative": "flat digital coloring, vector art, plastic surface, low texture",
        "recommended_cfg": 7.0,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "classical": {
        "label": "古典",
        "icon": "🏛️",
        "description": "庄重构图与古典美学",
        "positive": "classical fine art aesthetics, graceful composition, refined historical details, soft chiaroscuro lighting, timeless elegance",
        "negative": "modern casual style, neon lighting, futuristic props, visual clutter",
        "recommended_cfg": 6.5,
        "recommended_steps": 32,
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
    },
    "watercolor": {
        "label": "水彩画",
        "icon": "💧",
        "description": "透明颜料与纸张晕染",
        "positive": "delicate watercolor painting, translucent pigment washes, handmade paper texture, soft color bleeding, airy composition",
        "negative": "hard 3D render, thick oil paint, sharp vector edges, heavy contrast",
        "recommended_cfg": 6.0,
        "recommended_steps": 30,
        "sampler": "euler_ancestral",
        "scheduler": "normal",
    },
    "cartoon": {
        "label": "卡通",
        "icon": "😊",
        "description": "明快造型与轻松表达",
        "positive": "polished cartoon illustration, clean shapes, appealing character design, bold readable colors, playful visual storytelling",
        "negative": "photorealistic skin, muddy colors, overly complex texture, horror anatomy",
        "recommended_cfg": 7.0,
        "recommended_steps": 30,
        "sampler": "euler_ancestral",
        "scheduler": "normal",
    },
}


STYLE_STRENGTHS: dict[str, dict[str, float | str]] = {
    "weak": {"label": "弱", "weight": 0.70, "description": "轻度保留风格，主体描述优先"},
    "standard": {"label": "标准", "weight": 0.88, "description": "风格服务于主体，不覆盖用户描述"},
    "strong": {"label": "强", "weight": 1.05, "description": "明显强化风格，但仍保留主体描述"},
}


LEGACY_PRESET_MAP = {
    "uhd_landscape": "16:9",
    "uhd_portrait": "9:16",
    "square_4k": "1:1",
    "dci_4k": "16:9",
}


def public_image_options() -> dict[str, Any]:
    aspect_ratios = {
        key: {
            "label": spec["label"],
            "description": spec["description"],
            "base_width": spec["base_width"],
            "base_height": spec["base_height"],
            "output_width": spec["output_width"],
            "output_height": spec["output_height"],
        }
        for key, spec in ASPECT_RATIO_PRESETS.items()
    }
    styles = {
        key: {
            "label": spec["label"],
            "icon": spec["icon"],
            "description": spec["description"],
            "recommended_cfg": spec["recommended_cfg"],
            "recommended_steps": spec["recommended_steps"],
            "sampler": spec["sampler"],
            "scheduler": spec["scheduler"],
        }
        for key, spec in STYLE_PRESETS.items()
    }
    strengths = {
        key: {
            "label": spec["label"],
            "description": spec["description"],
        }
        for key, spec in STYLE_STRENGTHS.items()
    }
    return {
        "force_4k": True,
        "mandatory_ai_upscale": True,
        "aspect_ratios": aspect_ratios,
        "styles": styles,
        "style_strengths": strengths,
        "defaults": {
            "aspect_ratio": "16:9",
            "style_name": "portrait_photo",
            "style_strength": "standard",
        },
    }


def build_styled_prompts(
    *,
    positive_prompt: str,
    negative_prompt: str,
    aspect_ratio: str,
    style_name: str,
    style_strength: str,
    model_key: str = "",
) -> tuple[str, str]:
    """Generic fallback compiler. It never infers or rewrites user business semantics."""
    aspect = ASPECT_RATIO_PRESETS.get(aspect_ratio)
    if aspect is None:
        raise ValueError("不支持的画面比例")
    style = STYLE_PRESETS.get(style_name)
    if style is None:
        raise ValueError("不支持的图片风格")
    strength = STYLE_STRENGTHS.get(style_strength)
    if strength is None:
        raise ValueError("不支持的风格强度")

    style_text = str(style["positive"])
    weight = float(strength["weight"])
    if weight != 1.0:
        style_text = f"({style_text}:{weight:.2f})"

    positive_parts = [
        positive_prompt.strip(),
        str(aspect["composition_prompt"]),
        style_text,
        "clean detail, coherent structure, controlled contrast, refined lighting",
    ]
    negative_parts = [
        negative_prompt.strip(),
        str(style["negative"]),
        (
            "low quality, low resolution, blurry, malformed structure, duplicate subject, "
            "glitch, tearing, ghosting, double image, compression artifacts, "
            "oversharpening, halo, ringing, text, watermark"
        ),
    ]
    return (
        ", ".join(part for part in positive_parts if part),
        ", ".join(part for part in negative_parts if part),
    )


_MISSING = object()

OPENPOSE_MODEL = "thibaud_xl_openpose.safetensors"
FACE_DETECTOR_MODEL = "bbox/face_yolov8m.pt"
SAM_MODEL = "sam_vit_b_01ec64.pth"

# ===== V2.36.0A Z-IMAGE-TURBO RUNTIME =====
ZIMAGE_TURBO_KEY = "z_image_turbo"
ZIMAGE_TURBO_WORKFLOW_PATH = Path('/root/autodl-tmp/ai-studio/platform-v2/workflows/z_image_turbo_api.json')
ZIMAGE_TURBO_UNET = 'z_image_turbo_bf16.safetensors'
ZIMAGE_TURBO_CLIP = 'zimage_qwen_3_4b.safetensors'
ZIMAGE_TURBO_VAE = 'zimage_ae.safetensors'
ZIMAGE_TURBO_SIZES = {
    "1:1": (1024, 1024),
    "4:3": (1152, 864),
    "3:4": (864, 1152),
    "16:9": (1472, 832),
    "9:16": (832, 1472),
    "21:9": (1536, 640),
}
# ===== /V2.36.0A Z-IMAGE-TURBO RUNTIME =====


class ComfyUIService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_registry = ImageModelRegistry()

    def _workflow(self) -> dict[str, Any]:
        path = self.settings.comfyui_workflow_path
        if not path.is_file():
            raise FileNotFoundError(f"图片生成工作流不存在：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _required_inputs(node_info: dict[str, Any]) -> dict[str, Any]:
        value = node_info.get("input", {}).get("required", {})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _optional_inputs(node_info: dict[str, Any]) -> dict[str, Any]:
        value = node_info.get("input", {}).get("optional", {})
        return value if isinstance(value, dict) else {}

    @classmethod
    def _all_input_names(cls, node_info: dict[str, Any]) -> set[str]:
        return set(cls._required_inputs(node_info)) | set(cls._optional_inputs(node_info))

    @staticmethod
    def _input_default(spec: Any) -> Any:
        if not isinstance(spec, (list, tuple)) or not spec:
            return _MISSING
        first = spec[0]
        options = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
        if "default" in options:
            return options["default"]
        if isinstance(first, list) and first:
            return first[0]
        if first == "INT":
            return 0
        if first == "FLOAT":
            return 0.0
        if first == "BOOLEAN":
            return False
        if first == "STRING":
            return ""
        return _MISSING

    @classmethod
    def _enum_choice(
        cls,
        node_info: dict[str, Any],
        input_name: str,
        preferred: list[str],
    ) -> Any | None:
        spec = cls._required_inputs(node_info).get(input_name)
        if spec is None:
            spec = cls._optional_inputs(node_info).get(input_name)
        if not isinstance(spec, (list, tuple)) or not spec or not isinstance(spec[0], list):
            return None
        choices = spec[0]
        lowered = {str(item).lower(): item for item in choices}
        for wanted in preferred:
            if wanted.lower() in lowered:
                return lowered[wanted.lower()]
        for wanted in preferred:
            for item in choices:
                if wanted.lower() in str(item).lower():
                    return item
        return choices[0] if choices else None

    @classmethod
    def _node(
        cls,
        class_type: str,
        node_info: dict[str, Any],
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        required = cls._required_inputs(node_info)
        inputs: dict[str, Any] = {}
        for name, spec in required.items():
            value = cls._input_default(spec)
            if value is not _MISSING:
                inputs[name] = value
        inputs.update({key: value for key, value in overrides.items() if key in cls._all_input_names(node_info)})
        missing = [name for name in required if name not in inputs]
        if missing:
            raise RuntimeError(
                f"节点 {class_type} 缺少必填输入：{', '.join(missing)}；"
                "当前 ComfyUI 节点版本与平台工作流不兼容"
            )
        return {"inputs": inputs, "class_type": class_type}

    async def _object_info(self) -> dict[str, Any]:
        base = self.settings.comfyui_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f"{base}/object_info")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("ComfyUI object_info 返回格式异常")
        return payload

    @staticmethod
    def _checkpoint_choices(node_info: dict[str, Any]) -> list[str]:
        try:
            spec = node_info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"]
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(item) for item in spec[0]]
        except (KeyError, TypeError, IndexError):
            pass
        return []

    @staticmethod
    def _lora_choices(node_info: dict[str, Any]) -> list[str]:
        try:
            spec = node_info["LoraLoader"]["input"]["required"]["lora_name"]
            if isinstance(spec, list) and spec and isinstance(spec[0], list):
                return [str(item) for item in spec[0]]
        except (KeyError, TypeError, IndexError):
            pass
        return []

    @staticmethod
    def _choice_contains(choices: list[str], expected: str) -> str | None:
        expected_name = Path(expected).name.lower()
        for choice in choices:
            if Path(str(choice)).name.lower() == expected_name:
                return str(choice)
        return None

    def appearance_profile_keys(self) -> set[str]:
        return set(self.model_registry.appearance_status())

    async def compatible_appearance_profiles(self, model_key: str) -> list[dict[str, Any]]:
        if str(model_key or "").strip().lower() == ZIMAGE_TURBO_KEY:
            return []
        choices: list[str] = []
        try:
            node_info = await self._object_info()
            choices = self._lora_choices(node_info)
        except Exception:
            pass
        return self.model_registry.compatible_appearance_profiles(model_key, choices)

    async def resolve_appearance_enhancement(
        self,
        *,
        model_key: str,
        requested_mode: str,
        compiler_profile: str,
        strength: float = 0.30,
    ) -> dict[str, Any]:
        if str(model_key or "").strip().lower() == ZIMAGE_TURBO_KEY:
            return {
                "requested_mode": requested_mode or "off",
                "resolved_mode": "off",
                "enabled": False,
                "label": "关闭（Z-Image-Turbo）",
                "lora_name": "",
                "trigger": "",
                "trigger_weight": 0.0,
                "strength_model": 0.0,
                "strength_clip": 0.0,
            }
        mode = (requested_mode or "auto").strip().lower()
        configured_keys = self.appearance_profile_keys()
        if mode not in {"auto", "off"} | configured_keys:
            raise ValueError("不支持的人物外貌增强模式")

        selected = (compiler_profile or "off").strip().lower() if mode == "auto" else mode
        if selected not in configured_keys:
            selected = "off"
        if selected == "off":
            return {
                "requested_mode": mode,
                "resolved_mode": "off",
                "enabled": False,
                "label": "关闭",
                "lora_name": "",
                "trigger": "",
                "trigger_weight": 0.0,
                "strength_model": 0.0,
                "strength_clip": 0.0,
            }

        choices: list[str] = []
        try:
            node_info = await self._object_info()
            choices = self._lora_choices(node_info)
        except Exception:
            pass
        profile = self.model_registry.resolve_appearance_profile(
            profile_key=selected,
            model_key=model_key,
            comfyui_choices=choices,
        )
        normalized_strength = max(0.0, min(float(strength), 1.0))
        return {
            "requested_mode": mode,
            "resolved_mode": selected,
            "enabled": True,
            "label": str(profile["label"]),
            "lora_name": str(profile["resolved_lora"]),
            "trigger": str(profile.get("trigger", "")),
            "trigger_weight": float(profile.get("trigger_weight", 0.75)),
            "strength_model": normalized_strength,
            "strength_clip": normalized_strength,
        }

    async def public_models(self) -> dict[str, Any]:
        checkpoint_choices: list[str] = []
        lora_choices: list[str] = []
        try:
            node_info = await self._object_info()
            checkpoint_choices = self._checkpoint_choices(node_info)
            lora_choices = self._lora_choices(node_info)
        except Exception:
            pass
        status = self.model_registry.status(checkpoint_choices)
        status["appearance_enhancements"] = self.model_registry.appearance_status(lora_choices)
        return status

    async def resolve_model(
        self,
        model_key: str,
        style_name: str,
        required_model_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        if str(model_key or "").strip().lower() == ZIMAGE_TURBO_KEY:
            return {
                "key": ZIMAGE_TURBO_KEY,
                "label": "Z-Image-Turbo",
                "name": "Z-Image-Turbo",
                "checkpoint": ZIMAGE_TURBO_UNET,
                "resolved_checkpoint": ZIMAGE_TURBO_UNET,
                "prompt_adapter": "generic",
                "face_detailer": False,
                "face_detailer_denoise": 0.0,
                "upscaler": "",
                "smart_fallback": False,
            }
        choices: list[str] = []
        try:
            node_info = await self._object_info()
            choices = self._checkpoint_choices(node_info)
        except Exception:
            pass
        return self.model_registry.resolve(
            model_key,
            style_name,
            choices,
            required_model_keys=required_model_keys,
        )

    @staticmethod
    def _draw_openpose(path: Path, width: int, height: int, *, template: str) -> None:
        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        line_width = max(4, min(width, height) // 90)
        radius = max(5, min(width, height) // 70)
        cx = width * 0.5
        sx = width * (0.105 if width >= height else 0.15)

        if template == "neutral_full_body":
            ys = {
                "nose": 0.10, "neck": 0.19, "shoulder": 0.25, "elbow": 0.40,
                "wrist": 0.55, "hip": 0.49, "knee": 0.70, "ankle": 0.92,
                "eye": 0.085, "ear": 0.095,
            }
        else:
            ys = {
                "nose": 0.16, "neck": 0.29, "shoulder": 0.36, "elbow": 0.53,
                "wrist": 0.77, "hip": 0.82, "knee": 1.08, "ankle": 1.25,
                "eye": 0.135, "ear": 0.15,
            }

        pts: dict[int, tuple[int, int]] = {
            0: (int(cx), int(height * ys["nose"])),
            1: (int(cx), int(height * ys["neck"])),
            2: (int(cx - sx), int(height * ys["shoulder"])),
            3: (int(cx - sx * 1.18), int(height * ys["elbow"])),
            4: (int(cx - sx * 1.02), int(height * ys["wrist"])),
            5: (int(cx + sx), int(height * ys["shoulder"])),
            6: (int(cx + sx * 1.18), int(height * ys["elbow"])),
            7: (int(cx + sx * 1.02), int(height * ys["wrist"])),
            8: (int(cx - sx * 0.44), int(height * ys["hip"])),
            9: (int(cx - sx * 0.45), int(height * ys["knee"])),
            10: (int(cx - sx * 0.46), int(height * ys["ankle"])),
            11: (int(cx + sx * 0.44), int(height * ys["hip"])),
            12: (int(cx + sx * 0.45), int(height * ys["knee"])),
            13: (int(cx + sx * 0.46), int(height * ys["ankle"])),
            14: (int(cx - sx * 0.19), int(height * ys["eye"])),
            15: (int(cx + sx * 0.19), int(height * ys["eye"])),
            16: (int(cx - sx * 0.35), int(height * ys["ear"])),
            17: (int(cx + sx * 0.35), int(height * ys["ear"])),
        }
        colors = [
            (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0),
            (170, 255, 0), (85, 255, 0), (0, 255, 0), (0, 255, 85),
            (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
            (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255),
            (255, 0, 170), (255, 0, 85),
        ]
        limbs = [
            (1, 2), (2, 3), (3, 4), (1, 5), (5, 6), (6, 7),
            (1, 8), (8, 9), (9, 10), (1, 11), (11, 12), (12, 13),
            (1, 0), (0, 14), (14, 16), (0, 15), (15, 17), (8, 11),
        ]
        for index, (a, b) in enumerate(limbs):
            pa, pb = pts[a], pts[b]
            if pa[1] >= height or pb[1] >= height:
                continue
            draw.line([pa, pb], fill=colors[index % len(colors)], width=line_width)
        for index, point in pts.items():
            if point[1] >= height:
                continue
            color = colors[index % len(colors)]
            draw.ellipse(
                [point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius],
                fill=color,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG", compress_level=4)

    async def _upload_pose(self, path: Path) -> str:
        base = self.settings.comfyui_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=120) as client:
            with path.open("rb") as stream:
                response = await client.post(
                    f"{base}/upload/image",
                    data={"type": "input", "subfolder": "ai_studio_pose", "overwrite": "true"},
                    files={"image": (path.name, stream, "image/png")},
                )
            response.raise_for_status()
            payload = response.json()
        name = str(payload.get("name") or path.name)
        subfolder = str(payload.get("subfolder") or "")
        return f"{subfolder}/{name}" if subfolder else name

    def _prepare(
        self,
        *,
        positive: str,
        negative: str,
        checkpoint: str,
        base_width: int,
        base_height: int,
        output_width: int,
        output_height: int,
        steps: int,
        cfg: float,
        seed: int,
        sampler: str,
        scheduler: str,
        prefix: str,
        upscale_model: str,
        node_info: dict[str, Any],
        pose_image_name: str | None = None,
        pose_strength: float = 0.0,
        pose_end_percent: float = 0.78,
        enable_face_detailer: bool = False,
        face_detailer_denoise: float = 0.22,
        lora_name: str | None = None,
        lora_strength_model: float = 0.30,
        lora_strength_clip: float = 0.30,
    ) -> dict[str, Any]:
        workflow = copy.deepcopy(self._workflow())
        actual_seed = seed if seed >= 0 else random.randint(0, 2**63 - 1)
        workflow["4"]["inputs"]["ckpt_name"] = checkpoint
        model_ref: list[Any] = ["4", 0]
        clip_ref: list[Any] = ["4", 1]
        if lora_name:
            if "LoraLoader" not in node_info:
                raise RuntimeError("ComfyUI 缺少 LoraLoader 节点")
            workflow["21"] = self._node(
                "LoraLoader",
                node_info["LoraLoader"],
                {
                    "model": ["4", 0],
                    "clip": ["4", 1],
                    "lora_name": lora_name,
                    "strength_model": max(0.0, min(float(lora_strength_model), 1.0)),
                    "strength_clip": max(0.0, min(float(lora_strength_clip), 1.0)),
                },
            )
            model_ref = ["21", 0]
            clip_ref = ["21", 1]
        workflow["3"]["inputs"]["model"] = model_ref
        workflow["6"]["inputs"]["clip"] = clip_ref
        workflow["7"]["inputs"]["clip"] = clip_ref
        workflow["5"]["inputs"].update({"width": base_width, "height": base_height, "batch_size": 1})
        workflow["6"]["inputs"]["text"] = positive
        workflow["7"]["inputs"]["text"] = negative
        workflow["3"]["inputs"].update(
            {
                "seed": actual_seed,
                "steps": max(24, min(int(steps), 40)),
                "cfg": max(4.0, min(float(cfg), 8.0)),
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": 1.0,
            }
        )

        image_source: list[Any] = ["8", 0]
        if pose_image_name:
            required_nodes = ["LoadImage", "ControlNetLoader", "ControlNetApplyAdvanced"]
            missing = [name for name in required_nodes if name not in node_info]
            if missing:
                raise RuntimeError("ComfyUI 缺少姿态控制节点：" + ", ".join(missing))
            workflow["15"] = self._node(
                "ControlNetLoader",
                node_info["ControlNetLoader"],
                {"control_net_name": OPENPOSE_MODEL},
            )
            workflow["16"] = self._node(
                "LoadImage",
                node_info["LoadImage"],
                {"image": pose_image_name},
            )
            control_overrides: dict[str, Any] = {
                "positive": ["6", 0],
                "negative": ["7", 0],
                "control_net": ["15", 0],
                "image": ["16", 0],
                "strength": max(0.0, min(float(pose_strength), 1.0)),
                "start_percent": 0.0,
                "end_percent": max(0.10, min(float(pose_end_percent), 1.0)),
            }
            if "vae" in self._all_input_names(node_info["ControlNetApplyAdvanced"]):
                control_overrides["vae"] = ["4", 2]
            workflow["17"] = self._node(
                "ControlNetApplyAdvanced",
                node_info["ControlNetApplyAdvanced"],
                control_overrides,
            )
            workflow["3"]["inputs"]["positive"] = ["17", 0]
            workflow["3"]["inputs"]["negative"] = ["17", 1]

        if enable_face_detailer:
            required_nodes = ["UltralyticsDetectorProvider", "SAMLoader", "FaceDetailer"]
            missing = [name for name in required_nodes if name not in node_info]
            if missing:
                raise RuntimeError("ComfyUI 缺少局部人脸细化节点：" + ", ".join(missing))
            workflow["18"] = self._node(
                "UltralyticsDetectorProvider",
                node_info["UltralyticsDetectorProvider"],
                {"model_name": FACE_DETECTOR_MODEL},
            )
            workflow["19"] = self._node(
                "SAMLoader",
                node_info["SAMLoader"],
                {"model_name": SAM_MODEL},
            )
            face_info = node_info["FaceDetailer"]
            face_names = self._all_input_names(face_info)
            face_overrides: dict[str, Any] = {
                "image": ["8", 0],
                "model": model_ref,
                "clip": clip_ref,
                "vae": ["4", 2],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "bbox_detector": ["18", 0],
                "sam_model_opt": ["19", 0],
                "seed": (actual_seed + 104729) % (2**63 - 1),
                "steps": 16,
                "cfg": min(float(cfg), 6.0),
                "sampler_name": sampler,
                "scheduler": scheduler,
                "denoise": max(0.05, min(float(face_detailer_denoise), 0.45)),
                "feather": 5,
                "noise_mask": True,
                "force_inpaint": True,
                "guide_size": 768,
                "max_size": 1024,
                "bbox_threshold": 0.50,
                "bbox_dilation": 10,
                "bbox_crop_factor": 3.0,
                "sam_dilation": 0,
                "sam_threshold": 0.93,
                "sam_bbox_expansion": 0,
                "sam_mask_hint_threshold": 0.70,
                "drop_size": 10,
                "wildcard": "",
                "cycle": 1,
            }
            if "guide_size_for" in face_names:
                choice = self._enum_choice(face_info, "guide_size_for", ["bbox", "crop_region"])
                if choice is not None:
                    face_overrides["guide_size_for"] = choice
            if "sam_detection_hint" in face_names:
                choice = self._enum_choice(face_info, "sam_detection_hint", ["center-1", "center-2", "rectangle"])
                if choice is not None:
                    face_overrides["sam_detection_hint"] = choice
            if "sam_mask_hint_use_negative" in face_names:
                choice = self._enum_choice(face_info, "sam_mask_hint_use_negative", ["False", "false"])
                face_overrides["sam_mask_hint_use_negative"] = choice if choice is not None else False
            workflow["20"] = self._node("FaceDetailer", face_info, face_overrides)
            image_source = ["20", 0]

        workflow["13"] = {
            "inputs": {"model_name": upscale_model},
            "class_type": "UpscaleModelLoader",
        }
        workflow["14"] = {
            "inputs": {"upscale_model": ["13", 0], "image": image_source},
            "class_type": "ImageUpscaleWithModel",
        }
        workflow["12"] = {
            "inputs": {
                "upscale_method": "lanczos",
                "width": output_width,
                "height": output_height,
                "crop": "center",
                "image": ["14", 0],
            },
            "class_type": "ImageScale",
        }
        workflow["9"]["inputs"]["images"] = ["12", 0]
        workflow["9"]["inputs"]["filename_prefix"] = prefix
        return workflow

    @staticmethod
    def _extract_upscale_choices(payload: Any) -> list[str]:
        found: list[str] = []

        def add(values: Any) -> None:
            if not isinstance(values, list):
                return
            for value in values:
                if isinstance(value, str) and value.lower().endswith((".pth", ".pt", ".safetensors")):
                    found.append(value)

        def walk(value: Any, parent_key: str = "") -> None:
            if isinstance(value, dict):
                if str(value.get("name", "")).lower() == "model_name":
                    for key in ("options", "choices", "values", "items"):
                        add(value.get(key))
                for key, child in value.items():
                    lowered = str(key).lower()
                    if parent_key == "model_name" and lowered in {"options", "choices", "values", "items"}:
                        add(child)
                    walk(child, lowered)
            elif isinstance(value, list):
                if parent_key == "model_name":
                    add(value)
                for child in value:
                    walk(child, parent_key)

        try:
            legacy = payload["UpscaleModelLoader"]["input"]["required"]["model_name"]
            if isinstance(legacy, list) and legacy:
                add(legacy[0])
        except (KeyError, TypeError, IndexError):
            pass
        walk(payload)
        return list(dict.fromkeys(found))

    async def _available_upscale_models(self) -> list[str]:
        base = self.settings.comfyui_base_url.rstrip("/")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f"{base}/object_info/UpscaleModelLoader")
            response.raise_for_status()
            payload = response.json()
        return self._extract_upscale_choices(payload)

    async def _select_upscale_model(self, expected: str) -> str:
        expected = Path(expected).name
        model_dirs = (
            Path("/root/autodl-tmp/models/image/upscale_models"),
            Path("/root/autodl-tmp/ai-studio/ComfyUI/models/upscale_models"),
        )
        if not any((directory / expected).is_file() for directory in model_dirs):
            raise RuntimeError(f"强制 AI 超分模型文件不存在：{expected}")
        try:
            choices = await self._available_upscale_models()
        except Exception:
            choices = []
        if choices:
            for item in choices:
                if Path(item).name.lower() == expected.lower():
                    return item
            raise RuntimeError(f"ComfyUI 当前未登记 AI 超分模型：{expected}")
        return expected


    # ===== V2.36.0A Z-IMAGE-TURBO METHODS =====
    @staticmethod
    def _zimage_ratio_size(aspect_ratio: str) -> tuple[int, int]:
        return ZIMAGE_TURBO_SIZES.get(
            str(aspect_ratio or "16:9").strip(),
            ZIMAGE_TURBO_SIZES["16:9"],
        )

    @staticmethod
    def _zimage_choice(node_info: dict[str, Any], class_type: str, input_name: str, expected: str) -> str:
        try:
            choices = node_info[class_type]["input"]["required"][input_name][0]
        except (KeyError, TypeError, IndexError):
            raise RuntimeError(f"ComfyUI 缺少 Z-Image-Turbo 所需节点/输入：{class_type}.{input_name}")
        if not isinstance(choices, list):
            raise RuntimeError(f"ComfyUI {class_type}.{input_name} 没有可用模型列表")
        expected_name = Path(expected).name.lower()
        for choice in choices:
            if Path(str(choice)).name.lower() == expected_name:
                return str(choice)
        raise RuntimeError(f"ComfyUI 当前未识别 Z-Image-Turbo 文件：{expected}")

    def _zimage_prepare_workflow(
        self,
        *,
        positive: str,
        negative: str,
        width: int,
        height: int,
        seed: int,
        steps: int,
        cfg: float,
        sampler: str,
        scheduler: str,
        prefix: str,
        node_info: dict[str, Any],
    ) -> dict[str, Any]:
        if not ZIMAGE_TURBO_WORKFLOW_PATH.is_file():
            raise FileNotFoundError(f"Z-Image-Turbo 工作流不存在：{ZIMAGE_TURBO_WORKFLOW_PATH}")
        workflow = json.loads(ZIMAGE_TURBO_WORKFLOW_PATH.read_text(encoding="utf-8"))
        unet = self._zimage_choice(node_info, "UNETLoader", "unet_name", ZIMAGE_TURBO_UNET)
        clip = self._zimage_choice(node_info, "CLIPLoader", "clip_name", ZIMAGE_TURBO_CLIP)
        vae = self._zimage_choice(node_info, "VAELoader", "vae_name", ZIMAGE_TURBO_VAE)
        workflow["16"]["inputs"]["unet_name"] = unet
        workflow["18"]["inputs"]["clip_name"] = clip
        workflow["18"]["inputs"]["type"] = "lumina2"
        workflow["17"]["inputs"]["vae_name"] = vae
        workflow["6"]["inputs"]["text"] = positive.strip()
        workflow["7"]["inputs"]["text"] = negative.strip()
        workflow["13"]["inputs"].update({"width": int(width), "height": int(height), "batch_size": 1})
        workflow["3"]["inputs"].update({
            "seed": int(seed),
            "steps": int(steps),
            "cfg": float(cfg),
            "sampler_name": str(sampler),
            "scheduler": str(scheduler),
            "denoise": 1.0,
        })
        workflow["9"]["inputs"]["filename_prefix"] = prefix
        return workflow

    @staticmethod
    def _ensure_zimage_output(path: Path, width: int, height: int) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Z-Image-Turbo 输出文件不存在或为空")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if image.size != (width, height):
                    raise RuntimeError(
                        f"Z-Image-Turbo 输出尺寸异常：期望 {width}×{height}，"
                        f"实际 {image.size[0]}×{image.size[1]}"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Z-Image-Turbo 输出文件损坏：{exc}") from exc

    async def _generate_zimage_turbo(
        self,
        *,
        positive: str,
        negative: str,
        aspect_ratio: str,
        steps: int,
        cfg: float,
        seed: int,
        sampler: str,
        scheduler: str,
        count: int,
        output_dir: Path,
        log,
    ) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        width, height = self._zimage_ratio_size(aspect_ratio)
        # Legacy UI used 32 / 6.5 / dpmpp_2m / karras. When those defaults
        # arrive with Z-Image-Turbo, normalize to the official Turbo profile.
        actual_steps = 9 if int(steps or 0) in {0, 32} else max(1, min(int(steps), 50))
        actual_cfg = 1.0 if abs(float(cfg or 0) - 6.5) < 1e-9 or float(cfg or 0) <= 0 else float(cfg)
        actual_sampler = "euler" if str(sampler or "").strip() in {"", "dpmpp_2m"} else str(sampler)
        actual_scheduler = "simple" if str(scheduler or "").strip() in {"", "karras"} else str(scheduler)
        node_info = await self._object_info()
        await log(
            f"Z-Image-Turbo：工作分辨率 {width}×{height}；steps={actual_steps}；"
            f"CFG={actual_cfg:g}；sampler={actual_sampler}/{actual_scheduler}"
        )
        await log("Z-Image-Turbo：关闭 SDXL 专用 4K 强制超分、OpenPose、FaceDetailer 与外貌 LoRA 链")
        results: list[Path] = []
        for index in range(max(1, int(count))):
            current_seed = (
                int(time.time_ns() % 1125899906842624)
                if int(seed) < 0
                else int(seed) + index
            )
            workflow = self._zimage_prepare_workflow(
                positive=positive,
                negative=negative,
                width=width,
                height=height,
                seed=current_seed,
                steps=actual_steps,
                cfg=actual_cfg,
                sampler=actual_sampler,
                scheduler=actual_scheduler,
                prefix=f"AIStudio/ZImageTurbo/{output_dir.name}_{index + 1}",
                node_info=node_info,
            )
            path = output_dir / f"generated_{index + 1}_{width}x{height}.png"
            await self._run_one(workflow, path)
            self._ensure_zimage_output(path, width, height)
            await log(f"Z-Image-Turbo 第 {index + 1}/{count} 张完成：{width}×{height}")
            results.append(path)
        return results
    # ===== /V2.36.0A Z-IMAGE-TURBO METHODS =====

    async def generate(
        self,
        *,
        positive: str,
        negative: str,
        user_positive: str = "",
        model_key: str,
        model_label: str,
        checkpoint: str,
        subject_is_human: bool = False,
        pose_control: str = "off",
        pose_template: str = "off",
        face_detailer_enabled: bool = True,
        face_detailer_denoise: float = 0.22,
        appearance_enabled: bool = False,
        appearance_label: str = "关闭",
        appearance_lora_name: str = "",
        appearance_lora_trigger: str = "",
        appearance_lora_trigger_weight: float = 0.75,
        appearance_lora_strength_model: float = 0.30,
        appearance_lora_strength_clip: float = 0.30,
        upscaler: str = "4x-UltraSharp.pth",
        aspect_ratio: str = "16:9",
        base_width: int,
        base_height: int,
        output_width: int,
        output_height: int,
        steps: int,
        cfg: float,
        seed: int,
        sampler: str,
        scheduler: str,
        count: int,
        style_name: str = "portrait_photo",
        output_dir: Path,
        log,
    ) -> list[Path]:

        if str(model_key or "").strip().lower() == ZIMAGE_TURBO_KEY:
            return await self._generate_zimage_turbo(
                positive=positive,
                negative=negative,
                aspect_ratio=aspect_ratio,
                steps=steps,
                cfg=cfg,
                seed=seed,
                sampler=sampler,
                scheduler=scheduler,
                count=count,
                output_dir=output_dir,
                log=log,
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        results: list[Path] = []
        upscale_model = await self._select_upscale_model(upscaler)
        node_info = await self._object_info()
        choices = self._checkpoint_choices(node_info)
        choice_names = {Path(item).name.lower() for item in choices}
        if choices and Path(checkpoint).name.lower() not in choice_names:
            raise RuntimeError(f"ComfyUI 当前未识别 checkpoint：{checkpoint}")

        resolved_lora_name = ""
        effective_positive = positive
        if appearance_enabled:
            if "LoraLoader" not in node_info:
                raise RuntimeError("ComfyUI 缺少 LoraLoader 节点")
            lora_choices = self._lora_choices(node_info)
            resolved_lora_name = self._choice_contains(lora_choices, appearance_lora_name) or ""
            if not resolved_lora_name:
                raise RuntimeError(f"ComfyUI 当前未识别人物外貌 LoRA：{appearance_lora_name}")
            trigger = appearance_lora_trigger.strip()
            if trigger and trigger.lower() not in effective_positive.lower():
                weight = max(0.0, min(float(appearance_lora_trigger_weight), 2.0))
                effective_positive = f"{effective_positive}, ({trigger}:{weight:.2f})"

        pose_modes = {"off": 0.0, "light": 0.40, "standard": 0.58}
        if pose_control not in pose_modes:
            raise ValueError("不支持的姿态控制模式")
        if pose_template not in {"off", "neutral_full_body", "neutral_upper_body"}:
            raise ValueError("不支持的姿态模板")

        human_request = bool(subject_is_human or appearance_enabled or pose_control != "off")
        if not human_request:
            pose_control = "off"
            pose_template = "off"
        if pose_control == "off":
            pose_template = "off"
        elif pose_template == "off":
            pose_template = "neutral_upper_body"

        pose_strength = pose_modes[pose_control]
        if pose_template == "neutral_full_body" and pose_strength > 0:
            pose_strength = max(pose_strength, 0.68)

        await log(f"生成模型：{model_label}（{checkpoint}）")
        if appearance_enabled:
            await log(
                f"人物外貌增强：{appearance_label}；LoRA={resolved_lora_name}；"
                f"MODEL/CLIP 强度={appearance_lora_strength_model:.2f}/{appearance_lora_strength_clip:.2f}"
            )
        else:
            await log("人物外貌增强：关闭")

        pose_image_name: str | None = None
        if pose_strength > 0:
            control_target = Path("/root/autodl-tmp/models/image/controlnet") / OPENPOSE_MODEL
            if not control_target.is_file() or control_target.stat().st_size == 0:
                raise RuntimeError(f"SDXL OpenPose 模型不存在：{control_target}")
            pose_path = output_dir / f"control_pose_{aspect_ratio.replace(':', 'x')}.png"
            self._draw_openpose(
                pose_path,
                base_width,
                base_height,
                template=pose_template,
            )
            pose_image_name = await self._upload_pose(pose_path)
            pose_path.unlink(missing_ok=True)
            await log(
                f"姿态控制：{pose_control}；模板={pose_template}；OpenPose 强度 {pose_strength:.2f}；"
                "控制选择来自语义编译结果或用户显式选择"
            )
        else:
            await log("姿态控制：关闭；不扫描提示词关键词")

        enable_face_detailer = human_request and face_detailer_enabled
        if enable_face_detailer:
            await log(
                f"局部细化：FaceDetailer + {FACE_DETECTOR_MODEL} + {SAM_MODEL}；"
                f"denoise={face_detailer_denoise:.2f}，只处理人脸局部"
            )
        else:
            await log("局部细化：当前语义结果或模型配置未启用 FaceDetailer")

        await log(
            f"全比例基础采样：{base_width}×{base_height}；强制最终输出：{output_width}×{output_height}"
        )
        await log(f"AI 超分模型：{upscale_model}")

        for index in range(count):
            await log(f"正在生成第 {index + 1}/{count} 张 4K 图片")
            workflow = self._prepare(
                positive=effective_positive,
                negative=negative,
                checkpoint=checkpoint,
                base_width=base_width,
                base_height=base_height,
                output_width=output_width,
                output_height=output_height,
                steps=steps,
                cfg=cfg,
                seed=seed if seed < 0 else seed + index,
                sampler=sampler,
                scheduler=scheduler,
                prefix=f"AIStudioV294_{model_key}/{output_dir.name}_{index + 1}",
                upscale_model=upscale_model,
                node_info=node_info,
                pose_image_name=pose_image_name,
                pose_strength=pose_strength,
                pose_end_percent=0.90 if pose_template == "neutral_full_body" else 0.78,
                enable_face_detailer=enable_face_detailer,
                face_detailer_denoise=face_detailer_denoise,
                lora_name=resolved_lora_name or None,
                lora_strength_model=appearance_lora_strength_model,
                lora_strength_clip=appearance_lora_strength_clip,
            )
            path = output_dir / f"generated_{index + 1}_{output_width}x{output_height}.png"
            await self._run_one(workflow, path)
            self._ensure_exact_4k(path, output_width, output_height)
            await log(
                f"第 {index + 1} 张图片完成：模型={model_label}，"
                f"姿态控制={pose_control}/{pose_template}，人物外貌增强={appearance_label if appearance_enabled else '关闭'}，"
                f"AI 超分及 {output_width}×{output_height} 像素校验通过"
            )
            results.append(path)
        return results

    @staticmethod
    def _ensure_exact_4k(path: Path, width: int, height: int) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("4K 输出文件不存在或为空")
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise RuntimeError(f"4K 输出文件损坏：{exc}") from exc
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            if rgb.size != (width, height):
                rgb = ImageOps.fit(rgb, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
                rgb.save(path, format="PNG", compress_level=4)
        with Image.open(path) as verified:
            if verified.size != (width, height):
                raise RuntimeError(
                    f"4K 尺寸校验失败：期望 {width}×{height}，实际 {verified.size[0]}×{verified.size[1]}"
                )
            thumb = verified.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR)
            stats = ImageStat.Stat(thumb)
            extrema = thumb.getextrema()
            dynamic_ranges = [high - low for low, high in extrema]
            if max(dynamic_ranges) < 4 and max(stats.stddev) < 1.0:
                raise RuntimeError("4K 输出画面接近纯色或空白")

    async def _run_one(self, workflow: dict[str, Any], output_path: Path) -> None:
        base = self.settings.comfyui_base_url.rstrip("/")
        client_id = str(uuid.uuid4())
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{base}/prompt", json={"prompt": workflow, "client_id": client_id})
            if response.status_code >= 400:
                raise RuntimeError(f"ComfyUI 拒绝工作流：{response.text[-3000:]}")
            prompt_id = response.json()["prompt_id"]

        deadline = time.monotonic() + self.settings.comfyui_task_timeout_seconds
        history = None
        while time.monotonic() < deadline:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(f"{base}/history/{prompt_id}")
                response.raise_for_status()
                payload = response.json()
            if prompt_id in payload:
                history = payload[prompt_id]
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI 生成失败：{status.get('messages', [])}")
                if history.get("outputs"):
                    break
            await asyncio.sleep(2)

        if not history or not history.get("outputs"):
            raise TimeoutError("等待 ComfyUI 4K 出图超时")
        save_output = history["outputs"].get("9", {})
        images = save_output.get("images", [])
        if not images:
            raise RuntimeError("ComfyUI 完成任务但最终保存节点没有返回图片")
        image = images[0]
        params = {
            "filename": image["filename"],
            "subfolder": image.get("subfolder", ""),
            "type": image.get("type", "output"),
        }
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.get(f"{base}/view", params=params)
            response.raise_for_status()
        output_path.write_bytes(response.content)
        if output_path.stat().st_size == 0:
            raise RuntimeError("生成图片下载为空")
