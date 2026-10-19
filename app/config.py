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
    llm_start_timeout: int = 300

    comfyui_dir: Path = Path("/root/autodl-tmp/ComfyUI")
    comfyui_url: str = "http://127.0.0.1:8188"
    comfyui_timeout: int = 3600

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
    s.media_dir.mkdir(parents=True, exist_ok=True)
    return s
