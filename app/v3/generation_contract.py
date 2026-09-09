from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from typing import Any

from pydantic import BaseModel, Field

from .continuity import ContinuityRegistry, ReferenceContract
from .contracts import Capability
from .provider_gateway import ProviderRegistry, ProviderSelection


class ShotContract(BaseModel):
    shot_id: str = Field(min_length=1)
    source_text: str = Field(min_length=1)
    required_entity_ids: tuple[str, ...]
    camera_direction: str = ""
    action: str = ""
    duration_seconds: float = Field(default=3.0, gt=0, le=30)


@dataclass(frozen=True)
class GenerationContract:
    shot_id: str
    source_text: str
    entity_ids: tuple[str, ...]
    reference_ids: tuple[str, ...]
    provider_reference_ids: tuple[str, ...]
    provider_id: str
    model_id: str
    required_capabilities: frozenset[Capability]
    camera_direction: str = ""
    action: str = ""
    duration_seconds: float = 3.0
    # Explicit media parameters are part of the frozen generation contract.
    # They are defaults for backwards compatibility; provider profiles bind
    # only the fields they actually support.
    width: int = 1024
    height: int = 1024
    steps: int = 28
    cfg: float = 5.5
    seed: int = 0
    sampler_name: str = "dpmpp_2m"
    scheduler: str = "karras"
    visual_context: dict[str, str] = field(default_factory=dict)
    visual_direction: dict[str, Any] = field(default_factory=dict)
    generation_contract_id: str = ""
    character_appearances: tuple[dict[str, str], ...] = ()
    positive_prompt: str = ""
    negative_prompt: str = ""

    def compile_prompts(self) -> GenerationContract:
        from app.services.prompt_compiler import PromptCompiler
        from app.services.visual_direction import VisualDirection
        if self.positive_prompt:
            return self
        direction = VisualDirection(**{f.name: self.visual_direction[f.name]
                                       for f in fields(VisualDirection) if f.name in self.visual_direction})
        compiled = PromptCompiler().compile(
            asset_kind=self.visual_context.get("asset_identity_type", ""),
            asset_description=self.source_text, visual_direction=direction, reference=False,
            contract_context=f"身份: {self.entity_ids}; 固定视觉锚点: {self.reference_ids}; 形象版本: {self.character_appearances}",
            provider_negative=self.negative_prompt,
        )
        return replace(self, positive_prompt=compiled.positive_prompt, negative_prompt=compiled.negative_prompt)


def visual_contract_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {"visual_context": dict(payload.get("visual_context") or {}),
            "visual_direction": dict(payload.get("visual_direction") or {}),
            "generation_contract_id": str(payload.get("generation_contract_id") or ""),
            "character_appearances": tuple(payload.get("character_appearances") or []),
            "positive_prompt": str(payload.get("positive_prompt") or ""),
            "negative_prompt": str(payload.get("negative_prompt") or "")}


class GenerationContractCompiler:
    """Compile semantic shots into explicit provider capability contracts."""

    def __init__(self, *, continuity: ContinuityRegistry, providers: ProviderRegistry) -> None:
        self.continuity = continuity
        self.providers = providers

    def _references(self, shot: ShotContract) -> ReferenceContract:
        if not shot.required_entity_ids:
            return ReferenceContract(entity_ids=(), reference_ids=())
        return self.continuity.build_reference_contract(shot.required_entity_ids, require_anchor=True)

    def compile_image(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> GenerationContract:
        references = self._references(shot)
        provider_refs = references.reference_ids
        capabilities = {Capability.image_generation}
        if provider_refs:
            capabilities.add(Capability.image_reference)
        if len(provider_refs) > 1:
            capabilities.add(Capability.multi_reference)
        selection = self.providers.resolve(capabilities, provider_id=provider_id, model_id=model_id)
        self.providers.assert_reference_budget(selection, len(provider_refs))
        return self._build(shot, references, provider_refs, selection, capabilities)

    def compile_video(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        first_frame_reference_id: str | None = None,
        last_frame_reference_id: str | None = None,
        require_first_frame: bool = True,
    ) -> GenerationContract:
        references = self._references(shot)
        first = str(first_frame_reference_id or "").strip()
        last = str(last_frame_reference_id or "").strip()
        if require_first_frame and not first:
            raise ValueError("video generation requires an adopted first_frame_reference_id")
        provider_refs = tuple(dict.fromkeys(item for item in (first, last) if item))

        capabilities = {Capability.video_generation}
        if first:
            capabilities.add(Capability.first_frame)
            capabilities.add(Capability.image_reference)
        if last:
            capabilities.add(Capability.last_frame)
            capabilities.add(Capability.image_reference)
        if first and last:
            capabilities.add(Capability.first_last_frame)
        if len(provider_refs) > 1:
            capabilities.add(Capability.multi_reference)

        selection = self.providers.resolve(capabilities, provider_id=provider_id, model_id=model_id)
        self.providers.assert_reference_budget(selection, len(provider_refs))
        return self._build(shot, references, provider_refs, selection, capabilities)

    @staticmethod
    def _build(
        shot: ShotContract,
        references: ReferenceContract,
        provider_refs: tuple[str, ...],
        selection: ProviderSelection,
        capabilities: set[Capability],
    ) -> GenerationContract:
        return GenerationContract(
            shot_id=shot.shot_id,
            source_text=shot.source_text,
            entity_ids=references.entity_ids,
            reference_ids=references.reference_ids,
            provider_reference_ids=provider_refs,
            provider_id=selection.spec.provider_id,
            model_id=selection.spec.model_id,
            required_capabilities=frozenset(capabilities),
            camera_direction=shot.camera_direction,
            action=shot.action,
            duration_seconds=shot.duration_seconds,
        )
