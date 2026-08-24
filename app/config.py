from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    platform_host: str = "0.0.0.0"
    platform_port: int = 6008
    data_dir: Path = Path("/root/autodl-tmp/ai-studio/data/platform-v2")
    max_upload_mb: int = 2000
    task_workers: int = 1

    gemma_base_url: str = "http://127.0.0.1:6006/v1"
    gemma_model: str = "gemma"
    gemma_timeout_seconds: int = 300
    gemma_start_command: str = (
        "bash /root/autodl-tmp/ai-studio/platform-v2/scripts/start_gemma.sh"
    )
    gemma_stop_command: str = (
        "bash /root/autodl-tmp/ai-studio/platform-v2/scripts/stop_gemma.sh"
    )
    gemma_start_timeout_seconds: int = 600
    gemma_stop_timeout_seconds: int = 90
    gemma_compiler_cache_max_entries: int = 256
    gemma_mm_projector_path: Path | None = None
    gemma_multimodal_max_mb: int = 20
    llm_registry_path: Path = Path(
        "/root/autodl-tmp/ai-studio/platform-v2/config/llm_models.json"
    )
    llm_selection_path: Path = Path(
        "/root/autodl-tmp/ai-studio/data/platform-v2/llm_selection.json"
    )
    director_skill_root: Path = Path(
        "/root/autodl-tmp/ai-studio/skills/chuanzhangAIshijie"
    )
    director_manifest_path: Path = Path(
        "/root/autodl-tmp/ai-studio/platform-v2/config/director_skills.json"
    )
    director_projects_dir: Path = Path(
        "/root/autodl-tmp/ai-studio/data/platform-v2/director-projects"
    )
    director_source_context_max_chars: int = 28000

    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_workflow_path: Path = Path(
        "/root/autodl-tmp/ai-studio/platform-v2/workflows/sdxl_api.json"
    )
    comfyui_checkpoint: str = "lustifySDXLNSFWSFW_v20.safetensors"
    comfyui_start_command: str = "bash /root/autodl-tmp/ai-studio/start_comfy.sh"
    comfyui_stop_command: str = "bash /root/autodl-tmp/ai-studio/stop_comfy.sh"
    comfyui_start_timeout_seconds: int = 300
    comfyui_stop_timeout_seconds: int = 90
    comfyui_task_timeout_seconds: int = 1200
    h3_comfyui_dir: Path = Path("/root/autodl-tmp/ai-studio/ComfyUI")
    h3_fl2va_model: str = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
    h3_ref2va_model: str = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    h3_text_encoder: str = "qwen3vl_32b_h3_ultra_uncensored_heretic_int8_convrot.safetensors"
    h3_video_vae: str = "minimax_h3_video_vae_fp16.safetensors"
    h3_audio_vae: str = "minimax_h3_audio_vae_fp32.safetensors"
    h3_task_timeout_seconds: int = 7200
    h3_sampler: str = "euler"
    h3_scheduler: str = "simple"

    facefusion_dir: Path = Path("/root/autodl-tmp/ai-studio/facefusion")
    facefusion_python: Path = Path(
        "/root/autodl-tmp/envs/ai-studio-facefusion/bin/python"
    )
    facefusion_task_timeout_seconds: int = 1800
    facefusion_stop_command: str = "pkill -f '[f]acefusion.py (headless-run|job-run|batch-run)' || true"

    gpu_device_id: int = 0
    gpu_min_free_mb: int = 8000
    gpu_release_stable_samples: int = 3
    gpu_release_poll_seconds: float = 2.0
    gpu_drain_timeout_seconds: int = 1800
    gpu_switch_timeout_seconds: int = 900
    llm_startup_timeout_margin_seconds: int = 60
    gpu_default_owner: str = "gemma"

    stage04_required_model_id: str = "qwen3-32b-abliterated"
    stage04_required_model_alias: str = "qwen3-32b"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_llm_timeout_contract(self) -> "Settings":
        minimum = (
            self.gemma_start_timeout_seconds
            + self.llm_startup_timeout_margin_seconds
        )
        if self.gpu_switch_timeout_seconds < minimum:
            raise ValueError(
                "GPU_SWITCH_TIMEOUT_SECONDS 必须大于等于 "
                "GEMMA_START_TIMEOUT_SECONDS + "
                "LLM_STARTUP_TIMEOUT_MARGIN_SECONDS："
                f"{self.gpu_switch_timeout_seconds} < "
                f"{self.gemma_start_timeout_seconds} + "
                f"{self.llm_startup_timeout_margin_seconds}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
