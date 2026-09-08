from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings

from .contracts import Capability, ProviderModelSpec, ProviderTransport
from .provider_gateway import ProviderRegistry


def _capabilities(values: list[str] | tuple[str, ...]) -> set[Capability]:
    result: set[Capability] = set()
    for value in values:
        try:
            result.add(Capability(str(value)))
        except ValueError as exc:
            raise ValueError(f"unknown provider capability: {value}") from exc
    return result


def platform_provider_specs(settings: Settings) -> list[ProviderModelSpec]:
    """Describe the platform's existing local model services as V3 providers."""
    qwen_capabilities = {Capability.text, Capability.structured_output}
    projector = settings.gemma_mm_projector_path
    if projector and Path(projector).is_file():
        qwen_capabilities.add(Capability.vision)

    return [
        ProviderModelSpec(
            provider_id="local-qwen",
            model_id=str(settings.stage04_required_model_alias or settings.gemma_model),
            transport=ProviderTransport.local_openai_compatible,
            capabilities=qwen_capabilities,
            base_url=str(settings.gemma_base_url).rstrip("/"),
            priority=10,
            metadata={"source": "existing-platform", "legacy_service": "GemmaService"},
        ),
        ProviderModelSpec(
            provider_id="local-comfyui-image",
            model_id="configured-image-workflow",
            transport=ProviderTransport.local_comfyui,
            capabilities={Capability.image_generation},
            base_url=str(settings.comfyui_base_url).rstrip("/"),
            priority=10,
            metadata={
                "source": "existing-platform",
                "workflow_path": str(settings.comfyui_workflow_path),
                "capability_note": "reference capabilities stay disabled until the active workflow proves them",
            },
        ),
        ProviderModelSpec(
            provider_id="local-h3-video",
            model_id="minimax-h3",
            transport=ProviderTransport.local_comfyui,
            capabilities={
                Capability.video_generation,
                Capability.image_reference,
                Capability.first_frame,
                Capability.last_frame,
                Capability.first_last_frame,
            },
            base_url=str(settings.comfyui_base_url).rstrip("/"),
            priority=10,
            metadata={"source": "existing-platform", "legacy_service": "H3VideoService"},
        ),
    ]


def load_user_provider_specs(path: Path) -> list[ProviderModelSpec]:
    """Load user-owned local or API providers from a data-dir config.

    Secrets stay as `secret_ref`; this loader never reads secret values.
    """
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        raise ValueError("providers.v3.json must contain a models array")
    specs: list[ProviderModelSpec] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            raise ValueError("provider model entry must be an object")
        payload: dict[str, Any] = dict(raw)
        payload["capabilities"] = _capabilities(payload.get("capabilities") or [])
        payload["transport"] = ProviderTransport(str(payload.get("transport") or ""))
        specs.append(ProviderModelSpec.model_validate(payload))
    return specs


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    specs = platform_provider_specs(settings)
    specs.extend(load_user_provider_specs(Path(settings.data_dir) / "providers.v3.json"))
    return ProviderRegistry(specs)
