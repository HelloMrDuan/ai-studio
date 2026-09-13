from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings
from app.services.comfyui import ZIMAGE_TURBO_WORKFLOW_PATH

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


def _comfy_reference_profile(settings: Settings) -> dict[str, Any] | None:
    """Load an operator-proven Comfy reference workflow profile."""
    profile_path = Path(settings.data_dir) / "comfyui_reference_profile.v3.json"
    if not profile_path.is_file():
        return None
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("comfyui_reference_profile.v3.json must be an object")
    workflow_path = Path(str(raw.get("reference_workflow_path") or ""))
    bindings = raw.get("reference_bindings")
    contract_bindings = raw.get("contract_bindings") or []
    mode = str(raw.get("multi_reference_mode") or "static").strip().lower()
    if not workflow_path.is_file():
        raise ValueError(f"configured Comfy reference workflow does not exist: {workflow_path}")
    if not isinstance(bindings, list):
        raise ValueError("Comfy reference profile reference_bindings must be an array")
    if not isinstance(contract_bindings, list):
        raise ValueError("Comfy reference profile contract_bindings must be an array")
    max_refs = int(raw.get("max_references") or len(bindings) or 1)
    if max_refs < 1:
        raise ValueError("Comfy reference profile max_references must be positive")
    if mode == "role_aware_chain":
        if max_refs < 2:
            raise ValueError("role-aware multi-reference profile must support at least two references")
    else:
        if not bindings:
            raise ValueError("static Comfy reference profile requires reference_bindings")
        if len(bindings) < max_refs:
            raise ValueError("Comfy reference profile has insufficient binding slots")
    return {
        "profile_path": str(profile_path),
        "reference_workflow_path": str(workflow_path),
        "reference_bindings": bindings,
        "contract_bindings": contract_bindings,
        "max_references": max_refs,
        "identity_reference": bool(raw.get("identity_reference")),
        "ip_adapter": bool(raw.get("ip_adapter")),
        "multi_reference_mode": mode,
    }


def platform_provider_specs(settings: Settings) -> list[ProviderModelSpec]:
    """Describe the platform's existing local model services as V3 providers."""
    qwen_capabilities = {Capability.text, Capability.structured_output}
    projector = settings.gemma_mm_projector_path
    if projector and Path(projector).is_file():
        qwen_capabilities.add(Capability.vision)

    comfy_capabilities = {Capability.image_generation}
    comfy_metadata: dict[str, Any] = {
        "source": "existing-platform",
        "workflow_path": str(settings.comfyui_workflow_path),
        "capability_note": "reference capabilities require an operator-proven V3 reference profile",
    }
    comfy_max_refs = None
    reference_profile = _comfy_reference_profile(settings)
    if reference_profile:
        comfy_capabilities.add(Capability.image_reference)
        if int(reference_profile["max_references"]) > 1:
            comfy_capabilities.add(Capability.multi_reference)
        if reference_profile["identity_reference"]:
            comfy_capabilities.add(Capability.identity_reference)
        if reference_profile["ip_adapter"]:
            comfy_capabilities.add(Capability.ip_adapter)
        comfy_max_refs = int(reference_profile["max_references"])
        comfy_metadata.update(reference_profile)
        comfy_metadata["capability_note"] = "reference workflow/profile is configured and explicit"

    return [
        ProviderModelSpec(
            provider_id="local-qwen",
            model_id=str(settings.stage04_required_model_alias or settings.gemma_model),
            transport=ProviderTransport.local_openai_compatible,
            capabilities=qwen_capabilities,
            base_url=str(settings.gemma_base_url).rstrip("/"),
            priority=10,
            metadata={"source": "existing-platform", "adapter": "openai-compatible"},
        ),
        ProviderModelSpec(
            provider_id="local-zimage-image",
            model_id="z-image-turbo",
            transport=ProviderTransport.local_comfyui,
            capabilities={Capability.image_generation},
            base_url=str(settings.comfyui_base_url).rstrip("/"),
            priority=5,
            metadata={
                "source": "existing-platform",
                "adapter": "z-image-turbo-api-workflow",
                "workflow_path": str(ZIMAGE_TURBO_WORKFLOW_PATH),
                "reference_mode": "none",
                "prompt_contract": "provider_ready_frozen",
            },
        ),
        ProviderModelSpec(
            provider_id="local-comfyui-image",
            model_id="configured-image-workflow",
            transport=ProviderTransport.local_comfyui,
            capabilities=comfy_capabilities,
            base_url=str(settings.comfyui_base_url).rstrip("/"),
            priority=10,
            max_references=comfy_max_refs,
            metadata=comfy_metadata,
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
            metadata={"source": "existing-platform", "adapter": "v3-h3-workflow-compiler"},
        ),
    ]


def load_user_provider_specs(path: Path) -> list[ProviderModelSpec]:
    """Load user-owned local or API providers from a data-dir config."""
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
