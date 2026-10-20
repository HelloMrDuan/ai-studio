"""FaceFusion 封装：命令行 headless-run，支持人物与画面处理全部功能。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from .config import get_settings

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
            {"name": "background_remover_fill_color", "label": "背景 RGBA", "type": "text",
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


# 不在工具箱界面上展示、但程序内部会用到的人脸选择参数（人脸修正按位置/大小挑人）
INTERNAL_PARAMS = {"face_selector_order", "reference_face_position", "reference_face_distance",
                   "reference_frame_number"}


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg",".jpeg",".png",".webp",".bmp",".tif",".tiff"}


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def public_specs() -> dict[str, Any]:
    return PROCESSOR_SPECS


def common_params() -> list[dict[str, Any]]:
    return COMMON_PARAMS


async def run(processor: str, target: Path, output: Path, source: Path | None = None,
              params: dict[str, Any] | None = None, log=None) -> Path:
    """processor 可以是多个处理器，用空格分隔（如 "face_swapper face_enhancer"，按顺序串联处理）。"""
    s = get_settings()
    names = processor.split()
    specs = [PROCESSOR_SPECS.get(n) for n in names]
    if not names or None in specs:
        raise ValueError(f"不支持的处理功能：{processor}")
    for spec in specs:
        if spec.get("source_required") and source is None:
            raise ValueError(f"{spec['label']} 需要来源文件")
    if not s.facefusion_python.is_file():
        raise RuntimeError(f"找不到 FaceFusion Python：{s.facefusion_python}")
    params = params or {}
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(s.facefusion_python), "facefusion.py", "headless-run",
           "--processors", *names, "--target-path", str(target), "--output-path", str(output),
           "--execution-providers", "cuda", "--execution-thread-count", "4",
           "--face-selector-mode", str(params.pop("face_selector_mode", "many")), "--log-level", "info"]
    if source is not None:
        cmd += ["--source-paths", str(source)]
    allowed = {p["name"] for spec in specs for p in spec.get("params", [])} | {p["name"] for p in COMMON_PARAMS} \
        | INTERNAL_PARAMS
    for name, value in params.items():
        if name not in allowed or value in (None, ""):
            continue
        if name == "output_quality":  # FaceFusion 3.x 区分图片/视频质量参数
            name = "output_image_quality" if is_image(target) else "output_video_quality"
        if name.endswith("_color") and isinstance(value, str):
            value = value.split()
        flag = "--" + name.replace("_", "-")
        if isinstance(value, list):
            cmd += [flag, *[str(v) for v in value]]
        else:
            cmd += [flag, str(value)]
    env = dict(os.environ)
    # FaceFusion 检测到 CONDA_PREFIX 时会把 conda 的 lib 塞进 LD_LIBRARY_PATH，
    # 导致系统 ffmpeg/ffprobe 加载到不兼容的 libstdc++（GLIBCXX 缺失）→ 图片/视频处理失败
    conda_prefix = env.get("CONDA_PREFIX", "")
    for k in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PYTHON_EXE", "CONDA_EXE", "CONDA_PROMPT_MODIFIER"):
        env.pop(k, None)
    if env.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = ":".join(p for p in env["LD_LIBRARY_PATH"].split(":")
                                          if p and not (conda_prefix and p.startswith(conda_prefix)))
    env_root = s.facefusion_python.parent.parent
    nv_libs = sorted(str(p) for p in env_root.glob("lib/python3*/site-packages/nvidia/*/lib"))
    env["LD_LIBRARY_PATH"] = ":".join([*nv_libs, str(env_root / "lib"), env.get("LD_LIBRARY_PATH", "")])
    env["PATH"] = f"{env_root / 'bin'}:{env.get('PATH', '')}"
    proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, cwd=str(s.facefusion_dir), env=env,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    lines: list[str] = []
    try:
        async def reader():
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                lines.append(line)
                if log and line:
                    await log(line[-200:])
        await asyncio.wait_for(asyncio.gather(reader(), proc.wait()), timeout=s.facefusion_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("FaceFusion 处理超时")
    if proc.returncode != 0 or not output.is_file():
        raise RuntimeError("FaceFusion 处理失败：" + " | ".join(lines[-8:]))
    return output
