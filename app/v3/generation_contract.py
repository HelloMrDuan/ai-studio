from __future__ import annotations

from dataclasses import dataclass

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
    provider_id: str
    model_id: str
    required_capabilities: frozenset[Capability]
    camera_direction: str
    action: str
    duration_seconds: float


class GenerationContractCompiler:
    """Compile a shot from semantic identity + canonical references.

    A persistent entity shot is not allowed to silently become a naked text
    prompt. Every required entity must resolve to an adopted canonical anchor.
    Provider capability selection happens after the reference contract is built.
    """

    def __init__(
        self,
        *,
        continuity: ContinuityRegistry,
        providers: ProviderRegistry,
    ) -> None:
        self.continuity = continuity
        self.providers = providers

    def _references(self, shot: ShotContract) -> ReferenceContract:
        if not shot.required_entity_ids:
            return ReferenceContract(entity_ids=(), reference_ids=())
        return self.continuity.build_reference_contract(
            shot.required_entity_ids,
            require_anchor=True,
        )

    def compile_image(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> GenerationContract:
        references = self._references(shot)
        capabilities = {Capability.image_generation}
        if references.reference_ids:
            capabilities.add(Capability.image_reference)
        if len(references.reference_ids) > 1:
            capabilities.add(Capability.multi_reference)
        selection = self.providers.resolve(
            capabilities,
            provider_id=provider_id,
            model_id=model_id,
        )
        self.providers.assert_reference_budget(selection, len(references.reference_ids))
        return self._build(shot, references, selection, capabilities)

    def compile_video(
        self,
        shot: ShotContract,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        require_first_frame: bool = True,
    ) -> GenerationContract:
        references = self._references(shot)
        capabilities = {Capability.video_generation}
        if references.reference_ids:
            capabilities.add(Capability.image_reference)
        if len(references.reference_ids) > 1:
            capabilities.add(Capability.multi_reference)
        if require_first_frame:
            capabilities.add(Capability.first_frame)
        selection = self.providers.resolve(
            capabilities,
            provider_id=provider_id,
            model_id=model_id,
        )
        self.providers.assert_reference_budget(selection, len(references.reference_ids))
        return self._build(shot, references, selection, capabilities)

    @staticmethod
    def _build(
        shot: ShotContract,
        references: ReferenceContract,
        selection: ProviderSelection,
        capabilities: set[Capability],
    ) -> GenerationContract:
        return GenerationContract(
            shot_id=shot.shot_id,
            source_text=shot.source_text,
            entity_ids=references.entity_ids,
            reference_ids=references.reference_ids,
            provider_id=selection.spec.provider_id,
            model_id=selection.spec.model_id,
            required_capabilities=frozenset(capabilities),
            camera_direction=shot.camera_direction,
            action=shot.action,
            duration_seconds=shot.duration_seconds,
        )
