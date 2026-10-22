"""ComfyUI API 格式工作流（节点结构对齐 ComfyUI 官方模板）。

图片：Qwen-Image 2.1（image_qwen_image_2_1_turbo_t2i / _image_edit）
视频：MiniMax H3（video_minimax_h3_i2v / video_minimax_h3_r2v）
"""
from __future__ import annotations

import random

from .config import get_settings

QWEN_TURBO_SIGMAS = "1.0, 0.978453, 0.95418, 0.926626, 0.89508, 0.845148, 0.704534, 0.414568, 0"

IMAGE_SIZES = {"16:9": (1664, 928), "9:16": (928, 1664), "1:1": (1344, 1344), "4:3": (1472, 1104), "3:4": (1104, 1472),
               "2:3": (1024, 1536), "3:2": (1536, 1024), "21:9": (1904, 816)}
# 画幅：名称、用途（新建项目的图标选择器用）
ASPECT_INFO = {"9:16": ("竖屏", "抖音 / 快手 / 竖屏短剧"), "16:9": ("横屏", "B 站 / YouTube / 电视"),
               "1:1": ("方形", "朋友圈 / 封面"), "3:4": ("竖版", "小红书"), "4:3": ("横版", "经典画幅"),
               "2:3": ("竖版", "海报 / 漫画竖页"), "3:2": ("横版", "照片 / 漫画横页"), "21:9": ("宽银幕", "电影感大片")}
# 「智能」画幅：按题材 / 平台关键词推荐（命中越靠前越优先）
ASPECT_RULES = [("21:9", ("宽银幕", "电影大片", "大片感", "史诗", "院线", "21:9")),
                ("3:4", ("小红书", "3:4")),
                ("16:9", ("横屏", "b站", "bilibili", "youtube", "西瓜视频", "电视", "16:9", "纪录片", "宣传片")),
                ("1:1", ("方形", "朋友圈", "1:1")),
                ("9:16", ("竖屏", "抖音", "快手", "视频号", "短剧", "tiktok", "9:16"))]


def suggest_aspect(*texts: str) -> tuple[str, str]:
    """智能画幅：返回 (画幅, 理由)。没有线索时默认竖屏 9:16（短剧主流）。"""
    blob = " ".join(t or "" for t in texts).lower()
    for aspect, words in ASPECT_RULES:
        hit = next((w for w in words if w.lower() in blob), None)
        if hit:
            return aspect, f"内容里提到「{hit}」，推荐 {aspect} {ASPECT_INFO[aspect][0]}（{ASPECT_INFO[aspect][1]}）"
    return "9:16", "没有找到平台 / 画幅线索，按短剧主流推荐 9:16 竖屏"


VIDEO_SIZES = {
    "draft": {"16:9": (864, 480), "9:16": (480, 864), "1:1": (640, 640), "4:3": (768, 576), "3:4": (576, 768),
              "2:3": (512, 768), "3:2": (768, 512), "21:9": (1120, 480)},
    "final": {"16:9": (1344, 768), "9:16": (768, 1344), "1:1": (992, 992), "4:3": (1152, 864), "3:4": (864, 1152),
              "2:3": (768, 1152), "3:2": (1152, 768), "21:9": (1792, 768)},
}
MAX_IMAGE_REFS = 10
MAX_VIDEO_REFS = 9


def seed_or_random(seed: int | None) -> int:
    return seed if seed is not None and seed >= 0 else random.randint(1, 2**48)


def h3_length(seconds: float) -> int:
    """秒数 → H3 帧数（24fps，对齐 17k+5 网格，与官方模板公式一致）。"""
    frames = max(5, round(float(seconds) * 24))
    return frames + (5 - frames % 17) % 17


def _qwen_sampling(nodes: dict, positive: list, negative: list, latent: list, seed: int,
                   lora: tuple[str, float] | None = None) -> None:
    s = get_settings()
    turbo = "turbo" in s.image_unet
    base = ["1", 0]
    if lora:                     # 项目风格 LoRA（画风锁定）
        nodes["7"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": ["1", 0], "lora_name": lora[0], "strength_model": float(lora[1])}}
        base = ["7", 0]
    nodes.update({
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": s.image_unet, "weight_dtype": "default"}},
        "4": {"class_type": "QwenImage21Cache", "inputs": {"model": base, "device": "auto", "dtype": "default"}},
        "20": {"class_type": "CFGGuider", "inputs": {"model": ["4", 0], "positive": positive,
                                                     "negative": negative, "cfg": 1.0}},
        "21": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "22": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "23": ({"class_type": "ManualSigmas", "inputs": {"sigmas": QWEN_TURBO_SIGMAS}} if turbo else
               {"class_type": "BasicScheduler", "inputs": {"model": ["4", 0], "scheduler": "simple",
                                                           "steps": 25, "denoise": 1.0}}),
        "24": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["21", 0], "guider": ["20", 0], "sampler": ["22", 0], "sigmas": ["23", 0], "latent_image": latent}},
        "25": {"class_type": "VAEDecode", "inputs": {"samples": ["24", 0], "vae": ["3", 0]}},
        "26": {"class_type": "SaveImage", "inputs": {"images": ["25", 0], "filename_prefix": "studio/img"}},
    })


def image_workflow(prompt: str, aspect: str, refs: list[str] | None = None, seed: int | None = None,
                   size: tuple[int, int] | None = None, lora: tuple[str, float] | None = None) -> dict:
    """文生图（refs 为空）或多图参考生图（refs = 已上传到 ComfyUI 的图片名，按 <image1>.. 顺序）。"""
    s = get_settings()
    width, height = size or IMAGE_SIZES.get(aspect, IMAGE_SIZES["16:9"])
    refs = list(refs or [])[:MAX_IMAGE_REFS]
    encode = {"clip": ["2", 0], "prompt": prompt, "negative_prompt": "", "resolution": 1024}
    nodes: dict = {
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": s.image_clip, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": s.image_vae}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
    }
    if refs:
        encode["vae"] = ["3", 0]
        for i, name in enumerate(refs, start=1):
            node_id = str(100 + i)
            nodes[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
            encode[f"images.image_{i}"] = [node_id, 0]
    nodes["5"] = {"class_type": "TextEncodeQwenImage21", "inputs": encode}
    _qwen_sampling(nodes, ["5", 0], ["5", 1], ["6", 0], seed_or_random(seed), lora)
    return nodes


def video_cache_threshold(quality: str, steps: int, cache: float | None = None) -> float:
    """视频推理缓存阈值：草稿默认开、成品默认关；步数太少（4 步 Turbo）时缓存没有意义。"""
    s = get_settings()
    th = cache if cache is not None else (s.video_cache if quality == "draft" else s.video_cache_final)
    return th if th and th > 0 and steps >= 6 else 0.0


def _h3_common(nodes: dict, unet: str, lora: str | None, steps: int, seed: int, cache: float = 0.0) -> list:
    s = get_settings()
    nodes.update({
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": s.video_clip, "type": "minimax", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": s.video_vae}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": s.video_audio_vae}},
        "21": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "22": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
    })
    model = ["1", 0]
    if lora:
        nodes["7"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": ["1", 0], "lora_name": lora, "strength_model": 1.0}}
        model = ["7", 0]
    if cache > 0:
        nodes["9"] = {"class_type": "EasyCache", "inputs": {"model": model, "reuse_threshold": round(cache, 3),
                                                            "start_percent": 0.15, "end_percent": 0.95, "verbose": False}}
        model = ["9", 0]
    nodes["20"] = {"class_type": "BasicGuider", "inputs": {"model": model, "conditioning": ["10", 0]}}
    nodes["23"] = {"class_type": "BasicScheduler", "inputs": {"model": model, "scheduler": "simple",
                                                              "steps": steps, "denoise": 1.0}}
    nodes["24"] = {"class_type": "SamplerCustomAdvanced", "inputs": {
        "noise": ["21", 0], "guider": ["20", 0], "sampler": ["22", 0], "sigmas": ["23", 0], "latent_image": ["10", 1]}}
    nodes["25"] = {"class_type": "VAEDecode", "inputs": {"samples": ["24", 0], "vae": ["3", 0]}}
    nodes["26"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["24", 0], "vae": ["4", 0]}}
    nodes["27"] = {"class_type": "CreateVideo", "inputs": {"images": ["25", 0], "audio": ["26", 0], "fps": 24.0}}
    nodes["28"] = {"class_type": "SaveVideo", "inputs": {"video": ["27", 0], "filename_prefix": "studio/video",
                                                         "format": "auto", "codec": "auto"}}
    return model


def video_i2v_workflow(prompt: str, first_frame: str | None, aspect: str, seconds: float,
                       quality: str = "draft", turbo: bool = True, last_frame: str | None = None,
                       seed: int | None = None, cache: float | None = None) -> dict:
    s = get_settings()
    width, height = VIDEO_SIZES[quality].get(aspect, VIDEO_SIZES[quality]["16:9"])
    nodes: dict = {}
    steps = 8 if turbo else 20
    _h3_common(nodes, s.video_i2v_unet, s.video_i2v_turbo_lora if turbo else None,
               steps, seed_or_random(seed), video_cache_threshold(quality, steps, cache))
    inputs = {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "width": width, "height": height,
              "length": h3_length(seconds)}
    if first_frame:
        nodes["11"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
        inputs["first_frame"] = ["11", 0]
    if last_frame:
        nodes["12"] = {"class_type": "LoadImage", "inputs": {"image": last_frame}}
        inputs["last_frame"] = ["12", 0]
    nodes["10"] = {"class_type": "MiniMaxH3ImageToVideo", "inputs": inputs}
    return nodes


def video_r2v_workflow(prompt: str, refs: list[str], aspect: str, seconds: float,
                       quality: str = "draft", turbo: bool = False, seed: int | None = None,
                       cache: float | None = None) -> dict:
    s = get_settings()
    width, height = VIDEO_SIZES[quality].get(aspect, VIDEO_SIZES[quality]["16:9"])
    nodes: dict = {}
    steps = 4 if turbo else 20
    _h3_common(nodes, s.video_r2v_unet, s.video_r2v_turbo_lora if turbo else None,
               steps, seed_or_random(seed), video_cache_threshold(quality, steps, cache))
    inputs = {"clip": ["2", 0], "vae": ["3", 0], "audio_vae": ["4", 0], "prompt": prompt,
              "width": width, "height": height, "length": h3_length(seconds), "ref_image_size": "match"}
    for i, name in enumerate(refs[:MAX_VIDEO_REFS]):
        node_id = str(100 + i)
        nodes[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        inputs[f"ref_images.ref_image_{i}"] = [node_id, 0]
    nodes["10"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": inputs}
    return nodes


def required_models() -> dict[str, list[str]]:
    s = get_settings()
    return {
        "diffusion_models": [s.image_unet, s.video_i2v_unet],
        "text_encoders": [s.image_clip, s.video_clip],
        "vae": [s.image_vae, s.video_vae, s.video_audio_vae],
        "loras": [s.video_i2v_turbo_lora],
    }


def optional_models() -> dict[str, list[str]]:
    s = get_settings()
    return {"diffusion_models": [s.video_r2v_unet], "loras": [s.video_r2v_turbo_lora]}
