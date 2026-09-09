from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.prompt_compiler import PromptCompiler


@dataclass(frozen=True)
class MediaGenerationRequest:
    """Provider independent generation request."""

    asset_kind: str
    asset_description: str
    generation_contract: Any
    visual_direction: Any


class MediaGenerationPipeline:
    """Single orchestration layer before image providers.

    Existing providers (ComfyUI etc.) remain unchanged. They only receive
    compiled prompts from this layer.
    """

    def __init__(self) -> None:
        self.prompt_compiler = PromptCompiler()

    def compile_provider_payload(self, request: MediaGenerationRequest) -> dict[str, str]:
        request.generation_contract.validate()
        return self.prompt_compiler.compile(
            asset_kind=request.asset_kind,
            asset_description=request.asset_description,
            visual_direction=request.visual_direction,
            contract=request.generation_contract,
        )
