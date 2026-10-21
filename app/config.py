from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    base_dir: Path = Path("/root/autodl-tmp")
    data_dir: Path = Path("/root/autodl-tmp/data/studio")
    host: str = "0.0.0.0"
    port: int = 6006

    llm_provider: str = "local"  # local | api
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_api_key: str = "none"
    llm_model: str = "qwen3.6-35b-a3b"
    llm_gguf: Path = Path("/root/autodl-tmp/models/llm/Qwen_Qwen3.6-35B-A3B-Q4_K_M.gguf")
    llm_ctx: int = 32768
    llama_server_bin: Path = Path("/root/autodl-tmp/llama.cpp/build/bin/llama-server")
    llm_timeout: int = 600
    llm_mmproj: str = ""                   # 视觉投影 GGUF（mmproj）；设置后 llama-server 带视觉，审片用同一个模型
    vlm_provider: str = "auto"             # auto | llm（本地 llama-server + mmproj）| api（独立的视觉模型接口）| off
    vlm_base_url: str = ""
    vlm_api_key: str = "none"
    vlm_model: str = ""
    vlm_auto: bool = True                  # 视觉接口是远程 API 时，出图/出视频后自动审（本地模型只做批量审片，避免频繁切换显存）
    vlm_pass_score: int = 6                # 低于此分判为不合格
    asr_python: str = ""                   # 装了 faster-whisper 的 Python（音画核对）；空 = 不做
    asr_model: str = "small"
    music_ckpt: str = "ace_step_v1_3.5b.safetensors"   # ACE-Step 配乐模型（ComfyUI models/checkpoints）
    llm_start_timeout: int = 300

    comfyui_dir: Path = Path("/root/autodl-tmp/ComfyUI")
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_timeout: int = 3600
    comfyui_restart_cmd: str = ""      # 自动恢复时重启 ComfyUI 的命令，默认 deploy/start_comfyui.sh --restart
    auto_recover: bool = True          # 失败自动恢复重试（显存不足/服务崩溃/超时）

    image_unet: str = "qwen_image_2.1_turbo_int8_convrot.safetensors"
    image_clip: str = "qwen3vl_8b_int8_convrot.safetensors"
    image_vae: str = "qwen_image_2.1_vae_bf16.safetensors"

    video_i2v_unet: str = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    video_r2v_unet: str = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    video_clip: str = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
    video_vae: str = "minimax_h3_video_vae_int8_convrot.safetensors"
    video_audio_vae: str = "minimax_h3_audio_vae_fp32.safetensors"
    video_i2v_turbo_lora: str = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
    video_r2v_turbo_lora: str = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"

    facefusion_dir: Path = Path("/root/autodl-tmp/facefusion")
    facefusion_python: Path = Path("/root/autodl-tmp/envs/facefusion/bin/python")
    facefusion_timeout: int = 3600

    # 访问密码：留空则不校验（部署脚本会自动生成一个写进 .env）
    access_password: str = ""

    # 自动质检：每次出分镜图/视频后检查「多余字幕文字」和「人脸是否像角色」，不合格自动换种子重抽
    qa_enabled: bool = True
    qa_python: Path | None = None          # 默认复用 FaceFusion 的环境（有 onnxruntime + opencv）
    camera_loras: str = ""                 # 运镜 LoRA，JSON：{"推镜": "push_in.safetensors", ...}（放 ComfyUI/models/loras）
    motion_workflow: str = ""              # 动作参考工作流（ComfyUI API JSON，含 {{PROMPT}} {{IMAGE}} {{VIDEO}} 等占位符）
    qa_video_retry: int = 1                # 视频不合格时额外重抽次数（每次约 2~3 分钟）
    qa_keyframe_retry: int = 1
    qa_face_threshold: float = 0.25        # ArcFace 余弦相似度，低于它判为「不像」

    # 配音与对口型（deploy/06_voice.sh 安装）：IndexTTS2 固定音色配音 + LatentSync 1.6 对口型 + Demucs 去人声
    tts_dir: Path | None = None
    tts_python: Path | None = None
    lipsync_dir: Path | None = None
    lipsync_python: Path | None = None
    lipsync_steps: int = 20
    voice_timeout: int = 1800
    # 画质增强（deploy/10_enhance.sh）：超分用 ComfyUI 的 Python 环境（torch + spandrel）逐帧流式处理
    comfyui_python: Path | None = None
    upscale_model: str = "realesr-animevideov3.pth"     # ComfyUI/models/upscale_models 里的文件
    rife_ckpt: str = "rife47.pth"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studio.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def log_dir(self) -> Path:
        return self.base_dir / "logs"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    envs = s.base_dir / "envs"
    defaults = {"qa_python": s.facefusion_python, "tts_dir": s.base_dir / "index-tts",
                "tts_python": envs / "tts" / "bin" / "python", "lipsync_dir": s.base_dir / "LatentSync",
                "lipsync_python": envs / "lipsync" / "bin" / "python",
                "comfyui_python": envs / "comfyui" / "bin" / "python"}
    for key, value in defaults.items():
        if getattr(s, key) is None:
            setattr(s, key, value)
    s.media_dir.mkdir(parents=True, exist_ok=True)
    return s
