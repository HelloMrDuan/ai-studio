import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable

from PIL import Image

from app.config import Settings


PROCESSOR_SPECS: dict[str, dict[str, Any]] = {
    "face_swapper": {
        "label": "人物替换",
        "description": "把来源人物身份迁移到目标图片或视频。",
        "source_kind": "image",
        "source_required": True,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "face_swapper_model", "label": "替换模型", "type": "select",
             "default": "hyperswap_1a_256",
             "options": ["hyperswap_1a_256","hyperswap_1b_256","hyperswap_1c_256","inswapper_128","inswapper_128_fp16","simswap_256","simswap_unofficial_512","ghost_1_256","ghost_2_256","ghost_3_256","uniface_256"]},
            {"name": "face_swapper_pixel_boost", "label": "人脸处理分辨率", "type": "select",
             "default": "512x512", "options": ["128x128","256x256","384x384","512x512","768x768","1024x1024"]},
            {"name": "face_swapper_weight", "label": "来源人物权重", "type": "range",
             "default": 1.0, "min": 0, "max": 1, "step": 0.05},
        ],
    },
    "deep_swapper": {
        "label": "深度替换",
        "description": "使用已安装的 DeepFaceLive 人物模型进行深度替换。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "deep_swapper_model", "label": "深度替换模型", "type": "text",
             "default": "iperov/elon_musk_224"},
            {"name": "deep_swapper_morph", "label": "融合程度", "type": "range",
             "default": 80, "min": 0, "max": 100, "step": 1},
        ],
    },
    "age_modifier": {
        "label": "年龄调整",
        "description": "让目标人物看起来更年轻或更年长。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "age_modifier_direction", "label": "年龄方向", "type": "range",
             "default": 0, "min": -100, "max": 100, "step": 1,
             "hint": "负值变年轻，正值变年长"},
        ],
    },
    "expression_restorer": {
        "label": "表情修复",
        "description": "恢复或调整目标人物的面部表情区域。",
        "source_kind": "image",
        "source_required": True,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "expression_restorer_factor", "label": "修复强度", "type": "range",
             "default": 80, "min": 0, "max": 100, "step": 1},
            {"name": "expression_restorer_areas", "label": "修复区域", "type": "multiselect",
             "default": ["upper-face","lower-face"], "options": ["upper-face","lower-face"]},
        ],
    },
    "face_enhancer": {
        "label": "面部增强",
        "description": "修复和增强图片或视频中的人脸清晰度。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "face_enhancer_model", "label": "增强模型", "type": "select",
             "default": "gfpgan_1.4",
             "options": ["codeformer","gfpgan_1.2","gfpgan_1.3","gfpgan_1.4","gpen_bfr_256","gpen_bfr_512","gpen_bfr_1024","gpen_bfr_2048","restoreformer_plus_plus"]},
            {"name": "face_enhancer_blend", "label": "融合比例", "type": "range",
             "default": 80, "min": 0, "max": 100, "step": 1},
            {"name": "face_enhancer_weight", "label": "增强权重", "type": "range",
             "default": 0.5, "min": 0, "max": 1, "step": 0.05},
        ],
    },
    "face_editor": {
        "label": "面部编辑",
        "description": "调整眉毛、视线、眼睛、嘴部和头部方向。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "face_editor_eyebrow_direction", "label": "眉毛方向", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_eye_gaze_horizontal", "label": "视线水平", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_eye_gaze_vertical", "label": "视线垂直", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_eye_open_ratio", "label": "眼睛开合", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_lip_open_ratio", "label": "嘴唇开合", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_mouth_smile", "label": "微笑程度", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_mouth_pout", "label": "嘟嘴程度", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_head_pitch", "label": "抬头低头", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_head_yaw", "label": "左右转头", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
            {"name": "face_editor_head_roll", "label": "头部倾斜", "type": "range", "default": 0, "min": -1, "max": 1, "step": 0.05},
        ],
    },
    "lip_syncer": {
        "label": "口型同步",
        "description": "让目标视频中的嘴型与上传音频同步。",
        "source_kind": "audio",
        "source_required": True,
        "target_kinds": ["video"],
        "params": [
            {"name": "lip_syncer_model", "label": "口型模型", "type": "select",
             "default": "wav2lip_gan_96", "options": ["edtalk_256","wav2lip_96","wav2lip_gan_96"]},
            {"name": "lip_syncer_weight", "label": "同步权重", "type": "range",
             "default": 0.5, "min": 0, "max": 1, "step": 0.05},
        ],
    },
    "background_remover": {
        "label": "背景移除",
        "description": "移除图片或视频背景，可输出透明或指定颜色背景。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "background_remover_model", "label": "抠图模型", "type": "select",
             "default": "rmbg_2.0",
             "options": ["ben_2","birefnet_general","birefnet_portrait","isnet_general","modnet","rmbg_1.4","rmbg_2.0","silueta","u2net_cloth","u2net_general","u2net_human","u2net_portable"]},
            {"name": "background_remover_color", "label": "背景 RGBA", "type": "text",
             "default": "0 0 0 0", "hint": "例如透明：0 0 0 0；绿色：0 255 0 255"},
        ],
    },
    "frame_enhancer": {
        "label": "画面增强",
        "description": "对整张图片或视频逐帧增强和放大。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "frame_enhancer_model", "label": "画面增强模型", "type": "select",
             "default": "span_kendata_x4",
             "options": ["clear_reality_x4","lsdir_x4","nomos8k_sc_x4","real_esrgan_x2","real_esrgan_x2_fp16","real_esrgan_x4","real_esrgan_x4_fp16","real_esrgan_x8","real_esrgan_x8_fp16","real_hatgan_x4","real_web_photo_x4","realistic_rescaler_x4","remacri_x4","siax_x4","span_kendata_x4","swin2_sr_x4","ultra_sharp_x4","ultra_sharp_2_x4"]},
            {"name": "frame_enhancer_blend", "label": "增强融合比例", "type": "range",
             "default": 80, "min": 0, "max": 100, "step": 1},
        ],
    },
    "frame_colorizer": {
        "label": "画面上色",
        "description": "为黑白图片或视频自动上色。",
        "source_required": False,
        "target_kinds": ["image", "video"],
        "params": [
            {"name": "frame_colorizer_model", "label": "上色模型", "type": "select",
             "default": "ddcolor",
             "options": ["ddcolor","ddcolor_artistic","deoldify","deoldify_artistic","deoldify_stable"]},
            {"name": "frame_colorizer_size", "label": "处理尺寸", "type": "select",
             "default": "512x512", "options": ["256x256","384x384","512x512"]},
            {"name": "frame_colorizer_blend", "label": "上色融合比例", "type": "range",
             "default": 100, "min": 0, "max": 100, "step": 1},
        ],
    },
}

COMMON_PARAMS = [
    {"name": "face_selector_mode", "label": "人脸选择方式", "type": "select",
     "default": "one", "options": ["one","many","reference"]},
    {"name": "face_mask_types", "label": "人脸遮罩", "type": "multiselect",
     "default": ["box"], "options": ["box","occlusion","area","region"]},
    {"name": "output_quality", "label": "输出质量", "type": "range",
     "default": 95, "min": 1, "max": 100, "step": 1},
]


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg",".jpeg",".png",".webp",".bmp",".tif",".tiff"}


class FaceFusionService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._help_cache: str | None = None

    async def capabilities(self) -> dict[str, Any]:
        help_text = await self._headless_help()
        result = {}
        for key, spec in PROCESSOR_SPECS.items():
            marker = f"--{key.replace('_', '-')}"
            # 处理器本身可能不直接出现在 flag 中，用其首个专属参数二次判断。
            first_param = spec["params"][0]["name"] if spec.get("params") else ""
            param_marker = f"--{first_param.replace('_', '-')}" if first_param else ""
            available = key in help_text or marker in help_text or param_marker in help_text
            result[key] = {**spec, "common_params": COMMON_PARAMS, "available": available}
        return result

    async def run(
        self,
        *,
        processor: str,
        source_path: Path | None,
        target_path: Path,
        output_dir: Path,
        params: dict[str, Any],
        log: Callable[[str], Awaitable[None]],
    ) -> Path:
        if processor not in PROCESSOR_SPECS:
            raise ValueError("不支持的人物处理功能")
        spec = PROCESSOR_SPECS[processor]
        if spec.get("source_required") and not source_path:
            raise ValueError(f"{spec['label']}需要上传来源素材")
        if not target_path.is_file():
            raise FileNotFoundError("目标素材不存在")

        output_dir.mkdir(parents=True, exist_ok=True)
        normalized_target = await self._normalize_target(
            processor, target_path, output_dir
        )
        output_path = output_dir / f"result{normalized_target.suffix.lower()}"

        command = [
            str(self.settings.facefusion_python),
            "facefusion.py",
            "headless-run",
        ]
        if source_path:
            command += ["--source-paths", str(source_path)]
        command += [
            "--target-path", str(normalized_target),
            "--output-path", str(output_path),
            "--processors", processor,
            "--execution-providers", "cuda",
            "--execution-device-ids", "0",
            "--execution-thread-count", "4",
            "--video-memory-strategy", "strict",
            "--face-selector-mode", str(params.get("face_selector_mode", "one")),
            "--log-level", "info",
        ]

        mask_types = params.get("face_mask_types", ["box"])
        if isinstance(mask_types, str):
            mask_types = [item for item in mask_types.split(",") if item]
        if mask_types:
            command += ["--face-mask-types", *[str(item) for item in mask_types]]

        allowed = {p["name"]: p for p in spec.get("params", [])}
        for name, definition in allowed.items():
            value = params.get(name, definition.get("default"))
            if value is None or value == "":
                continue
            flag = "--" + name.replace("_", "-")
            if isinstance(value, list):
                command += [flag, *[str(item) for item in value]]
            elif name == "background_remover_color":
                command += [flag, *str(value).split()]
            else:
                command += [flag, str(value)]

        quality = int(params.get("output_quality", 95))
        if is_image(normalized_target):
            command += ["--output-image-quality", str(quality)]
        else:
            command += ["--output-video-quality", str(quality)]

        await log("执行命令：" + " ".join(command))
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.settings.facefusion_dir),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert process.stdout is not None
            async with asyncio.timeout(self.settings.facefusion_task_timeout_seconds):
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    await log(line.decode("utf-8", errors="replace").rstrip())
                return_code = await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("FaceFusion 处理超时，已终止进程")

        if return_code != 0:
            raise RuntimeError(f"FaceFusion 返回码：{return_code}")
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("FaceFusion 未生成输出文件")
        return output_path

    async def _normalize_target(
        self, processor: str, target: Path, output_dir: Path
    ) -> Path:
        if not is_image(target):
            return target
        # 统一为 PNG，解决 FaceFusion 3.8 对 .jpg/.jpeg 扩展名严格匹配的问题；
        # 背景移除也可保留 Alpha 通道。
        normalized = output_dir / "target.png"
        with Image.open(target) as image:
            image.convert("RGBA").save(normalized)
        return normalized

    async def _headless_help(self) -> str:
        if self._help_cache is not None:
            return self._help_cache
        process = await asyncio.create_subprocess_exec(
            str(self.settings.facefusion_python),
            "facefusion.py",
            "headless-run",
            "--help",
            cwd=str(self.settings.facefusion_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        self._help_cache = stdout.decode("utf-8", errors="replace")
        return self._help_cache
