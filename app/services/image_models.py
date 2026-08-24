from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ImageModelRegistry:
    """Configuration-driven image checkpoint and appearance-profile registry."""

    def __init__(self, config_path: Path | None = None) -> None:
        platform_root = Path(__file__).resolve().parents[2]
        self.config_path = config_path or platform_root / "config" / "image_models.json"
        self.model_dirs = (
            Path("/root/autodl-tmp/models/image/checkpoints"),
            Path("/root/autodl-tmp/ai-studio/ComfyUI/models/checkpoints"),
        )
        self.lora_dirs = (
            Path("/root/autodl-tmp/models/image/loras"),
            Path("/root/autodl-tmp/ai-studio/ComfyUI/models/loras"),
        )

    def _load(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise FileNotFoundError(f"图片模型注册文件不存在：{self.config_path}")
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
            raise RuntimeError("图片模型注册文件格式错误")
        profiles = payload.get("appearance_profiles", {})
        if profiles is not None and not isinstance(profiles, dict):
            raise RuntimeError("人物外貌配置格式错误")
        return payload

    @staticmethod
    def _names(spec: dict[str, Any]) -> list[str]:
        values = [str(spec.get("checkpoint", "")).strip()]
        aliases = spec.get("aliases", [])
        if isinstance(aliases, list):
            values.extend(str(item).strip() for item in aliases)
        return list(dict.fromkeys(value for value in values if value))

    @staticmethod
    def _locate_named_file(names: list[str], directories: tuple[Path, ...]) -> Path | None:
        lowered = {Path(name).name.lower() for name in names if name}
        for directory in directories:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                try:
                    if path.name.lower() in lowered and path.is_file() and path.stat().st_size > 0:
                        return path.resolve()
                except OSError:
                    continue
        return None

    def _locate_model(self, spec: dict[str, Any]) -> Path | None:
        return self._locate_named_file(self._names(spec), self.model_dirs)

    @staticmethod
    def _choice_match_names(names: list[str], choices: list[str]) -> str | None:
        if not choices:
            return None
        by_lower = {Path(str(choice)).name.lower(): str(choice) for choice in choices}
        for name in names:
            matched = by_lower.get(Path(name).name.lower())
            if matched:
                return matched
        return None

    def status(self, comfyui_choices: list[str] | None = None) -> dict[str, Any]:
        payload = self._load()
        choices = comfyui_choices or []
        models: list[dict[str, Any]] = []
        for key, raw in payload["models"].items():
            spec = dict(raw)
            path = self._locate_model(spec)
            choice = self._choice_match_names(self._names(spec), choices)
            file_available = path is not None
            comfy_available = choice is not None if choices else file_available
            available = file_available and comfy_available
            reason = ""
            if not file_available:
                reason = "模型文件未安装"
            elif choices and not choice:
                reason = "ComfyUI 尚未识别该模型"
            models.append(
                {
                    "key": key,
                    "label": str(spec.get("label", key)),
                    "name": str(spec.get("name", key)),
                    "description": str(spec.get("description", "")),
                    "category": str(spec.get("category", "other")),
                    "checkpoint": str(spec.get("checkpoint", "")),
                    "resolved_checkpoint": choice or (path.name if path else ""),
                    "installed_path": str(path) if path else "",
                    "available": available,
                    "reason": reason,
                    "prompt_adapter": str(spec.get("prompt_adapter", "default")),
                    "face_detailer": bool(spec.get("face_detailer", False)),
                    "face_detailer_denoise": float(spec.get("face_detailer_denoise", 0.0)),
                    "upscaler": str(spec.get("upscaler", "4x-UltraSharp.pth")),
                    "steps": int(spec.get("steps", 30)),
                    "cfg": float(spec.get("cfg", 6.0)),
                    "sampler": str(spec.get("sampler", "dpmpp_2m")),
                    "scheduler": str(spec.get("scheduler", "karras")),
                    "is_default": key == payload.get("default_model"),
                }
            )
        return {
            "version": int(payload.get("version", 1)),
            "default_model": str(payload.get("default_model", "lustify")),
            "models": models,
            "installed_count": sum(1 for item in models if item["available"]),
            "total_count": len(models),
        }

    def appearance_status(self, comfyui_choices: list[str] | None = None) -> dict[str, dict[str, Any]]:
        payload = self._load()
        choices = comfyui_choices or []
        output: dict[str, dict[str, Any]] = {}
        for key, raw in payload.get("appearance_profiles", {}).items():
            spec = dict(raw)
            lora_name = str(spec.get("lora_name", "")).strip()
            path = self._locate_named_file([lora_name], self.lora_dirs) if lora_name else None
            choice = self._choice_match_names([lora_name], choices) if lora_name else None
            file_available = path is not None
            comfy_available = choice is not None if choices else file_available
            available = file_available and comfy_available
            reason = ""
            if not lora_name:
                reason = "配置缺少 LoRA 文件名"
            elif not file_available:
                reason = "LoRA 文件未安装"
            elif choices and not choice:
                reason = "ComfyUI 尚未识别该 LoRA"
            supported = spec.get("supported_models", [])
            supported_models = [str(item) for item in supported] if isinstance(supported, list) else []
            output[key] = {
                "key": key,
                "label": str(spec.get("label", key)),
                "description": str(spec.get("description", "")),
                "lora_name": lora_name,
                "resolved_lora": choice or (path.name if path else ""),
                "installed_path": str(path) if path else "",
                "trigger": str(spec.get("trigger", "")).strip(),
                "trigger_weight": float(spec.get("trigger_weight", 0.75)),
                "strength_model": float(spec.get("strength_model", 0.30)),
                "strength_clip": float(spec.get("strength_clip", 0.30)),
                "default_strength": float(spec.get("strength_model", 0.30)),
                "supported_models": supported_models,
                "available": available,
                "reason": reason,
            }
        return output

    def compatible_appearance_profiles(
        self,
        model_key: str,
        comfyui_choices: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return [
            dict(profile)
            for profile in self.appearance_status(comfyui_choices).values()
            if profile.get("available") and model_key in profile.get("supported_models", [])
        ]

    def get_appearance_profile(self, key: str) -> dict[str, Any] | None:
        return self.appearance_status().get(key)

    def resolve_appearance_profile(
        self,
        *,
        profile_key: str,
        model_key: str,
        comfyui_choices: list[str] | None = None,
    ) -> dict[str, Any]:
        profile = self.appearance_status(comfyui_choices).get(profile_key)
        if profile is None:
            raise ValueError("不支持的人物外貌增强配置")
        if model_key not in profile.get("supported_models", []):
            raise ValueError(f"人物外貌增强“{profile['label']}”不支持当前生成模型")
        if not profile.get("available"):
            raise RuntimeError(
                f"人物外貌增强“{profile['label']}”当前不可用：{profile.get('reason') or '未知原因'}"
            )
        return dict(profile)

    def resolve(
        self,
        requested_key: str,
        style_name: str,
        comfyui_choices: list[str] | None = None,
        required_model_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        payload = self._load()
        status = self.status(comfyui_choices)
        by_key = {item["key"]: item for item in status["models"]}

        requested = (requested_key or "smart").strip().lower()
        candidates: list[str] = []
        if requested == "smart":
            routes = payload.get("smart_routes", {})
            style_routes = routes.get(style_name, []) if isinstance(routes, dict) else []
            if isinstance(style_routes, list):
                candidates.extend(str(item) for item in style_routes)
            fallback = payload.get("smart_fallback_order", [])
            if isinstance(fallback, list):
                candidates.extend(str(item) for item in fallback)
            candidates.append(str(payload.get("default_model", "lustify")))
        else:
            if requested not in by_key:
                raise ValueError("不支持的图片生成模型")
            candidates.append(requested)

        required = set(required_model_keys or set())
        for key in dict.fromkeys(candidates):
            if required and key not in required:
                continue
            item = by_key.get(key)
            if item and item["available"]:
                result = dict(item)
                result["requested_key"] = requested
                result["smart_fallback"] = requested == "smart" and key != candidates[0]
                return result

        if requested != "smart":
            item = by_key[requested]
            if required and requested not in required:
                raise RuntimeError(f"模型“{item['label']}”与所选人物外貌增强不兼容")
            raise RuntimeError(f"模型“{item['label']}”当前不可用：{item['reason'] or '未知原因'}")
        if required:
            raise RuntimeError("没有已安装且兼容所选人物外貌增强的生成模型")
        raise RuntimeError("模型池中没有可用模型，请先安装至少一个 checkpoint")
