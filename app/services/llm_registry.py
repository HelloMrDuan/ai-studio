from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.config import Settings


class LLMRegistryService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry_path = Path(settings.llm_registry_path)
        self.selection_path = Path(settings.llm_selection_path)
        self._lock = asyncio.Lock()

    def _load_registry(self) -> dict[str, Any]:
        data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        models = data.get("models")
        if not isinstance(models, list) or not models:
            raise RuntimeError("LLM 模型注册表为空")
        return data

    def _models_by_id(self) -> dict[str, dict[str, Any]]:
        registry = self._load_registry()
        result: dict[str, dict[str, Any]] = {}
        for item in registry["models"]:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id:
                result[model_id] = dict(item)
        return result

    def _selected_id_unlocked(self) -> str:
        registry = self._load_registry()
        default_id = str(registry.get("default_model") or "").strip()
        try:
            data = json.loads(self.selection_path.read_text(encoding="utf-8"))
            selected = str(data.get("selected_model") or "").strip()
        except Exception:
            selected = ""
        models = self._models_by_id()
        if selected in models:
            return selected
        if default_id in models:
            return default_id
        return next(iter(models))

    def selected_model(self) -> dict[str, Any]:
        models = self._models_by_id()
        selected_id = self._selected_id_unlocked()
        model = dict(models[selected_id])
        model["installed"] = Path(str(model.get("path") or "")).is_file()
        model["selected"] = True
        return model

    def list_models(self, active_alias: str = "") -> dict[str, Any]:
        registry = self._load_registry()
        selected_id = self._selected_id_unlocked()
        items = []
        for raw in registry["models"]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            model_id = str(item.get("id") or "").strip()
            path = Path(str(item.get("path") or ""))
            alias = str(item.get("alias") or "").strip()
            item["installed"] = path.is_file()
            item["size_bytes"] = path.stat().st_size if path.is_file() else 0
            item["selected"] = model_id == selected_id
            item["active"] = bool(active_alias and alias == active_alias)
            items.append(item)
        return {
            "schema_version": registry.get("schema_version", "1.0"),
            "default_model": registry.get("default_model"),
            "selected_model": selected_id,
            "active_alias": active_alias,
            "models": items,
        }

    async def select(self, model_id: str) -> dict[str, Any]:
        async with self._lock:
            models = self._models_by_id()
            if model_id not in models:
                raise KeyError(f"未知 LLM 模型：{model_id}")
            model = dict(models[model_id])
            path = Path(str(model.get("path") or ""))
            if not path.is_file():
                raise FileNotFoundError(f"LLM 模型文件不存在：{path}")
            self.selection_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.selection_path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {"selected_model": model_id},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            temp.replace(self.selection_path)
            model["installed"] = True
            model["selected"] = True
            return model
